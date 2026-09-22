"""Build and read the cached baseline climatology.

This is the expensive half of the project, and it runs rarely.

For each city we download the full reference period once (chunked by decade),
bucket every day onto the 365-day no-leap seasonal calendar, then for each
target day-of-year pool the ``+/- window`` buckets into one sample and reduce it
to a :class:`~app.stats.distributions.DistributionSketch` plus a histogram. The
daily pipeline then reads 5 small rows per city instead of re-downloading 30
years of weather.

Storage tradeoff
----------------
50 cities x 5 metrics x 365 days is ~91k baseline rows, each carrying ~45
quantile values. That is on the order of a hundred megabytes — comfortably within
a modest Postgres volume, and far cheaper than the ~550k raw daily rows that
recomputing on demand would require. The cost is that the sketch is lossy: we
can answer "what percentile is 41.2 °C" exactly to the resolution of the stored
grid and tail order statistics, but we cannot recover the original 450 values.
That is the right trade for this product, and it is stated on the methodology
page rather than buried here.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.domain import ALL_METRICS, METHODOLOGY_VERSION, DataTier, Metric
from app.models import BaselineStatistic, City
from app.providers.base import WeatherProvider
from app.providers.open_meteo import chunk_date_range
from app.stats.anomaly import Baseline
from app.stats.distributions import DistributionSketch, build_histogram, build_sketch
from app.stats.seasonal import (
    DAYS_IN_NOLEAP_YEAR,
    noleap_day_of_year,
    reference_period_dates,
    seasonal_window,
)

logger = logging.getLogger(__name__)


def baselines_exist(session: Session, city_id: str, settings: Settings | None = None) -> bool:
    """Whether this city already has a complete-enough baseline set to skip."""
    settings = settings or get_settings()
    count = session.scalar(
        select(BaselineStatistic.id)
        .where(
            BaselineStatistic.city_id == city_id,
            BaselineStatistic.reference_start_year == settings.baseline_start_year,
            BaselineStatistic.reference_end_year == settings.baseline_end_year,
            BaselineStatistic.window_days == settings.baseline_seasonal_window_days,
            BaselineStatistic.methodology_version == METHODOLOGY_VERSION,
        )
        .limit(1)
    )
    return count is not None


def build_city_baselines(
    session: Session,
    city: City,
    provider: WeatherProvider,
    settings: Settings | None = None,
    *,
    force: bool = False,
) -> dict:
    """Download the reference period for one city and cache its sketches.

    Returns a summary dict. Safe to re-run: existing rows for the same
    (city, reference period, window, methodology version) are replaced
    atomically within the caller's transaction.
    """
    settings = settings or get_settings()

    if not force and baselines_exist(session, city.id, settings):
        logger.info("baselines already cached for %s; skipping", city.id)
        return {
            "city_id": city.id,
            "skipped": True,
            "rows": 0,
            "provider_requests": 0,
            "days_fetched": 0,
        }

    start, end = reference_period_dates(settings.baseline_start_year, settings.baseline_end_year)

    # values[metric][doy] = list of (value, year)
    values: dict[Metric, dict[int, list[tuple[float, int]]]] = {
        m: defaultdict(list) for m in ALL_METRICS
    }
    days_fetched = 0
    provider_requests = 0
    dataset = "unknown"

    for chunk_start, chunk_end in chunk_date_range(start, end, settings.baseline_chunk_years):
        provider_requests += 1
        records = provider.fetch_daily(
            city_id=city.id,
            latitude=city.latitude,
            longitude=city.longitude,
            timezone=city.timezone,
            start_date=chunk_start,
            end_date=chunk_end,
            tier=DataTier.FINAL,
        )
        logger.info(
            "fetched %d days for %s (%s..%s)",
            len(records),
            city.id,
            chunk_start,
            chunk_end,
        )
        for record in records:
            days_fetched += 1
            dataset = record.source_dataset
            doy = noleap_day_of_year(record.local_date)
            year = record.local_date.year
            for metric in ALL_METRICS:
                value = record.value_for(metric)
                if value is not None:
                    values[metric][doy].append((value, year))

    if days_fetched == 0:
        raise RuntimeError(f"no reference data returned for {city.id}")

    # Replace any prior rows for this exact identity, then insert fresh ones.
    session.execute(
        delete(BaselineStatistic).where(
            BaselineStatistic.city_id == city.id,
            BaselineStatistic.reference_start_year == settings.baseline_start_year,
            BaselineStatistic.reference_end_year == settings.baseline_end_year,
            BaselineStatistic.window_days == settings.baseline_seasonal_window_days,
            BaselineStatistic.methodology_version == METHODOLOGY_VERSION,
        )
    )

    rows = 0
    sufficient_rows = 0
    window_days = settings.baseline_seasonal_window_days

    for metric in ALL_METRICS:
        per_doy = values[metric]
        if not per_doy:
            logger.warning("no %s data at all for %s", metric.value, city.id)
            continue
        for target_doy in range(1, DAYS_IN_NOLEAP_YEAR + 1):
            pooled: list[tuple[float, int]] = []
            for doy in seasonal_window(target_doy, window_days):
                pooled.extend(per_doy.get(doy, ()))
            if not pooled:
                continue

            sample = [v for v, _ in pooled]
            years = {y for _, y in pooled}
            row = _make_baseline_row(
                city_id=city.id,
                metric=metric,
                target_doy=target_doy,
                sample=sample,
                n_years=len(years),
                settings=settings,
                dataset=dataset,
            )
            session.add(row)
            rows += 1
            if row.sufficient:
                sufficient_rows += 1

    session.flush()
    summary = {
        "city_id": city.id,
        "skipped": False,
        "days_fetched": days_fetched,
        "provider_requests": provider_requests,
        "rows": rows,
        "sufficient_rows": sufficient_rows,
        "reference_period": f"{settings.baseline_start_year}-{settings.baseline_end_year}",
        "window_days": window_days,
        "source_dataset": dataset,
    }
    logger.info("baselines built: %s", summary)
    return summary


def _make_baseline_row(
    *,
    city_id: str,
    metric: Metric,
    target_doy: int,
    sample: list[float],
    n_years: int,
    settings: Settings,
    dataset: str,
) -> BaselineStatistic:
    sketch = build_sketch(sample)

    zero_fraction = None
    nonzero_n = None
    nonzero_quantiles = None
    if metric == Metric.PRECIPITATION:
        # Split the mixture explicitly. A "wet day" threshold of strictly > 0 is
        # used rather than the meteorological 0.2 mm/1.0 mm conventions, because
        # the reanalysis reports continuous trace amounts and any threshold we
        # imposed would be an undocumented editorial choice. Stated in the docs.
        wet = [v for v in sample if v > 0.0]
        nonzero_n = len(wet)
        zero_fraction = 1.0 - (len(wet) / len(sample)) if sample else None
        if wet:
            nonzero_quantiles = build_sketch(wet).to_json()

    sufficient = (
        sketch.n >= settings.baseline_min_samples and n_years >= settings.baseline_min_years
    )
    if metric == Metric.PRECIPITATION:
        sufficient = sufficient and (nonzero_n or 0) >= settings.baseline_min_wet_days

    return BaselineStatistic(
        city_id=city_id,
        metric=metric.value,
        day_of_year=target_doy,
        reference_start_year=settings.baseline_start_year,
        reference_end_year=settings.baseline_end_year,
        window_days=settings.baseline_seasonal_window_days,
        n_samples=sketch.n,
        n_years=n_years,
        sufficient=sufficient,
        mean=sketch.mean,
        std=sketch.std,
        median=sketch.median,
        p25=sketch.p25,
        p75=sketch.p75,
        iqr=sketch.iqr,
        min_value=sketch.minimum,
        max_value=sketch.maximum,
        quantiles={"grid": list(sketch.grid), "values": list(sketch.values)},
        low_order_stats=list(sketch.low_order_stats),
        high_order_stats=list(sketch.high_order_stats),
        histogram=build_histogram(sample, settings.baseline_histogram_bins),
        zero_fraction=zero_fraction,
        nonzero_n=nonzero_n,
        nonzero_quantiles=nonzero_quantiles,
        source_dataset=dataset,
        methodology_version=METHODOLOGY_VERSION,
    )


# ---------------------------------------------------------------------------
# Reading baselines back
# ---------------------------------------------------------------------------


def row_to_baseline(row: BaselineStatistic) -> Baseline:
    """Rehydrate a stored row into the pure-Python :class:`Baseline`."""
    quantiles = row.quantiles or {}
    sketch = DistributionSketch(
        n=row.n_samples,
        mean=row.mean,
        std=row.std,
        median=row.median,
        p25=row.p25,
        p75=row.p75,
        iqr=row.iqr,
        minimum=row.min_value,
        maximum=row.max_value,
        grid=tuple(quantiles.get("grid") or ()),
        values=tuple(quantiles.get("values") or ()),
        low_order_stats=tuple(row.low_order_stats or ()),
        high_order_stats=tuple(row.high_order_stats or ()),
    )
    nonzero_sketch = (
        DistributionSketch.from_json(row.nonzero_quantiles) if row.nonzero_quantiles else None
    )
    return Baseline(
        metric=Metric(row.metric),
        day_of_year=row.day_of_year,
        window_days=row.window_days,
        reference_start_year=row.reference_start_year,
        reference_end_year=row.reference_end_year,
        n_samples=row.n_samples,
        n_years=row.n_years,
        sufficient=row.sufficient,
        sketch=sketch,
        source_dataset=row.source_dataset,
        zero_fraction=row.zero_fraction,
        nonzero_n=row.nonzero_n,
        nonzero_sketch=nonzero_sketch,
    )


def load_baselines_for_date(
    session: Session,
    local_date: date,
    city_ids: list[str] | None = None,
    settings: Settings | None = None,
) -> dict[tuple[str, Metric], Baseline]:
    """All baselines needed to score one calendar date, in a single query."""
    settings = settings or get_settings()
    doy = noleap_day_of_year(local_date)

    stmt = select(BaselineStatistic).where(
        BaselineStatistic.day_of_year == doy,
        BaselineStatistic.reference_start_year == settings.baseline_start_year,
        BaselineStatistic.reference_end_year == settings.baseline_end_year,
        BaselineStatistic.window_days == settings.baseline_seasonal_window_days,
        BaselineStatistic.methodology_version == METHODOLOGY_VERSION,
    )
    if city_ids:
        stmt = stmt.where(BaselineStatistic.city_id.in_(city_ids))

    result: dict[tuple[str, Metric], Baseline] = {}
    for row in session.scalars(stmt).all():
        result[(row.city_id, Metric(row.metric))] = row_to_baseline(row)
    return result


def get_baseline_row(
    session: Session,
    city_id: str,
    metric: Metric,
    local_date: date,
    settings: Settings | None = None,
) -> BaselineStatistic | None:
    """Single baseline row, as used by the agent's ``get_city_baseline`` tool."""
    settings = settings or get_settings()
    return session.scalars(
        select(BaselineStatistic)
        .where(
            BaselineStatistic.city_id == city_id,
            BaselineStatistic.metric == metric.value,
            BaselineStatistic.day_of_year == noleap_day_of_year(local_date),
            BaselineStatistic.reference_start_year == settings.baseline_start_year,
            BaselineStatistic.reference_end_year == settings.baseline_end_year,
            BaselineStatistic.window_days == settings.baseline_seasonal_window_days,
            BaselineStatistic.methodology_version == METHODOLOGY_VERSION,
        )
        .limit(1)
    ).first()


