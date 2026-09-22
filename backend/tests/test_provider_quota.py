"""Tests for the Open-Meteo quota accounting.

These defend a single claim: **the application knows what a request costs before
it sends it.** Open-Meteo does not bill in HTTP requests, it bills in weighted API
calls, and one 10-year chunk of five daily variables is one request but roughly
130 calls. A naive request counter therefore ran 26x over the minutely allowance
and lost twelve cities to 429s — the failure this module exists to prevent.

Specifically:

* the weight estimate reproduces Open-Meteo's own published worked examples;
* the estimate is read off the outgoing parameters, so both endpoint shapes
  (explicit dates, ``past_days``) are priced rather than one silently costing
  nothing;
* a short wait for a window to reopen is waited out, and a long one raises
  instead of blocking a pipeline for hours;
* a 429 is retried after a full window rather than after two seconds, because a
  reset that happens on a minute boundary cannot be outrun by exponential
  backoff.

Nothing here touches the network. The one test that exercises retry timing
substitutes a transport that counts attempts and a clock that records sleeps.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.config import Settings
from app.providers.base import (
    ProviderBudgetExhausted,
    ProviderRateLimited,
    ProviderUnavailable,
)
from app.providers.open_meteo import (
    DAILY_VARIABLES,
    Budget,
    OpenMeteoProvider,
    RateLimiter,
    _request_weight,
    _retry_after_seconds,
    estimate_call_weight,
)


class TestPublishedWeighting:
    """The estimate matches Open-Meteo's documented examples, not our guesses."""

    def test_a_small_request_costs_one_call(self):
        # "more than 10 weather variables or ... more than 2 weeks" is where
        # multiplication starts, so anything smaller is a single call.
        assert estimate_call_weight(variable_count=5, day_count=1) == 1.0
        assert estimate_call_weight(variable_count=10, day_count=14) == 1.0

    @pytest.mark.parametrize(
        ("days", "variables", "expected"),
        [
            # Both worked examples from open-meteo.com/en/pricing, retrieved
            # 2026-09-22: "a request for 2 weeks of data with 15 weather
            # variables will be calculated as 1.5 API calls, while 4 weeks of
            # data equals 3.0 API calls".
            (14, 15, 1.5),
            (28, 15, 3.0),
        ],
    )
    def test_reproduces_the_documented_worked_examples(self, days, variables, expected):
        assert estimate_call_weight(variable_count=variables, day_count=days) == pytest.approx(
            expected
        )

    def test_a_decade_of_daily_history_is_expensive_and_says_so(self):
        # The number that matters: this is what a baseline chunk actually costs,
        # and why fifty cities cannot be built inside one day's free allowance.
        weight = estimate_call_weight(variable_count=len(DAILY_VARIABLES), day_count=3653)
        assert weight == pytest.approx(130.46, abs=0.01)
        # Three chunks per city, fifty cities, against a published 10,000/day.
        assert weight * 3 * 50 > 10_000

    def test_a_single_day_for_every_city_is_cheap(self):
        # The counterpart reassurance: the *daily* pipeline is nowhere near the
        # limit, so the quota is a one-time setup cost, not an operating one.
        daily = estimate_call_weight(variable_count=len(DAILY_VARIABLES), day_count=1)
        assert daily * 50 < 600


class TestPricingTheOutgoingRequest:
    """Weight is derived from the parameters actually being sent."""

    def test_prices_an_archive_request_from_its_explicit_dates(self):
        params = {
            "daily": ",".join(DAILY_VARIABLES),
            "start_date": "1991-01-01",
            "end_date": "2000-12-31",
        }
        assert _request_weight(params) == pytest.approx(130.46, abs=0.01)

    def test_prices_a_forecast_request_from_past_days(self):
        # Different shape, same accounting. If this returned 1.0 the near-real-time
        # tier would be spending quota invisibly.
        params = {"daily": ",".join(DAILY_VARIABLES), "past_days": 92, "forecast_days": 1}
        assert _request_weight(params) == pytest.approx(
            estimate_call_weight(variable_count=5, day_count=93)
        )

    def test_a_malformed_date_is_priced_as_cheap_rather_than_crashing(self):
        # Quota accounting must never be the thing that takes the pipeline down.
        params = {"daily": "temperature_2m_max", "start_date": "not-a-date", "end_date": "x"}
        assert _request_weight(params) == 1.0


