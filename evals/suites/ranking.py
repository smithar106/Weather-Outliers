"""Evaluator 2 — top-10 ranking correctness.

Ranking is where a statistics bug becomes a *published* statistics bug, and it is
the one stage whose output a reader cannot check by eye: the board looks equally
plausible whether or not the sort is right. So this suite attacks it from five
directions rather than asserting one happy path.

1. **Each tie-break link in isolation.** Five hand-built pairs, each equal on
   every earlier link and differing only on the one under test. A chain where
   link 4 is never reached is a chain with an untested link, and the failure mode
   it guards against — a board that reorders itself between runs — is invisible
   in any single run.
2. **Permutation invariance.** The same candidates in a different input order must
   produce a byte-identical board. This is the property that makes the daily rerun
   reproducible, and the one that silently breaks when a sort key stops being a
   total order.
3. **Structural invariants.** Contiguous ranks from 1, no city twice, scores
   non-increasing down the board, nothing ineligible published.
4. **One-event-per-city and its backfill.** A regional heat dome must not sweep
   the board; a quiet day must still fill ten slots if ten events exist.
5. **The real published board, re-derived.** Reads every stored candidate for the
   scratch world's analysis date and re-selects the board with
   :func:`evals.reference.select_board`, which reimplements the chain over plain
   dicts. Compared against what the pipeline actually wrote to ``daily_rankings``.

The reference selector is the independent half. It shares no code with
``app.stats.ranking`` — different data structures, separately written comparison
key — so agreement is evidence rather than tautology.
"""

from __future__ import annotations

import itertools
import random
import traceback

from evals import reference
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

SUITE_ID = "ranking"
TITLE = "Top-10 ranking correctness"
DESCRIPTION = (
    "Exercises every link of the tie-break chain in isolation, checks the board is "
    "invariant to input order, and re-derives the published board with an "
    "independently written selector."
)

#: Fixed seed for the permutation and invariant checks. A shuffle whose order
#: changed between runs would make a failure unreproducible, which is the one
#: thing a determinism test must not be.
SHUFFLE_SEED = 20260922


def _candidate(
    *,
    city_id: str,
    metric,
    score: float,
    tail: float | None = 0.01,
    robust: float | None = 1.0,
    eligible: bool = True,
):
    """A minimal candidate carrying only the fields the sort key reads."""
    from app.domain import UNITS, Direction
    from app.stats.anomaly import AnomalyCandidate

    return AnomalyCandidate(
        city_id=city_id,
        local_date="2025-07-15",
        metric=metric,
        direction=Direction.ABOVE,
        observed_value=0.0,
        unit=UNITS[metric],
        anomaly_score=score,
        tail_probability=tail,
        robust_deviation=robust,
        eligible=eligible,
    )


def _as_row(candidate) -> dict:
    """The candidate as the reference selector sees it: a plain dict."""
    return {
        "city_id": candidate.city_id,
        "metric": candidate.metric.value
        if hasattr(candidate.metric, "value")
        else str(candidate.metric),
        "anomaly_score": candidate.anomaly_score,
        "tail_probability": candidate.tail_probability,
        "robust_deviation": candidate.robust_deviation,
        "eligible": candidate.eligible,
    }


def _tiebreak_cases(suite: Suite) -> int:
    """Five pairs, one per link. Returns the number of links actually decided."""
    from app.domain import METRIC_TIEBREAK_ORDER
    from app.domain import Metric as AppMetric
    from app.stats.ranking import rank_candidates

    links = [
        (
            "link1_anomaly_score",
            "Higher anomaly score wins",
            _candidate(city_id="aaa", metric=AppMetric.TEMP_MAX, score=2.0),
            _candidate(city_id="bbb", metric=AppMetric.TEMP_MAX, score=3.0),
            "bbb",
        ),
        (
            "link2_tail_probability",
            "Equal scores: the rarer tail wins",
            _candidate(city_id="aaa", metric=AppMetric.TEMP_MAX, score=2.0, tail=0.02),
            _candidate(city_id="bbb", metric=AppMetric.TEMP_MAX, score=2.0, tail=0.01),
            "bbb",
        ),
        (
            "link3_robust_deviation",
            "Equal score and tail: the larger absolute robust deviation wins",
            _candidate(city_id="aaa", metric=AppMetric.TEMP_MAX, score=2.0, tail=0.01, robust=1.0),
            _candidate(city_id="bbb", metric=AppMetric.TEMP_MAX, score=2.0, tail=0.01, robust=-4.0),
            "bbb",
        ),
        (
            "link4_metric_precedence",
            "Equal on all three numbers: fixed metric precedence decides",
            _candidate(city_id="aaa", metric=AppMetric.TEMP_MEAN, score=2.0, tail=0.01, robust=1.0),
            _candidate(city_id="bbb", metric=AppMetric.TEMP_MAX, score=2.0, tail=0.01, robust=1.0),
            "bbb",
        ),
        (
            "link5_city_id",
            "Identical in every respect: city id makes the order total",
            _candidate(city_id="zzz", metric=AppMetric.TEMP_MAX, score=2.0, tail=0.01, robust=1.0),
            _candidate(city_id="aaa", metric=AppMetric.TEMP_MAX, score=2.0, tail=0.01, robust=1.0),
            "aaa",
        ),
    ]

    decided = 0
    for case_id, title, first, second, expected_winner in links:
        # Both input orders, because a tie broken by input order rather than by the
        # key would pass one of them by luck.
        winners = set()
        reference_winners = set()
        for pair in ((first, second), (second, first)):
            result = rank_candidates(list(pair), top_n=2)
            winners.add(result.ranked[0].candidate.city_id)
            board = reference.select_board([_as_row(c) for c in pair], top_n=2)
            reference_winners.add(board[0]["city_id"])

        stable = len(winners) == 1
        correct = winners == {expected_winner}
        agrees = reference_winners == winners
        if correct and stable:
            decided += 1

        suite.cases.append(
            Case(
                id=case_id,
                title=title,
                passed=stable and correct and agrees,
                category="tiebreak",
                expected=f"{expected_winner} first, in either input order",
                observed=(f"production {sorted(winners)}, reference {sorted(reference_winners)}"),
                detail=(
                    None
                    if stable
                    else "the winner depended on input order, so the key is not a total order"
                ),
            )
        )

    suite.notes.append(
        "Metric precedence under test: " + ", ".join(m.value for m in METRIC_TIEBREAK_ORDER)
    )
    return decided


def _synthetic_board(suite: Suite) -> None:
    """Invariants and permutation invariance on a larger constructed set."""
    from app.domain import ALL_METRICS
    from app.stats.ranking import rank_candidates

    rng = random.Random(SHUFFLE_SEED)
    candidates = []
    # Deliberately coarse scores: rounding to one decimal manufactures exact ties
    # across cities and metrics, which is the regime the chain exists for and the
    # one a continuous random score would almost never produce.
    for i in range(60):
        candidates.append(
            _candidate(
                city_id=f"city-{i % 18:02d}",
                metric=ALL_METRICS[i % len(ALL_METRICS)],
                score=round(rng.uniform(0.5, 4.0), 1),
                tail=round(rng.uniform(0.001, 0.3), 3),
                robust=round(rng.uniform(-5.0, 5.0), 1),
                eligible=i % 11 != 0,
            )
        )

    result = rank_candidates(candidates, top_n=10)
    ranked = result.ranked

    ranks = [r.rank for r in ranked]
    cities = [r.candidate.city_id for r in ranked]
    scores = [r.candidate.anomaly_score for r in ranked]

    suite.cases.append(
        Case(
            id="ranks_contiguous_from_one",
            title="Ranks are 1..N with no gaps",
            passed=ranks == list(range(1, len(ranked) + 1)),
            category="invariant",
            expected=f"1..{len(ranked)}",
            observed=str(ranks),
        )
    )
    suite.cases.append(
        Case(
            id="one_event_per_city",
            title="No city appears twice on the board",
            passed=len(set(cities)) == len(cities),
            category="invariant",
            expected=f"{len(cities)} distinct cities",
            observed=f"{len(set(cities))} distinct",
        )
    )
    suite.cases.append(
        Case(
            id="scores_non_increasing",
            title="Scores never rise as rank increases",
            passed=all(a >= b for a, b in itertools.pairwise(scores)),
            category="invariant",
            expected="monotone non-increasing",
            observed=str([round(s, 3) for s in scores]),
        )
    )
    suite.cases.append(
        Case(
            id="only_eligible_published",
            title="Nothing ineligible reaches the board",
            passed=all(r.candidate.eligible for r in ranked),
            category="invariant",
            expected="every published candidate eligible",
            observed=f"{sum(1 for r in ranked if not r.candidate.eligible)} ineligible published",
        )
    )

    # Permutation invariance.
    signature = [(r.rank, r.candidate.city_id, r.candidate.metric.value) for r in ranked]
    mismatched_permutations = 0
    for _ in range(25):
        shuffled = candidates[:]
        rng.shuffle(shuffled)
        other = rank_candidates(shuffled, top_n=10).ranked
        if [(r.rank, r.candidate.city_id, r.candidate.metric.value) for r in other] != signature:
            mismatched_permutations += 1

    suite.cases.append(
        Case(
            id="permutation_invariant",
            title="25 input permutations all produce the identical board",
            passed=mismatched_permutations == 0,
            category="invariant",
            expected="0 permutations differ",
            observed=f"{mismatched_permutations} differ",
            detail=(
                "Input order reaches the sort as database and dictionary iteration "
                "order, neither of which is guaranteed stable."
            ),
        )
    )

    # Independent selector over the same inputs.
    ref_board = reference.select_board([_as_row(c) for c in candidates], top_n=10)
    ref_signature = [(i, row["city_id"], row["metric"]) for i, row in enumerate(ref_board, start=1)]
    suite.cases.append(
        Case(
            id="independent_selector_agrees_synthetic",
            title="Independently written selector picks the same ten, in the same order",
            passed=ref_signature == signature,
            category="independent",
            expected=str(signature[:3]) + " …",
            observed=str(ref_signature[:3]) + " …",
        )
    )

    # Backfill: one city holding the five strongest events must not sweep, and the
    # board must still fill to ten rather than stopping at the number of cities.
    dominant = []
    from app.domain import ALL_METRICS as METRICS

    for i, metric in enumerate(METRICS):
        dominant.append(_candidate(city_id="sweeper", metric=metric, score=9.0 - i * 0.1))
    for i in range(4):
        dominant.append(_candidate(city_id=f"other-{i}", metric=METRICS[0], score=5.0 - i))

    swept = rank_candidates(dominant, top_n=10)
    sweeper_rows = [r for r in swept.ranked if r.candidate.city_id == "sweeper"]
    ref_swept = reference.select_board([_as_row(c) for c in dominant], top_n=10)
    ref_sweeper_rows = [r for r in ref_swept if r["city_id"] == "sweeper"]

    suite.cases.append(
        Case(
            id="regional_sweep_backfills",
            title="One city with the five strongest events does not sweep the board",
            passed=(
                len(sweeper_rows) == len(ref_sweeper_rows)
                and len(swept.ranked) == len(ref_swept)
                and swept.diagnostics["backfilled_slots"] > 0
            ),
            category="invariant",
            expected=(
                f"reference: {len(ref_sweeper_rows)} rows for 'sweeper' out of {len(ref_swept)}"
            ),
            observed=(
                f"production: {len(sweeper_rows)} rows for 'sweeper' out of "
                f"{len(swept.ranked)}, {swept.diagnostics['backfilled_slots']} backfilled"
            ),
            detail=(
                "With 9 candidates across 5 cities and a top-10 board, the first pass "
                "yields 5 rows and the documented backfill supplies the rest — so "
                "'sweeper' legitimately reappears, but only after every other city "
                "has had its turn."
            ),
        )
    )


def _published_board(suite: Suite, world) -> None:
    """Re-derive the scratch world's published board from its stored candidates."""
    from sqlalchemy import select

    from app.models import AnomalyEvent, DailyRanking

    with world.session() as session:
        published = list(
            session.execute(
                select(DailyRanking)
                .where(DailyRanking.analysis_date == world.analysis_date)
                .order_by(DailyRanking.rank)
            ).scalars()
        )
        stored = list(
            session.execute(
                select(AnomalyEvent).where(AnomalyEvent.local_date == world.analysis_date)
            ).scalars()
        )
        published_signature = [(row.rank, row.event.city_id, row.event.metric) for row in published]
        rows = [
            {
                "city_id": e.city_id,
                "metric": e.metric,
                "anomaly_score": e.anomaly_score,
                "tail_probability": e.tail_probability,
                "robust_deviation": e.robust_deviation,
                "eligible": e.eligible,
            }
            for e in stored
        ]
        # Reported straight from the stored flags rather than inferred. Deciding
        # whether each event fell in the sketch's lossless tail would need the raw
        # baseline sample, which is not persisted — the score evaluator measures
        # that split on its own dataset instead of guessing at it here.
        beyond_sample = sum(1 for row in published if row.event.beyond_baseline_sample)
        on_floor = sum(1 for row in published if row.event.tail_probability_is_bounded)

    from app.config import Settings

    top_n = Settings().ranking_top_n
    ref_board = reference.select_board(rows, top_n=top_n)
    ref_signature = [(i, row["city_id"], row["metric"]) for i, row in enumerate(ref_board, start=1)]

    suite.cases.append(
        Case(
            id="published_board_reproduced",
            title="The published board is exactly what the independent selector picks",
            passed=ref_signature == published_signature,
            category="independent",
            expected=f"{len(ref_signature)} rows: {ref_signature[:3]} …",
            observed=f"{len(published_signature)} rows: {published_signature[:3]} …",
            detail=(f"re-derived from all {len(rows)} stored candidates for {world.analysis_date}"),
        )
    )

    suite.metrics.append(
        Metric(
            "Candidates stored for the analysis date",
            len(rows),
            detail="every scored city-metric pair, eligible or not",
        )
    )
    suite.metrics.append(
        Metric(
            "Published events beyond the whole baseline sample",
            f"{beyond_sample}/{len(published)}",
            detail="no day in the reference window reached this value",
        )
    )
    suite.metrics.append(
        Metric(
            "Published events on the probability floor",
            f"{on_floor}/{len(published)}",
            detail="reported as 'at least this rare' rather than as a point estimate",
        )
    )


def run(world=None) -> Suite:
    suite = Suite(id=SUITE_ID, title=TITLE, description=DESCRIPTION)
    timer = Timer()

    try:
        with timer:
            from app.domain import METRIC_TIEBREAK_ORDER
            from app.stats.ranking import TIEBREAK_CHAIN

            # The reference restates the chain; a divergence would mean the two
            # selectors are implementing different documents.
            suite.cases.append(
                Case(
                    id="tiebreak_chain_matches",
                    title="Reference restatement of the tie-break chain matches the application",
                    passed=tuple(reference.TIEBREAK_CHAIN) == tuple(TIEBREAK_CHAIN),
                    category="drift",
                    expected=str(list(TIEBREAK_CHAIN)),
                    observed=str(list(reference.TIEBREAK_CHAIN)),
                )
            )
            suite.cases.append(
                Case(
                    id="metric_precedence_matches",
                    title="Reference restatement of metric precedence matches the application",
                    passed=(
                        tuple(reference.METRIC_PRECEDENCE)
                        == tuple(m.value for m in METRIC_TIEBREAK_ORDER)
                    ),
                    category="drift",
                    expected=str([m.value for m in METRIC_TIEBREAK_ORDER]),
                    observed=str(list(reference.METRIC_PRECEDENCE)),
                )
            )

            links_decided = _tiebreak_cases(suite)
            _synthetic_board(suite)
            if world is not None:
                _published_board(suite, world)

            suite.metrics.insert(
                0,
                Metric(
                    "Tie-break links decided in isolation",
                    f"{links_decided}/{len(reference.TIEBREAK_CHAIN)}",
                    detail="each pair equal on every earlier link",
                ),
            )
            suite.metrics.append(Metric("Permutations tested", 25, detail=f"seed {SHUFFLE_SEED}"))
            suite.metrics.append(
                Metric(
                    "Checks passed",
                    ratio(sum(1 for c in suite.cases if c.passed), len(suite.cases)),
                    unit="fraction",
                )
            )
            if world is None:
                suite.notes.append(
                    "Ran without a scratch world, so the published-board comparison "
                    "was not performed."
                )

        failed = [c for c in suite.cases if not c.passed]
        suite.status = STATUS_FAILED if failed else STATUS_PASSED
        suite.duration_ms = timer.elapsed_ms

    except Exception:
        suite.status = STATUS_ERROR
        suite.error = traceback.format_exc(limit=6)
        suite.duration_ms = timer.elapsed_ms

    return suite
