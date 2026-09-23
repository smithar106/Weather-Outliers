"""Evaluator 3 — agreement between explanations and the source metrics.

The grounding suite already asks a weaker question: is every number in the prose
*citable* — does it appear somewhere in what the tools returned? That catches
invention, but it cannot catch misattribution. An explanation that swapped the
median for the 25th percentile, or quoted the sample maximum as the observed
value, or said "below average" about a hot day, would pass a whitelist check
cleanly: every number is real, and the words are unremarkable.

So this evaluator reads the published prose as a set of *claims* and checks each
one against the stored calculation for that specific event:

* **Numeric claims** are extracted by pattern — the deviation quoted next to
  "above its … average", the number following "median", the pair bracketing
  "middle half", the sample size preceding "values", the percentile, the tail
  probability, the return period, the sample extremes. Each is compared against
  the column it is supposed to come from, rounded to *the number of decimals the
  prose actually used*. That last detail is what makes the comparison exact rather
  than tolerant: the text says 10.5, the stored deviation is 10.547555…, and
  ``round(10.547555, 1) == 10.5`` either holds or it does not.

* **Directional claims** must agree with ``event.direction``. A sign error is the
  one mistake that a citable-numbers guard is structurally blind to.

* **Hedging claims** must match ``tail_probability_is_bounded``. Where the
  probability sits on the floor set by the sample size, it is a bound and the
  prose has to say so; where it does not, the prose must not hedge as though it
  did. Both directions are wrong in the same way — describing the evidence as
  something other than what it is.

* **Prohibited claims** are checked because the methodology forbids them, not
  because a model happened to avoid them: no record claim without an
  authoritative source to verify it against, no describing a model-analysis
  estimate as an instrument reading, and no z-score quoted for a distribution
  where ``z_valid`` is false.

Two honest limits. First, the extractors are written against the deterministic
template's phrasing; a live model that words things differently will yield fewer
extractable claims, so the suite counts what it extracted and fails if any event
yielded almost nothing rather than passing on a silent no-match. Second, a claim
this evaluator cannot parse is a claim it cannot check — the coverage metric is
part of the result, not a footnote to it.
"""

from __future__ import annotations

import re
import traceback

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

SUITE_ID = "explanation_agreement"
TITLE = "Explanations agree with their source metrics"
DESCRIPTION = (
    "Extracts the numeric and directional claims from each published explanation and "
    "checks them against the stored calculation for that event, not merely against a "
    "whitelist of citable numbers."
)

#: Below this many extracted claims, an event's prose has not really been checked
#: and the suite says so instead of reporting a vacuous pass.
MIN_CLAIMS_PER_EVENT = 4

#: Tight phrase lists. Deliberately phrases rather than bare words: "above" occurs
#: in "the probability above is quoted as a bound", which is not a directional
#: claim about the weather.
DIRECTION_MARKERS = {
    "above": ("above its", "above the", "warmer than", "hotter than", "climbed to", "upper tail"),
    "below": ("below its", "below the", "colder than", "cooler than", "dropped to", "lower tail"),
}

#: A record claim needs an authoritative record source to check against, and none
#: is implemented. The template's own disclaimer contains the word "record", so the
#: check looks for the word *outside* an explicit negation — and matches on word
#: boundaries, because "the gust was recorded at 56.8 km/h" is not a record claim
#: and flagging it would train a reader to ignore this check.
RECORD_NEGATIONS = (
    "not an official record",
    "no official record",
    "not a record",
    "does not constitute a record",
)
RECORD_PATTERNS = (
    r"\brecords?\b",
    r"\ball[- ]time\b",
    r"\bunprecedented\b",
    r"\b(?:hottest|coldest|wettest|driest|windiest)\s+(?:ever|on record|in history)\b",
)

#: Phrasing that would present a model-analysis estimate as an instrument reading.
#: The template's caveat says "not a reading from an instrument", so again the test
#: is for these phrases outside that negation.
INSTRUMENT_NEGATIONS = (
    "not a reading from an instrument",
    "not a reading",
    "not measured at",
    "not an observation from",
)
INSTRUMENT_PATTERNS = (
    r"\bweather station\b",
    r"\brecorded at the station\b",
    r"\bmeasured by an instrument\b",
    r"\bstation observation\b",
    r"\bgauge reading\b",
)

#: Normal-theory language that must not appear when the distribution does not
#: support it.
Z_SCORE_PATTERNS = (r"\bstandard deviations?\b", r"\bz[- ]scores?\b", r"\bsigma\b")

