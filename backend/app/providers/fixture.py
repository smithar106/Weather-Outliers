"""Deterministic synthetic provider.

Exists for three reasons:

1. **Tests must not hit the network.** The integration suite exercises the real
   ingestion → baseline → anomaly → ranking → API path against this provider, so
   it is fast, offline, and stable in CI.
2. **Contributors without a network-dependent setup can still run the app.**
   ``WEATHER_PROVIDER=fixture`` produces a fully populated local instance.
3. **Reproducible statistical fixtures.** Values are a pure function of
   ``(city_id, date)``, so a baseline built from this provider is identical on
   every machine and the ranking tests can assert exact ordering.

The numbers are physically plausible — latitude-dependent annual cycle, skewed
zero-inflated precipitation, right-skewed gusts — but they are **synthetic** and
must never be presented as observations. Runs made with this provider record
``source_dataset="synthetic_fixture_v1"``, which the UI surfaces.
"""

from __future__ import annotations

import hashlib
import math
import struct
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from app.domain import DataTier, ObservationType
from app.providers.base import DailyRecord
from app.stats.seasonal import utc_offset_seconds

PROVIDER_NAME = "fixture"
DATASET = "synthetic_fixture_v1"


def _unit_random(*parts: object) -> float:
    """Deterministic uniform value in [0, 1) from an arbitrary key.

    BLAKE2b over the joined key gives a stable, well-distributed stream that does
    not depend on Python's hash randomisation.
    """
    key = "|".join(str(p) for p in parts).encode("utf-8")
    digest = hashlib.blake2b(key, digest_size=8).digest()
    (value,) = struct.unpack(">Q", digest)
    return value / float(1 << 64)


def _gauss(*parts: object) -> float:
    """Standard-normal draw via Box-Muller on two deterministic uniforms."""
    u1 = max(_unit_random("g1", *parts), 1e-12)
    u2 = _unit_random("g2", *parts)
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


class FixtureProvider:
    """Synthetic but deterministic daily weather."""

    name = PROVIDER_NAME

    def __init__(self, seed: str = "weather-outliers", inject_gaps: bool = True) -> None:
        self.seed = seed
        #: When True, ~0.4% of city-days come back with a missing metric so the
        #: incomplete-data paths are exercised rather than assumed.
        self.inject_gaps = inject_gaps
        self.request_count = 0
        self.error_count = 0

    def fetch_daily(
        self,
        *,
        city_id: str,
        latitude: float,
        longitude: float,
        timezone: str,
        start_date: date,
        end_date: date,
        tier: DataTier = DataTier.FINAL,
    ) -> list[DailyRecord]:
        if end_date < start_date:
            raise ValueError("end_date must be >= start_date")
        self.request_count += 1

        tz = ZoneInfo(timezone)
        records: list[DailyRecord] = []
        cursor = start_date
        while cursor <= end_date:
            records.append(
                self._make_record(city_id, latitude, longitude, tz, cursor, tier)
            )
            cursor += timedelta(days=1)
        return records

    def _make_record(
        self,
        city_id: str,
        latitude: float,
        longitude: float,
        tz: ZoneInfo,
        day: date,
        tier: DataTier,
    ) -> DailyRecord:
        doy = day.timetuple().tm_yday
        # Annual cycle: warmer at low latitude, larger swing at high latitude,
        # peaking around day 200 in the northern hemisphere.
        annual_mean = 28.0 - 0.42 * abs(latitude)
        amplitude = 2.0 + 0.30 * abs(latitude)
        seasonal = amplitude * math.cos(2.0 * math.pi * (doy - 200) / 365.25)

        noise = 3.2 * _gauss(self.seed, city_id, day.isoformat(), "t")
        mean_t = annual_mean + seasonal + noise
        spread = 6.0 + 2.0 * _unit_random(self.seed, city_id, day.isoformat(), "spread")
        temp_max = mean_t + spread / 2.0
        temp_min = mean_t - spread / 2.0

        # Zero-inflated precipitation: a wet-day probability that varies by city
        # and season, then an exponential amount on wet days.
        wet_p = 0.14 + 0.22 * _unit_random(self.seed, city_id, "wetbase") + 0.08 * math.sin(
            2.0 * math.pi * doy / 365.25
        )
        draw = _unit_random(self.seed, city_id, day.isoformat(), "wet")
        if draw < wet_p:
            scale = 3.0 + 9.0 * _unit_random(self.seed, city_id, "pscale")
            u = max(_unit_random(self.seed, city_id, day.isoformat(), "pamt"), 1e-9)
            precipitation = round(-scale * math.log(u), 1)
        else:
            precipitation = 0.0

        # Right-skewed gusts: lognormal-ish around a city-specific base.
        gust_base = 26.0 + 16.0 * _unit_random(self.seed, city_id, "gustbase")
        gust = gust_base * math.exp(0.32 * _gauss(self.seed, city_id, day.isoformat(), "gust"))

        record = DailyRecord(
            city_id=city_id,
            local_date=day,
            temp_max_c=round(temp_max, 1),
            temp_min_c=round(temp_min, 1),
            temp_mean_c=round(mean_t, 1),
            precipitation_mm=precipitation,
            wind_gust_max_kmh=round(gust, 1),
            source_provider=PROVIDER_NAME,
            source_dataset=DATASET,
            source_endpoint="local://fixture",
            # Synthetic data is not reanalysis and is not an observation. Calling
            # it MODEL_ANALYSIS is the least misleading available label, and the
            # dataset name makes its nature unmistakable.
            observation_type=ObservationType.MODEL_ANALYSIS,
            data_tier=tier,
            units={
                "temp_max_c": "°C",
                "temp_min_c": "°C",
                "temp_mean_c": "°C",
                "precipitation_mm": "mm",
                "wind_gust_max_kmh": "km/h",
            },
            utc_offset_seconds=utc_offset_seconds(day, tz),
            grid_latitude=latitude,
            grid_longitude=longitude,
            grid_elevation_m=None,
        )

        if self.inject_gaps and _unit_random(self.seed, city_id, day.isoformat(), "gap") < 0.004:
            record.wind_gust_max_kmh = None

        record.data_quality = record.classify_quality()
        return record

    def close(self) -> None:
        return None
