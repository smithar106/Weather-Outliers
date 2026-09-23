"""``wo traces`` — list the traces recorded for an experiment.

An empty list is a valid, informative answer — "no traces recorded" — and is
distinct from "the tracking store is unreachable", which is reported separately
and exits non-zero.
"""

from __future__ import annotations

import argparse

from wo.aggregate import summarize_trace
from wo.render import render_trace_table
from wo.stores.base import StoreUnavailable, TraceStore


def run(args: argparse.Namespace, config, app_store, trace_store: TraceStore) -> int:
    try:
        records = trace_store.list_traces(config.mlflow_experiment, limit=args.limit)
    except StoreUnavailable as exc:
        print(f"mlflow unavailable: {exc}")
        return 1

    if not records:
        print(
            f"no traces recorded in experiment {config.mlflow_experiment!r} "
            "(this is distinct from zero failures — see `wo failures` once it exists)"
        )
        return 0

    summaries = [summarize_trace(record) for record in records]
    print(render_trace_table(summaries))
    return 0