#: Claims every explanation has to make, whatever its phrasing. Without these two
#: the prose is not describing the calculation the methodology documents, and a
#: missing one would otherwise just reduce the claim count silently.
REQUIRED_CLAIM_KINDS = ("sample_size", "tail_probability")


def _decimals(literal: str) -> int:
    """How many decimal places the prose actually used."""
    return len(literal.split(".")[1]) if "." in literal else 0


def _agrees(literal: str, actual: float | None) -> bool:
    """Does the printed literal equal the stored value at the printed precision?"""
    if actual is None:
        return False
    try:
        claimed = float(literal)
    except ValueError:  # pragma: no cover - the patterns only capture numerals
        return False
    return round(actual, _decimals(literal)) == claimed


def _claims(text: str, event, unit_pattern: str) -> list[tuple[str, str, float | None]]:
    """Every numeric claim the extractors can find, as (kind, literal, expected)."""
    found: list[tuple[str, str, float | None]] = []

    def scan(kind: str, pattern: str, expected: float | None) -> None:
        for match in re.finditer(pattern, text):
            for literal in match.groups():
                if literal is not None:
                    found.append((kind, literal, expected))

    # "10.5 °C above its 1991-2020 mid-July average"
    deviation = abs(event.deviation) if event.deviation is not None else None
    scan(
        "deviation",
        rf"(-?\d+(?:\.\d+)?)\s*{unit_pattern}\s+(?:above|below)\s+its",
        deviation,
    )
    scan("median", rf"median\s+(-?\d+(?:\.\d+)?)\s*{unit_pattern}", event.baseline_median)
    # "middle half 24.9 to 29.4 °C" — two claims from one phrase.
    for match in re.finditer(
        rf"middle half\s+(-?\d+(?:\.\d+)?)\s+to\s+(-?\d+(?:\.\d+)?)\s*{unit_pattern}", text
    ):
        found.append(("p25", match.group(1), event.baseline_p25))
        found.append(("p75", match.group(2), event.baseline_p75))
    scan("sample_size", r"(\d+)\s+(?:[a-z ]*?)values\b", float(event.baseline_n))
    scan("sample_size", r"among the\s+(\d+)\s+samples", float(event.baseline_n))
    scan("percentile", r"(-?\d+(?:\.\d+)?)\s+percentile", event.percentile)
    # Any probability-shaped literal in the prose has to be *the* tail probability;
    # the template emits exactly one and there is nothing else it could be.
    scan("tail_probability", r"\b(0\.\d{3,5})\b", event.tail_probability)
    scan("return_period", r"every\s+(\d+(?:\.\d+)?)\s+years", event.return_period_years)
    scan(
        "sample_max",
        r"highest value among the \d+ samples is\s+(-?\d+(?:\.\d+)?)",
        event.baseline_max,
    )
    scan(
        "sample_min",
        r"lowest value among the \d+ samples is\s+(-?\d+(?:\.\d+)?)",
        event.baseline_min,
    )
    # "ranges from 10.7 to 66.8 km/h"
    for match in re.finditer(
        rf"ranges from\s+(-?\d+(?:\.\d+)?)\s+to\s+(-?\d+(?:\.\d+)?)\s*{unit_pattern}", text
    ):
        found.append(("sample_min", match.group(1), event.baseline_min))
        found.append(("sample_max", match.group(2), event.baseline_max))
    return found


def _mentions(text: str, patterns, negations=()) -> bool:
    """Does any of ``patterns`` match outside every one of ``negations``?"""
    stripped = text
    for negation in negations:
        stripped = stripped.replace(negation, " ")
    return any(re.search(pattern, stripped) for pattern in patterns)


