"""Open-Meteo provider.

Two tiers, one provider class
-----------------------------
Open-Meteo's ERA5 archive is the right source for both the 30-year baseline and
the definitive daily value, but it publishes with roughly a **five-day delay**.
A product whose headline is "yesterday" therefore cannot use it alone.

So this adapter exposes two tiers:

``DataTier.FINAL``
    ``/v1/archive`` with ``models=era5_seamless`` — ERA5-Land where available,
    ERA5 elsewhere. Reanalysis. Available ~5 days after the fact.

``DataTier.PROVISIONAL``
    ``/v1/forecast`` with ``past_days`` — the operational best-match model's
    analysis of the last few days. Available immediately.

Pinning the archive to an explicit ``models`` value matters more than it looks:
the baseline and the final daily observation are then drawn from *the same
dataset*, so the anomaly is not contaminated by a change of model between the
climatology and the day being tested.

The provisional tier does not have that guarantee — it is a different model than
ERA5 — which is precisely why provisional rankings are labelled as such and why
``pipeline finalize`` recomputes the date against ERA5 once the archive catches
up. See docs/methodology.md.

Licensing: the free endpoints are for non-commercial use and the data is
CC-BY-4.0. Setting ``OPEN_METEO_API_KEY`` switches to the paid customer
endpoints with no other change. See docs/data-sources.md.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx

from app.config import Settings, get_settings
from app.domain import DataTier, ObservationType
from app.providers.base import (
    DailyRecord,
    ProviderBadRequest,
    ProviderBudgetExhausted,
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "open_meteo"

#: Explicit reanalysis model for the archive tier. Pinned so the baseline and
#: the final daily value always come from the same dataset.
ARCHIVE_MODEL = "era5_seamless"
ARCHIVE_DATASET = "era5_seamless"
#: The near-real-time tier is the operational forecast model's analysis of days
#: that have already happened. Named distinctly so it can never be mistaken for
#: reanalysis in the database or the UI.
FORECAST_DATASET = "open_meteo_best_match_analysis"

DAILY_VARIABLES = (
    "temperature_2m_max",
    "temperature_2m_min",
    "temperature_2m_mean",
    "precipitation_sum",
    "wind_gusts_10m_max",
)

_FIELD_BY_VARIABLE = {
    "temperature_2m_max": "temp_max_c",
    "temperature_2m_min": "temp_min_c",
    "temperature_2m_mean": "temp_mean_c",
    "precipitation_sum": "precipitation_mm",
    "wind_gusts_10m_max": "wind_gust_max_kmh",
}


#: Open-Meteo does not count requests; it counts *weighted* calls. Its published
#: rule (open-meteo.com/en/pricing, retrieved 2026-09-22) is that a request
#: covering more than 10 weather variables, or more than two weeks for a single
#: location, is "considered multiple API calls", and the documented worked example
#: — "2 weeks of data with 15 weather variables will be calculated as 1.5 API
#: calls, while 4 weeks of data equals 3.0" — pins the arithmetic to the product of
#: two ratios with a floor of one call.
#:
#: This matters enormously here, and getting it wrong is what a naive request
#: counter does: one 10-year chunk of 5 daily variables is a single HTTP request
#: but roughly 130 calls against the quota. Counting requests instead of calls put
#: us 26x over the minutely allowance and produced a wall of 429s.
CALL_WEIGHT_VARIABLES_PER_CALL = 10
CALL_WEIGHT_DAYS_PER_CALL = 14


def estimate_call_weight(*, variable_count: int, day_count: int) -> float:
    """Weighted API calls one request will cost, per Open-Meteo's published rule.

    An estimate of *their* accounting, not a measurement of it — the provider does
    not return its own tally — so the budgets it is spent against carry headroom.
    The estimate did reproduce observed behaviour: it prices our 10-year archive
    chunk at 130.5 calls, so a 600-call minute affords 4.6 of them, and a run that
    ignored weighting was throttled to exactly 5 successful archive requests per
    minute for six consecutive minutes.
    """
    variables = max(1, variable_count)
    days = max(1, day_count)
    weight = (variables / CALL_WEIGHT_VARIABLES_PER_CALL) * (days / CALL_WEIGHT_DAYS_PER_CALL)
    return max(1.0, weight)


@dataclass(frozen=True, slots=True)
class Budget:
    """One published quota window, with our own headroom already applied."""

    label: str
    seconds: float
    max_weight: float


class RateLimiter:
    """Thread-safe sliding-window limiter over *weighted* calls.

    Holds one window per published quota (minute, hour, day) rather than just the
    minutely one, because the binding constraint for a 30-year baseline build is
    the daily allowance, and discovering that by being cut off mid-run leaves a
    half-built climatology.

    Waits that are short are waited out. Waits that are long — an hour or a day —
    raise :class:`ProviderBudgetExhausted` instead, because a pipeline blocking
    silently for twenty hours is a worse failure than one that stops and says so.
    Being a good citizen of a free service is a requirement, not an optimisation.
    """

    def __init__(self, budgets: Sequence[Budget], *, max_sleep_seconds: float = 180.0) -> None:
        if not budgets:
            raise ValueError("at least one budget is required")
        self.budgets = tuple(budgets)
        self.max_sleep_seconds = max_sleep_seconds
        #: (monotonic timestamp, weight) of every call we have made, pruned to the
        #: longest window.
        self._spent: deque[tuple[float, float]] = deque()
        self._lock = threading.Lock()
        self._longest = max(budget.seconds for budget in self.budgets)

    def acquire(self, weight: float = 1.0) -> None:
        weight = max(0.0, weight)
        while True:
            with self._lock:
                now = time.monotonic()
                while self._spent and now - self._spent[0][0] >= self._longest:
                    self._spent.popleft()

                wait_for = 0.0
                blocking: Budget | None = None
                for budget in self.budgets:
                    spent = sum(w for ts, w in self._spent if now - ts < budget.seconds)
                    if spent + weight <= budget.max_weight:
                        continue
                    # Wait until enough of this window's oldest spending has aged
                    # out to fit the request.
                    freed = 0.0
                    needed = spent + weight - budget.max_weight
                    budget_wait = budget.seconds
                    for ts, w in self._spent:
                        if now - ts >= budget.seconds:
                            continue
                        freed += w
                        if freed >= needed:
                            budget_wait = budget.seconds - (now - ts) + 0.05
                            break
                    if budget_wait > wait_for:
                        wait_for, blocking = budget_wait, budget

                if blocking is None:
                    self._spent.append((now, weight))
                    return

            if wait_for > self.max_sleep_seconds:
                raise ProviderBudgetExhausted(
                    f"{blocking.label} provider budget of {blocking.max_weight:.0f} weighted "
                    f"calls is spent; the next request of {weight:.1f} calls would have to wait "
                    f"{wait_for / 60:.0f} min. Stopping rather than blocking — rerun to resume."
                )
            logger.info(
                "provider budget: waiting %.1fs for the %s window before spending %.1f calls",
                wait_for,
                blocking.label,
                weight,
            )
            time.sleep(max(wait_for, 0.01))


class OpenMeteoProvider:
    """Open-Meteo archive + near-real-time adapter."""

    name = PROVIDER_NAME

    def __init__(
        self, settings: Settings | None = None, client: httpx.Client | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(self.settings.provider_request_timeout_seconds),
            headers={"User-Agent": "weather-outliers/0.1 (+https://github.com/smithar106/Weather-Outliers)"},
            follow_redirects=True,
        )
        self._limiter = RateLimiter(
            [
                Budget("minutely", 60.0, self.settings.provider_max_call_weight_per_minute),
                Budget("hourly", 3600.0, self.settings.provider_max_call_weight_per_hour),
                Budget("daily", 86400.0, self.settings.provider_max_call_weight_per_day),
            ],
            max_sleep_seconds=self.settings.provider_max_budget_wait_seconds,
        )
        self.request_count = 0
        self.error_count = 0
        #: Weighted calls this provider instance has spent. Reported by the
        #: pipeline so a run's true cost against the free tier is visible, since
        #: the request count alone understates it by two orders of magnitude.
        self.call_weight_spent = 0.0

    # ------------------------------------------------------------------ public
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

        if tier == DataTier.FINAL:
            payload = self._request_archive(latitude, longitude, timezone, start_date, end_date)
            dataset, endpoint = ARCHIVE_DATASET, self.settings.archive_url
            observation_type = ObservationType.REANALYSIS
        else:
            payload = self._request_forecast(latitude, longitude, timezone, start_date, end_date)
            dataset, endpoint = FORECAST_DATASET, self.settings.forecast_url
            observation_type = ObservationType.MODEL_ANALYSIS

        return self._parse(
            payload,
            city_id=city_id,
            dataset=dataset,
            endpoint=endpoint,
            observation_type=observation_type,
            tier=tier,
            start_date=start_date,
            end_date=end_date,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OpenMeteoProvider:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ----------------------------------------------------------------- requests
    def _base_params(self, latitude: float, longitude: float, timezone: str) -> dict:
        params: dict[str, object] = {
            "latitude": f"{latitude:.4f}",
            "longitude": f"{longitude:.4f}",
            "daily": ",".join(DAILY_VARIABLES),
            # An IANA zone name, never a fixed offset: this is what makes the
            # returned day boundaries correct across DST transitions.
            "timezone": timezone,
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
        }
        if self.settings.open_meteo_api_key:
            params["apikey"] = self.settings.open_meteo_api_key
        return params

    def _request_archive(
        self, latitude: float, longitude: float, timezone: str, start: date, end: date
    ) -> dict:
        params = self._base_params(latitude, longitude, timezone)
        params.update(
            {
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "models": ARCHIVE_MODEL,
            }
        )
        return self._get(self.settings.archive_url, params)

    def _request_forecast(
        self, latitude: float, longitude: float, timezone: str, start: date, end: date
    ) -> dict:
        """Near-real-time tier via ``past_days``.

        ``past_days`` is capped by the API, so we request the span needed to
        cover ``start`` and filter the response down to the requested window.
        """
        today_utc = datetime.now(UTC).date()
        # +1 day of slack absorbs the case where the city's local date is still
        # "yesterday" while UTC has already ticked over.
        past_days = (today_utc - start).days + 1
        past_days = max(1, min(past_days, 92))
        params = self._base_params(latitude, longitude, timezone)
        params.update({"past_days": past_days, "forecast_days": 1})
        return self._get(self.settings.forecast_url, params)

    def _get(self, url: str, params: dict) -> dict:
        """GET with budget accounting, bounded backoff, and typed failures."""
        attempts = self.settings.provider_max_retries + 1
        base = self.settings.provider_backoff_base_seconds
        weight = _request_weight(params)
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            retry_after: float | None = None
            self._limiter.acquire(weight)
            try:
                self.request_count += 1
                self.call_weight_spent += weight
                response = self._client.get(url, params=params)
            except httpx.TimeoutException as exc:
                last_error = ProviderUnavailable(f"timeout calling {url}: {exc}")
            except httpx.HTTPError as exc:
                last_error = ProviderUnavailable(f"transport error calling {url}: {exc}")
            else:
                if response.status_code == 200:
                    return response.json()
                detail = _extract_reason(response)
                if response.status_code == 429:
                    last_error = ProviderRateLimited(f"429 from {url}: {detail}")
                    retry_after = _retry_after_seconds(response, default=60.0)
                elif 400 <= response.status_code < 500:
                    # Our fault. Retrying will not help and would waste quota.
                    self.error_count += 1
                    raise ProviderBadRequest(f"{response.status_code} from {url}: {detail}")
                else:
                    last_error = ProviderUnavailable(
                        f"{response.status_code} from {url}: {detail}"
                    )

            if attempt < attempts:
                delay = base * (2 ** (attempt - 1))
                if retry_after is not None:
                    # Exponential backoff is the wrong shape for a quota window.
                    # Open-Meteo's minutely counter resets on a boundary, so the
                    # only delay that helps is one that crosses it; 2s then 4s
                    # just spends three more attempts inside the same exhausted
                    # minute and fails the city. Honour Retry-After when sent.
                    delay = max(delay, retry_after)
                logger.warning(
                    "provider request failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt,
                    attempts,
                    delay,
                    last_error,
                )
                time.sleep(delay)

        self.error_count += 1
        raise last_error or ProviderError(f"request to {url} failed")

    # ------------------------------------------------------------------ parsing
    def _parse(
        self,
        payload: dict,
        *,
        city_id: str,
        dataset: str,
        endpoint: str,
        observation_type: ObservationType,
        tier: DataTier,
        start_date: date,
        end_date: date,
    ) -> list[DailyRecord]:
        daily = payload.get("daily")
        if not isinstance(daily, dict) or "time" not in daily:
            raise ProviderError(f"malformed response for {city_id}: missing 'daily.time'")

        times = daily["time"]
        units = payload.get("daily_units") or {}
        retrieved_at = datetime.now(UTC)
        records: list[DailyRecord] = []

        for index, day_str in enumerate(times):
            try:
                local_date = date.fromisoformat(day_str)
            except (TypeError, ValueError):
                logger.warning("skipping unparseable date %r for %s", day_str, city_id)
                continue
            if local_date < start_date or local_date > end_date:
                continue  # trim the forecast tier's wider window

            record = DailyRecord(
                city_id=city_id,
                local_date=local_date,
                source_provider=PROVIDER_NAME,
                source_dataset=dataset,
                source_endpoint=endpoint,
                observation_type=observation_type,
                data_tier=tier,
                units={
                    _FIELD_BY_VARIABLE[v]: units.get(v, "")
                    for v in DAILY_VARIABLES
                    if v in _FIELD_BY_VARIABLE
                },
                utc_offset_seconds=_as_int(payload.get("utc_offset_seconds")),
                grid_latitude=_as_float(payload.get("latitude")),
                grid_longitude=_as_float(payload.get("longitude")),
                grid_elevation_m=_as_float(payload.get("elevation")),
                retrieved_at=retrieved_at,
            )
            for variable, field_name in _FIELD_BY_VARIABLE.items():
                series = daily.get(variable)
                value = series[index] if isinstance(series, list) and index < len(series) else None
                setattr(record, field_name, _as_float(value))

            record.data_quality = record.classify_quality()
            records.append(record)

        records.sort(key=lambda r: r.local_date)
        return records


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # filter NaN


def _as_int(value: object) -> int | None:
    f = _as_float(value)
    return int(f) if f is not None else None


def _request_weight(params: dict) -> float:
    """Price a built parameter dict in weighted API calls.

    Reads the outgoing request rather than taking the caller's word for it, so a
    change to how a span is expressed — explicit dates on the archive endpoint,
    ``past_days`` on the forecast endpoint — cannot silently stop being counted.
    """
    variables = str(params.get("daily") or "")
    variable_count = len([name for name in variables.split(",") if name.strip()])

    start, end = params.get("start_date"), params.get("end_date")
    if isinstance(start, str) and isinstance(end, str):
        try:
            day_count = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
        except ValueError:
            day_count = 1
    else:
        day_count = int(params.get("past_days") or 0) + int(params.get("forecast_days") or 1)

    return estimate_call_weight(variable_count=variable_count, day_count=day_count)


def _retry_after_seconds(response: httpx.Response, *, default: float) -> float:
    """``Retry-After`` in seconds, falling back to ``default``.

    Accepts only the delta-seconds form. The HTTP-date form is legal but this
    provider does not send it, and mis-parsing a date into a multi-hour sleep
    would be worse than waiting out one window.
    """
    raw = response.headers.get("retry-after")
    if raw is None:
        return default
    try:
        seconds = float(raw.strip())
    except ValueError:
        return default
    if seconds <= 0:
        return default
    # A provider asking for an implausibly long wait is treated as asking for the
    # window it actually enforces; the caller's retry budget is finite.
    return min(seconds, 300.0)


def _extract_reason(response: httpx.Response) -> str:
    """Open-Meteo returns ``{"error": true, "reason": "..."}`` on failure."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict) and "reason" in body:
        return str(body["reason"])[:300]
    return str(body)[:200]


def chunk_date_range(
    start: date, end: date, chunk_years: int
) -> list[tuple[date, date]]:
    """Split a long span into request-sized chunks.

    Open-Meteo weights an API call by how much data it returns, so a single
    30-year daily request for five variables is a large draw against a free
    quota — and a large blast radius if it times out. Chunking by decade keeps
    each request modest and makes partial progress recoverable.
    """
    if end < start:
        raise ValueError("end must be >= start")
    if chunk_years < 1:
        raise ValueError("chunk_years must be >= 1")

    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        try:
            next_start = cursor.replace(year=cursor.year + chunk_years)
        except ValueError:  # 29 Feb + N years
            next_start = cursor.replace(year=cursor.year + chunk_years, day=28)
        chunk_end = min(next_start - timedelta(days=1), end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks
