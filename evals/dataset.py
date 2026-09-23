"""Loader for the versioned scoring dataset.

Turns ``evals/cases/scoring.json`` into concrete baseline samples and observed
values. Two properties matter more than anything else here:

**Determinism across machines and Python versions.** The generators use only
``random.Random(seed).random()`` — a Mersenne Twister whose output stream for
``random()`` is fixed by the language — plus explicit arithmetic. Nothing calls
``random.gauss``, ``random.gammavariate`` or NumPy, whose algorithms are
implementation details and have changed between releases. A dataset that
generated different floats after an interpreter upgrade would silently
invalidate every recorded result.

**The baseline is built by production, not here.** ``build_baseline`` hands the
generated sample to the application's own ``_make_baseline_row`` so the sketch,
the wet/dry split and the sufficiency flag are all derived by the code under
test. The independent arithmetic lives in :mod:`evals.reference` and consumes the
same raw sample; keeping the two apart is what makes the comparison meaningful.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CASES_PATH = Path(__file__).resolve().parent / "cases" / "scoring.json"

TWO_PI = 2.0 * math.pi


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


def _uniforms(rng: random.Random, count: int) -> list[float]:
    """``count`` draws on (0, 1), with exact zero pushed off the boundary.

    ``random()`` can return 0.0, and both ``log`` and the Box-Muller radius
    would fail on it. Substituting the smallest positive double keeps the stream
    position unchanged, which a re-draw would not.
    """
    out = []
    for _ in range(count):
        u = rng.random()
        out.append(u if u > 0.0 else 5e-324)
    return out


def normal_sample(n: int, seed: int, mean: float, sd: float) -> list[float]:
    """Box-Muller, two uniforms per pair of normals."""
    rng = random.Random(seed)
    values: list[float] = []
    while len(values) < n:
        u1, u2 = _uniforms(rng, 2)
        radius = math.sqrt(-2.0 * math.log(u1))
        values.append(mean + sd * radius * math.cos(TWO_PI * u2))
        if len(values) < n:
            values.append(mean + sd * radius * math.sin(TWO_PI * u2))
    return values


def _erlang(rng: random.Random, shape: int, scale: float) -> float:
    """Gamma with integer shape: the sum of ``shape`` exponentials."""
    total = 0.0
    for u in _uniforms(rng, shape):
        total -= math.log(u)
    return scale * total


def _gamma(n: int, seed: int, shape: int, scale: float) -> list[float]:
    rng = random.Random(seed)
    return [_erlang(rng, shape, scale) for _ in range(n)]


def _zero_inflated_gamma(
    n: int, seed: int, wet_probability: float, shape: int, scale: float
) -> list[float]:
    """A point mass at exactly zero, mixed with a gamma wet-day distribution."""
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(n):
        if rng.random() < wet_probability:
            values.append(_erlang(rng, shape, scale))
        else:
            values.append(0.0)
    return values


def generate_sample(spec: dict[str, Any]) -> list[float] | None:
    """Build the raw reference sample for one case.

    ``None`` means "no baseline row at all", which is a different failure from
    "a baseline row whose sample is empty" — the dataset distinguishes them and
    so must this.
    """
    kind = spec["kind"]
    if kind == "none":
        return None
    if kind == "empty":
        return []
    if kind == "constant":
        return [float(spec["value"])] * int(spec["n"])
    if kind == "normal":
        return normal_sample(
            int(spec["n"]), int(spec["seed"]), float(spec["mean"]), float(spec["sd"])
        )
    if kind == "gamma":
        return _gamma(int(spec["n"]), int(spec["seed"]), int(spec["shape"]), float(spec["scale"]))
    if kind == "zero_inflated_gamma":
        return _zero_inflated_gamma(
            int(spec["n"]),
            int(spec["seed"]),
            float(spec["wet_probability"]),
            int(spec["shape"]),
            float(spec["scale"]),
        )
    raise ValueError(f"unknown sample kind {kind!r}")


# ---------------------------------------------------------------------------
# Observed values
# ---------------------------------------------------------------------------


def resolve_observed(spec: Any, sample: list[float] | None) -> float | None:
    """Resolve the ``observed`` field, which is a literal or a rule.

    Rules are positions in the generated distribution — the maximum, the fifth
    largest, the 90th percentile. Expressing them as positions rather than floats
    is what keeps each case's intent legible, and stops a retuned recipe from
    quietly turning an "equals the record" case into an ordinary one.
    """
    if spec is None:
        return None
    if isinstance(spec, int | float):
        return float(spec)
    if isinstance(spec, str):
        if spec == "nan":
            return float("nan")
        raise ValueError(f"unknown observed literal {spec!r}")

    if sample is None:
        raise ValueError("an observed rule needs a sample to resolve against")

    # Imported here so this module stays importable without the app on the path.
    from evals.reference import quantile, wet_days

    ordered = sorted(sample)
    rule = spec["rule"]

    if rule == "quantile":
        return quantile(ordered, float(spec["p"]))
    if rule == "order_stat_from_top":
        return ordered[-int(spec["index"])]
    if rule == "order_stat_from_bottom":
        return ordered[int(spec["index"]) - 1]
    if rule == "above_max":
        return ordered[-1] + float(spec["by"])
    if rule == "below_min":
        return ordered[0] - float(spec["by"])
    if rule == "wet_order_stat_from_top":
        return wet_days(ordered)[-int(spec["index"])]
    if rule == "above_wet_max":
        return wet_days(ordered)[-1] + float(spec["by"])
    raise ValueError(f"unknown observed rule {rule!r}")


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ScoringCase:
    """One dataset row, with its sample and observation already resolved."""

    id: str
    category: str
    title: str
    metric: str
    sample: list[float] | None
    observed: float | None
    n_years: int
    window_days: int
    expect: dict[str, Any]
    min_wet_days_override: int | None
    note: str | None

    @property
    def expects_none(self) -> bool:
        return bool(self.expect.get("returns_none"))


@dataclass(slots=True)
class ScoringDataset:
    """The dataset as loaded, carrying the version that must reach MLflow."""

    schema_version: int
    dataset_version: str
    description: str
    notes: tuple[str, ...]
    cases: tuple[ScoringCase, ...]

    def by_category(self, category: str) -> tuple[ScoringCase, ...]:
        return tuple(c for c in self.cases if c.category == category)

    @property
    def category_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for case in self.cases:
            counts[case.category] = counts.get(case.category, 0) + 1
        return counts


def load_dataset(path: Path | None = None) -> ScoringDataset:
    """Read, validate and resolve the dataset."""
    raw = json.loads((path or CASES_PATH).read_text(encoding="utf-8"))
    defaults = raw.get("defaults", {})

    seen: set[str] = set()
    cases: list[ScoringCase] = []
    for entry in raw["cases"]:
        case_id = entry["id"]
        if case_id in seen:
            raise ValueError(f"duplicate case id {case_id!r} in the dataset")
        seen.add(case_id)

        sample = generate_sample(entry["sample"])
        cases.append(
            ScoringCase(
                id=case_id,
                category=entry["category"],
                title=entry["title"],
                metric=entry["metric"],
                sample=sample,
                observed=resolve_observed(entry.get("observed"), sample),
                n_years=int(entry.get("n_years", defaults.get("n_years", 30))),
                window_days=int(entry.get("window_days", defaults.get("window_days", 7))),
                expect=entry.get("expect", {}),
                min_wet_days_override=entry.get("min_wet_days_override"),
                note=entry.get("note"),
            )
        )

    return ScoringDataset(
        schema_version=int(raw["schema_version"]),
        dataset_version=str(raw["dataset_version"]),
        description=str(raw["description"]),
        notes=tuple(raw.get("notes", ())),
        cases=tuple(cases),
    )


# ---------------------------------------------------------------------------
# Production baseline construction
# ---------------------------------------------------------------------------


def build_baseline(case: ScoringCase, *, source_dataset: str = "synthetic_eval_v1"):
    """Build the application's own ``Baseline`` from a case's raw sample.

    Deliberately routed through ``_make_baseline_row``, private though it is:
    that function is where the sketch, the wet/dry split and the ``sufficient``
    flag are actually decided, and an evaluator that reimplemented any of them
    would be grading its own homework. It needs no session, so no database is
    touched.
    """
    from app.config import Settings
    from app.domain import Metric
    from app.ingest.baselines import _make_baseline_row, row_to_baseline

    if case.sample is None:
        return None

    settings = Settings()
    row = _make_baseline_row(
        city_id="eval",
        metric=Metric(case.metric),
        target_doy=196,
        sample=list(case.sample),
        n_years=case.n_years,
        settings=settings,
        dataset=source_dataset,
    )
    # An empty sample yields a row whose sketch is empty rather than an error,
    # which is what the "baseline row exists but has no data" case needs:
    # `compute_anomaly` reports `no_baseline` for it, the same as for a row that
    # was never written.
    return row_to_baseline(row)
