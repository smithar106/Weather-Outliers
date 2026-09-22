"""Empirical distribution sketches and tail probabilities.

Why a sketch instead of raw samples
-----------------------------------
A 1991-2020 reference period with a +/-7 day seasonal window is up to 450
samples per city / metric / day-of-year. Storing all of them for 50 cities x 5
metrics x 365 days would be ~41 million floats. Storing summary *moments* only
(mean, sd) would be cheap but wrong: precipitation is zero-inflated and
right-skewed, and gusts are right-skewed, so normal-theory probabilities from a
mean and a standard deviation misstate exactly the tail we care about.

The compromise is a **quantile sketch**: a tail-dense probability grid plus the
five most extreme order statistics at each end, which is ~45 numbers per bucket
and preserves the shape where it matters. Percentiles and exceedance
probabilities are then read off a monotone piecewise-linear empirical CDF.

Conventions
-----------
* Plotting positions use the Weibull formula ``p_i = i / (n + 1)``, so the
  largest of ``n`` samples has an estimated exceedance probability of
  ``1/(n+1)`` rather than zero. This is the honest floor: with 450 samples you
  cannot resolve a probability finer than about 1-in-451, and we flag any result
  that lands on the floor as *bounded* rather than pretending to more precision.
* Quantile interpolation is the standard linear-between-order-statistics rule
  (``h = (n-1) p``), matching NumPy's default and R's type 7.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from statistics import fmean

# Tail-dense probability grid. Uniform grids waste resolution in the middle of
# the distribution and lose it precisely at the extremes we are ranking on.
QUANTILE_GRID: tuple[float, ...] = (
    0.0,
    0.001,
    0.002,
    0.005,
    0.01,
    0.02,
    0.03,
    0.05,
    0.075,
    0.10,
    0.15,
    0.20,
    0.25,
    0.30,
    0.35,
    0.40,
    0.45,
    0.50,
    0.55,
    0.60,
    0.65,
    0.70,
    0.75,
    0.80,
    0.85,
    0.90,
    0.925,
    0.95,
    0.97,
    0.98,
    0.99,
    0.995,
    0.998,
    0.999,
    1.0,
)

#: How many extreme order statistics to retain verbatim at each tail.
TAIL_ORDER_STATS = 5


@dataclass(slots=True)
class DistributionSketch:
    """Compact, reconstructable summary of an empirical sample."""

    n: int
    mean: float | None = None
    std: float | None = None
    median: float | None = None
    p25: float | None = None
    p75: float | None = None
    iqr: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    grid: tuple[float, ...] = field(default_factory=tuple)
    values: tuple[float, ...] = field(default_factory=tuple)
    low_order_stats: tuple[float, ...] = field(default_factory=tuple)
    high_order_stats: tuple[float, ...] = field(default_factory=tuple)

    # -------------------------------------------------------------- helpers
    @property
    def is_empty(self) -> bool:
        return self.n == 0

    @property
    def probability_floor(self) -> float:
        """Finest resolvable probability: ``1/(n+1)``."""
        return 1.0 / (self.n + 1) if self.n > 0 else 1.0

    def to_json(self) -> dict:
        d = asdict(self)
        for k in ("grid", "values", "low_order_stats", "high_order_stats"):
            d[k] = list(d[k])
        return d

    @classmethod
    def from_json(cls, payload: dict) -> DistributionSketch:
        return cls(
            n=int(payload["n"]),
            mean=payload.get("mean"),
            std=payload.get("std"),
            median=payload.get("median"),
            p25=payload.get("p25"),
            p75=payload.get("p75"),
            iqr=payload.get("iqr"),
            minimum=payload.get("minimum"),
            maximum=payload.get("maximum"),
            grid=tuple(payload.get("grid") or ()),
            values=tuple(payload.get("values") or ()),
            low_order_stats=tuple(payload.get("low_order_stats") or ()),
            high_order_stats=tuple(payload.get("high_order_stats") or ()),
        )


def quantile(sorted_values: list[float], p: float) -> float:
    """Linear-interpolated quantile of an already-sorted sample (R type 7)."""
    if not sorted_values:
        raise ValueError("quantile() of an empty sample")
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"p must be in [0, 1], got {p}")
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    h = (n - 1) * p
    lo = math.floor(h)
    hi = math.ceil(h)
    if lo == hi:
        return sorted_values[int(h)]
    weight = h - lo
    return sorted_values[lo] * (1.0 - weight) + sorted_values[hi] * weight


def sample_std(values: list[float]) -> float | None:
    """Sample standard deviation (Bessel-corrected). ``None`` when undefined."""
    n = len(values)
    if n < 2:
        return None
    mu = fmean(values)
    var = sum((v - mu) ** 2 for v in values) / (n - 1)
    return math.sqrt(var)


def build_sketch(values: list[float]) -> DistributionSketch:
    """Reduce a sample to a :class:`DistributionSketch`."""
    clean = sorted(float(v) for v in values if v is not None and math.isfinite(v))
    n = len(clean)
    if n == 0:
        return DistributionSketch(n=0)

    p25 = quantile(clean, 0.25)
    p75 = quantile(clean, 0.75)
    k = min(TAIL_ORDER_STATS, n)

    return DistributionSketch(
        n=n,
        mean=fmean(clean),
        std=sample_std(clean),
        median=quantile(clean, 0.5),
        p25=p25,
        p75=p75,
        iqr=p75 - p25,
        minimum=clean[0],
        maximum=clean[-1],
        grid=QUANTILE_GRID,
        values=tuple(quantile(clean, p) for p in QUANTILE_GRID),
        low_order_stats=tuple(clean[:k]),
        high_order_stats=tuple(clean[-k:]),
    )


def _cdf_knots(sketch: DistributionSketch) -> list[tuple[float, float]]:
    """Monotone ``(value, non-exceedance probability)`` knots for interpolation.

    Combines the stored quantile grid with the retained tail order statistics,
    the latter carrying Weibull plotting positions so the extremes keep full
    resolution.
    """
    n = sketch.n
    if n == 0:
        return []
    denom = n + 1
    knots: list[tuple[float, float]] = []

    for i, value in enumerate(sketch.low_order_stats, start=1):
        knots.append((value, i / denom))
    for j, value in enumerate(sketch.high_order_stats):
        rank = n - len(sketch.high_order_stats) + 1 + j
        knots.append((value, rank / denom))

    # Only take grid points strictly inside the region the order statistics
    # already describe, otherwise the two sources fight over the same span.
    inner_lo = len(sketch.low_order_stats) / denom
    inner_hi = (n - len(sketch.high_order_stats) + 1) / denom
    for p, value in zip(sketch.grid, sketch.values, strict=False):
        if inner_lo < p < inner_hi:
            knots.append((value, p))

    knots.sort(key=lambda kv: (kv[0], kv[1]))

    # Enforce a non-decreasing CDF: ties in value collapse to the highest
    # probability seen so far (a step in the empirical CDF).
    monotone: list[tuple[float, float]] = []
    running = 0.0
    for value, p in knots:
        running = max(running, p)
        if monotone and math.isclose(monotone[-1][0], value, rel_tol=0.0, abs_tol=1e-12):
            monotone[-1] = (value, running)
        else:
            monotone.append((value, running))
    return monotone


def empirical_cdf(sketch: DistributionSketch, x: float) -> float:
    """Estimated ``P(X <= x)`` from the sketch, in [0, 1]."""
    knots = _cdf_knots(sketch)
    if not knots:
        return float("nan")
    if x <= knots[0][0]:
        # At or below the smallest retained sample. Weibull position of the
        # minimum is 1/(n+1); anything below that is unresolvable, so report 0.
        return knots[0][1] if math.isclose(x, knots[0][0], abs_tol=1e-12) else 0.0
    if x >= knots[-1][0]:
        return 1.0 if x > knots[-1][0] else knots[-1][1]

    lo_v, lo_p = knots[0]
    for value, p in knots[1:]:
        if x <= value:
            if math.isclose(value, lo_v, abs_tol=1e-12):
                return p
            weight = (x - lo_v) / (value - lo_v)
            return lo_p + weight * (p - lo_p)
        lo_v, lo_p = value, p
    return 1.0  # pragma: no cover - unreachable given the bounds check above


@dataclass(slots=True)
class TailResult:
    """Outcome of a one-sided tail-probability query."""

    probability: float
    percentile: float
    bounded: bool  # probability hit the 1/(n+1) resolution floor
    beyond_sample: bool  # observation lies outside the reference sample's range


def tail_probability(
    sketch: DistributionSketch, x: float, direction: str
) -> TailResult:
    """One-sided empirical exceedance probability in the observed direction.

    ``direction`` is ``"above"`` or ``"below"``. The returned probability is
    floored at ``1/(n+1)``; when that floor binds, ``bounded`` is True and the
    caller must present the result as "at least this rare", never as an exact
    figure.
    """
    if direction not in ("above", "below"):
        raise ValueError(f"direction must be 'above' or 'below', got {direction!r}")
    if sketch.is_empty:
        return TailResult(float("nan"), float("nan"), False, False)

    f = empirical_cdf(sketch, x)
    raw = (1.0 - f) if direction == "above" else f
    floor = sketch.probability_floor
    bounded = raw < floor
    probability = max(raw, floor)

    beyond = bool(
        (direction == "above" and sketch.maximum is not None and x > sketch.maximum)
        or (direction == "below" and sketch.minimum is not None and x < sketch.minimum)
    )
    return TailResult(
        probability=probability,
        percentile=100.0 * f,
        bounded=bounded,
        beyond_sample=beyond,
    )


def zero_inflated_upper_tail(
    zero_fraction: float,
    nonzero_sketch: DistributionSketch,
    x: float,
    total_n: int,
) -> TailResult:
    """Upper-tail probability for a zero-inflated variable (daily precipitation).

    Daily rainfall is a *mixture*: a point mass at exactly zero with probability
    ``zero_fraction``, and a continuous wet-day distribution otherwise. So

        P(X >= x) = P(X > 0) * P(X >= x | X > 0)   for x > 0

    Treating the combined sample as one continuous distribution — or worse,
    summarising it with a mean and standard deviation — badly understates heavy
    rain in dry climates, where most of the probability mass sits on zero.

    ``x <= 0`` returns probability 1.0: a dry day is not an upper-tail event.
    Dry *spells* are a multi-day question and are out of scope for a daily
    metric; see docs/methodology.md.
    """
    if x <= 0.0:
        return TailResult(probability=1.0, percentile=0.0, bounded=False, beyond_sample=False)
    if nonzero_sketch.is_empty:
        return TailResult(float("nan"), float("nan"), False, False)

    p_wet = max(0.0, min(1.0, 1.0 - zero_fraction))
    f_given_wet = empirical_cdf(nonzero_sketch, x)
    raw = p_wet * (1.0 - f_given_wet)

    floor = 1.0 / (total_n + 1) if total_n > 0 else nonzero_sketch.probability_floor
    bounded = raw < floor
    probability = max(raw, floor)

    # Unconditional non-exceedance: the whole dry mass sits below any x > 0.
    percentile = 100.0 * (1.0 - raw)
    beyond = nonzero_sketch.maximum is not None and x > nonzero_sketch.maximum

    return TailResult(
        probability=probability,
        percentile=percentile,
        bounded=bounded,
        beyond_sample=bool(beyond),
    )


def build_histogram(values: list[float], bins: int = 24) -> dict | None:
    """Fixed-bin histogram of a sample, for the distribution chart.

    Published alongside the sketch so the frontend can draw the real seasonal
    distribution without the API either storing or shipping hundreds of raw
    values per bucket. Degenerate samples (all identical) get a single
    zero-width-safe bin rather than a division by zero.
    """
    clean = [float(v) for v in values if v is not None and math.isfinite(v)]
    if not clean or bins < 1:
        return None
    lo, hi = min(clean), max(clean)
    if math.isclose(lo, hi):
        return {"bin_edges": [lo, lo], "counts": [len(clean)], "degenerate": True}

    width = (hi - lo) / bins
    counts = [0] * bins
    for v in clean:
        idx = int((v - lo) / width)
        if idx >= bins:  # the maximum lands exactly on the top edge
            idx = bins - 1
        counts[idx] += 1
    edges = [lo + i * width for i in range(bins + 1)]
    return {"bin_edges": edges, "counts": counts, "degenerate": False}


def robust_deviation(x: float, median: float | None, iqr: float | None) -> float | None:
    """``(x - median) / IQR``: a scale-free deviation that survives skew.

    Used for tie-breaking and for reporting alongside z-scores, because it is
    defined for distributions where a standard deviation is not a meaningful
    summary. ``None`` when the IQR is zero or unknown.
    """
    if median is None or iqr is None or iqr <= 0:
        return None
    return (x - median) / iqr


def return_period_years(probability: float, window_day_count: int) -> float | None:
    """Approximate recurrence interval for an event *at this time of year*.

    The tail probability is a per-day exceedance probability estimated within a
    seasonal window of ``window_day_count`` days. The expected number of
    exceedances per year inside that window is ``probability * window_day_count``,
    so the recurrence interval is its reciprocal.

    This deliberately answers "how often is a day this extreme expected during
    this part of the calendar", which is the question the ranking asks. It
    assumes exceedances within the window are independent; consecutive days are
    in fact correlated, so real-world recurrence is somewhat longer. The figure
    is labelled approximate everywhere it is shown.
    """
    if not math.isfinite(probability) or probability <= 0 or window_day_count <= 0:
        return None
    expected_per_year = probability * window_day_count
    if expected_per_year <= 0:
        return None
    return 1.0 / expected_per_year


def normal_sf(z: float) -> float:
    """Upper-tail probability of the standard normal, ``P(Z > z)``.

    Reported for comparison on the methodology page only. It is never used for
    ranking, because assuming normality is the mistake this project exists to
    avoid.
    """
    return 0.5 * math.erfc(z / math.sqrt(2.0))
