"""Tests for the statistical core: distributions, anomalies, calendar, ranking.

These are the tests that decide whether the product is honest. The pipeline and
API suites prove the plumbing works; this module proves the numbers mean what the
methodology page says they mean. It is organised around the specific claims the
project makes, and each class states the claim it is defending:

* an empirical tail probability read back off a stored sketch matches the
  analytically known value for a sample whose quantiles we can compute by hand;
* rainfall is scored as a zero-inflated mixture, and normal theory applied to the
  same sample would be wrong by orders of magnitude;
* a small or missing sample yields a *bounded* answer rather than a confident
  one;
* leap days and daylight saving time do not silently shift the seasonal window
  or the analysed calendar day;
* two events with the same empirical rarity score the same regardless of unit or
  distribution shape, even when their z-scores differ substantially;
* exact ties resolve through a fixed chain, so a rerun reproduces the board.

Nothing here touches the network or the database.
"""

from __future__ import annotations

import inspect
import itertools
import math
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain import (
    METHODOLOGY_VERSION,
    METRIC_TIEBREAK_ORDER,
    Direction,
    Metric,
    build_event_id,
    metric_tiebreak_index,
)
from app.stats import anomaly as anomaly_module
from app.stats import ranking as ranking_module
from app.stats.anomaly import (
    COMPARISON_DECIMALS,
    MARGIN_WEIGHT,
    AnomalyCandidate,
    compute_anomaly,
)
from app.stats.distributions import (
    QUANTILE_GRID,
    TAIL_ORDER_STATS,
    DistributionSketch,
    build_histogram,
    build_sketch,
    empirical_cdf,
    normal_sf,
    quantile,
    return_period_years,
    robust_deviation,
    sample_std,
    tail_probability,
    zero_inflated_upper_tail,
)
from app.stats.ranking import TIEBREAK_CHAIN, rank_candidates, ranking_sort_key
from app.stats.seasonal import (
    DAYS_IN_NOLEAP_YEAR,
    LEAP_DAY_FOLD_INDEX,
    _date_from_noleap_index,
    is_leap_day,
    is_local_date_complete,
    latest_eligible_local_date,
    local_date_now,
    local_midnight_utc,
    noleap_day_of_year,
    reference_period_dates,
    seasonal_window,
    utc_offset_seconds,
    window_label,
)
from tests.conftest import baseline_from_values, synthetic_temperatures

NEW_YORK = ZoneInfo("America/New_York")
PHOENIX = ZoneInfo("America/Phoenix")
DENVER = ZoneInfo("America/Denver")
HAVANA = ZoneInfo("America/Havana")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def uniform_sample(n: int = 1000) -> list[float]:
    """``0.0 .. n-1``: every quantile is computable in closed form by hand."""
    return [float(i) for i in range(n)]


def solve_for_tail_probability(
    sketch: DistributionSketch, target: float, *, lo: float, hi: float
) -> float:
    """Find the observation whose upper-tail probability is ``target``.

    Bisection on a monotone function. Used to place two *different* metrics at the
    same empirical rarity so the cross-metric comparability claim can be tested
    directly rather than asserted.
    """
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if tail_probability(sketch, mid, "above").probability > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def candidate(
    *,
    city_id: str,
    metric: Metric = Metric.TEMP_MAX,
    score: float,
    tail: float = 0.01,
    robust: float = 1.0,
    eligible: bool = True,
    excluded_reason: str | None = None,
) -> AnomalyCandidate:
    """A bare candidate for ranking tests, with the sort-key fields pinned."""
    return AnomalyCandidate(
        city_id=city_id,
        local_date="2026-09-21",
        metric=metric,
        direction=Direction.ABOVE,
        observed_value=40.0,
        unit="°C",
        anomaly_score=score,
        tail_probability=tail,
        robust_deviation=robust,
        eligible=eligible,
        excluded_reason=excluded_reason,
    )


# ---------------------------------------------------------------------------
# Quantiles and sketches
# ---------------------------------------------------------------------------


