"""Request-scoped concerns: rate limiting, pagination bounds, date parsing.

The API is public, unauthenticated, and read-only, which makes the interesting
failure mode not abuse but expense: an unbounded query that scans every
observation for every city, hit repeatedly. Everything here exists to make that
impossible rather than merely discouraged.

The rate limiter is a fixed-window in-process counter. That is the right amount
of machinery for this deployment — a single backend replica in front of a
precomputed table — and the wrong amount for a fleet, so the limitation is stated
rather than hidden: with two replicas the effective limit doubles. Moving to a
shared store would mean adding Redis, and adding a service to solve a problem
this project does not have is exactly the cost control the design is trying to
avoid.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from fastapi import Depends, HTTPException, Query, Request, status

from app.config import Settings, get_settings

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

#: Paths that never count against the limit, so an uptime check or a scheduler
#: probe cannot lock out real traffic.
EXEMPT_PATHS = frozenset({"/health", "/", "/docs", "/openapi.json", "/redoc"})


@dataclass
class _Window:
    started: float
    count: int


class FixedWindowLimiter:
    """Per-client fixed-window counter with a hard memory bound."""

    #: Distinct clients tracked at once. Past this the oldest windows are dropped,
    #: which bounds memory under a spray of forged X-Forwarded-For headers.
    MAX_TRACKED_CLIENTS = 20_000

    def __init__(self, requests_per_minute: int, burst: int) -> None:
        self.limit = requests_per_minute
        self.burst = burst
        self._windows: dict[str, _Window] = {}
        self._hits: defaultdict[str, int] = defaultdict(int)

    def check(self, key: str, now: float | None = None) -> tuple[bool, int, int]:
        """Return ``(allowed, remaining, retry_after_seconds)``."""
        now = now if now is not None else time.monotonic()
        window = self._windows.get(key)

        if window is None or now - window.started >= 60.0:
            if len(self._windows) >= self.MAX_TRACKED_CLIENTS:
                self._evict(now)
            self._windows[key] = _Window(started=now, count=1)
            return True, self.limit + self.burst - 1, 0

        window.count += 1
        ceiling = self.limit + self.burst
        if window.count > ceiling:
            retry_after = max(1, int(61.0 - (now - window.started)))
            return False, 0, retry_after
        return True, ceiling - window.count, 0

    def _evict(self, now: float) -> None:
        stale = [k for k, w in self._windows.items() if now - w.started >= 60.0]
        for key in stale:
            del self._windows[key]
        if not stale:
            # Everything is live; drop the oldest half rather than grow unbounded.
            ordered = sorted(self._windows.items(), key=lambda kv: kv[1].started)
            for key, _ in ordered[: len(ordered) // 2]:
                del self._windows[key]

    def reset(self) -> None:
        self._windows.clear()
        self._hits.clear()


_limiter: FixedWindowLimiter | None = None


def get_limiter(settings: Settings | None = None) -> FixedWindowLimiter:
    global _limiter
    if _limiter is None:
        settings = settings or get_settings()
        _limiter = FixedWindowLimiter(
            settings.rate_limit_requests_per_minute, settings.rate_limit_burst
        )
    return _limiter


def reset_limiter() -> None:
    """Test hook, and the way a config change takes effect after a reload."""
    global _limiter
    _limiter = None


def client_key(request: Request, settings: Settings) -> str:
    """Identify the caller.

    Behind Railway's proxy the socket address is the proxy, so the left-most
    ``X-Forwarded-For`` entry is the only thing that distinguishes clients. That
    header is trivially forged, which is why ``MAX_TRACKED_CLIENTS`` exists and
    why trusting it is a setting rather than an assumption.
    """
    if settings.rate_limit_trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Pagination and date bounds
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Page:
    limit: int
    offset: int


def pagination(
    limit: int = Query(50, ge=1, description="Rows to return. Capped by MAX_PAGE_SIZE."),
    offset: int = Query(0, ge=0, le=100_000, description="Rows to skip."),
    settings: Settings = Depends(get_settings),
) -> Page:
    """Bounded pagination.

    ``limit`` is clamped rather than rejected: a client asking for 10,000 rows
    gets the maximum page and a clear ``count`` telling it what it received,
    which is friendlier than a 422 and equally safe.
    """
    return Page(limit=min(limit, settings.max_page_size), offset=offset)


def parse_iso_date(value: str, field: str = "date") -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field} must be an ISO calendar date, YYYY-MM-DD (got {value!r})",
        ) from None


def history_range(
    start: str | None = Query(None, description="Inclusive ISO start date."),
    end: str | None = Query(None, description="Inclusive ISO end date."),
    days: int = Query(
        90, ge=1, description="Days back from end when start is omitted. Capped."
    ),
    settings: Settings = Depends(get_settings),
) -> tuple[date, date]:
    """A closed date range that can never exceed ``MAX_HISTORY_DAYS``.

    This is the endpoint most exposed to an expensive query, so the window is
    clamped from both directions: an over-long explicit range is truncated from
    the ``end`` backwards, which keeps the most recent data the caller asked for.
    """
    end_date = parse_iso_date(end, "end") if end else date.today()
    max_days = settings.max_history_days

    if start:
        start_date = parse_iso_date(start, "start")
        if start_date > end_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="start must not be after end",
            )
        if (end_date - start_date).days + 1 > max_days:
            start_date = end_date - timedelta(days=max_days - 1)
    else:
        start_date = end_date - timedelta(days=min(days, max_days) - 1)

    return start_date, end_date
