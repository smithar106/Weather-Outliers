# `wo` — read-only observability CLI

A small CLI for analysing the MLflow traces and the application database that
Weather Outliers produces. It answers, at the command line, the questions you
would otherwise answer by hand in the MLflow UI or in `psql`: what ran, what
failed, what it cost, and which prompt/model version produced a result.

**It never writes.** No span is started, no experiment is created, no run is
logged, and no application row is changed. Every store the CLI talks to is
read-only.

## Install and run

The CLI runs from the repository root with the backend virtualenv — the same
arrangement as `evals`:

```bash
# One-time: a venv with the backend and the (optional) MLflow client.
cd backend && uv venv .venv && uv pip install -e ".[dev,tracing]" && cd ..

backend/.venv/bin/python -m wo summary
backend/.venv/bin/python -m wo traces
backend/.venv/bin/python -m wo trace <trace_id>
```

`mlflow` is optional and only needed by `traces`/`trace`. `summary` also reads
the application database and works with or without MLflow installed.

## Configuration

The CLI reads the same environment variables the pipeline and evaluation harness
already use, so pointing it at a deployment is just exporting what is already
exported:

| Variable | Used by | Notes |
| --- | --- | --- |
| `DATABASE_URL` | `summary` | The application's own database. The scheme is normalised by the app's settings, so Railway's `postgres://…` works verbatim. |
| `MLFLOW_TRACKING_URI` | `traces`, `trace` | The tracking server (e.g. `http://mlflow.railway.internal:5000`, or a local `sqlite:///…` file). |
| `MLFLOW_EXPERIMENT` | all | Defaults to `weather-outliers`. |
| `MLFLOW_TRACKING_USERNAME` / `_PASSWORD` | `traces`, `trace` | Basic auth, passed through to MLflow's own client. |

Each flag has a `--tracking-uri`, `--experiment`, or `--database-url` override.

## Commands

### `summary`

One screen of status: whether the tracking store is reachable and how many
traces it holds, baseline coverage, the recent pipeline runs, and the
explanation generator mix — with the pricing state made explicit.

```
$ backend/.venv/bin/python -m wo summary
weather-outliers — summary  (experiment 'weather-outliers')

  mlflow:      tracking store reachable
  traces:      1
  baselines:   50 cities, 91,250 rows (91,250 sufficient)
  pricing:     not configured
  explanations: 0 model-written, 10 deterministic (across recent runs)

  recent runs:
    2026-09-22 09:30 daily     succeeded published   2026-09-21  10 events  50/50 cities
        llm: 0 calls, $0.0000 (not priced)
```

### `traces`

List the traces in the experiment, newest first. An empty list is a valid,
informative answer — "no traces recorded" — and is printed as such, distinct
from a tracking store that cannot be reached.

```
$ backend/.venv/bin/python -m wo traces
TRACE ID      TIME (UTC)        STATUS   DURATION  SPANS  ROOT SPAN          MODEL    RUN
----------------------------------------------------------------------------------------
tr-4c7e5d9a…  2026-09-22 09:30  OK       9.4s      16     pipeline.run_daily —        daily-…
```

`--limit N` bounds the listing (default 20). The model and version columns are
read from the trace's spans, so a trace recorded before those attributes existed
simply shows `—` rather than a fabricated value.

### `trace <trace_id>`

Render one trace's span tree: each span's status, latency, type, and the
interesting attributes (model, prompt version, generator, fallback reason).

```
$ backend/.venv/bin/python -m wo trace tr-4c7e5d9ace174552ddd541f759c3d0d0
trace tr-4c7e5d9ace174552ddd541f759c3d0d0
  status OK  ·  2026-09-22 09:30  ·  9.4s  ·  16 spans

  pipeline.run_daily       OK   9.4s    CHAIN prompt_version=1.0.0
    fetch_observations     OK   8.9s    RETRIEVER
    explain_events         OK   89ms    CHAIN
      explain_event        OK   3ms     AGENT generator=template fallback_reason=no LLM provider configured
```

## Honesty rules

The CLI follows the same rule as the evaluation harness: a figure that was not
measured does not appear as a plausible number.

- **Cost.** `$0.0000 (not priced)` means token prices are not configured — not
  that inference was free. `—` means there was no data at all.
- **No traces vs no failures.** "no traces recorded" is not the same as "zero
  failures"; the two are reported separately and in different words.
- **Empty is not an error.** An empty database or a trace-less experiment is a
  valid answer and exits `0`. A store that *cannot be reached* is reported as
  "unavailable — …" and, for the query commands, exits `1`.
- **Partial instrumentation degrades.** A trace with no LLM spans, no model
  attribute, or no recorded latency renders as missing columns, never as a crash.

Exit codes: `0` the command completed and printed its report; `1` the tracking
store could not be reached, or the requested trace does not exist.

## Architecture

Commands depend on two store *interfaces*, never on SQLAlchemy or MLflow
directly (`wo/stores/base.py`). That is what keeps MLflow's schema changes and
the application's ORM changes out of the command implementations.

```
wo/
  cli.py            argparse + dispatch
  config.py         env + flag resolution
  models.py         plain dataclasses (SpanNode, TraceRecord, RunRecord, …)
  normalize.py      MLflow entity → models, defensively (never raises)
  aggregate.py      pure shaping: trace → table row, spans → tree
  render.py         pure text rendering
  stores/
    base.py         StoreUnavailable + the AppDbStore / TraceStore protocols
    app_db.py       application Postgres (reuses the app's own ORM + queries)
    mlflow_api.py   MLflow client API (search_traces / get_trace)
  commands/
    summary.py, traces.py, trace.py
  tests/
```

- **`app_db`** reuses the application's own `baseline_coverage` and ORM models,
  so `wo summary` reports the same numbers as the health endpoint rather than a
  near-miss reimplementation.
- **`mlflow_api`** uses only the supported client surface — `search_traces`,
  `get_trace`, `get_experiment_by_name` — and calls `search_traces` with
  `return_type="list"` because `mlflow-skinny` ships no pandas. It never calls
  `set_experiment` (which would create the experiment).
- A third store, **direct MLflow Postgres access**, is reserved for span-level
  aggregations the client API cannot express. It is not needed by `summary`,
  `traces` or `trace`, so it is not implemented yet.

## Tests

```bash
backend/.venv/bin/python -m pytest wo/tests -q
backend/.venv/bin/ruff check wo
```

The tests cover normalisation against malformed/missing span attributes,
trace-to-row summarisation, span-tree assembly (including orphans), the app
database store against an in-memory SQLite database (empty and populated), and
graceful degradation at the command boundary when a store is unavailable.