class TestQuantilesMatchTheDocumentedConvention:
    """R type 7 / NumPy default: ``h = (n-1)p``, linear between order statistics.

    Hand-computed expectations, not self-comparisons. If someone swaps in a
    different quantile definition the sketch silently changes meaning, so the
    convention is pinned here with arithmetic anyone can check on paper.
    """

    def test_interpolates_between_order_statistics(self):
        # n=4, p=0.25 -> h = 3*0.25 = 0.75 -> 10*0.25 + 20*0.75 = 17.5
        assert quantile([10.0, 20.0, 30.0, 40.0], 0.25) == pytest.approx(17.5)
        # p=0.5 -> h = 1.5 -> midpoint of 20 and 30
        assert quantile([10.0, 20.0, 30.0, 40.0], 0.5) == pytest.approx(25.0)
        # p=0.75 -> h = 2.25 -> 30*0.75 + 40*0.25 = 32.5
        assert quantile([10.0, 20.0, 30.0, 40.0], 0.75) == pytest.approx(32.5)

    def test_lands_exactly_on_an_order_statistic_when_h_is_an_integer(self):
        # n=5, p=0.25 -> h = 1.0 -> the second value, no interpolation
        assert quantile([1.0, 2.0, 3.0, 4.0, 5.0], 0.25) == pytest.approx(2.0)
        assert quantile([1.0, 2.0, 3.0, 4.0, 5.0], 0.1) == pytest.approx(1.4)

    def test_endpoints_are_the_extremes(self):
        values = [3.0, 7.0, 11.0]
        assert quantile(values, 0.0) == 3.0
        assert quantile(values, 1.0) == 11.0

    def test_a_single_observation_is_its_own_every_quantile(self):
        assert quantile([4.2], 0.0) == 4.2
        assert quantile([4.2], 0.99) == 4.2

    def test_an_empty_sample_is_an_error_not_a_zero(self):
        with pytest.raises(ValueError, match="empty sample"):
            quantile([], 0.5)

    def test_a_probability_outside_the_unit_interval_is_rejected(self):
        with pytest.raises(ValueError, match=r"p must be in \[0, 1\]"):
            quantile([1.0, 2.0], 1.5)

    def test_sample_std_is_bessel_corrected(self):
        # mean 3, sum of squared deviations 10, /4 = 2.5, sqrt = 1.5811388...
        assert sample_std([1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx(math.sqrt(2.5))

    def test_sample_std_is_undefined_below_two_observations(self):
        assert sample_std([]) is None
        assert sample_std([9.0]) is None


class TestSketchesPreserveWhatTheyPromise:
    def test_a_sketch_stores_the_tail_dense_grid_and_the_extreme_order_statistics(self):
        sketch = build_sketch(uniform_sample(1000))
        assert sketch.n == 1000
        assert sketch.grid == QUANTILE_GRID
        assert len(sketch.values) == len(QUANTILE_GRID)
        assert len(sketch.low_order_stats) == TAIL_ORDER_STATS
        assert len(sketch.high_order_stats) == TAIL_ORDER_STATS
        assert sketch.low_order_stats == (0.0, 1.0, 2.0, 3.0, 4.0)
        assert sketch.high_order_stats == (995.0, 996.0, 997.0, 998.0, 999.0)

    def test_summary_statistics_are_the_hand_computed_ones(self):
        sketch = build_sketch(uniform_sample(1000))
        assert sketch.median == pytest.approx(499.5)
        assert sketch.p25 == pytest.approx(249.75)
        assert sketch.p75 == pytest.approx(749.25)
        assert sketch.iqr == pytest.approx(499.5)
        assert sketch.minimum == 0.0
        assert sketch.maximum == 999.0

    def test_non_finite_and_missing_values_are_dropped_not_propagated(self):
        sketch = build_sketch([1.0, None, float("nan"), 3.0, float("inf"), 2.0])  # type: ignore[list-item]
        assert sketch.n == 3
        assert sketch.maximum == 3.0
        assert sketch.mean == pytest.approx(2.0)

    def test_an_empty_sample_produces_an_empty_sketch_rather_than_raising(self):
        sketch = build_sketch([])
        assert sketch.is_empty
        assert sketch.n == 0
        assert sketch.mean is None
        assert sketch.probability_floor == 1.0

    def test_a_sketch_survives_the_json_round_trip_the_database_puts_it_through(self):
        original = build_sketch(synthetic_temperatures(n=300))
        restored = DistributionSketch.from_json(original.to_json())
        assert restored == original

    def test_the_probability_floor_is_one_over_n_plus_one(self):
        assert build_sketch(uniform_sample(450)).probability_floor == pytest.approx(1 / 451)
        assert build_sketch(uniform_sample(10)).probability_floor == pytest.approx(1 / 11)

    def test_the_reconstructed_cdf_is_monotone(self):
        sketch = build_sketch(uniform_sample(1000))
        previous = -1.0
        for step in range(0, 2100, 5):
            current = empirical_cdf(sketch, step / 2.0)
            assert current >= previous - 1e-12
            previous = current

    def test_the_cdf_of_an_empty_sketch_is_not_a_number_rather_than_a_guess(self):
        assert math.isnan(empirical_cdf(DistributionSketch(n=0), 10.0))


# ---------------------------------------------------------------------------
# Tail probabilities on an analytically known sample
# ---------------------------------------------------------------------------


class TestTailProbabilitiesOnAKnownSample:
    """Read back probabilities whose true values we can compute by hand.

    The sample is ``0..999``, so the empirical CDF is the identity scaled by
    1/999 and every expectation below is arithmetic rather than a re-run of the
    implementation.
    """

    @pytest.fixture
    def sketch(self) -> DistributionSketch:
        return build_sketch(uniform_sample(1000))

    def test_the_median_splits_the_distribution_in_half_in_both_directions(self, sketch):
        above = tail_probability(sketch, 499.5, "above")
        below = tail_probability(sketch, 499.5, "below")
        assert above.probability == pytest.approx(0.5, abs=1e-9)
        assert below.probability == pytest.approx(0.5, abs=1e-9)
        assert above.percentile == pytest.approx(50.0, abs=1e-9)

    def test_the_ninety_ninth_percentile_returns_a_one_percent_tail(self, sketch):
        # quantile(0.99) of 0..999 is h = 999*0.99 = 989.01
        result = tail_probability(sketch, 989.01, "above")
        assert result.probability == pytest.approx(0.01, abs=1e-9)
        assert result.percentile == pytest.approx(99.0, abs=1e-9)
        assert not result.bounded
        assert not result.beyond_sample

    def test_the_ninety_fifth_percentile_returns_a_five_percent_tail(self, sketch):
        result = tail_probability(sketch, 949.05, "above")
        assert result.probability == pytest.approx(0.05, abs=1e-9)

    def test_the_sample_maximum_is_reported_as_at_least_as_rare_as_the_floor(self, sketch):
        result = tail_probability(sketch, 999.0, "above")
        assert result.probability == pytest.approx(1 / 1001)
        assert result.bounded, "hitting the resolution floor must be flagged, not hidden"
        assert not result.beyond_sample

    def test_an_unprecedented_value_is_flagged_as_outside_the_sample(self, sketch):
        result = tail_probability(sketch, 1500.0, "above")
        assert result.probability == pytest.approx(1 / 1001)
        assert result.bounded
        assert result.beyond_sample

    def test_probability_never_exceeds_one_or_falls_to_zero(self, sketch):
        for x in (-500.0, 0.0, 250.0, 999.0, 2000.0):
            for direction in ("above", "below"):
                p = tail_probability(sketch, x, direction).probability
                assert 0.0 < p <= 1.0

    def test_percentile_and_upper_tail_are_consistent_away_from_the_floor(self, sketch):
        for x in (200.0, 400.0, 600.0, 800.0):
            result = tail_probability(sketch, x, "above")
            assert result.percentile == pytest.approx(100.0 * (1.0 - result.probability))

    def test_an_unknown_direction_is_a_programming_error(self, sketch):
        with pytest.raises(ValueError, match="direction must be"):
            tail_probability(sketch, 500.0, "sideways")

    def test_an_empty_sketch_yields_nan_rather_than_a_fabricated_probability(self):
        result = tail_probability(DistributionSketch(n=0), 40.0, "above")
        assert math.isnan(result.probability)
        assert math.isnan(result.percentile)


# ---------------------------------------------------------------------------
# Zero-heavy precipitation
# ---------------------------------------------------------------------------

#: 400 reference days, 360 of them dry, wet days of 1..40 mm. A deliberately
#: dry climate: 90% of the probability mass sits on exactly zero.
DRY_CLIMATE_PRECIP = [0.0] * 360 + [float(i) for i in range(1, 41)]

#: 400 reference days, half wet, amounts 0.5..50 mm.
WET_CLIMATE_PRECIP = [0.0] * 200 + [0.5 + i * 0.25 for i in range(200)]


class TestZeroHeavyPrecipitation:
    """Rainfall is a mixture, and treating it as one continuous variable is wrong.

    The spec's requirement is an empirical distribution that handles zero-heavy
    rainfall. The tests below check both halves of that: the mixture arithmetic
    itself, and the magnitude of the error that the naive normal-theory
    alternative would have made on the identical sample.
    """

    @pytest.fixture
    def dry(self):
        return baseline_from_values(DRY_CLIMATE_PRECIP, metric=Metric.PRECIPITATION)

    def test_the_baseline_splits_the_mixture_explicitly(self, dry):
        assert dry.n_samples == 400
        assert dry.nonzero_n == 40
        assert dry.zero_fraction == pytest.approx(0.9)
        assert dry.nonzero_sketch is not None
        assert dry.nonzero_sketch.n == 40

    def test_a_dry_day_is_not_an_upper_tail_event(self, dry):
        result = zero_inflated_upper_tail(
            zero_fraction=dry.zero_fraction,
            nonzero_sketch=dry.nonzero_sketch,
            x=0.0,
            total_n=dry.n_samples,
        )
        assert result.probability == 1.0
        assert result.percentile == 0.0
        assert not result.bounded

    def test_the_upper_tail_never_exceeds_the_wet_day_probability(self, dry):
        # P(X >= x) = P(wet) * P(X >= x | wet) <= P(wet) = 0.1
        for x in (0.1, 1.0, 5.0, 20.0):
            result = zero_inflated_upper_tail(
                zero_fraction=dry.zero_fraction,
                nonzero_sketch=dry.nonzero_sketch,
                x=x,
                total_n=dry.n_samples,
            )
            assert result.probability <= 0.1 + 1e-12

    def test_the_mixture_is_the_product_of_the_two_stages(self, dry):
        x = 20.0
        result = zero_inflated_upper_tail(
            zero_fraction=dry.zero_fraction,
            nonzero_sketch=dry.nonzero_sketch,
            x=x,
            total_n=dry.n_samples,
        )
        p_wet = 0.1
        conditional = 1.0 - empirical_cdf(dry.nonzero_sketch, x)
        assert result.probability == pytest.approx(p_wet * conditional)

    def test_normal_theory_on_the_same_sample_would_be_wrong_by_orders_of_magnitude(self, dry):
        """The claim the methodology page makes, measured.

        A 40 mm day is the wettest in 400 reference days: an empirical
        probability of about 1-in-400. Summarising the zero-heavy sample by its
        mean and standard deviation and reading the normal tail instead gives
        1-in-17-million, which would put a merely-wet day at the top of the
        board every time it rained in a dry city.
        """
        combined = build_sketch(DRY_CLIMATE_PRECIP)
        empirical = zero_inflated_upper_tail(
            zero_fraction=dry.zero_fraction,
            nonzero_sketch=dry.nonzero_sketch,
            x=40.0,
            total_n=dry.n_samples,
        ).probability
        normal_theory = normal_sf((40.0 - combined.mean) / combined.std)

        assert empirical == pytest.approx(1 / 401)
        assert normal_theory < 1e-6
        assert empirical / normal_theory > 1000.0

    def test_the_same_rainfall_is_rarer_in_a_dry_climate_than_a_wet_one(self, dry):
        wet = baseline_from_values(WET_CLIMATE_PRECIP, metric=Metric.PRECIPITATION)
        x = 25.0
        dry_p = zero_inflated_upper_tail(
            zero_fraction=dry.zero_fraction,
            nonzero_sketch=dry.nonzero_sketch,
            x=x,
            total_n=dry.n_samples,
        ).probability
        wet_p = zero_inflated_upper_tail(
            zero_fraction=wet.zero_fraction,
            nonzero_sketch=wet.nonzero_sketch,
            x=x,
            total_n=wet.n_samples,
        ).probability
        assert dry_p < wet_p, "identical magnitude, different climates: rarity must differ"

    def test_a_climate_with_no_recorded_wet_day_yields_nan_rather_than_a_guess(self):
        result = zero_inflated_upper_tail(
            zero_fraction=1.0,
            nonzero_sketch=DistributionSketch(n=0),
            x=5.0,
            total_n=400,
        )
        assert math.isnan(result.probability)

    def test_a_dry_day_is_recorded_but_excluded_from_the_board(self, dry):
        scored = compute_anomaly(
            city_id="us-phoenix-az",
            local_date="2026-09-21",
            metric=Metric.PRECIPITATION,
            observed_value=0.0,
            baseline=dry,
        )
        assert scored is not None
        assert not scored.eligible
        assert scored.excluded_reason == "dry_day_not_ranked"

    def test_a_z_score_is_withheld_entirely_for_precipitation(self, dry):
        scored = compute_anomaly(
            city_id="us-phoenix-az",
            local_date="2026-09-21",
            metric=Metric.PRECIPITATION,
            observed_value=55.0,
            baseline=dry,
        )
        assert scored.z_score is None
        assert scored.z_valid is False
        assert scored.evidence["calculation"]["z_score"] is None

    def test_a_climate_with_too_few_wet_days_is_excluded_with_a_reason(self):
        # 8 wet days in 400: the mixture's wet-day component is not estimable.
        barely_ever_rains = [0.0] * 392 + [float(i) for i in range(1, 9)]
        baseline = baseline_from_values(barely_ever_rains, metric=Metric.PRECIPITATION)
        scored = compute_anomaly(
            city_id="us-phoenix-az",
            local_date="2026-09-21",
            metric=Metric.PRECIPITATION,
            observed_value=8.0,
            baseline=baseline,
            min_wet_days=15,
        )
        assert not scored.eligible
        assert scored.excluded_reason in ("insufficient_baseline", "insufficient_wet_days")


# ---------------------------------------------------------------------------
# Missing data and small samples
# ---------------------------------------------------------------------------


class TestMissingDataAndSmallSamples:
    """A thin or absent baseline must produce a bounded answer, never a bold one."""

    @pytest.fixture
    def temps(self):
        return baseline_from_values(synthetic_temperatures(n=450), metric=Metric.TEMP_MAX)

    def test_a_missing_observation_scores_nothing_at_all(self, temps):
        assert (
            compute_anomaly(
                city_id="a",
                local_date="2026-09-21",
                metric=Metric.TEMP_MAX,
                observed_value=None,
                baseline=temps,
            )
            is None
        )

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_observation_scores_nothing_at_all(self, temps, bad):
        assert (
            compute_anomaly(
                city_id="a",
                local_date="2026-09-21",
                metric=Metric.TEMP_MAX,
                observed_value=bad,
                baseline=temps,
            )
            is None
        )

    def test_a_missing_baseline_is_reported_as_a_reason_not_dropped_silently(self):
        scored = compute_anomaly(
            city_id="a",
            local_date="2026-09-21",
            metric=Metric.TEMP_MAX,
            observed_value=41.0,
            baseline=None,
        )
        assert scored is not None
        assert not scored.eligible
        assert scored.excluded_reason == "no_baseline"
        assert scored.anomaly_score == 0.0

    def test_an_empty_baseline_sketch_is_treated_the_same_as_a_missing_one(self, temps):
        temps.sketch = DistributionSketch(n=0)
        scored = compute_anomaly(
            city_id="a",
            local_date="2026-09-21",
            metric=Metric.TEMP_MAX,
            observed_value=41.0,
            baseline=temps,
        )
        assert scored.excluded_reason == "no_baseline"

    def test_a_ten_sample_baseline_cannot_manufacture_certainty(self):
        """The core small-sample guarantee.

        With ten reference values the finest resolvable probability is 1/11, so
        the surprisal term is capped at ``-log10(1/11)`` ~= 1.04 no matter how
        absurd the observation is. A 60 °C reading against a 17-26 °C sample must
        not be reported as a one-in-a-million event.
        """
        baseline = baseline_from_values([17.0 + i for i in range(10)], metric=Metric.TEMP_MAX)
        assert baseline.n_samples == 10
        assert baseline.sketch.probability_floor == pytest.approx(1 / 11)

        scored = compute_anomaly(
            city_id="a",
            local_date="2026-09-21",
            metric=Metric.TEMP_MAX,
            observed_value=60.0,
            baseline=baseline,
        )
        assert scored.tail_probability == pytest.approx(1 / 11)
        assert scored.tail_probability_is_bounded
        assert scored.surprisal == pytest.approx(-math.log10(1 / 11))
        assert scored.surprisal < 1.05

    def test_an_insufficient_baseline_is_never_eligible_however_extreme_the_day(self):
        baseline = baseline_from_values([17.0 + i for i in range(10)], metric=Metric.TEMP_MAX)
        assert not baseline.sufficient
        scored = compute_anomaly(
            city_id="a",
            local_date="2026-09-21",
            metric=Metric.TEMP_MAX,
            observed_value=60.0,
            baseline=baseline,
        )
        assert not scored.eligible
        assert scored.excluded_reason == "insufficient_baseline"

    def test_a_z_score_is_only_marked_valid_on_a_sufficient_symmetric_baseline(self):
        thin = baseline_from_values([17.0 + i for i in range(10)], metric=Metric.TEMP_MAX)
        scored = compute_anomaly(
            city_id="a",
            local_date="2026-09-21",
            metric=Metric.TEMP_MAX,
            observed_value=30.0,
            baseline=thin,
        )
        assert scored.z_score is not None, "reported for familiarity"
        assert scored.z_valid is False, "but not to be read as a probability"

    def test_an_ordinary_day_falls_below_the_score_threshold(self, temps):
        scored = compute_anomaly(
            city_id="a",
            local_date="2026-09-21",
            metric=Metric.TEMP_MAX,
            observed_value=temps.sketch.median,
            baseline=temps,
            min_score=0.5,
        )
        assert scored.tail_probability == pytest.approx(0.5, abs=1e-9)
        assert scored.anomaly_score == pytest.approx(-math.log10(0.5), abs=1e-6)
        assert not scored.eligible
        assert scored.excluded_reason == "below_score_threshold"

    def test_every_excluded_reason_the_ranker_reports_is_one_the_scorer_can_emit(self):
        """Guards against a reason string drifting out of sync with the UI copy."""
        source = inspect.getsource(anomaly_module.compute_anomaly)
        for reason in (
            "no_baseline",
            "insufficient_baseline",
            "dry_day_not_ranked",
            "insufficient_wet_days",
            "tail_probability_unavailable",
            "below_score_threshold",
        ):
            assert f'"{reason}"' in source


# ---------------------------------------------------------------------------
# Temperature anomalies, direction, and the score formula
# ---------------------------------------------------------------------------


class TestTemperatureAnomalies:
    @pytest.fixture
    def temps(self):
        return baseline_from_values(
            synthetic_temperatures(n=450, centre=22.0, spread=3.0), metric=Metric.TEMP_MAX
        )

    def test_a_hot_day_is_tested_in_the_upper_tail_and_a_cold_one_in_the_lower(self, temps):
        hot = compute_anomaly(
            city_id="a",
            local_date="2026-09-21",
            metric=Metric.TEMP_MAX,
            observed_value=temps.sketch.maximum,
            baseline=temps,
        )
        cold = compute_anomaly(
            city_id="a",
            local_date="2026-09-21",
            metric=Metric.TEMP_MAX,
            observed_value=temps.sketch.minimum,
            baseline=temps,
        )
        assert hot.direction == Direction.ABOVE
        assert cold.direction == Direction.BELOW

    def test_both_tails_of_a_symmetric_sample_score_alike(self, temps):
        """Temperature is two-tailed: a record cold morning ranks like a heatwave."""
        hot = compute_anomaly(
            city_id="a",
            local_date="2026-09-21",
            metric=Metric.TEMP_MAX,
            observed_value=temps.sketch.maximum,
            baseline=temps,
        )
        cold = compute_anomaly(
            city_id="a",
            local_date="2026-09-21",
            metric=Metric.TEMP_MAX,
            observed_value=temps.sketch.minimum,
            baseline=temps,
        )
        assert hot.tail_probability == pytest.approx(cold.tail_probability, rel=0.05)
        assert hot.anomaly_score == pytest.approx(cold.anomaly_score, abs=0.2)

    def test_the_score_is_the_documented_formula(self, temps):
        scored = compute_anomaly(
            city_id="a",
            local_date="2026-09-21",
            metric=Metric.TEMP_MAX,
            observed_value=temps.sketch.maximum + 3.0,
            baseline=temps,
        )
        expected = scored.surprisal + scored.margin_bonus
        assert scored.anomaly_score == pytest.approx(round(expected, COMPARISON_DECIMALS))
        assert scored.evidence["calculation"]["score_formula"] == (
            "anomaly_score = -log10(p_tail) + 0.5 * log10(1 + margin_iqr)"
        )

    def test_the_margin_term_only_applies_beyond_the_reference_sample(self, temps):
        inside = compute_anomaly(
            city_id="a",
            local_date="2026-09-21",
            metric=Metric.TEMP_MAX,
            observed_value=temps.sketch.maximum,
            baseline=temps,
        )
        outside = compute_anomaly(
            city_id="a",
            local_date="2026-09-21",
            metric=Metric.TEMP_MAX,
            observed_value=temps.sketch.maximum + temps.sketch.iqr,
            baseline=temps,
        )
        assert inside.margin_bonus == 0.0
        assert inside.evidence["calculation"]["out_of_sample_margin_scale"] == "none"
        assert outside.margin_bonus == pytest.approx(MARGIN_WEIGHT * math.log10(2.0), rel=1e-6)
        assert outside.evidence["calculation"]["out_of_sample_margin_scale"] == "iqr"

    def test_the_margin_term_cannot_let_magnitude_overturn_rarity(self, temps):
        """An absurd margin adds less than one order of magnitude to the score.

        This is the guard on the spec's "raw magnitude never ranks": ten IQRs past
        the record adds ~0.52, while a single decade of extra rarity adds 1.0.
        """
        absurd = compute_anomaly(
            city_id="a",
            local_date="2026-09-21",
            metric=Metric.TEMP_MAX,
            observed_value=temps.sketch.maximum + 10 * temps.sketch.iqr,
            baseline=temps,
        )
        assert absurd.margin_bonus < 1.0

    def test_deviation_is_measured_from_the_mean_for_temperature(self, temps):
        scored = compute_anomaly(
            city_id="a",
            local_date="2026-09-21",
            metric=Metric.TEMP_MAX,
            observed_value=30.0,
            baseline=temps,
        )
        assert scored.evidence["calculation"]["deviation_reference"] == "mean"
        assert scored.deviation == pytest.approx(30.0 - temps.sketch.mean)

    def test_deviation_is_measured_from_the_median_for_skewed_metrics(self):
        gusts = baseline_from_values(
            [26.0 * math.exp(0.32 * ((i % 37) / 37 * 4 - 2)) for i in range(450)],
            metric=Metric.WIND_GUST,
        )
        scored = compute_anomaly(
            city_id="a",
            local_date="2026-09-21",
            metric=Metric.WIND_GUST,
            observed_value=60.0,
            baseline=gusts,
        )
        assert scored.evidence["calculation"]["deviation_reference"] == "median"
        assert scored.deviation == pytest.approx(60.0 - gusts.sketch.median)

    def test_the_evidence_trace_carries_the_whole_calculation(self, temps):
        scored = compute_anomaly(
            city_id="a",
            local_date="2026-09-21",
            metric=Metric.TEMP_MAX,
            observed_value=33.0,
            baseline=temps,
        )
        baseline_evidence = scored.evidence["baseline"]
        assert baseline_evidence["reference_period"] == "1991-2020"
        assert baseline_evidence["seasonal_window_days"] == 7
        assert baseline_evidence["seasonal_window_day_count"] == 15
        assert baseline_evidence["n_samples"] == 450
        assert scored.evidence["methodology_version"] == METHODOLOGY_VERSION
        calculation = scored.evidence["calculation"]
        for key in (
            "tail_probability",
            "percentile",
            "surprisal_neg_log10_p",
            "margin_bonus",
            "anomaly_score",
            "probability_floor",
            "approx_return_period_years",
        ):
            assert key in calculation

    def test_the_return_period_is_stated_per_seasonal_window(self, temps):
        scored = compute_anomaly(
            city_id="a",
            local_date="2026-09-21",
            metric=Metric.TEMP_MAX,
            observed_value=temps.sketch.maximum,
            baseline=temps,
        )
        expected = 1.0 / (scored.tail_probability * 15)
        assert scored.return_period_years == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Leap years
# ---------------------------------------------------------------------------


class TestLeapYears:
    """Day-of-year is not a seasonal coordinate; the no-leap calendar is."""

    def test_the_first_of_march_is_the_same_seasonal_day_in_every_year(self):
        assert noleap_day_of_year(date(2020, 3, 1)) == 60  # leap year
        assert noleap_day_of_year(date(2021, 3, 1)) == 60  # common year
        assert date(2020, 3, 1).timetuple().tm_yday != date(2021, 3, 1).timetuple().tm_yday

    def test_the_leap_day_folds_onto_the_twenty_eighth_of_february(self):
        assert noleap_day_of_year(date(2020, 2, 29)) == LEAP_DAY_FOLD_INDEX == 59
        assert noleap_day_of_year(date(2020, 2, 28)) == 59
        assert noleap_day_of_year(date(2021, 2, 28)) == 59
        assert is_leap_day(date(2020, 2, 29))
        assert not is_leap_day(date(2021, 2, 28))

    def test_the_year_ends_on_index_three_six_five_in_leap_and_common_years(self):
        assert noleap_day_of_year(date(2020, 12, 31)) == DAYS_IN_NOLEAP_YEAR
        assert noleap_day_of_year(date(2021, 12, 31)) == DAYS_IN_NOLEAP_YEAR
        assert noleap_day_of_year(date(2021, 1, 1)) == 1

    def test_the_mapping_covers_all_three_six_five_indices_exactly_once_per_common_year(self):
        indices = [
            noleap_day_of_year(date(2021, 1, 1) + timedelta(days=i)) for i in range(365)
        ]
        assert sorted(indices) == list(range(1, 366))

    def test_the_reference_period_pools_leap_days_at_the_documented_cost(self):
        """Every bucket gets 30 samples except 28 February, which gets 38.

        1991-2020 contains 8 leap years, and their 29 February observations are
        pooled rather than discarded — the documented tradeoff. If this ever
        returned 30 for bucket 59 the leap observations would be silently lost;
        if it returned 31 buckets of varying size the window would be misaligned.
        """
        start, end = reference_period_dates(1991, 2020)
        counts: dict[int, int] = {}
        day = start
        while day <= end:
            index = noleap_day_of_year(day)
            counts[index] = counts.get(index, 0) + 1
            day += timedelta(days=1)

        assert len(counts) == DAYS_IN_NOLEAP_YEAR
        assert counts[LEAP_DAY_FOLD_INDEX] == 38
        assert sorted(set(counts.values())) == [30, 38]

    def test_the_seasonal_window_wraps_the_year_boundary(self):
        assert seasonal_window(1, 2) == [364, 365, 1, 2, 3]
        assert seasonal_window(365, 2) == [363, 364, 365, 1, 2]
        assert seasonal_window(180, 2) == [178, 179, 180, 181, 182]

    def test_a_window_is_always_odd_sized_and_free_of_repeats(self):
        for centre in (1, 59, 60, 182, 365):
            window = seasonal_window(centre, 7)
            assert len(window) == 15
            assert len(set(window)) == 15
            assert centre in window

    def test_a_zero_width_window_is_the_day_itself(self):
        assert seasonal_window(100, 0) == [100]

    def test_an_over_wide_window_collapses_to_the_whole_year_instead_of_repeating_days(self):
        assert seasonal_window(100, 200) == list(range(1, 366))

    @pytest.mark.parametrize("bad_doy", [0, 366, -1])
    def test_a_day_index_outside_the_calendar_is_rejected(self, bad_doy):
        with pytest.raises(ValueError, match="center_doy must be"):
            seasonal_window(bad_doy, 7)

    def test_a_negative_window_is_rejected(self):
        with pytest.raises(ValueError, match="window_days must be >= 0"):
            seasonal_window(100, -1)

    def test_the_index_inverts_back_to_a_calendar_date_for_labelling(self):
        assert _date_from_noleap_index(60, 2021) == date(2021, 3, 1)
        assert _date_from_noleap_index(1, 2021) == date(2021, 1, 1)
        assert _date_from_noleap_index(365, 2021) == date(2021, 12, 31)

    def test_inverting_against_a_leap_year_is_refused_rather_than_silently_wrong(self):
        with pytest.raises(ValueError, match="common"):
            _date_from_noleap_index(60, 2020)

    def test_the_window_label_reads_as_a_date_range(self):
        assert window_label(195, 7) == "7 Jul – 21 Jul"
        assert window_label(1, 7) == "25 Dec – 8 Jan"

    def test_an_inverted_reference_period_is_refused(self):
        with pytest.raises(ValueError, match="end_year must be"):
            reference_period_dates(2020, 1991)

    def test_the_reference_period_is_inclusive_of_both_end_years(self):
        assert reference_period_dates(1991, 2020) == (date(1991, 1, 1), date(2020, 12, 31))


# ---------------------------------------------------------------------------
# DST boundaries
# ---------------------------------------------------------------------------


class TestDaylightSavingBoundaries:
    """"Yesterday" is a local calendar concept, and a fixed UTC offset gets it wrong."""

    def test_a_spring_forward_day_is_twenty_three_hours_long(self):
        # 8 March 2026: US clocks go 02:00 -> 03:00.
        start = local_midnight_utc(date(2026, 3, 8), NEW_YORK)
        end = local_midnight_utc(date(2026, 3, 9), NEW_YORK)
        assert start == datetime(2026, 3, 8, 5, 0, tzinfo=UTC)
        assert end == datetime(2026, 3, 9, 4, 0, tzinfo=UTC)
        assert end - start == timedelta(hours=23)

    def test_a_fall_back_day_is_twenty_five_hours_long(self):
        # 1 November 2026: US clocks go 02:00 -> 01:00.
        start = local_midnight_utc(date(2026, 11, 1), NEW_YORK)
        end = local_midnight_utc(date(2026, 11, 2), NEW_YORK)
        assert start == datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
        assert end == datetime(2026, 11, 2, 5, 0, tzinfo=UTC)
        assert end - start == timedelta(hours=25)

    def test_a_zone_that_skips_local_midnight_resolves_to_the_first_real_instant(self):
        """Havana starts DST at 00:00, so 8 March 2026 has no local midnight.

        The naive answer — pretending 00:00 existed — silently shifts the day
        boundary by an hour. The implementation steps forward to 01:00 local,
        which is the true start of the local day.
        """
        resolved = local_midnight_utc(date(2026, 3, 8), HAVANA)
        assert resolved == datetime(2026, 3, 8, 5, 0, tzinfo=UTC)
        local = resolved.astimezone(HAVANA)
        assert local.date() == date(2026, 3, 8)
        assert local.hour == 1, "00:00 does not exist on this date in Havana"

    def test_the_utc_offset_changes_across_the_transition(self):
        """Sampled at local noon, which is why 1 November already reads as EST.

        Both US transitions happen at 02:00 local, so the offset in effect at noon
        is the post-transition one on the day of the change. Noon is the sampling
        point precisely because it is never skipped or repeated.
        """
        assert utc_offset_seconds(date(2026, 3, 7), NEW_YORK) == -5 * 3600
        assert utc_offset_seconds(date(2026, 3, 8), NEW_YORK) == -4 * 3600
        assert utc_offset_seconds(date(2026, 10, 31), NEW_YORK) == -4 * 3600
        assert utc_offset_seconds(date(2026, 11, 1), NEW_YORK) == -5 * 3600

    def test_a_zone_that_never_shifts_keeps_one_offset_all_year(self):
        assert utc_offset_seconds(date(2026, 1, 15), PHOENIX) == -7 * 3600
        assert utc_offset_seconds(date(2026, 7, 15), PHOENIX) == -7 * 3600

    def test_a_fixed_utc_offset_would_pick_the_wrong_local_day(self):
        """The concrete failure a hardcoded offset causes.

        At 04:30 UTC on 1 July, New York is half an hour into 1 July (EDT, -4).
        Code carrying a fixed -5 h "Eastern" offset would still be on 30 June and
        would analyse the wrong calendar day.
        """
        instant = datetime(2026, 7, 1, 4, 30, tzinfo=UTC)
        assert local_date_now(NEW_YORK, instant) == date(2026, 7, 1)
        naive_fixed_offset = (instant - timedelta(hours=5)).date()
        assert naive_fixed_offset == date(2026, 6, 30)

    def test_two_neighbouring_zones_can_be_on_different_local_days(self):
        """Phoenix does not observe DST, so it diverges from Denver every summer."""
        instant = datetime(2026, 7, 1, 6, 30, tzinfo=UTC)
        assert local_date_now(DENVER, instant) == date(2026, 7, 1)
        assert local_date_now(PHOENIX, instant) == date(2026, 6, 30)

    def test_the_eligible_date_differs_between_a_shifting_and_a_non_shifting_zone(self):
        instant = datetime(2026, 7, 1, 12, 30, tzinfo=UTC)
        assert latest_eligible_local_date(DENVER, instant) == date(2026, 6, 30)
        assert latest_eligible_local_date(PHOENIX, instant) == date(2026, 6, 29)

    def test_a_day_becomes_complete_exactly_when_the_source_lag_elapses(self):
        # 8 March 2026 ends at 04:00 UTC on 9 March; +6 h lag -> 10:00 UTC.
        assert not is_local_date_complete(
            date(2026, 3, 8), NEW_YORK, datetime(2026, 3, 9, 9, 59, tzinfo=UTC)
        )
        assert is_local_date_complete(
            date(2026, 3, 8), NEW_YORK, datetime(2026, 3, 9, 10, 0, tzinfo=UTC)
        )

    def test_the_lag_window_is_honoured_by_the_eligible_date(self):
        assert latest_eligible_local_date(
            NEW_YORK, datetime(2026, 3, 9, 10, 0, tzinfo=UTC)
        ) == date(2026, 3, 8)
        assert latest_eligible_local_date(
            NEW_YORK, datetime(2026, 3, 9, 9, 59, tzinfo=UTC)
        ) == date(2026, 3, 7), "not yet published upstream: step back, never analyse a partial day"

    def test_a_zero_lag_still_refuses_a_day_that_has_not_ended(self):
        # Local time is 22:00 on 30 June; the day is not over anywhere yet.
        instant = datetime(2026, 7, 1, 2, 0, tzinfo=UTC)
        assert local_date_now(NEW_YORK, instant) == date(2026, 6, 30)
        assert not is_local_date_complete(date(2026, 6, 30), NEW_YORK, instant, lag_hours=0)
        assert latest_eligible_local_date(NEW_YORK, instant, lag_hours=0) == date(2026, 6, 29)

    def test_a_naive_datetime_is_refused_rather_than_assumed_to_be_utc(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            latest_eligible_local_date(NEW_YORK, datetime(2026, 3, 9, 10, 0))


# ---------------------------------------------------------------------------
# Cross-metric comparability
# ---------------------------------------------------------------------------


class TestCrossMetricComparability:
    """One scale for temperature, rainfall, and wind — and it is not the z-score."""

    @pytest.fixture
    def temps(self):
        return baseline_from_values(
            synthetic_temperatures(n=450, centre=22.0, spread=3.0), metric=Metric.TEMP_MAX
        )

    @pytest.fixture
    def gusts(self):
        return baseline_from_values(
            [26.0 * math.exp(0.32 * ((i % 37) / 37 * 4 - 2)) for i in range(450)],
            metric=Metric.WIND_GUST,
        )

    def test_equal_rarity_scores_equally_across_units_and_shapes(self, temps, gusts):
        """A 1-in-100 warm day and a 1-in-100 gust must sit at the same height.

        One is degrees Celsius on a near-symmetric distribution, the other is
        km/h on a right-skewed one. The ranking is over ``-log10 p``, so both
        land on 2.0.
        """
        warm = solve_for_tail_probability(
            temps.sketch, 0.01, lo=temps.sketch.median, hi=temps.sketch.maximum
        )
        windy = solve_for_tail_probability(
            gusts.sketch, 0.01, lo=gusts.sketch.median, hi=gusts.sketch.maximum
        )

        hot = compute_anomaly(
            city_id="a",
            local_date="2026-09-21",
            metric=Metric.TEMP_MAX,
            observed_value=warm,
            baseline=temps,
        )
        gust = compute_anomaly(
            city_id="b",
            local_date="2026-09-21",
            metric=Metric.WIND_GUST,
            observed_value=windy,
            baseline=gusts,
        )

        assert hot.anomaly_score == pytest.approx(2.0, abs=1e-4)
        assert gust.anomaly_score == pytest.approx(2.0, abs=1e-4)
        assert hot.anomaly_score == pytest.approx(gust.anomaly_score, abs=1e-4)

    def test_the_z_scores_of_those_same_two_events_disagree_substantially(self, temps, gusts):
        """Why the z-score is not the ranking statistic.

        The two events above are equally rare empirically, yet their z-scores
        differ by more than half a standard deviation because one distribution is
        skewed. Ranking on z would have reordered them on the strength of a
        distributional assumption rather than on the evidence.
        """
        warm = solve_for_tail_probability(
            temps.sketch, 0.01, lo=temps.sketch.median, hi=temps.sketch.maximum
        )
        windy = solve_for_tail_probability(
            gusts.sketch, 0.01, lo=gusts.sketch.median, hi=gusts.sketch.maximum
        )
        hot = compute_anomaly(
            city_id="a",
            local_date="2026-09-21",
            metric=Metric.TEMP_MAX,
            observed_value=warm,
            baseline=temps,
        )
        gust = compute_anomaly(
            city_id="b",
            local_date="2026-09-21",
            metric=Metric.WIND_GUST,
            observed_value=windy,
            baseline=gusts,
        )
        assert abs(hot.z_score - gust.z_score) > 0.5
        assert hot.z_valid is True
        assert gust.z_valid is False, "reported, but explicitly not comparable"

    def test_a_ranking_run_orders_three_metric_families_by_rarity_alone(self, temps, gusts):
        precip = baseline_from_values(WET_CLIMATE_PRECIP, metric=Metric.PRECIPITATION)
        candidates = [
            compute_anomaly(
                city_id="us-phoenix-az",
                local_date="2026-09-21",
                metric=Metric.TEMP_MAX,
                observed_value=solve_for_tail_probability(
                    temps.sketch, 0.02, lo=temps.sketch.median, hi=temps.sketch.maximum
                ),
                baseline=temps,
            ),
            compute_anomaly(
                city_id="ca-toronto-on",
                local_date="2026-09-21",
                metric=Metric.WIND_GUST,
                observed_value=gusts.sketch.maximum + 30.0,
                baseline=gusts,
            ),
            compute_anomaly(
                city_id="mx-monterrey-nl",
                local_date="2026-09-21",
                metric=Metric.PRECIPITATION,
                observed_value=25.0,
                baseline=precip,
            ),
        ]
        result = rank_candidates(candidates, top_n=3)
        scores = [event.candidate.anomaly_score for event in result.ranked]
        assert scores == sorted(scores, reverse=True)
        # The rarest event wins even though its raw number is the smallest of the
        # three in absolute terms.
        winner = result.ranked[0].candidate
        assert winner.tail_probability == min(
            c.tail_probability for c in candidates if c.eligible
        )

    def test_raw_magnitude_does_not_decide_between_two_cities(self):
        """Forty degrees in Phoenix loses to thirty in Vancouver, as designed."""
        desert = baseline_from_values(
            synthetic_temperatures(n=450, centre=39.0, spread=3.0), metric=Metric.TEMP_MAX
        )
        coastal = baseline_from_values(
            synthetic_temperatures(n=450, centre=21.0, spread=2.0, seed=11),
            metric=Metric.TEMP_MAX,
        )
        phoenix = compute_anomaly(
            city_id="us-phoenix-az",
            local_date="2026-09-21",
            metric=Metric.TEMP_MAX,
            observed_value=40.0,
            baseline=desert,
        )
        vancouver = compute_anomaly(
            city_id="ca-vancouver-bc",
            local_date="2026-09-21",
            metric=Metric.TEMP_MAX,
            observed_value=32.0,
            baseline=coastal,
        )
        assert phoenix.observed_value > vancouver.observed_value
        assert vancouver.anomaly_score > phoenix.anomaly_score

        result = rank_candidates([phoenix, vancouver], top_n=2)
        assert result.ranked[0].candidate.city_id == "ca-vancouver-bc"

    def test_normal_theory_is_confined_to_the_methodology_page(self):
        """``normal_sf`` must not appear anywhere in the scoring or ranking path."""
        assert "normal_sf" not in inspect.getsource(anomaly_module)
        assert "normal_sf" not in inspect.getsource(ranking_module)


# ---------------------------------------------------------------------------
# Deterministic ranking and tie-breaking
# ---------------------------------------------------------------------------


class TestDeterministicRanking:
    """Two runs over identical inputs must produce an identical board."""

    def test_the_documented_tiebreak_chain_is_the_one_implemented(self):
        assert TIEBREAK_CHAIN == (
            "anomaly_score desc",
            "tail_probability asc",
            "abs(robust_deviation) desc",
            "metric precedence",
            "city_id asc",
        )
        key = ranking_sort_key(candidate(city_id="a", score=2.0, tail=0.01, robust=1.5))
        assert len(key) == len(TIEBREAK_CHAIN)

    def test_a_higher_score_wins_first(self):
        result = rank_candidates(
            [candidate(city_id="a", score=1.0), candidate(city_id="b", score=3.0)], top_n=2
        )
        assert [e.candidate.city_id for e in result.ranked] == ["b", "a"]

    def test_equal_scores_break_on_the_rarer_tail(self):
        result = rank_candidates(
            [
                candidate(city_id="a", score=2.0, tail=0.01),
                candidate(city_id="b", score=2.0, tail=0.001),
            ],
            top_n=2,
        )
        assert [e.candidate.city_id for e in result.ranked] == ["b", "a"]

    def test_equal_scores_and_tails_break_on_the_larger_robust_deviation(self):
        result = rank_candidates(
            [
                candidate(city_id="a", score=2.0, tail=0.01, robust=1.0),
                candidate(city_id="b", score=2.0, tail=0.01, robust=-4.0),
            ],
            top_n=2,
        )
        assert [e.candidate.city_id for e in result.ranked] == ["b", "a"], (
            "the magnitude is compared, not the sign: a cold anomaly is not demoted"
        )

    def test_a_full_numeric_tie_breaks_on_the_fixed_metric_precedence(self):
        candidates = [
            candidate(city_id="a", metric=metric, score=2.0, tail=0.01, robust=1.0)
            for metric in reversed(METRIC_TIEBREAK_ORDER)
        ]
        result = rank_candidates(candidates, top_n=5, one_event_per_city=False)
        assert [e.candidate.metric for e in result.ranked] == list(METRIC_TIEBREAK_ORDER)
        assert metric_tiebreak_index(Metric.TEMP_MAX) == 0

    def test_the_last_resort_is_the_city_id_which_guarantees_a_total_order(self):
        candidates = [
            candidate(city_id=city, score=2.0, tail=0.01, robust=1.0)
            for city in ("us-tulsa-ok", "ca-halifax-ns", "mx-merida-yuc")
        ]
        result = rank_candidates(candidates, top_n=3)
        assert [e.candidate.city_id for e in result.ranked] == [
            "ca-halifax-ns",
            "mx-merida-yuc",
            "us-tulsa-ok",
        ]

    def test_floating_point_noise_below_the_rounding_threshold_cannot_reorder(self):
        """Scores are rounded to six decimals before comparison.

        Without this, the board could change between two runs of the same
        arithmetic on the same data, which would make the archive worthless.
        """
        epsilon = 10 ** -(COMPARISON_DECIMALS + 3)
        result = rank_candidates(
            [
                candidate(city_id="b", score=2.0, tail=0.01, robust=1.0),
                candidate(city_id="a", score=2.0 + epsilon, tail=0.01, robust=1.0),
            ],
            top_n=2,
        )
        assert [e.candidate.city_id for e in result.ranked] == ["a", "b"], (
            "the sub-threshold score difference must be ignored and the city id decide"
        )

    def test_the_board_is_identical_under_every_input_ordering(self):
        candidates = [
            candidate(city_id="a", score=2.0, tail=0.01, robust=1.0),
            candidate(city_id="b", score=2.0, tail=0.01, robust=1.0),
            candidate(city_id="c", score=2.0, tail=0.01, robust=1.0, metric=Metric.WIND_GUST),
            candidate(city_id="d", score=3.0, tail=0.001, robust=2.0),
            candidate(city_id="e", score=1.0, tail=0.1, robust=0.5),
        ]
        expected = [
            (e.rank, e.candidate.city_id, e.candidate.metric)
            for e in rank_candidates(candidates, top_n=5).ranked
        ]
        for permutation in itertools.permutations(candidates):
            observed = [
                (e.rank, e.candidate.city_id, e.candidate.metric)
                for e in rank_candidates(list(permutation), top_n=5).ranked
            ]
            assert observed == expected

    def test_ranks_are_a_dense_one_based_sequence(self):
        candidates = [candidate(city_id=f"c{i}", score=float(i)) for i in range(12)]
        result = rank_candidates(candidates, top_n=10)
        assert [e.rank for e in result.ranked] == list(range(1, 11))
        assert result.event_count == 10

    def test_ineligible_candidates_never_reach_the_board_but_are_counted(self):
        candidates = [
            candidate(city_id="a", score=3.0),
            candidate(
                city_id="b", score=9.0, eligible=False, excluded_reason="insufficient_baseline"
            ),
            candidate(city_id="c", score=8.0, eligible=False, excluded_reason="dry_day_not_ranked"),
            candidate(city_id="d", score=7.0, eligible=False, excluded_reason="dry_day_not_ranked"),
        ]
        result = rank_candidates(candidates, top_n=10)
        assert [e.candidate.city_id for e in result.ranked] == ["a"]
        assert result.diagnostics["candidates_total"] == 4
        assert result.diagnostics["candidates_eligible"] == 1
        assert result.diagnostics["excluded_reasons"] == {
            "insufficient_baseline": 1,
            "dry_day_not_ranked": 2,
        }

    def test_one_event_per_city_keeps_a_single_city_from_sweeping_the_board(self):
        candidates = [
            candidate(city_id="a", metric=Metric.TEMP_MAX, score=9.0),
            candidate(city_id="a", metric=Metric.TEMP_MIN, score=8.0),
            candidate(city_id="a", metric=Metric.WIND_GUST, score=7.0),
            candidate(city_id="b", metric=Metric.TEMP_MAX, score=6.0),
            candidate(city_id="c", metric=Metric.TEMP_MAX, score=5.0),
        ]
        result = rank_candidates(candidates, top_n=3, one_event_per_city=True)
        assert [e.candidate.city_id for e in result.ranked] == ["a", "b", "c"]
        assert result.diagnostics["backfilled_slots"] == 0
        # Every candidate is still in the dataset; the cap is on presentation.
        assert len(result.sorted_candidates) == 5

    def test_disabling_the_cap_lets_a_single_city_take_every_slot(self):
        candidates = [
            candidate(city_id="a", metric=Metric.TEMP_MAX, score=9.0),
            candidate(city_id="a", metric=Metric.TEMP_MIN, score=8.0),
            candidate(city_id="b", metric=Metric.TEMP_MAX, score=1.0),
        ]
        result = rank_candidates(candidates, top_n=2, one_event_per_city=False)
        assert [e.candidate.city_id for e in result.ranked] == ["a", "a"]

    def test_a_board_short_of_cities_is_backfilled_and_says_so(self):
        candidates = [
            candidate(city_id="a", metric=Metric.TEMP_MAX, score=9.0),
            candidate(city_id="a", metric=Metric.TEMP_MIN, score=8.0),
            candidate(city_id="b", metric=Metric.TEMP_MAX, score=7.0),
        ]
        result = rank_candidates(candidates, top_n=3, one_event_per_city=True)
        assert result.event_count == 3
        assert result.diagnostics["backfilled_slots"] == 1
        assert result.diagnostics["distinct_cities_eligible"] == 2
        scores = [e.candidate.anomaly_score for e in result.ranked]
        assert scores == sorted(scores, reverse=True), "ranks stay monotone after backfill"

    def test_an_empty_day_produces_an_empty_board_rather_than_an_error(self):
        result = rank_candidates([], top_n=10)
        assert result.ranked == []
        assert result.diagnostics["candidates_total"] == 0
        assert result.diagnostics["top_n_published"] == 0

    def test_the_diagnostics_record_the_settings_the_board_was_built_under(self):
        result = rank_candidates([candidate(city_id="a", score=2.0)], top_n=10)
        assert result.diagnostics["top_n_requested"] == 10
        assert result.diagnostics["one_event_per_city"] is True
        assert result.diagnostics["tiebreak_chain"] == list(TIEBREAK_CHAIN)

    def test_a_candidate_missing_its_tail_probability_sorts_last_not_first(self):
        missing = candidate(city_id="b", score=2.0)
        missing.tail_probability = None
        missing.robust_deviation = None
        result = rank_candidates(
            [missing, candidate(city_id="a", score=2.0, tail=0.5, robust=0.1)], top_n=2
        )
        assert [e.candidate.city_id for e in result.ranked] == ["a", "b"]


# ---------------------------------------------------------------------------
# Small supporting functions
# ---------------------------------------------------------------------------


class TestSupportingStatistics:
    def test_robust_deviation_is_iqr_scaled(self):
        assert robust_deviation(30.0, 20.0, 5.0) == pytest.approx(2.0)
        assert robust_deviation(10.0, 20.0, 5.0) == pytest.approx(-2.0)

    @pytest.mark.parametrize(
        ("median", "iqr"),
        [(None, 5.0), (20.0, None), (20.0, 0.0), (20.0, -1.0)],
    )
    def test_robust_deviation_is_undefined_without_a_usable_scale(self, median, iqr):
        assert robust_deviation(30.0, median, iqr) is None

    def test_the_return_period_is_the_reciprocal_of_expected_exceedances_in_the_window(self):
        assert return_period_years(0.01, 15) == pytest.approx(1 / 0.15)
        assert return_period_years(0.5, 15) == pytest.approx(1 / 7.5)

    @pytest.mark.parametrize(
        ("probability", "window"),
        [(0.0, 15), (-0.1, 15), (float("nan"), 15), (0.01, 0)],
    )
    def test_the_return_period_is_withheld_when_it_would_be_meaningless(
        self, probability, window
    ):
        assert return_period_years(probability, window) is None

    def test_the_histogram_bins_the_whole_sample(self):
        histogram = build_histogram(uniform_sample(100), bins=10)
        assert histogram["counts"] == [10] * 10
        assert len(histogram["bin_edges"]) == 11
        assert sum(histogram["counts"]) == 100
        assert histogram["degenerate"] is False

    def test_a_constant_sample_gets_one_bin_instead_of_a_division_by_zero(self):
        histogram = build_histogram([5.0, 5.0, 5.0], bins=24)
        assert histogram["degenerate"] is True
        assert histogram["counts"] == [3]

    def test_the_maximum_lands_inside_the_top_bin_rather_than_off_the_end(self):
        histogram = build_histogram([0.0, 0.5, 1.0], bins=2)
        assert sum(histogram["counts"]) == 3

    @pytest.mark.parametrize(("values", "bins"), [([], 10), ([1.0, 2.0], 0)])
    def test_a_histogram_of_nothing_is_none(self, values, bins):
        assert build_histogram(values, bins) is None

    def test_the_normal_survival_function_matches_known_values(self):
        assert normal_sf(0.0) == pytest.approx(0.5)
        assert normal_sf(1.0) == pytest.approx(0.158655, abs=1e-6)
        assert normal_sf(1.959964) == pytest.approx(0.025, abs=1e-6)

    def test_event_ids_are_pure_functions_of_date_city_metric_and_methodology(self):
        first = build_event_id("2026-09-21", "us-phoenix-az", Metric.TEMP_MAX)
        assert first == build_event_id("2026-09-21", "us-phoenix-az", "temp_max")
        assert first == f"2026-09-21_us-phoenix-az_temp_max@{METHODOLOGY_VERSION}"
        assert first != build_event_id(
            "2026-09-21", "us-phoenix-az", Metric.TEMP_MAX, methodology_version="2.0.0"
        )
