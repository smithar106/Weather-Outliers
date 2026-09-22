"""Deterministic cross-metric ranking.

Given every scored candidate for an analysis date, produce the published top-N
board. Three properties matter more than cleverness here:

**Determinism.** Two runs over identical inputs must produce an identical board,
including in the presence of exact ties. The sort key below is a *total* order:
every component is either a rounded number or a fixed lookup, and the final
component is the city id, which is unique. Nothing depends on dictionary or
database iteration order.

**Comparability.** Candidates are ordered by surprisal derived from empirical
tail probability, so a 1-in-400 rainfall total and a 1-in-400 cold morning sit
at the same height regardless of their physical units or distribution shapes.

**Diversity.** A single heat dome can generate a genuine top-10 sweep across one
metro region. That is meteorologically real but makes for a useless daily board,
so by default one event per city reaches the published top 10. Every other event
is still stored, still scored, and still queryable through the API — the
constraint applies to presentation, not to the dataset.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain import metric_tiebreak_index
from app.stats.anomaly import COMPARISON_DECIMALS, AnomalyCandidate

#: Documented, stable tie-break chain, applied in order:
#:
#: 1. higher anomaly score
#: 2. lower (rarer) tail probability
#: 3. larger absolute robust deviation, (x - median) / IQR — always defined,
#:    unlike a z-score, so it can break ties between different metric families
#: 4. fixed metric precedence (see ``METRIC_TIEBREAK_ORDER``)
#: 5. city id, ascending — guarantees a total order and therefore reproducibility
TIEBREAK_CHAIN: tuple[str, ...] = (
    "anomaly_score desc",
    "tail_probability asc",
    "abs(robust_deviation) desc",
    "metric precedence",
    "city_id asc",
)


def ranking_sort_key(candidate: AnomalyCandidate) -> tuple:
    """Total-order sort key implementing :data:`TIEBREAK_CHAIN`."""
    score = round(candidate.anomaly_score, COMPARISON_DECIMALS)
    tail = candidate.tail_probability
    tail = round(tail, COMPARISON_DECIMALS) if tail is not None else 1.0
    robust = candidate.robust_deviation
    robust = round(abs(robust), COMPARISON_DECIMALS) if robust is not None else 0.0
    return (-score, tail, -robust, metric_tiebreak_index(candidate.metric), candidate.city_id)


@dataclass(slots=True)
class RankedEvent:
    rank: int
    candidate: AnomalyCandidate


@dataclass(slots=True)
class RankingResult:
    ranked: list[RankedEvent]
    sorted_candidates: list[AnomalyCandidate]
    diagnostics: dict

    @property
    def event_count(self) -> int:
        return len(self.ranked)


def rank_candidates(
    candidates: list[AnomalyCandidate],
    *,
    top_n: int = 10,
    one_event_per_city: bool = True,
) -> RankingResult:
    """Select and order the published board.

    Only ``eligible`` candidates can be ranked. With ``one_event_per_city``, a
    first pass takes each city's single strongest event; if that yields fewer
    than ``top_n`` rows (few cities reporting, or a quiet day), a documented
    second pass backfills with the next strongest remaining events regardless of
    city, so the board is never short for a purely cosmetic reason.
    """
    eligible = [c for c in candidates if c.eligible]
    ordered = sorted(eligible, key=ranking_sort_key)

    selected: list[AnomalyCandidate] = []
    backfilled = 0

    if one_event_per_city:
        seen_cities: set[str] = set()
        for candidate in ordered:
            if len(selected) >= top_n:
                break
            if candidate.city_id in seen_cities:
                continue
            seen_cities.add(candidate.city_id)
            selected.append(candidate)

        if len(selected) < top_n:
            already = {id(c) for c in selected}
            for candidate in ordered:
                if len(selected) >= top_n:
                    break
                if id(candidate) in already:
                    continue
                selected.append(candidate)
                backfilled += 1
            # Re-apply the canonical order after backfilling so ranks stay
            # monotone in score.
            selected.sort(key=ranking_sort_key)
    else:
        selected = ordered[:top_n]

    ranked = [RankedEvent(rank=i, candidate=c) for i, c in enumerate(selected, start=1)]

    excluded_reasons: dict[str, int] = {}
    for c in candidates:
        if not c.eligible:
            key = c.excluded_reason or "unknown"
            excluded_reasons[key] = excluded_reasons.get(key, 0) + 1

    diagnostics = {
        "candidates_total": len(candidates),
        "candidates_eligible": len(eligible),
        "candidates_excluded": len(candidates) - len(eligible),
        "excluded_reasons": excluded_reasons,
        "distinct_cities_eligible": len({c.city_id for c in eligible}),
        "one_event_per_city": one_event_per_city,
        "backfilled_slots": backfilled,
        "top_n_requested": top_n,
        "top_n_published": len(ranked),
        "tiebreak_chain": list(TIEBREAK_CHAIN),
    }

    return RankingResult(ranked=ranked, sorted_candidates=ordered, diagnostics=diagnostics)
