"""Grounding guards: the reason an LLM is allowed near this data at all.

A model is good at turning a calculation trace into a readable paragraph and bad
at resisting the pull of a dramatic claim. These guards assume the second thing.
Every generated explanation is checked, and anything that fails is discarded in
favour of the deterministic template — the pipeline never publishes prose it
could not verify.

Four checks, in order of how often they actually fire:

1. **Numeric grounding.** Every number in the text must correspond to a number a
   tool returned. The whitelist is built mechanically from the tool results, so
   it cannot drift from what the agent was actually shown. A handful of
   legitimate transformations are allowed: a probability read as a percentage, a
   probability read as "1 in N", and rounding to fewer decimals than the source.
2. **Record claims.** "Record", "all-time", "hottest ever" and friends are
   banned outright, because this project does not verify against any records
   archive. It computes statistical outliers.
3. **Causal claims.** No tool returns synoptic data, so *any* named weather
   system — heat dome, atmospheric river, polar vortex — is ungrounded by
   construction, whatever else the sentence says.
4. **Source references.** Naming NOAA, Environment Canada or a URL implies a
   provenance the pipeline never touched.

Negation is handled deliberately: the ``caveats`` field is *supposed* to say
"this is not an official record", and flagging that would make the guards
unusable. A banned term preceded by a negation marker is treated as a permitted
disclaimer, and counted so the behaviour stays visible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Numbers, with or without thousands separators. The leading lookbehind stops
# "log10" and "temperature_2m" from contributing spurious citations.
_NUMBER_RE = re.compile(
    r"(?<![\w.])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])-?\d+(?:\.\d+)?"
)

#: Numbers any sentence may use without a tool having returned them: counts and
#: percentages small enough to be structural rather than evidential.
TRIVIAL_NUMBERS: frozenset[float] = frozenset({0.0, 1.0, 2.0, 10.0, 100.0})

RECORD_TERMS: tuple[str, ...] = (
    "record",
    "records",
    "record-breaking",
    "record breaking",
    "all-time",
    "all time",
    "unprecedented",
    "hottest ever",
    "coldest ever",
    "wettest ever",
    "driest ever",
    "windiest ever",
    "highest ever",
    "lowest ever",
    "warmest ever",
    "first time in history",
    "never before",
    "in recorded history",
)

#: Named atmospheric phenomena. No tool in this system returns synoptic data, so
#: mentioning any of these is an ungrounded causal claim by construction.
PHENOMENON_TERMS: tuple[str, ...] = (
    "heat dome",
    "heat wave",
    "heatwave",
    "cold snap",
    "el nino",
    "el niño",
    "la nina",
    "la niña",
    "atmospheric river",
    "polar vortex",
    "jet stream",
    "cold front",
    "warm front",
    "blocking high",
    "omega block",
    "cut-off low",
    "cutoff low",
    "upper-level low",
    "upper level ridge",
    "monsoon",
    "hurricane",
    "tropical storm",
    "tropical depression",
    "nor'easter",
    "noreaster",
    "derecho",
    "chinook",
    "santa ana",
    "lake-effect",
    "lake effect",
    "bomb cyclone",
    "bombogenesis",
    "climate change",
    "global warming",
    "greenhouse",
)

CAUSAL_PHRASES: tuple[str, ...] = (
    "caused by",
    "was caused",
    "were caused",
    "driven by",
    "brought on by",
    "the result of a",
    "owing to a",
    "thanks to a",
    "triggered by",
    "attributable to",
)

ORG_TERMS: tuple[str, ...] = (
    "noaa",
    "nws",
    "national weather service",
    "environment canada",
    "conagua",
    "world meteorological",
    "met office",
    "accuweather",
    "weather channel",
    "weather underground",
    "wikipedia",
)

_URL_RE = re.compile(r"https?://|www\.")

_NEGATION_MARKERS: tuple[str, ...] = (
    "not ",
    "no ",
    "n't ",
    "never ",
    "without ",
    "rather than ",
    "instead of ",
    "cannot ",
    "isn't ",
    "aren't ",
    "neither ",
    "nor ",
    "avoid ",
    "unlike ",
    "other than ",
)

#: How far back to look for a negation marker before a banned term.
_NEGATION_WINDOW = 40


@dataclass
class GuardReport:
    """Outcome of validating one explanation."""

    ok: bool = True
    numbers_found: int = 0
    numbers_grounded: int = 0
    ungrounded_numbers: list[float] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    negated_terms: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "numbers_found": self.numbers_found,
            "numbers_grounded": self.numbers_grounded,
            "ungrounded_numbers": self.ungrounded_numbers,
            "violations": self.violations,
            "warnings": self.warnings,
            "negated_terms": self.negated_terms,
        }

    @property
    def summary(self) -> str:
        return "; ".join(self.violations) if self.violations else "ok"


# ---------------------------------------------------------------------------
# Numeric grounding
# ---------------------------------------------------------------------------


def extract_numbers(text: str) -> list[float]:
    """Every number a reader would see as a number."""
    out: list[float] = []
    for match in _NUMBER_RE.finditer(text):
        raw = match.group(0).replace(",", "")
        try:
            out.append(float(raw))
        except ValueError:  # pragma: no cover - regex guarantees parseability
            continue
    return out


def build_allowed_numbers(numbers: set[float], texts: list[str]) -> set[float]:
    """Expand the raw tool numbers into the set a sentence may legitimately cite.

    Numbers embedded in tool *strings* count too: a reference period returned as
    ``"1991-2020"`` licenses both years. Probabilities additionally license their
    percentage form and their ``1 in N`` reading, because those are faithful
    restatements rather than new facts.
    """
    allowed: set[float] = set(TRIVIAL_NUMBERS)
    allowed |= {float(n) for n in numbers}
    for text in texts:
        allowed.update(extract_numbers(text))

    derived: set[float] = set()
    for value in allowed:
        # "11.3 °C below the mean" is a faithful reading of a deviation of -11.3.
        derived.add(abs(value))
        if 0.0 < value <= 1.0:
            derived.add(value * 100.0)
            derived.add(1.0 / value)
        if value > 1.0:
            derived.add(1.0 / value * 100.0)
    return allowed | derived


def _decimals(value: float) -> int:
    text = repr(value)
    if "." not in text or "e" in text or "E" in text:
        return 0
    return len(text.split(".", 1)[1].rstrip("0"))


def is_grounded(number: float, allowed: set[float]) -> bool:
    """Whether ``number`` is a faithful restatement of something in ``allowed``.

    The proportional tolerance for large figures deliberately excludes the trivial
    constants. Otherwise the structurally permitted ``100`` would license anything
    from 98 to 102, and percentiles live there — an invented "99.97th percentile"
    would pass while the tools had returned 99.8. A tool that genuinely returns 100
    still matches it exactly.
    """
    precision = _decimals(number)
    for value in allowed:
        if number == value:
            return True
        if round(value, precision) == number:
            return True
        # Large figures are routinely rounded in prose ("about 1 in 500 years").
        if (
            abs(value) >= 100.0
            and value not in TRIVIAL_NUMBERS
            and abs(number - value) <= 0.02 * abs(value)
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Claim checks
# ---------------------------------------------------------------------------


def _is_negated(haystack: str, start: int) -> bool:
    window = haystack[max(0, start - _NEGATION_WINDOW) : start]
    return any(marker in window for marker in _NEGATION_MARKERS)


def _find_terms(text: str, terms: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """Return (asserted, negated) matches for a term list."""
    lowered = text.lower()
    asserted: list[str] = []
    negated: list[str] = []
    for term in terms:
        pattern = (
            re.compile(rf"\b{re.escape(term)}\b")
            if term[-1].isalnum()
            else re.compile(re.escape(term))
        )
        for match in pattern.finditer(lowered):
            if _is_negated(lowered, match.start()):
                negated.append(term)
            else:
                asserted.append(term)
            break
    return asserted, negated


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def validate_explanation(
    *,
    text: str,
    evidence_values: list[float | int | str],
    allowed_numbers: set[float],
    required_numbers: list[float] | None = None,
    required_terms: list[str] | None = None,
) -> GuardReport:
    """Run every guard over one candidate explanation.

    Parameters
    ----------
    text:
        All prose the reader will see, concatenated.
    evidence_values:
        The ``evidence[].value`` entries the model claims to be citing. Numeric
        ones must themselves be grounded — a fabricated evidence row is as bad as
        a fabricated sentence.
    allowed_numbers:
        Output of :func:`build_allowed_numbers`.
    required_numbers:
        Numbers the explanation must actually cite, normally just the observed
        value. An explanation that never states the measurement is not an
        explanation.
    required_terms:
        Strings that must appear, normally the city name, so a mix-up between
        concurrently investigated events cannot slip through.
    """
    report = GuardReport()

    # 1. Numeric grounding.
    found = extract_numbers(text)
    report.numbers_found = len(found)
    for number in found:
        if is_grounded(number, allowed_numbers):
            report.numbers_grounded += 1
        elif number not in report.ungrounded_numbers:
            report.ungrounded_numbers.append(number)
    for number in report.ungrounded_numbers:
        report.violations.append(f"ungrounded_number:{number:g}")

    # 1b. Evidence rows must be grounded too.
    for value in evidence_values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not is_grounded(float(value), allowed_numbers):
            report.violations.append(f"ungrounded_evidence_value:{float(value):g}")

    # 2/3/4. Claim checks.
    for terms, label in (
        (RECORD_TERMS, "unsupported_record_claim"),
        (PHENOMENON_TERMS, "unsupported_causal_claim"),
        (CAUSAL_PHRASES, "unsupported_causal_claim"),
        (ORG_TERMS, "unsupported_source_reference"),
    ):
        asserted, negated = _find_terms(text, terms)
        for term in asserted:
            report.violations.append(f"{label}:{term}")
        report.negated_terms.extend(negated)

    if _URL_RE.search(text.lower()):
        report.violations.append("unsupported_source_reference:url")

    # 5. Required content.
    for number in required_numbers or []:
        if not any(abs(number - candidate) <= 0.051 for candidate in found):
            report.violations.append(f"required_number_missing:{number:g}")
    lowered = text.lower()
    for term in required_terms or []:
        if term.lower() not in lowered:
            report.violations.append(f"required_term_missing:{term}")

    report.ok = not report.violations
    return report
