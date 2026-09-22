"""Load, validate, and sync the version-controlled city registry.

The registry is a reviewed JSON file in ``data/cities.json`` rather than a
database table that someone edits in production. Coverage changes therefore
arrive as pull requests with a diff, which is the only way a "curated sample"
claim stays true over time.

Validation is strict on purpose: a typo'd timezone or a swapped
latitude/longitude would produce plausible-looking but wrong anomalies, and that
is the worst failure mode this project has.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import City

logger = logging.getLogger(__name__)

VALID_COUNTRIES = frozenset({"US", "CA", "MX"})


class CityRecord(BaseModel):
    """One validated registry entry."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=3, max_length=64, pattern=r"^[a-z]{2}-[a-z0-9-]+$")
    name: str = Field(min_length=1, max_length=128)
    admin: str = Field(min_length=1, max_length=128)
    country: str = Field(min_length=2, max_length=2)
    region: str = Field(min_length=1, max_length=64)
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    timezone: str = Field(min_length=3, max_length=64)
    population: int | None = Field(default=None, ge=0)

    @field_validator("country")
    @classmethod
    def _known_country(cls, v: str) -> str:
        if v not in VALID_COUNTRIES:
            raise ValueError(f"country must be one of {sorted(VALID_COUNTRIES)}, got {v!r}")
        return v

    @field_validator("timezone")
    @classmethod
    def _resolvable_timezone(cls, v: str) -> str:
        """Reject anything the tz database cannot resolve.

        This catches both typos and the tempting-but-wrong habit of writing a
        fixed offset such as ``"UTC-7"``, which would silently break every DST
        transition.
        """
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unresolvable IANA timezone {v!r}: {exc}") from exc
        return v

    @field_validator("id")
    @classmethod
    def _id_prefix_matches_country(cls, v: str) -> str:
        return v

    def check_consistency(self) -> None:
        """Cross-field checks that a single-field validator cannot express."""
        if not self.id.startswith(f"{self.country.lower()}-"):
            raise ValueError(
                f"city id {self.id!r} must start with its lowercased country code "
                f"({self.country.lower()}-)"
            )
        # North America only, per the documented scope. A sign-flipped longitude
        # is the classic coordinate bug and would land in the eastern hemisphere.
        if not (-180.0 <= self.longitude <= -50.0):
            raise ValueError(
                f"longitude {self.longitude} for {self.id!r} is outside the North American "
                "range (-180..-50); check for a sign error"
            )
        if not (14.0 <= self.latitude <= 72.0):
            raise ValueError(
                f"latitude {self.latitude} for {self.id!r} is outside the North American "
                "range (14..72)"
            )


class CityRegistry(BaseModel):
    """The whole validated registry file."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int
    registry_version: str
    population_sources: dict[str, str] = Field(default_factory=dict)
    cities: list[CityRecord]

    @field_validator("cities")
    @classmethod
    def _non_empty_unique(cls, v: list[CityRecord]) -> list[CityRecord]:
        if not v:
            raise ValueError("registry contains no cities")
        ids = [c.id for c in v]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate city ids: {duplicates}")
        for city in v:
            city.check_consistency()
        return v

    def by_id(self) -> dict[str, CityRecord]:
        return {c.id: c for c in self.cities}

    def population_source_for(self, country: str) -> str | None:
        return self.population_sources.get(country)


def find_registry_path() -> Path:
    """Locate ``data/cities.json``.

    Checks the explicit setting first, then walks up from this file so the loader
    works identically from the repo root, from ``backend/``, inside a container
    where the repo layout is preserved, and under pytest.
    """
    configured = get_settings().cities_file
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"CITIES_FILE={configured!r} does not exist")
        return path

    for parent in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        candidate = parent / "data" / "cities.json"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "could not locate data/cities.json; set CITIES_FILE to an explicit path"
    )


def load_registry(path: Path | str | None = None) -> CityRegistry:
    """Parse and validate the registry. Raises on any inconsistency."""
    resolved = Path(path) if path else find_registry_path()
    with resolved.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    registry = CityRegistry.model_validate(payload)
    logger.info(
        "loaded city registry version=%s cities=%d from %s",
        registry.registry_version,
        len(registry.cities),
        resolved,
    )
    return registry


@lru_cache(maxsize=1)
def cached_registry() -> CityRegistry:
    return load_registry()


def sync_registry(session: Session, registry: CityRegistry | None = None) -> dict:
    """Upsert the registry into the ``cities`` table.

    Idempotent: re-running makes no changes when the file has not moved. Cities
    removed from the file are *deactivated* rather than deleted, so their
    historical observations and archived rankings survive.
    """
    registry = registry or load_registry()
    existing = {c.id: c for c in session.scalars(select(City)).all()}
    inserted = updated = unchanged = 0

    for record in registry.cities:
        pop_source = registry.population_source_for(record.country)
        row = existing.get(record.id)
        if row is None:
            session.add(
                City(
                    id=record.id,
                    name=record.name,
                    admin=record.admin,
                    country=record.country,
                    region=record.region,
                    latitude=record.latitude,
                    longitude=record.longitude,
                    timezone=record.timezone,
                    population=record.population,
                    population_source=pop_source,
                    registry_version=registry.registry_version,
                    is_active=True,
                )
            )
            inserted += 1
            continue

        fields = {
            "name": record.name,
            "admin": record.admin,
            "country": record.country,
            "region": record.region,
            "latitude": record.latitude,
            "longitude": record.longitude,
            "timezone": record.timezone,
            "population": record.population,
            "population_source": pop_source,
            "registry_version": registry.registry_version,
            "is_active": True,
        }
        changed = [k for k, v in fields.items() if getattr(row, k) != v]
        if changed:
            for key, value in fields.items():
                setattr(row, key, value)
            updated += 1
            logger.info("city %s updated: %s", record.id, changed)
        else:
            unchanged += 1

    registry_ids = {c.id for c in registry.cities}
    deactivated = 0
    for city_id, row in existing.items():
        if city_id not in registry_ids and row.is_active:
            row.is_active = False
            deactivated += 1
            logger.warning("city %s no longer in registry; deactivated", city_id)

    session.flush()
    summary = {
        "registry_version": registry.registry_version,
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "deactivated": deactivated,
        "total_in_registry": len(registry.cities),
    }
    logger.info("city registry sync: %s", summary)
    return summary


def active_cities(session: Session) -> list[City]:
    """Active cities in a stable, deterministic order."""
    return list(
        session.scalars(select(City).where(City.is_active.is_(True)).order_by(City.id)).all()
    )
