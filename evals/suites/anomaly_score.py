"""Evaluator 1 — anomaly scores against independently calculated references.

The reference arithmetic lives in :mod:`evals.reference` and shares no code with
the application: it keeps the whole sorted sample and interpolates between
adjacent order statistics, where production reduces the sample to a 45-number
quantile sketch first. Both follow ``docs/methodology.md``.

What this suite is entitled to assert
-------------------------------------
Not "the two agree everywhere", which is false and would have to be papered over
with a tolerance picked to make the run pass. Instead, two assertions and one
measurement, in descending order of strength:

1. **Exact agreement in the retained tails.** Production stores the five most
   extreme order statistics verbatim, so when the observation sits at or beyond
   the fifth from either end, both routes interpolate between *the same knots*.
   Required to match to 1e-9. This is the regime that matters: it is where an
   event has to be to reach the board at all.

2. **A derived bound in the interior.** Elsewhere the sketch interpolates linearly
   across gaps in its probability grid. Because that interpolant is monotone and
   pinned at the knots, the non-exceedance it reports and the true one both lie
   inside the same bracketing grid interval — so their difference cannot exceed
   that interval's width. That is a property of the construction, not a tolerance:
   the bound tightens automatically wherever the grid is dense, which is exactly
   in the tails.

3. **The measured worst case, reported either way.** The largest observed
   discrepancy is published as a metric in both probability and score units, so a
   reader can see the size of the approximation rather than take "passed" on faith.

The interior discrepancy is dominated by a definitional difference, not by error:
the stored grid holds R type-7 quantiles (``h = (n-1)p``) while both tail
estimates use Weibull plotting positions (``i/(n+1)``). Those disagree by about
one order statistic, which near the 98th percentile is a couple of parts in a
thousand of probability.
"""

from __future__ import annotations

import bisect
import math
import traceback

from evals import reference
from evals.dataset import ScoringCase, build_baseline, load_dataset, normal_sample
from evals.harness import (
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_PASSED,
    Case,
    Metric,
    Suite,
    Timer,
    ratio,
)

SUITE_ID = "anomaly_score"
TITLE = "Anomaly scores vs independent reference"
DESCRIPTION = (
    "Recomputes every dataset case with arithmetic that shares no code with the "
    "application, requires exact agreement where the quantile sketch is lossless, "
    "and bounds the remainder by the sketch's own grid resolution."
)

#: Exact-regime tolerance. Both routes do the same arithmetic in a different
#: order, so only floating-point reassociation separates them.
EXACT_TOLERANCE = 1e-9

#: Points at which the full distribution is swept. The hand-written cases pin
#: specific behaviours; this sweep is what stops the bound being true only at the
#: six interior values someone happened to choose.
SWEEP_POINTS = 999
SWEEP_SEED = 999
SWEEP_N = 450


def _grid_knot_probabilities(n: int) -> list[float]:
    """Non-exceedance probabilities of every knot the sketch's CDF is pinned at.

    Mirrors the knot selection in ``_cdf_knots``: the retained order statistics at
    their Weibull positions, plus grid points strictly inside the span those
    already describe. Restated rather than imported, for the same reason the rest
    of the reference is.
    """
    from app.stats.distributions import QUANTILE_GRID

    denom = n + 1
    k = min(reference.TAIL_ORDER_STATS, n)
    inner_lo = k / denom
    inner_hi = (n - k + 1) / denom
    knots = [i / denom for i in range(1, k + 1)]
    knots += [(n - k + 1 + j) / denom for j in range(k)]
    knots += [p for p in QUANTILE_GRID if inner_lo < p < inner_hi]
    return sorted(set(knots))


def _bracketing_width(knots: list[float], f: float) -> float:
    """Width of the grid interval containing non-exceedance ``f``."""
    if not knots:
        return 1.0
    j = bisect.bisect_left(knots, f)
    lo = knots[max(j - 1, 0)]
    hi = knots[min(j, len(knots) - 1)]
    return max(hi - lo, 1.0 / (len(knots) + 1))


