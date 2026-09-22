"""Idempotent daily observation ingestion.

Upserts on the natural key ``(city_id, local_date, source_dataset)``. Rerunning
a date updates the existing rows in place, so a retry after a partial failure
converges instead of accumulating duplicates. The provisional and final datasets
carry different ``source_dataset`` values, so they coexist for the same day —
which is what lets ``pipeline finalize`` upgrade a day without destroying the
provisional record that was actually published at the time.
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain import DataQuality, DataTier
from app.models import WeatherObservation
from app.providers.base import DailyRecord

logger = logging.getLogger(__name__)

_MUTABLE_FIELDS = (
    "temp_max_c",
    "temp_min_c",
    "temp_mean_c",
    "precipitation_mm",
    "wind_gust_max_kmh",
    "source_provider",
    "source_endpoint",
    "observation_type",
    "data_tier",
    "data_quality",
    "missing_fields",
    "units",
    "utc_offset_seconds",
    "grid_latitude",
    "grid_longitude",
    "grid_elevation_m",
    "retrieved_at",
)


def upsert_observations(session: Session, records: list[DailyRecord]) -> dict:
    """Insert or update observations. Returns counts, never raises on duplicates."""
    if not records:
        return {"inserted": 0, "updated": 0, "total": 0}

    keys = {(r.city_id, r.local_date, r.source_dataset) for r in records}
    city_ids = {r.city_id for r in records}
    dates = {r.local_date for r in records}
    datasets = {r.source_dataset for r in records}

    # One bounded query instead of a per-record SELECT. The extra rows the
    # coarse filter may return are discarded by the exact-key lookup below.
    existing_rows = session.scalars(
        select(WeatherObservation).where(
            WeatherObservation.city_id.in_(city_ids),
            WeatherObservation.local_date.in_(dates),
            WeatherObservation.source_dataset.in_(datasets),
        )
    ).all()
    existing = {
        (row.city_id, row.local_date, row.source_dataset): row
        for row in existing_rows
        if (row.city_id, row.local_date, row.source_dataset) in keys
    }

    inserted = updated = 0
    for record in records:
        key = (record.city_id, record.local_date, record.source_dataset)
        payload = {
            field: _coerce(getattr(record, field)) for field in _MUTABLE_FIELDS
        }
        row = existing.get(key)
        if row is None:
            row = WeatherObservation(
                city_id=record.city_id,
                local_date=record.local_date,
                source_dataset=record.source_dataset,
                **payload,
            )
            session.add(row)
            existing[key] = row
            inserted += 1
        else:
            for field, value in payload.items():
                setattr(row, field, value)
            updated += 1

    session.flush()
    return {"inserted": inserted, "updated": updated, "total": len(records)}


def _coerce(value):
    """Normalise enum members to their string value for portable JSON/column types."""
    return value.value if hasattr(value, "value") else value


def get_observation(
    session: Session,
    city_id: str,
    local_date: date,
    *,
    prefer_tier: DataTier | None = None,
) -> WeatherObservation | None:
    """Best available observation for a city-day.

    When both tiers exist, ``final`` wins by default — the reanalysis value is
    the settled one. Passing ``prefer_tier`` lets a run reproduce exactly what it
    saw at the time.
    """
    rows = list(
        session.scalars(
            select(WeatherObservation).where(
                WeatherObservation.city_id == city_id,
                WeatherObservation.local_date == local_date,
            )
        ).all()
    )
    if not rows:
        return None
    if prefer_tier is not None:
        for row in rows:
            if row.data_tier == prefer_tier.value:
                return row
        return None

    def preference(row: WeatherObservation) -> tuple:
        tier_rank = 0 if row.data_tier == DataTier.FINAL.value else 1
        quality_rank = {
            DataQuality.OK.value: 0,
            DataQuality.INCOMPLETE.value: 1,
            DataQuality.MISSING.value: 2,
        }.get(row.data_quality, 3)
        return (tier_rank, quality_rank, -(row.id or 0))

    return sorted(rows, key=preference)[0]


def observations_for_date(
    session: Session, local_date: date, *, tier: DataTier | None = None
) -> dict[str, WeatherObservation]:
    """All observations for one local date, keyed by city id."""
    stmt = select(WeatherObservation).where(WeatherObservation.local_date == local_date)
    if tier is not None:
        stmt = stmt.where(WeatherObservation.data_tier == tier.value)
    rows = session.scalars(stmt.order_by(WeatherObservation.city_id)).all()

    best: dict[str, WeatherObservation] = {}
    for row in rows:
        current = best.get(row.city_id)
        if current is None:
            best[row.city_id] = row
            continue
        # Prefer final over provisional, and ok over incomplete.
        current_rank = (
            0 if current.data_tier == DataTier.FINAL.value else 1,
            0 if current.data_quality == DataQuality.OK.value else 1,
        )
        row_rank = (
            0 if row.data_tier == DataTier.FINAL.value else 1,
            0 if row.data_quality == DataQuality.OK.value else 1,
        )
        if row_rank < current_rank:
            best[row.city_id] = row
    return best
