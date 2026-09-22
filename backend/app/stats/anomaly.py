"""Per-metric anomaly computation.

The central decision of this project is that **raw magnitude never ranks**.
A 40 °C day in Phoenix is ordinary; a 40 °C day in Vancouver would be
extraordinary. What ranks is how far out in that city's own seasonal
distribution the value sits, expressed as a calibrated empirical tail
probability so that temperature, rainfall, and wind can be compared on one
scale.

Per-metric treatment
--------------------
=================  ==========================  ==================================
Metric             Distribution assumption     Ranking statistic
=================  ==========================  ==================================
temp_max/min/mean  approximately symmetric     two-tailed empirical exceedance;
                                               z-score reported and *valid*
precipitation      zero-inflated, right-skew   mixture upper tail; z-score
                                               withheld entirely
wind_gust          right-skewed, positive      upper tail; z-score reported but
                                               flagged not comparable
=================  ==========================  ==================================
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.domain import (
    METHODOLOGY_VERSION,
    UNITS,
    UPPER_TAIL_ONLY_METRICS,
    Z_SCORE_METRICS,
    Direction,
    Metric,
)
from app.stats.distributions import (
    DistributionSketch,
    TailResult,
    return_period_years,
    robust_deviation,
    tail_probability,
    zero_inflated_upper_tail,
)

# ---------------------------------------------------------------------------
# Scoring constants (documented in docs/methodology.md; changing any of these
# is a methodology change and requires bumping METHODOLOGY_VERSION)
# ---------------------------------------------------------------------------

#: Weight on the out-of-sample margin term. Chosen so that an observation one
#: full IQR beyond the reference-period extreme adds 0.5 * log10(2) ~= 0.15 to
#: the score — enough to separate genuinely unprecedented values from ones that
#: merely equal the record, and far too small to let magnitude overturn rarity.
MARGIN_WEIGHT = 0.5

#: Scores and probabilities are rounded before comparison so that floating-point
#: noise can never reorder two mathematically equal events. This is what makes
#: reruns bit-for-bit reproducible.
COMPARISON_DECIMALS = 6


@dataclass(slots=True)
class Baseline:
    """Plain-Python view of a cached baseline row, decoupled from the ORM."""

    metric: Metric
    day_of_year: int
    window_days: int
    reference_start_year: int
    reference_end_year: int
    n_samples: int
    n_years: int
    sufficient: bool
    sketch: DistributionSketch
    source_dataset: str
    zero_fraction: float | None = None
    nonzero_n: int | None = None
    nonzero_sketch: DistributionSketch | None = None

    @property
    def window_day_count(self) -> int:
        return 2 * self.window_days + 1

    @property
    def reference_period(self) -> str:
        return f"{self.reference_start_year}-{self.reference_end_year}"


@dataclass(slots=True)
class AnomalyCandidate:
    """A scored anomaly plus the complete trace behind the score."""

    city_id: str
    local_date: str
    metric: Metric
    direction: Direction
    observed_value: float
    unit: str

    baseline_mean: float | None = None
    baseline_median: float | None = None
    baseline_std: float | None = None
    baseline_p25: float | None = None
    baseline_p75: float | None = None
    baseline_min: float | None = None
    baseline_max: float | None = None
    baseline_n: int = 0
    baseline_sufficient: bool = False

    deviation: float | None = None
    robust_deviation: float | None = None
    z_score: float | None = None
    z_valid: bool = False

    percentile: float | None = None
    tail_probability: float | None = None
    tail_probability_is_bounded: bool = False
    beyond_baseline_sample: bool = False
    return_period_years: float | None = None

    surprisal: float = 0.0
    margin_bonus: float = 0.0
    anomaly_score: float = 0.0

    eligible: bool = True
    excluded_reason: str | None = None
    evidence: dict = field(default_factory=dict)
    methodology_version: str = METHODOLOGY_VERSION


def _direction_for(metric: Metric, observed: float, baseline: Baseline) -> Direction:
    """Which tail the observation is being tested in."""
    if metric in UPPER_TAIL_ONLY_METRICS:
        return Direction.ABOVE
    centre = baseline.sketch.median
    if centre is None:
        centre = baseline.sketch.mean or 0.0
    return Direction.ABOVE if observed >= centre else Direction.BELOW


def _out_of_sample_margin(
    observed: float, direction: Direction, sketch: DistributionSketch
) -> tuple[float, str]:
    """How far past the reference-period extreme the observation lies, in IQRs.

    Falls back to the standard deviation when the IQR is degenerate (possible for
    a metric that is constant across most of the window, e.g. gusts in a very
    sheltered location). Returns ``(margin, scale_used)``.
    """
    if direction == Direction.ABOVE:
        reference = sketch.maximum
        excess = (observed - reference) if reference is not None else None
    else:
        reference = sketch.minimum
        excess = (reference - observed) if reference is not None else None

    if excess is None or excess <= 0:
        return 0.0, "none"

    if sketch.iqr and sketch.iqr > 0:
        return excess / sketch.iqr, "iqr"
    if sketch.std and sketch.std > 0:
        return excess / sketch.std, "std"
    return 0.0, "unavailable"


def compute_anomaly(
    *,
    city_id: str,
    local_date: str,
    metric: Metric,
    observed_value: float | None,
    baseline: Baseline | None,
    min_wet_days: int = 15,
    min_score: float = 0.0,
) -> AnomalyCandidate | None:
    """Score one city-metric-day. Returns ``None`` when there is nothing to score.

    An ineligible candidate is still returned (with ``eligible=False`` and a
    machine-readable ``excluded_reason``) rather than dropped, so the reason a
    city is absent from the board is inspectable instead of invisible.
    """
    unit = UNITS[metric]

    if observed_value is None or not math.isfinite(observed_value):
        return None

    if baseline is None or baseline.sketch.is_empty:
        return AnomalyCandidate(
            city_id=city_id,
            local_date=local_date,
            metric=metric,
            direction=Direction.ABOVE,
            observed_value=observed_value,
            unit=unit,
            eligible=False,
            excluded_reason="no_baseline",
            evidence={"note": "no cached baseline for this city/metric/day-of-year"},
        )

    sketch = baseline.sketch
    direction = _direction_for(metric, observed_value, baseline)

    # ----------------------------------------------------------- tail estimate
    if metric == Metric.PRECIPITATION:
        nonzero = baseline.nonzero_sketch or DistributionSketch(n=0)
        tail: TailResult = zero_inflated_upper_tail(
            zero_fraction=baseline.zero_fraction if baseline.zero_fraction is not None else 1.0,
            nonzero_sketch=nonzero,
            x=observed_value,
            total_n=baseline.n_samples,
        )
    else:
        tail = tail_probability(sketch, observed_value, direction.value)

    # ------------------------------------------------------ deviation measures
    # Temperature is summarised against the mean (its distribution is close to
    # symmetric, so the mean is a fair centre). Skewed metrics are summarised
    # against the median, where the mean would be dragged by the tail.
    if metric in Z_SCORE_METRICS:
        centre = sketch.mean
    else:
        centre = sketch.median
    deviation = (observed_value - centre) if centre is not None else None
    robust_dev = robust_deviation(observed_value, sketch.median, sketch.iqr)

    z_score: float | None = None
    z_valid = False
    if metric != Metric.PRECIPITATION and sketch.std and sketch.std > 0 and sketch.mean is not None:
        z_score = (observed_value - sketch.mean) / sketch.std
        # Valid only for the near-symmetric temperature metrics with a
        # sufficient sample. For gusts it is reported for familiarity but
        # explicitly not comparable across metrics.
        z_valid = metric in Z_SCORE_METRICS and baseline.sufficient

    # ------------------------------------------------------------------ score
    if tail.probability is None or not math.isfinite(tail.probability):
        surprisal = 0.0
        margin = 0.0
        margin_scale = "none"
        margin_bonus = 0.0
    else:
        surprisal = -math.log10(max(tail.probability, 1e-12))
        if tail.beyond_sample:
            margin, margin_scale = _out_of_sample_margin(observed_value, direction, sketch)
        else:
            margin, margin_scale = 0.0, "none"
        margin_bonus = MARGIN_WEIGHT * math.log10(1.0 + max(margin, 0.0))

    score = round(surprisal + margin_bonus, COMPARISON_DECIMALS)

    # ------------------------------------------------------------ eligibility
    eligible = True
    excluded_reason: str | None = None

    if not baseline.sufficient:
        eligible, excluded_reason = False, "insufficient_baseline"
    elif metric == Metric.PRECIPITATION and observed_value <= 0:
        eligible, excluded_reason = False, "dry_day_not_ranked"
    elif metric == Metric.PRECIPITATION and (baseline.nonzero_n or 0) < min_wet_days:
        eligible, excluded_reason = False, "insufficient_wet_days"
    elif not math.isfinite(tail.probability):
        eligible, excluded_reason = False, "tail_probability_unavailable"
    elif score < min_score:
        eligible, excluded_reason = False, "below_score_threshold"

    rp = return_period_years(tail.probability, baseline.window_day_count)

    evidence = {
        "metric": metric.value,
        "unit": unit,
        "direction": direction.value,
        "observed_value": observed_value,
        "baseline": {
            "reference_period": baseline.reference_period,
            "seasonal_window_days": baseline.window_days,
            "seasonal_window_day_count": baseline.window_day_count,
            "day_of_year_noleap": baseline.day_of_year,
            "n_samples": baseline.n_samples,
            "n_years": baseline.n_years,
            "sufficient": baseline.sufficient,
            "mean": sketch.mean,
            "std": sketch.std,
            "median": sketch.median,
            "p25": sketch.p25,
            "p75": sketch.p75,
            "iqr": sketch.iqr,
            "min": sketch.minimum,
            "max": sketch.maximum,
            "source_dataset": baseline.source_dataset,
            "zero_fraction": baseline.zero_fraction,
            "nonzero_n": baseline.nonzero_n,
        },
        "calculation": {
            "deviation": deviation,
            "deviation_reference": "mean" if metric in Z_SCORE_METRICS else "median",
            "robust_deviation_iqr": robust_dev,
            "z_score": z_score,
            "z_score_valid_for_probability": z_valid,
            "percentile": tail.percentile,
            "tail_probability": tail.probability,
            "tail_probability_is_bounded": tail.bounded,
            "probability_floor": sketch.probability_floor,
            "beyond_baseline_sample": tail.beyond_sample,
            "out_of_sample_margin": margin,
            "out_of_sample_margin_scale": margin_scale,
            "surprisal_neg_log10_p": surprisal,
            "margin_weight": MARGIN_WEIGHT,
            "margin_bonus": margin_bonus,
            "anomaly_score": score,
            "approx_return_period_years": rp,
            "score_formula": "anomaly_score = -log10(p_tail) + 0.5 * log10(1 + margin_iqr)",
        },
        "eligibility": {"eligible": eligible, "excluded_reason": excluded_reason},
        "methodology_version": METHODOLOGY_VERSION,
    }

    return AnomalyCandidate(
        city_id=city_id,
        local_date=local_date,
        metric=metric,
        direction=direction,
        observed_value=observed_value,
        unit=unit,
        baseline_mean=sketch.mean,
        baseline_median=sketch.median,
        baseline_std=sketch.std,
        baseline_p25=sketch.p25,
        baseline_p75=sketch.p75,
        baseline_min=sketch.minimum,
        baseline_max=sketch.maximum,
        baseline_n=baseline.n_samples,
        baseline_sufficient=baseline.sufficient,
        deviation=deviation,
        robust_deviation=robust_dev,
        z_score=z_score,
        z_valid=z_valid,
        percentile=tail.percentile,
        tail_probability=tail.probability,
        tail_probability_is_bounded=tail.bounded,
        beyond_baseline_sample=tail.beyond_sample,
        return_period_years=rp,
        surprisal=surprisal,
        margin_bonus=margin_bonus,
        anomaly_score=score,
        eligible=eligible,
        excluded_reason=excluded_reason,
        evidence=evidence,
    )
