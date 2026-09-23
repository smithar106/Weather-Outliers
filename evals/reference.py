"""Reference arithmetic for the scoring evaluators, re-derived from the methodology.

An evaluator that calls the code under test to produce the number it then checks
has measured nothing. So nothing in this module imports ``app.stats``. Every
quantity here is written from ``docs/methodology.md`` and reaches the same answer
by a deliberately different route:

* production reduces a reference sample to a ~45-number quantile **sketch** and
  reads probabilities off a piecewise-linear CDF fitted to that sketch, because
  storing 41 million raw floats is not an option at 50 cities x 5 metrics x 365
  days;
* this module keeps the whole sorted sample and interpolates between adjacent
  order statistics directly, which is what the sketch is an approximation *of*.

Where the two must agree, and where they cannot
----------------------------------------------
Both use Weibull plotting positions, ``p_i = i / (n + 1)``. Production retains
the five most extreme order statistics verbatim at each end, so for an
observation out in either tail the two routes interpolate between *the same
knots* and must agree to floating-point noise. In the interior, production
interpolates across the gaps in its probability grid where this module
interpolates between neighbouring samples, so small disagreements there are
expected: they are a property of the sketch, not a defect. The dataset tags each
case with which regime it sits in, and the interior cases report the observed
discrepancy as a measured number rather than asserting it away.

A second, subtler reason this file exists: it is written against the *documented*
methodology. If the two implementations disagree, that is interesting whichever
one turns out to be wrong.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Restated constants
#
# Deliberately literals rather than imports from `app`, so that a change to the
# application cannot silently redefine what this module is checking against.
# `evals/suites/scoring.py` carries a case that compares each of these against
# the application's own value, so restating them cannot drift unnoticed either.
# ---------------------------------------------------------------------------

#: Order statistics production keeps verbatim at each tail. Inside this many
#: samples of either extreme, the sketch is lossless and agreement must be exact.
TAIL_ORDER_STATS = 5

#: Weight on the out-of-sample margin term.
MARGIN_WEIGHT = 0.5

#: Decimals the score is rounded to before any comparison or sort.
COMPARISON_DECIMALS = 6

#: Metrics tested in the upper tail only, because a low value is not the kind of
#: event being ranked (a dry day, a calm day).
UPPER_TAIL_ONLY = frozenset({"precipitation", "wind_gust"})

#: Metrics whose distribution is close enough to symmetric for a z-score to be a
#: meaningful summary, and whose deviation is quoted against the mean.
Z_SCORE_METRICS = frozenset({"temp_max", "temp_min", "temp_mean"})

#: Fixed metric precedence, tie-break link 4.
METRIC_PRECEDENCE = ("temp_max", "temp_min", "precipitation", "wind_gust", "temp_mean")

#: The documented tie-break chain, in order.
TIEBREAK_CHAIN = (
    "anomaly_score desc",
    "tail_probability asc",
    "abs(robust_deviation) desc",
    "metric precedence",
    "city_id asc",
)


# ---------------------------------------------------------------------------
# Order statistics
# ---------------------------------------------------------------------------


def quantile(sorted_sample: Sequence[float], p: float) -> float:
    """Linear-interpolated quantile, ``h = (n - 1) p`` (R type 7, NumPy default).

    The interpolation rule is part of the documented methodology, so
    re-implementing it identically is the point rather than a coupling: what is
    being checked is that production applies the stated rule, not that it invented
    a different one.
    """
    if not sorted_sample:
        raise ValueError("quantile() of an empty sample")
    n = len(sorted_sample)
    if n == 1:
        return float(sorted_sample[0])
    h = (n - 1) * p
    lo = math.floor(h)
    hi = math.ceil(h)
    if lo == hi:
        return float(sorted_sample[int(h)])
    return float(sorted_sample[lo] + (h - lo) * (sorted_sample[hi] - sorted_sample[lo]))


def non_exceedance(sorted_sample: Sequence[float], x: float) -> float:
    """``P(X <= x)`` read off the full empirical CDF with Weibull positions.

    The i-th smallest of n samples sits at ``i / (n + 1)``, so the largest sample
    is at ``n / (n + 1)`` rather than at 1.0 — which is what keeps the estimated
    exceedance probability of the sample maximum at ``1 / (n + 1)`` instead of
    zero. Ties collapse to a single step, as an empirical CDF requires.
    """
    n = len(sorted_sample)
    if n == 0:
        return float("nan")
    denom = n + 1

    knots: list[tuple[float, float]] = []
    running = 0.0
    for i, value in enumerate(sorted_sample, start=1):
        p = i / denom
        running = max(running, p)
        if knots and math.isclose(knots[-1][0], value, rel_tol=0.0, abs_tol=1e-12):
            knots[-1] = (float(value), running)
        else:
            knots.append((float(value), running))

    if x <= knots[0][0]:
        # Below the smallest retained sample there is no resolution left to
        # report, so this is 0.0 rather than an extrapolation.
        return knots[0][1] if math.isclose(x, knots[0][0], abs_tol=1e-12) else 0.0
    if x >= knots[-1][0]:
        return 1.0 if x > knots[-1][0] else knots[-1][1]

    lo_v, lo_p = knots[0]
    for value, p in knots[1:]:
        if x <= value:
            if math.isclose(value, lo_v, abs_tol=1e-12):
                return p
            return lo_p + ((x - lo_v) / (value - lo_v)) * (p - lo_p)
        lo_v, lo_p = value, p
    return 1.0


@dataclass(slots=True)
class ReferenceTail:
    """A one-sided tail estimate plus the resolution limit that produced it."""

    probability: float
    percentile: float
    bounded: bool
    beyond_sample: bool
    floor: float


def tail(sorted_sample: Sequence[float], x: float, direction: str) -> ReferenceTail:
    """One-sided exceedance probability, floored at the resolvable ``1/(n+1)``.

    ``bounded`` is the honesty flag: when the raw estimate falls below the floor,
    the number returned is a *bound* set by the sample size, and any prose built
    on it has to say so.
    """
    if direction not in ("above", "below"):
        raise ValueError(f"direction must be 'above' or 'below', got {direction!r}")
    n = len(sorted_sample)
    if n == 0:
        return ReferenceTail(float("nan"), float("nan"), False, False, 1.0)

    f = non_exceedance(sorted_sample, x)
    raw = (1.0 - f) if direction == "above" else f
    floor = 1.0 / (n + 1)
    beyond = (x > sorted_sample[-1]) if direction == "above" else (x < sorted_sample[0])
    return ReferenceTail(
        probability=max(raw, floor),
        percentile=100.0 * f,
        bounded=raw < floor,
        beyond_sample=bool(beyond),
        floor=floor,
    )


def wet_days(sample: Sequence[float]) -> list[float]:
    """The strictly-positive members of a precipitation sample, sorted.

    Strictly ``> 0`` rather than a 0.2 mm or 1.0 mm meteorological wet-day
    threshold: the reanalysis reports continuous trace amounts, and any threshold
    imposed here would be an undocumented editorial choice.
    """
    return sorted(float(v) for v in sample if v > 0.0)


def zero_inflated_tail(sample: Sequence[float], x: float) -> ReferenceTail:
    """Upper tail of a zero-inflated variable: ``P(X>0) * P(X>=x | X>0)``.

    Daily rainfall is a mixture of a point mass at exactly zero and a continuous
    wet-day distribution. Treating it as one continuous variable badly understates
    heavy rain in dry climates, where most of the mass sits on zero.
    """
    total_n = len(sample)
    if x <= 0.0:
        return ReferenceTail(1.0, 0.0, False, False, 1.0 / (total_n + 1) if total_n else 1.0)
    wet = wet_days(sample)
    if not wet:
        return ReferenceTail(float("nan"), float("nan"), False, False, 1.0)

    p_wet = len(wet) / total_n if total_n else 0.0
    raw = p_wet * (1.0 - non_exceedance(wet, x))
    floor = 1.0 / (total_n + 1) if total_n > 0 else 1.0 / (len(wet) + 1)
    return ReferenceTail(
        probability=max(raw, floor),
        # Unconditional non-exceedance: the whole dry mass sits below any x > 0.
        percentile=100.0 * (1.0 - raw),
        bounded=raw < floor,
        beyond_sample=x > wet[-1],
        floor=floor,
    )


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReferenceScore:
    """Every intermediate quantity, so a mismatch says *which* step diverged."""

    direction: str
    percentile: float
    tail_probability: float
    tail_probability_is_bounded: bool
    beyond_baseline_sample: bool
    surprisal: float
    out_of_sample_margin: float
    margin_bonus: float
    anomaly_score: float
    deviation: float | None
    robust_deviation: float | None
    z_score: float | None
    in_exact_tail: bool


def out_of_sample_margin(
    sorted_sample: Sequence[float], x: float, direction: str, iqr: float, std: float | None
) -> float:
    """How far past the reference extreme the observation lies, in IQRs.

    Falls back to the standard deviation when the IQR is degenerate, which is
    possible for a metric that is near-constant across the window (gusts in a
    sheltered valley). Zero when the observation is inside the sample.
    """
    if not sorted_sample:
        return 0.0
    reference = sorted_sample[-1] if direction == "above" else sorted_sample[0]
    excess = (x - reference) if direction == "above" else (reference - x)
    if excess <= 0:
        return 0.0
    if iqr > 0:
        return excess / iqr
    if std and std > 0:
        return excess / std
    return 0.0


def score(sample: Sequence[float], x: float, *, metric: str) -> ReferenceScore:
    """The full scoring chain for one observation against one raw sample.

    ``anomaly_score = -log10(p_tail) + 0.5 * log10(1 + margin_iqr)``. The first
    term is rarity, which is the whole ranking; the second is a small bonus for
    clearing the reference-period extreme, weighted so that an observation a full
    IQR past the record adds ~0.15 — enough to separate the unprecedented from
    the merely record-equalling, far too little to let magnitude overturn rarity.
    """
    ordered = sorted(float(v) for v in sample if v is not None and math.isfinite(v))
    n = len(ordered)
    if n == 0:
        raise ValueError("score() needs a non-empty sample")

    median = quantile(ordered, 0.5)
    p25 = quantile(ordered, 0.25)
    p75 = quantile(ordered, 0.75)
    iqr = p75 - p25
    mean = math.fsum(ordered) / n
    std: float | None = None
    if n >= 2:
        std = math.sqrt(math.fsum((v - mean) ** 2 for v in ordered) / (n - 1))

    if metric in UPPER_TAIL_ONLY:
        direction = "above"
    else:
        direction = "above" if x >= median else "below"

    if metric == "precipitation":
        result = zero_inflated_tail(ordered, x)
    else:
        result = tail(ordered, x, direction)
    # The margin is always measured in IQRs of the *full* sample, including the
    # dry days for precipitation. For an upper-tail metric the reference extreme
    # is the sample maximum either way — the zeros sit at the other end — so the
    # only thing the choice affects is the scale, and the full-sample IQR is the
    # documented one. In a desert that IQR is zero and the fallback to the
    # standard deviation is what keeps the term finite.

    if not math.isfinite(result.probability):
        surprisal = margin = bonus = 0.0
    else:
        surprisal = -math.log10(max(result.probability, 1e-12))
        margin = (
            out_of_sample_margin(ordered, x, direction, iqr, std) if result.beyond_sample else 0.0
        )
        bonus = MARGIN_WEIGHT * math.log10(1.0 + max(margin, 0.0))

    centre = mean if metric in Z_SCORE_METRICS else median
    z: float | None = None
    if metric != "precipitation" and std and std > 0:
        z = (x - mean) / std

    return ReferenceScore(
        direction=direction,
        percentile=result.percentile,
        tail_probability=result.probability,
        tail_probability_is_bounded=result.bounded,
        beyond_baseline_sample=result.beyond_sample,
        surprisal=surprisal,
        out_of_sample_margin=margin,
        margin_bonus=bonus,
        anomaly_score=round(surprisal + bonus, COMPARISON_DECIMALS),
        deviation=x - centre,
        robust_deviation=((x - median) / iqr) if iqr > 0 else None,
        z_score=z,
        in_exact_tail=in_exact_tail(ordered, x, metric),
    )


def in_exact_tail(sorted_sample: Sequence[float], x: float, metric: str) -> bool:
    """Whether the sketch is lossless for this observation.

    True when ``x`` sits at or outside the fifth-from-the-end order statistic, the
    region production stores verbatim. Inside that region the two routes read the
    same knots and must agree exactly; outside it, the sketch interpolates across
    grid gaps and a small difference is expected.
    """
    reference = wet_days(sorted_sample) if metric == "precipitation" else list(sorted_sample)
    n = len(reference)
    if n == 0:
        return False
    k = min(TAIL_ORDER_STATS, n)
    return x >= reference[n - k] or x <= reference[k - 1]


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def metric_precedence(metric: str) -> int:
    """Position in the fixed metric order; unknown metrics sort last."""
    try:
        return METRIC_PRECEDENCE.index(metric)
    except ValueError:
        return len(METRIC_PRECEDENCE)


def sort_key(row: dict) -> tuple:
    """The documented tie-break chain as a total-order key.

    Every component is a rounded number or a fixed lookup, and the last is the
    city id, which is unique — so the order is total and two runs over identical
    inputs cannot disagree, even on exact ties.
    """
    score_value = round(float(row["anomaly_score"]), COMPARISON_DECIMALS)
    tail_value = row.get("tail_probability")
    tail_value = round(float(tail_value), COMPARISON_DECIMALS) if tail_value is not None else 1.0
    robust = row.get("robust_deviation")
    robust = round(abs(float(robust)), COMPARISON_DECIMALS) if robust is not None else 0.0
    return (
        -score_value,
        tail_value,
        -robust,
        metric_precedence(str(row["metric"])),
        str(row["city_id"]),
    )


def select_board(
    rows: Sequence[dict], *, top_n: int = 10, one_event_per_city: bool = True
) -> list[dict]:
    """The published board: rank, one event per city, backfilled if short.

    A single heat dome can sweep a metro region legitimately, which makes for a
    useless daily board, so the first pass takes each city's strongest event only.
    If that leaves fewer than ``top_n`` rows — few cities reporting, or a quiet
    day — a second pass backfills from the remaining events regardless of city,
    and the canonical order is re-applied so ranks stay monotone in score.
    """
    eligible = [r for r in rows if r.get("eligible", True)]
    ordered = sorted(eligible, key=sort_key)

    if not one_event_per_city:
        return ordered[:top_n]

    selected: list[dict] = []
    seen: set[str] = set()
    for row in ordered:
        if len(selected) >= top_n:
            break
        city = str(row["city_id"])
        if city in seen:
            continue
        seen.add(city)
        selected.append(row)

    if len(selected) < top_n:
        chosen = {id(r) for r in selected}
        for row in ordered:
            if len(selected) >= top_n:
                break
            if id(row) not in chosen:
                selected.append(row)
        selected.sort(key=sort_key)

    return selected