class TestRateLimiter:
    """Short waits are waited out; long ones stop the run instead."""

    def test_spending_within_the_window_does_not_block(self):
        limiter = RateLimiter([Budget("minutely", 60.0, 100.0)])
        for _ in range(10):
            limiter.acquire(10.0)  # exactly the budget, no sleeping needed

    def test_exceeding_a_minute_waits_for_the_window(self, monkeypatch):
        slept: list[float] = []
        clock = {"now": 1_000.0}

        monkeypatch.setattr("app.providers.open_meteo.time.monotonic", lambda: clock["now"])

        def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            clock["now"] += seconds

        monkeypatch.setattr("app.providers.open_meteo.time.sleep", fake_sleep)

        limiter = RateLimiter([Budget("minutely", 60.0, 100.0)])
        limiter.acquire(100.0)
        limiter.acquire(100.0)

        # One wait, long enough to clear the whole window rather than a token pause.
        assert len(slept) == 1
        assert slept[0] == pytest.approx(60.05, abs=0.1)

    def test_an_exhausted_daily_window_raises_instead_of_sleeping_for_hours(self):
        limiter = RateLimiter(
            [Budget("daily", 86_400.0, 100.0)],
            max_sleep_seconds=180.0,
        )
        limiter.acquire(100.0)
        with pytest.raises(ProviderBudgetExhausted) as excinfo:
            limiter.acquire(100.0)
        # The message has to be actionable: which window, and that rerunning resumes.
        assert "daily" in str(excinfo.value)
        assert "resume" in str(excinfo.value)

    def test_the_tightest_window_is_the_one_that_binds(self, monkeypatch):
        # A request can fit the daily budget and still have to wait for the minute.
        monkeypatch.setattr("app.providers.open_meteo.time.sleep", lambda _s: None)
        limiter = RateLimiter(
            [Budget("minutely", 60.0, 10.0), Budget("daily", 86_400.0, 10_000.0)],
            max_sleep_seconds=0.0,  # any wait at all becomes an error we can observe
        )
        limiter.acquire(10.0)
        with pytest.raises(ProviderBudgetExhausted) as excinfo:
            limiter.acquire(10.0)
        assert "minutely" in str(excinfo.value)


class TestRetryAfter:
    def test_absent_header_falls_back_to_the_window(self):
        response = httpx.Response(429)
        assert _retry_after_seconds(response, default=60.0) == 60.0

    def test_delta_seconds_is_honoured(self):
        response = httpx.Response(429, headers={"Retry-After": "17"})
        assert _retry_after_seconds(response, default=60.0) == 17.0

    def test_an_http_date_is_not_mistaken_for_a_number(self):
        # The date form is legal HTTP but this provider does not send it, and
        # mis-parsing one into a multi-hour sleep is worse than waiting a minute.
        response = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        assert _retry_after_seconds(response, default=60.0) == 60.0

    def test_an_implausible_wait_is_capped(self):
        response = httpx.Response(429, headers={"Retry-After": "86400"})
        assert _retry_after_seconds(response, default=60.0) == 300.0


class TestRateLimitedRetries:
    """A 429 is retried after a window, not after two seconds."""

    @staticmethod
    def _settings() -> Settings:
        return Settings(
            environment="test",
            database_url="postgresql+psycopg://u:p@127.0.0.1:5432/db",
            weather_provider="open_meteo",
            provider_max_retries=2,
            provider_backoff_base_seconds=2.0,
        )

    def test_a_minutely_429_backs_off_past_the_minute_boundary(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("app.providers.open_meteo.time.sleep", slept.append)

        attempts = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(
                429,
                json={
                    "error": True,
                    "reason": "Minutely API request limit exceeded. Please try again in one minute.",
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        provider = OpenMeteoProvider(settings=self._settings(), client=client)

        with pytest.raises(ProviderRateLimited):
            provider.fetch_daily(
                city_id="test",
                latitude=45.0,
                longitude=-75.0,
                timezone="UTC",
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 7),
            )

        assert attempts["n"] == 3  # the initial call plus two retries
        # The regression this file exists for: every wait crosses a minute
        # boundary. The old exponential schedule slept 2s then 4s and failed.
        assert slept and all(delay >= 60.0 for delay in slept), slept

    def test_the_weight_of_every_attempt_is_counted(self, monkeypatch):
        monkeypatch.setattr("app.providers.open_meteo.time.sleep", lambda _s: None)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="upstream oops")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        provider = OpenMeteoProvider(settings=self._settings(), client=client)

        with pytest.raises(ProviderUnavailable):
            provider.fetch_daily(
                city_id="test",
                latitude=45.0,
                longitude=-75.0,
                timezone="UTC",
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 7),
            )

        # Failed attempts cost quota too; pretending otherwise is how a retry
        # storm turns into a day-long lockout.
        assert provider.request_count == 3
        assert provider.call_weight_spent == pytest.approx(3.0)