def baseline_coverage(session: Session, settings: Settings | None = None) -> dict:
    """Operational summary of what is cached — surfaced on the health endpoint."""
    settings = settings or get_settings()
    base_filter = (
        BaselineStatistic.reference_start_year == settings.baseline_start_year,
        BaselineStatistic.reference_end_year == settings.baseline_end_year,
        BaselineStatistic.window_days == settings.baseline_seasonal_window_days,
        BaselineStatistic.methodology_version == METHODOLOGY_VERSION,
    )
    total = session.scalar(
        select(BaselineStatistic.id).where(*base_filter).limit(1)
    )
    if total is None:
        return {"cities_with_baselines": 0, "rows": 0, "sufficient_rows": 0}

    from sqlalchemy import func

    rows = session.scalar(select(func.count()).select_from(BaselineStatistic).where(*base_filter))
    sufficient = session.scalar(
        select(func.count())
        .select_from(BaselineStatistic)
        .where(*base_filter, BaselineStatistic.sufficient.is_(True))
    )
    cities = session.scalar(
        select(func.count(func.distinct(BaselineStatistic.city_id))).where(*base_filter)
    )
    return {
        "cities_with_baselines": int(cities or 0),
        "rows": int(rows or 0),
        "sufficient_rows": int(sufficient or 0),
        "reference_period": settings.reference_period_label,
        "window_days": settings.baseline_seasonal_window_days,
        "methodology_version": METHODOLOGY_VERSION,
    }
