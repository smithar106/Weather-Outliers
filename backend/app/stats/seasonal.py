"""Calendar and timezone handling for seasonal comparisons.

Two problems live here, and both are places where weather analyses quietly go
wrong:

1. **Leap days.** Day-of-year is not a stable seasonal coordinate: 1 March is
   day 60 in common years and day 61 in leap years, so a naive
   ``date.timetuple().tm_yday`` bucketing silently shifts half the reference
   period by one day. We therefore index every date on a fixed 365-day *no-leap*
   calendar derived from (month, day). 29 February is pooled into the 28
   February bucket rather than discarded, so leap-year observations still count.

2. **Daylight saving time.** "Yesterday" is a local calendar concept. Resolving
   it requires a real IANA zone, because a fixed UTC offset is wrong for half the
   year in most of North America — and wrong all year in Phoenix, Regina,
   Cancún, and Hermosillo, which never shift. Every function below takes a
   ``ZoneInfo`` and does arithmetic on aware datetimes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DAYS_IN_NOLEAP_YEAR = 365

# Cumulative days *before* each month in a common (non-leap) year.
_CUMULATIVE_DAYS_BEFORE_MONTH = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)

# The no-leap index that 29 February is folded into (28 February).
LEAP_DAY_FOLD_INDEX = 59


def noleap_day_of_year(d: date) -> int:
    """Map a date onto a fixed 365-day seasonal calendar (1..365).

    29 February folds onto 28 February's index. This keeps the seasonal window
    aligned across leap and common years, at the documented cost of the 28
    February bucket carrying a few extra samples (one per leap year in the
    reference period).

    >>> noleap_day_of_year(date(2021, 3, 1))
    60
    >>> noleap_day_of_year(date(2020, 3, 1))   # leap year: still 60
    60
    >>> noleap_day_of_year(date(2020, 2, 29))  # folded onto 28 Feb
    59
    """
    if d.month == 2 and d.day == 29:
        return LEAP_DAY_FOLD_INDEX
    return _CUMULATIVE_DAYS_BEFORE_MONTH[d.month - 1] + d.day


def is_leap_day(d: date) -> bool:
    return d.month == 2 and d.day == 29


def seasonal_window(center_doy: int, window_days: int) -> list[int]:
    """No-leap day indices within +/- ``window_days`` of ``center_doy``.

    The window wraps around the year boundary, so a target in late December
    correctly borrows samples from early January.

    >>> seasonal_window(1, 2)
    [364, 365, 1, 2, 3]
    """
    if not 1 <= center_doy <= DAYS_IN_NOLEAP_YEAR:
        raise ValueError(f"center_doy must be 1..365, got {center_doy}")
    if window_days < 0:
        raise ValueError("window_days must be >= 0")
    if window_days >= DAYS_IN_NOLEAP_YEAR // 2:
        return list(range(1, DAYS_IN_NOLEAP_YEAR + 1))

    offsets = range(-window_days, window_days + 1)
    return [((center_doy - 1 + o) % DAYS_IN_NOLEAP_YEAR) + 1 for o in offsets]


def window_label(center_doy: int, window_days: int, reference_year: int = 2001) -> str:
    """Human-readable seasonal window, e.g. ``"14 Jul – 28 Jul"``.

    ``reference_year`` is any common year; it only supplies month names.
    """
    idx = seasonal_window(center_doy, window_days)
    first = _date_from_noleap_index(idx[0], reference_year)
    last = _date_from_noleap_index(idx[-1], reference_year)
    return f"{first.day} {first.strftime('%b')} – {last.day} {last.strftime('%b')}"


def _date_from_noleap_index(doy: int, year: int) -> date:
    """Inverse of :func:`noleap_day_of_year` for a common year."""
    if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0):
        raise ValueError("_date_from_noleap_index requires a common (non-leap) year")
    return date(year, 1, 1) + timedelta(days=doy - 1)


# ---------------------------------------------------------------------------
# Timezone-aware "yesterday"
# ---------------------------------------------------------------------------


def local_midnight_utc(d: date, tz: ZoneInfo) -> datetime:
    """The UTC instant at which local calendar day ``d`` begins.

    Handles the pathological case of a DST spring-forward that skips local
    midnight itself (rare, but real — Havana and Santiago have done it). When
    00:00 does not exist we advance in one-minute steps to the first wall-clock
    time that does, which is the true start of the local day.
    """
    candidate = datetime.combine(d, time.min, tzinfo=tz)
    # Round-trip through UTC: if the wall clock changes, the time was skipped.
    for _ in range(24 * 60):
        round_tripped = candidate.astimezone(UTC).astimezone(tz)
        if round_tripped.date() == d and round_tripped.time() == candidate.time():
            return candidate.astimezone(UTC)
        candidate += timedelta(minutes=1)
    raise RuntimeError(f"could not resolve local midnight for {d} in {tz}")  # pragma: no cover


def utc_offset_seconds(d: date, tz: ZoneInfo) -> int:
    """UTC offset in effect for ``d`` in ``tz``, sampled at local noon.

    Noon is used because it is never ambiguous or non-existent under any DST
    rule in the tz database, unlike midnight or 02:00.
    """
    noon = datetime.combine(d, time(12, 0), tzinfo=tz)
    offset = noon.utcoffset()
    assert offset is not None  # aware by construction
    return int(offset.total_seconds())


def local_date_now(tz: ZoneInfo, now_utc: datetime | None = None) -> date:
    """Today's local calendar date in ``tz``."""
    now_utc = now_utc or datetime.now(UTC)
    return now_utc.astimezone(tz).date()


def latest_eligible_local_date(
    tz: ZoneInfo,
    now_utc: datetime | None = None,
    lag_hours: int = 6,
) -> date:
    """Most recent local calendar day that is both finished and old enough to fetch.

    A day is *finished* once local midnight has passed. It is *eligible* once a
    further ``lag_hours`` have elapsed, giving the upstream provider time to
    publish the completed day. If yesterday is not yet eligible we step back to
    the day before, rather than analysing a partial day.
    """
    now_utc = now_utc or datetime.now(UTC)
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")

    candidate = local_date_now(tz, now_utc) - timedelta(days=1)
    # At most a couple of steps in practice; the bound just prevents a spin.
    for _ in range(4):
        day_after = candidate + timedelta(days=1)
        available_at = local_midnight_utc(day_after, tz) + timedelta(hours=lag_hours)
        if now_utc >= available_at:
            return candidate
        candidate -= timedelta(days=1)
    return candidate  # pragma: no cover - lag_hours would have to exceed ~72h


def is_local_date_complete(
    d: date, tz: ZoneInfo, now_utc: datetime | None = None, lag_hours: int = 6
) -> bool:
    """Whether local day ``d`` has ended in ``tz`` and the source lag has elapsed."""
    now_utc = now_utc or datetime.now(UTC)
    available_at = local_midnight_utc(d + timedelta(days=1), tz) + timedelta(hours=lag_hours)
    return now_utc >= available_at


def reference_period_dates(
    start_year: int, end_year: int
) -> tuple[date, date]:
    """Inclusive calendar bounds of the climatological reference period."""
    if end_year < start_year:
        raise ValueError("end_year must be >= start_year")
    return date(start_year, 1, 1), date(end_year, 12, 31)
