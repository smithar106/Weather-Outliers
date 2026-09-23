"""``wo trace <trace_id>`` — render one trace's span tree."""

from __future__ import annotations

import argparse

from wo.render import render_trace
from wo.stores.base import StoreUnavailable, TraceStore


def run(args: argparse.Namespace, config, app_store, trace_store: TraceStore) -> int:
    try:
        record = trace_store.get_trace(args.trace_id)
    except StoreUnavailable as exc:
        print(f"mlflow unavailable: {exc}")
        return 1

    if record is None:
        print(f"trace {args.trace_id!r} not found in the tracking store")
        return 1

    print(render_trace(record))
    return 0