def run() -> Suite:
    suite = Suite(id=SUITE_ID, title=TITLE, description=DESCRIPTION)
    timer = Timer()

    try:
        with timer:
            from app.domain import Metric as AppMetric
            from app.stats.anomaly import (
                COMPARISON_DECIMALS,
                MARGIN_WEIGHT,
                compute_anomaly,
            )
            from app.stats.distributions import TAIL_ORDER_STATS

            dataset = load_dataset()
            suite.notes.append(f"dataset {dataset.dataset_version}")

            # -- constant drift check -------------------------------------------
            # The reference restates production's constants as literals so the
            # application cannot redefine what it is being checked against. That
            # protection is only honest if a divergence is reported rather than
            # silently changing the meaning of every case below.
            restated = {
                "TAIL_ORDER_STATS": (reference.TAIL_ORDER_STATS, TAIL_ORDER_STATS),
                "MARGIN_WEIGHT": (reference.MARGIN_WEIGHT, MARGIN_WEIGHT),
                "COMPARISON_DECIMALS": (reference.COMPARISON_DECIMALS, COMPARISON_DECIMALS),
            }
            for name, (ours, theirs) in restated.items():
                suite.cases.append(
                    Case(
                        id=f"constant_{name.lower()}",
                        title=f"Reference restatement of {name} still matches the application",
                        passed=ours == theirs,
                        category="drift",
                        expected=f"{name}={theirs}",
                        observed=f"reference={ours}",
                    )
                )

            # -- per-case comparison -------------------------------------------
            exact_checked = 0
            interior_checked = 0
            worst_exact_score = 0.0
            worst_interior_p = 0.0
            worst_interior_score = 0.0
            worst_interior_case = None
            bound_violations = 0

            for case in dataset.cases:
                if case.sample is None or not case.sample or case.observed is None:
                    continue
                if not math.isfinite(case.observed):
                    continue

                baseline = build_baseline(case)
                candidate = compute_anomaly(
                    city_id="eval",
                    local_date="2025-07-15",
                    metric=AppMetric(case.metric),
                    observed_value=case.observed,
                    baseline=baseline,
                    min_wet_days=case.min_wet_days_override or 15,
                    min_score=0.0,
                )
                if candidate is None:  # pragma: no cover - dataset guards this
                    continue

                ref = reference.score(case.sample, case.observed, metric=case.metric)
                score_delta = abs(ref.anomaly_score - candidate.anomaly_score)
                prob_delta = abs(ref.tail_probability - (candidate.tail_probability or 0.0))

                # Direction, boundedness and out-of-sample status are categorical
                # and must match regardless of regime — an approximation in the
                # probability is acceptable, a disagreement about which tail the
                # observation is in is not.
                categorical = {
                    "direction": (ref.direction, candidate.direction.value),
                    "bounded": (
                        ref.tail_probability_is_bounded,
                        candidate.tail_probability_is_bounded,
                    ),
                    "beyond_sample": (
                        ref.beyond_baseline_sample,
                        candidate.beyond_baseline_sample,
                    ),
                }
                mismatched = [k for k, (a, b) in categorical.items() if a != b]

                if ref.in_exact_tail:
                    exact_checked += 1
                    worst_exact_score = max(worst_exact_score, score_delta)
                    passed = score_delta <= EXACT_TOLERANCE and not mismatched
                    suite.cases.append(
                        Case(
                            id=f"exact_{case.id}",
                            title=case.title,
                            passed=passed,
                            category=f"{case.category}/lossless-tail",
                            expected=f"score {ref.anomaly_score:.9f} (exact)",
                            observed=f"score {candidate.anomaly_score:.9f}, Δ={score_delta:.2e}",
                            detail=(
                                f"categorical mismatch: {', '.join(mismatched)}"
                                if mismatched
                                else None
                            ),
                        )
                    )
                else:
                    interior_checked += 1
                    knots = _grid_knot_probabilities(len(case.sample))
                    non_exceedance = 1.0 - ref.tail_probability
                    if ref.direction == "below":
                        non_exceedance = ref.tail_probability
                    width = _bracketing_width(knots, non_exceedance)
                    within = prob_delta <= width
                    if not within:
                        bound_violations += 1
                    if prob_delta > worst_interior_p:
                        worst_interior_p = prob_delta
                        worst_interior_case = case.id
                    worst_interior_score = max(worst_interior_score, score_delta)
                    suite.cases.append(
                        Case(
                            id=f"bounded_{case.id}",
                            title=case.title,
                            passed=within and not mismatched,
                            category=f"{case.category}/interpolated-interior",
                            expected=f"|Δp| ≤ bracketing grid width {width:.6f}",
                            observed=f"|Δp| = {prob_delta:.6f}, Δscore = {score_delta:.6f}",
                            detail=(
                                f"categorical mismatch: {', '.join(mismatched)}"
                                if mismatched
                                else None
                            ),
                        )
                    )

            # -- full-distribution sweep ---------------------------------------
            # One synthetic baseline, every thousandth of the distribution. The
            # hand-written cases say what the behaviour should be at points chosen
            # to be interesting; this says the bound holds everywhere, including
            # the points nobody thought about.
            sweep_sample = normal_sample(SWEEP_N, SWEEP_SEED, 25.0, 4.0)
            sweep_baseline = build_baseline(
                ScoringCase(
                    id="sweep",
                    category="sweep",
                    title="sweep",
                    metric="temp_max",
                    sample=sweep_sample,
                    observed=None,
                    n_years=30,
                    window_days=7,
                    expect={},
                    min_wet_days_override=None,
                    note=None,
                )
            )
            sweep_knots = _grid_knot_probabilities(SWEEP_N)
            ordered = sorted(sweep_sample)
            sweep_violations = 0
            sweep_worst_p = 0.0
            sweep_worst_ratio = 0.0
            sweep_exact_mismatches = 0
            sweep_exact_points = 0

            for i in range(1, SWEEP_POINTS + 1):
                p = i / (SWEEP_POINTS + 1)
                x = reference.quantile(ordered, p)
                got = compute_anomaly(
                    city_id="eval",
                    local_date="2025-07-15",
                    metric=AppMetric.TEMP_MAX,
                    observed_value=x,
                    baseline=sweep_baseline,
                    min_score=0.0,
                )
                ref = reference.score(sweep_sample, x, metric="temp_max")
                prob_delta = abs(ref.tail_probability - (got.tail_probability or 0.0))

                if ref.in_exact_tail:
                    sweep_exact_points += 1
                    if abs(ref.anomaly_score - got.anomaly_score) > EXACT_TOLERANCE:
                        sweep_exact_mismatches += 1
                    continue

                non_exceedance = (
                    ref.tail_probability if ref.direction == "below" else 1.0 - ref.tail_probability
                )
                width = _bracketing_width(sweep_knots, non_exceedance)
                sweep_worst_p = max(sweep_worst_p, prob_delta)
                sweep_worst_ratio = max(sweep_worst_ratio, prob_delta / width)
                if prob_delta > width:
                    sweep_violations += 1

            suite.cases.append(
                Case(
                    id="sweep_exact_tail_agreement",
                    title=f"Sweep: exact agreement at all {sweep_exact_points} lossless-tail points",
                    passed=sweep_exact_mismatches == 0,
                    category="sweep",
                    expected="0 mismatches beyond 1e-9",
                    observed=f"{sweep_exact_mismatches} mismatches",
                )
            )
            suite.cases.append(
                Case(
                    id="sweep_interior_bound",
                    title=(
                        f"Sweep: interior discrepancy within the bracketing grid interval "
                        f"at all {SWEEP_POINTS - sweep_exact_points} interior points"
                    ),
                    passed=sweep_violations == 0,
                    category="sweep",
                    expected="0 points exceed their bracketing grid width",
                    observed=(
                        f"{sweep_violations} exceed; worst |Δp| = {sweep_worst_p:.6f}, "
                        f"worst fraction of the available width = {sweep_worst_ratio:.3f}"
                    ),
                )
            )

            suite.metrics = [
                Metric("Dataset version", dataset.dataset_version),
                Metric("Cases in dataset", len(dataset.cases)),
                Metric(
                    "Lossless-tail comparisons",
                    exact_checked,
                    detail="observation at or beyond the 5th order statistic; exact match required",
                ),
                Metric(
                    "Worst lossless-tail score difference",
                    worst_exact_score,
                    unit="score",
                    detail=f"tolerance {EXACT_TOLERANCE:g}",
                ),
                Metric(
                    "Interpolated-interior comparisons",
                    interior_checked,
                    detail="bounded by the sketch's grid resolution, not required to match",
                ),
                Metric(
                    "Worst interior probability difference",
                    worst_interior_p,
                    unit="probability",
                    detail=(
                        f"worst case: {worst_interior_case}"
                        if worst_interior_case
                        else "no interior cases"
                    ),
                ),
                Metric(
                    "Worst interior score difference",
                    worst_interior_score,
                    unit="score",
                ),
                Metric("Interior bound violations", bound_violations + sweep_violations),
                Metric(
                    "Sweep points",
                    SWEEP_POINTS,
                    detail=f"seeded normal sample, n={SWEEP_N}",
                ),
                Metric(
                    "Sweep: worst fraction of available grid width used",
                    sweep_worst_ratio,
                    detail="1.0 would mean the bound is exactly saturated",
                ),
                Metric(
                    "Checks passed",
                    ratio(sum(1 for c in suite.cases if c.passed), len(suite.cases)),
                    unit="fraction",
                ),
            ]

            suite.notes.append(
                "Exact agreement is required only where the quantile sketch retains "
                "the raw order statistics. Elsewhere the two routes are bounded by "
                "the grid interval that brackets the observation, which is a "
                "property of monotone piecewise-linear interpolation rather than a "
                "chosen tolerance."
            )
            suite.notes.append(
                "The residual interior difference is mostly definitional: the stored "
                "grid holds R type-7 quantiles while both tail estimates use Weibull "
                "plotting positions. The two disagree by roughly one order statistic."
            )

        failed = [c for c in suite.cases if not c.passed]
        suite.status = STATUS_FAILED if failed else STATUS_PASSED
        suite.duration_ms = timer.elapsed_ms

    except Exception:
        suite.status = STATUS_ERROR
        suite.error = traceback.format_exc(limit=6)
        suite.duration_ms = timer.elapsed_ms

    return suite