def run(world) -> Suite:
    suite = Suite(id=SUITE_ID, title=TITLE, description=DESCRIPTION)
    timer = Timer()

    try:
        with timer:
            from sqlalchemy import select

            from app.models import (
                AgentExplanation,
                AnomalyEvent,
                City,
                DailyRanking,
                PipelineRun,
            )

            with world.session() as session:
                run_row = session.scalars(
                    select(PipelineRun)
                    .where(PipelineRun.published.is_(True))
                    .order_by(PipelineRun.analysis_date.desc())
                    .limit(1)
                ).first()
                if run_row is None:
                    suite.status = STATUS_FAILED
                    suite.duration_ms = timer.elapsed_ms
                    suite.notes.append(
                        "no published run in the scratch world, so no explanation could "
                        "be checked against its metrics"
                    )
                    return suite

                rows = session.execute(
                    select(DailyRanking.rank, AnomalyEvent, City, AgentExplanation)
                    .join(AnomalyEvent, AnomalyEvent.id == DailyRanking.event_id)
                    .join(City, City.id == AnomalyEvent.city_id)
                    .join(
                        AgentExplanation,
                        (AgentExplanation.event_id == AnomalyEvent.id)
                        & (AgentExplanation.run_id == run_row.id),
                    )
                    .where(DailyRanking.run_id == run_row.id)
                    .order_by(DailyRanking.rank)
                ).all()

                generators: dict[str, int] = {}
                total_claims = 0
                agreeing_claims = 0
                kinds_seen: set[str] = set()
                thin_events: list[str] = []
                missing_required: list[str] = []
                direction_violations: list[str] = []
                no_direction_marker: list[str] = []
                hedging_violations: list[str] = []
                record_violations: list[str] = []
                instrument_violations: list[str] = []
                z_violations: list[str] = []

                for rank, event, city, explanation in rows:
                    generators[explanation.generator] = generators.get(explanation.generator, 0) + 1
                    label = f"#{rank} {city.name} {event.metric}"
                    text = " ".join(
                        [
                            explanation.headline,
                            explanation.statistical_explanation,
                            explanation.historical_context,
                            explanation.caveats,
                        ]
                    )
                    lower = text.lower()
                    unit_pattern = re.escape(event.unit)

                    # -- numeric claims ----------------------------------------
                    claims = _claims(text, event, unit_pattern)
                    disagreements = [
                        (kind, literal, expected)
                        for kind, literal, expected in claims
                        if not _agrees(literal, expected)
                    ]
                    total_claims += len(claims)
                    agreeing_claims += len(claims) - len(disagreements)
                    kinds_seen.update(kind for kind, _, _ in claims)
                    if len(claims) < MIN_CLAIMS_PER_EVENT:
                        thin_events.append(label)
                    present_kinds = {kind for kind, _, _ in claims}
                    absent = [k for k in REQUIRED_CLAIM_KINDS if k not in present_kinds]
                    if absent:
                        missing_required.append(f"{label} (no {', '.join(absent)})")

                    observed_present = f"{event.observed_value:.1f}" in explanation.headline

                    suite.cases.append(
                        Case(
                            id=f"claims:{event.id}",
                            title=f"{label} — {len(claims)} claims vs the stored calculation",
                            passed=not disagreements and observed_present,
                            category="numeric_agreement",
                            expected=(
                                f"all {len(claims)} extracted claims match their stored "
                                "column, and the headline states the observed value"
                            ),
                            observed=(
                                f"{len(claims) - len(disagreements)}/{len(claims)} agree"
                                + ("" if observed_present else "; observed value absent")
                            ),
                            detail=(
                                "; ".join(
                                    f"{kind}: prose {literal}, stored {expected}"
                                    for kind, literal, expected in disagreements
                                )
                                or None
                            ),
                        )
                    )

                    # -- directional claim -------------------------------------
                    opposite = "below" if event.direction == "above" else "above"
                    if any(p in lower for p in DIRECTION_MARKERS[opposite]):
                        direction_violations.append(label)
                    if not any(p in lower for p in DIRECTION_MARKERS[event.direction]):
                        no_direction_marker.append(label)

                    # -- hedging matches boundedness ---------------------------
                    bound_language = "at most" in lower or "a bound" in lower
                    if event.tail_probability_is_bounded and not bound_language:
                        hedging_violations.append(f"{label} (bounded, stated as a point value)")
                    if not event.tail_probability_is_bounded and bound_language:
                        hedging_violations.append(f"{label} (not bounded, hedged as a bound)")

                    # -- prohibited claims -------------------------------------
                    if _mentions(lower, RECORD_PATTERNS, RECORD_NEGATIONS):
                        record_violations.append(label)
                    if event.observation_type != "station_observation" and _mentions(
                        lower, INSTRUMENT_PATTERNS, INSTRUMENT_NEGATIONS
                    ):
                        instrument_violations.append(label)
                    if not event.z_valid and _mentions(lower, Z_SCORE_PATTERNS):
                        z_violations.append(label)

            aggregate = [
                (
                    "direction_agreement",
                    "No explanation states the opposite direction to its stored event",
                    direction_violations,
                    "a sign error is invisible to a citable-numbers check",
                ),
                (
                    "direction_stated",
                    "Every explanation states the direction at all",
                    no_direction_marker,
                    "prose with no directional language cannot be checked for one",
                ),
                (
                    "bounded_probabilities_hedged",
                    "Hedging matches whether the probability is on the sample-size floor",
                    hedging_violations,
                    "a floored probability is a bound; stating it as a frequency overclaims",
                ),
                (
                    "no_unverified_record_claims",
                    "No record claim appears outside an explicit negation",
                    record_violations,
                    "no authoritative record source is implemented to verify one against",
                ),
                (
                    "modelled_data_not_called_an_observation",
                    "No model-analysis estimate is described as an instrument reading",
                    instrument_violations,
                    "the provider returns model analysis, not station observations",
                ),
                (
                    "z_score_withheld_when_invalid",
                    "No normal-theory language where z_valid is false",
                    z_violations,
                    "z-scores are not comparable across non-normal distributions",
                ),
            ]
            for case_id, title, violations, why in aggregate:
                suite.cases.append(
                    Case(
                        id=case_id,
                        title=title,
                        passed=not violations,
                        category="claim_agreement",
                        expected=f"0 of {len(rows)} explanations",
                        observed=(
                            f"{len(violations)}: {', '.join(violations[:4])}"
                            if violations
                            else f"0 of {len(rows)}"
                        ),
                        detail=why,
                    )
                )

            suite.cases.append(
                Case(
                    id="required_claims_present",
                    title=("Every explanation states the sample size and the tail probability"),
                    passed=not missing_required,
                    category="coverage",
                    expected=f"both of {', '.join(REQUIRED_CLAIM_KINDS)} in each explanation",
                    observed=(
                        f"{len(missing_required)} incomplete: {', '.join(missing_required[:4])}"
                        if missing_required
                        else f"all {len(rows)} complete"
                    ),
                    detail=(
                        "These two carry the methodology. Dropping the word that anchors "
                        "one of them would otherwise only lower the claim count, which is "
                        "not a failure this suite would notice."
                    ),
                )
            )
            suite.cases.append(
                Case(
                    id="claim_extraction_not_vacuous",
                    title=(
                        f"Every explanation yielded at least {MIN_CLAIMS_PER_EVENT} "
                        "checkable claims"
                    ),
                    passed=not thin_events,
                    category="coverage",
                    expected=f"≥{MIN_CLAIMS_PER_EVENT} extracted claims per explanation",
                    observed=(
                        f"{len(thin_events)} below the floor: {', '.join(thin_events[:4])}"
                        if thin_events
                        else f"all {len(rows)} above the floor"
                    ),
                    detail=(
                        "A pattern that matches nothing passes silently, which would make "
                        "this whole suite decorative. This is the check that stops that."
                    ),
                )
            )

            suite.metrics = [
                Metric("Published explanations checked", len(rows)),
                Metric(
                    "Generators",
                    ", ".join(f"{k}: {v}" for k, v in sorted(generators.items())) or "none",
                ),
                Metric("Numeric claims extracted", total_claims),
                Metric(
                    "Numeric claims agreeing with the stored calculation",
                    ratio(agreeing_claims, total_claims),
                    unit="fraction",
                    detail=f"{agreeing_claims}/{total_claims}",
                ),
                Metric(
                    "Claim kinds exercised",
                    ", ".join(sorted(kinds_seen)) or "none",
                    detail="each maps to a specific stored column",
                ),
                Metric(
                    "Claims per explanation",
                    ratio(total_claims, len(rows)),
                    detail="mean; the coverage floor is per-explanation",
                ),
                Metric(
                    "Checks passed",
                    ratio(sum(1 for c in suite.cases if c.passed), len(suite.cases)),
                    unit="fraction",
                ),
            ]

            suite.notes.append(
                "The claim extractors are written against the deterministic template's "
                "phrasing. A live model phrasing things differently would yield fewer "
                "extractable claims, which the coverage floor reports rather than hides."
            )
            suite.notes.append(
                "Numeric agreement is exact at the precision the prose used: the stored "
                "value is rounded to the number of decimals printed and must then match."
            )

        failed = [c for c in suite.cases if not c.passed]
        suite.status = STATUS_FAILED if failed else STATUS_PASSED
        suite.duration_ms = timer.elapsed_ms

    except Exception:
        suite.status = STATUS_ERROR
        suite.error = traceback.format_exc(limit=6)
        suite.duration_ms = timer.elapsed_ms

    return suite
