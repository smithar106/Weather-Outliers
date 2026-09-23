# Evaluation and Tracing

How this project is measured, what each measurement means, and how to reproduce
every number in it.

Two things live here. **Tracing** records what the pipeline did on a given run:
which spans ran, how long each took, whether it failed, which model and prompt
version produced an explanation. **Evaluation** is a versioned suite that decides
whether the system is *correct*, by recomputing its arithmetic independently and
comparing. They share a store — MLflow — and nothing else.

One rule governs both, and the rest of this document is downstream of it: a figure
that was not measured by the run that prints it does not appear. Suites that cannot
measure something report `null` and say so. No accuracy, latency or cost figure in
this repository is an estimate dressed as a measurement.

---

## Quick start

```bash
# Install the tracking client (optional; absence is not an error)
VIRTUAL_ENV=$PWD/backend/.venv uv pip install "./backend[tracing]"

# Run every deterministic evaluator. No keys, no network, no database needed.
backend/.venv/bin/python -m evals.runner

# Same suites, logged to MLflow as one experiment run
backend/.venv/bin/python -m evals.experiment
```

Both must be run from the repository root — `evals` is a top-level package, and
`python -m evals.runner` from inside `backend/` fails with
`No module named 'evals'`.

Neither command needs an API key, a weather-provider credential, or a running
database. `evals/environment.py` pins `DATABASE_URL` to an in-memory SQLite
database, pins the weather provider to a deterministic fixture, and **removes**
`ANTHROPIC_API_KEY` and `OPENAI_API_KEY` from the environment before anything from
`app` is imported. An evaluation cannot spend money by accident; it has to be asked
to, with `--live-llm` or `--judge`.

### Measured result

From the run at `2026-09-23T01:25:01Z`, recorded in `evals/reports/latest.json`:

```
8/8 suites, 138/138 checks, 23661 ms total          PASSED
```

| Suite | Result | Duration |
| --- | --- | --- |
| `unit_tests` | 6/6 checks · 270 pytest tests passed, 0 failed, 0 skipped | 15820 ms |
| `anomaly_score` | 24/24 checks | 208 ms |
| `missing_data` | 18/18 checks | 16 ms |
| `ranking` | 15/15 checks | 5 ms |
| `grounding` | 25/25 checks | 46 ms |
| `explanation_agreement` | 18/18 checks | 4 ms |
| `reproducibility` | 5/5 checks | 7353 ms |
| `api_contract` | 27/27 checks | 209 ms |

Durations are wall-clock on one laptop and will differ on yours; they are here to
show where the time goes, not as a benchmark. The report's own `git_commit` field
reads `dirty`, which is correct and unavoidable: a report committed in the same
commit as the code it describes cannot record that commit's own hash.

`explanation_quality` is optional and was not run: it needs a paid model. Its
absence means explanation quality was **not measured**, not that it passed.

---

## Architecture

### What is instrumented

`app/observability/tracing.py` is a thin wrapper over MLflow's tracing SDK. Eleven
spans cover the five stages the instrumentation was asked to cover:

| Span | Kind | Stage |
| --- | --- | --- |
| `pipeline.run_daily` | CHAIN | the whole daily run |
| `fetch_observations` | RETRIEVER | weather data retrieval |
| `pipeline.build_baselines` | CHAIN | historical baseline construction |
| `build_city_baseline` | RETRIEVER | one city's 30-year baseline |
| `require_completeness` | PARSER | the data-completeness gate |
| `score_anomalies` | CHAIN | anomaly scoring |
| `rank_events` | CHAIN | ranking |
| `explain_events` | CHAIN | explanation generation, all events |
| `explain_event` | AGENT | one event's explanation |
| `llm_completion` | LLM | one model call |
| `validate_explanation` | PARSER | output validation |

Each records latency, error status, and non-sensitive attributes: counts, city and
metric identifiers, the methodology version, the prompt version and its SHA-256,
the model name, token counts, and estimated cost when token prices are configured.

### What is deliberately not recorded

- **Credentials.** `_SENSITIVE_KEY` in `tracing.py` drops attributes whose keys
  look like secrets, whatever their value. No API key, database URL or token is an
  attribute anywhere.
- **Personal data.** The pipeline holds none: its inputs are city coordinates and
  public weather figures.
- **Anything, when telemetry breaks.** Every MLflow call goes through `_safe`.
  After `_MAX_FAILURES = 3` consecutive faults, tracing switches itself off for the
  rest of the process and the pipeline carries on. `configure()` never raises and
  returns a boolean nobody branches on. `import_mlflow()` returns `None` when MLflow
  is not installed — absence is not an error, and the full test suite runs without
  it. A traced run and an untraced run produce byte-identical rankings; the
  `reproducibility` suite is what holds that true.

### The harness

`evals/` sits beside `backend/`, not inside it, because it evaluates the whole
system — statistics, pipeline, guards, and HTTP surface. It is never copied into
either container image.

```
evals/
  environment.py   Pins the process offline. Must run before `app` is imported.
  world.py         Builds a scratch world: in-memory DB, fixture provider, one
                   real pipeline run, one published board.
  dataset.py       The versioned scoring dataset and its generators.
  reference.py     Independent reimplementation of the scoring and selection
                   arithmetic. Written from the methodology, not from `app/`.
  harness.py       Report structures: Suite, Case, Metric, Report.
  runner.py        `execute()` runs the suites; `main()` is the CLI.
  experiment.py    Same suites, logged to MLflow.
  suites/          One module per evaluator.
  cases/           Hand-written case files, versioned.
  reports/latest.json   Committed. Rendered at /evaluation.
```

`reference.py` is the load-bearing piece. An evaluator that calls the production
function to compute its own expectation proves only that the function is
deterministic. The reference module recomputes tail probabilities, anomaly scores,
metric precedence and board selection from the written methodology, and the
evaluators compare the two.

---

## The dataset

`evals/cases/scoring.json` — `dataset_version: "scoring-1.0.0"`,
`schema_version: 1`, **23 cases**. Bump `dataset_version` on any change to a case;
the version is logged with every experiment run, so a score is never comparable
across dataset versions by accident.

Three kinds of case, by design:

- **Representative** — the shapes real weather actually takes. Roughly normal
  temperature distributions; right-skewed and zero-inflated precipitation, where a
  z-score is meaningless and the tail probability is not.
- **Edge** — the observation exactly on a grid knot, exactly at the sample maximum,
  beyond the whole sample, on the probability floor, a baseline of the minimum
  admissible size, zero variance, all-zero precipitation.
- **Synthetic failure** — 11 absence cases: no baseline, baseline too small,
  missing observation, unusable value, a city below the completeness floor.

**Determinism.** The generators use only `random.Random` with fixed seeds and
arithmetic from the standard library — no numpy, no scipy, nothing whose sampler
can change between releases. `normal_sample` builds a normal deviate from uniforms
by the Box-Muller transform; `_gamma` builds an Erlang from integer-shape sums of
exponentials. The same case produces the same sample on any machine and any Python
3.11+. Regenerating the dataset is therefore not a source of drift.

---

## The evaluators

### 1. `anomaly_score` — scores against independent arithmetic

Recomputes every case's tail probability, surprisal, margin bonus and anomaly score
from `reference.py` and compares against the pipeline. Then sweeps **999 points**
across a 450-sample distribution (`SWEEP_SEED = 999`) so the comparison covers the
whole range rather than a few hand-picked values.

**Two regimes, and this is the important part.** Production reduces a baseline to a
35-point tail-dense quantile sketch plus the 5 outermost order statistics at each
end.

- At or beyond the 5th order statistic from either end the sketch is **lossless**,
  so exact agreement is required — `EXACT_TOLERANCE = 1e-9`.
- In the interior the sketch interpolates between grid knots, so the two routes are
  required to agree within **the width of the grid interval bracketing the
  observation**. That bound is a property of monotone piecewise-linear interpolation
  pinned at its knots, derived from the construction — not a tolerance widened until
  the suite passed.

Measured: 12 lossless-tail comparisons, worst score difference **0.0**; 7 interior
comparisons, worst probability difference **0.00213**, worst score difference
**0.0439**; **0** bound violations across 999 sweep points, worst case using
**32%** of the available grid width.

An earlier version of this evaluator bounded the interior by `2/(n+1)`, a
plotting-position argument, and failed 240 of 999 sweep points. The bound above is
what the construction actually implies; the failure was the argument, not the code.

### 2. `ranking` — top-10 correctness

Five things, each attacked separately:

- **Tie-break chain.** Five hand-built pairs isolate one link each — anomaly score,
  tail probability, robust deviation, metric precedence, city id — and each is run
  in **both input orders**. Measured: 5/5 links decided, and `reference.select_board`
  agrees on every one.
- **Order independence.** A 60-candidate synthetic board with deliberately coarse
  rounding, so exact ties occur, shuffled **25 times** (`SHUFFLE_SEED = 20260922`).
  All 25 boards identical.
- **Structure.** Ranks contiguous from 1, scores non-increasing, one event per city,
  only eligible events published, backfill correct when a regional sweep would
  otherwise fill the board with one storm.
- **Independent agreement.** The published board is re-derived from all **50**
  stored candidates by `reference.select_board` and compared row by row. It matched
  exactly.
- **Drift guards** on the tie-break chain and metric precedence, so the evaluator
  cannot silently stop testing the real order.

### 3. `explanation_agreement` — prose against its source metrics

Strictly stronger than `grounding`, which checks that each number in an explanation
appears somewhere in the tool results. That whitelist cannot catch a number
attributed to the wrong field, or a sign flip. This evaluator extracts every numeric
claim with its *kind* — deviation, median, p25, p75, sample size, percentile, tail
probability, return period, sample max, sample min — and compares each to the
specific stored field it claims to be.

The comparison rule is exact at the precision printed: the stored value is rounded
to however many decimals the prose used, and must then equal it. `56.8` must match a
stored `56.8xx`; a stored `56.75` printed as `56.8` passes, printed as `56.9` fails.

It also checks direction agreement (above/below the baseline), that a bounded
probability is hedged as a bound rather than stated as a point value, that no
unverified record claim appears, that modelled analysis data is never called a
station observation, and that a z-score is withheld when `z_valid` is false.

Measured: 10 explanations, **96 numeric claims extracted, 96/96 agreeing**, all 10
claim kinds exercised, 9.6 claims per explanation.

**Negative controls.** Every one of these was verified by corrupting the stored data
and confirming the suite fails: a changed median, a flipped direction, boundedness
flipped in both directions, an injected record claim, an injected instrument claim,
a z-score published when invalid, and a deleted anchor word. Two real defects came
out of that exercise — `\brecord\b` was matching inside "recorded", and deleting a
keyword silently dropped a claim without failing anything, which is why
`REQUIRED_CLAIM_KINDS` and the `claim_extraction_not_vacuous` case exist.

### 4. `missing_data` — absence handled, not scored

Drives 11 absence cases through the real code paths and requires that each is
excluded with a **specific stated reason** rather than scored against nothing. Also
exercises the completeness gate directly: a run below
`PIPELINE_MIN_CITY_COMPLETENESS` must fail, and a failed run must not overwrite the
last good board.

Measured: 11 absence cases, **5 of 6** exclusion reasons reached (the report names
the one this dataset does not reach: `tail_probability_unavailable`), **0**
ineligible cases scored anyway.

### 5. `explanation_quality` — optional model-based judge

`--judge`. Off by default, costs money, and it is the weakest evidence in the
harness. `RUBRIC_VERSION = "quality-1.0.0"`; the rubric's SHA-256 is logged so a
score is traceable to the exact wording that produced it. Four ordinal dimensions
(clarity, calibration, hedging, usefulness, 1–5) and three policy booleans, returned
through a forced `submit_verdict` tool call at `temperature = 0.0`.

**Quality scores are reported as metrics and never asserted.** The only pass/fail
conditions are that the verdict is well-formed, and that policy flags — a claimed
record, a claimed cause, model data presented as observation — are surfaced as
*model-flagged, needs human confirmation*. A judge does not get a vote on whether
this repository's tests pass.

Its own limitations, from its module docstring: one judge, so no inter-rater
agreement; shared-family bias when judge and author are related models; temperature
0 is not determinism; n=10 with no confidence interval; it cannot check arithmetic,
which is evaluator 3's job; ordinal scores are not interval data and should not be
averaged as though they were.

### Also in the harness

`unit_tests` (270 pytest tests), `grounding` (15 hand-labelled cases plus every
published explanation), `reproducibility` (byte-identical reruns, and across two
independent databases), `api_contract` (9 endpoints, 150 fields cross-checked
against the database).

---

## Experiments: logging to MLflow

```bash
backend/.venv/bin/python -m evals.experiment
```

Runs the same suites through `runner.execute()` — not a reimplementation, and not a
subprocess whose output is parsed back — then logs one MLflow run.

| Logged | Contents |
| --- | --- |
| Params (20) | git commit and dirty flag, methodology version, prompt version + SHA-256, dataset version + schema version + case count, weather provider, LLM mode/provider/**model name**, whether token prices are configured, python, platform, which suites were requested, judge provider/model/rubric version/rubric SHA/temperature when `--judge` |
| Metrics (120) | totals and pass rate; per suite `cases_total`, `cases_passed`, `pass_rate`, `duration_ms`; every numeric metric each suite measured; `a/b`-shaped counts split into `_count`, `_total`, `_ratio`; `cost_usd_estimated_total` **only when token prices are configured** |
| Tags | evaluation status, harness, generated-at |
| Artifacts | `evals/reports/latest.json`, and `judge-verdicts.json` when `--judge` ran |

Params are an explicit hand-written allowlist. Nothing iterates over the
environment or over settings. As a backstop, `_redact` replaces any substring that
matches the value of a credential-shaped environment variable, so an upstream
mistake still cannot publish a key.

Two deliberate omissions:

- **No API base URL.** A base URL can carry a token in its path. The model *name*
  is what a reader needs to reproduce a run.
- **No cost metric when prices are not configured.** `estimate_usd` returns `0.0`
  when no per-Mtok prices are set, and a logged `0.0` reads as "this run was free"
  rather than "nobody told the system what tokens cost". So the total appears only
  when there are prices to derive it from.

`--trace` additionally turns on pipeline tracing during the run, so the spans and
the evaluation run that produced them sit in the same experiment. Off by default: a
traced run is not a like-for-like latency comparison with an untraced one.

The counts above are measured, from a full run logged to the local SQLite store:
20 params, 120 metrics, one attached report artifact, `cost_usd_estimated_total`
absent because no token prices are configured, and `llm_pricing_configured=False`
recorded so that absence is legible rather than mysterious.

---

## Where traces and runs are stored

### Local

```bash
export MLFLOW_TRACKING_URI="sqlite:///$PWD/mlflow.db"
export MLFLOW_TRACING_ENABLED=true
export MLFLOW_EXPERIMENT=weather-outliers
```

Two gotchas, both verified on MLflow 3.16.1:

1. **MLflow 3 rejects bare `file:` tracking URIs.** Use `sqlite:///` with an
   absolute path — note four slashes for an absolute path:
   `sqlite:////Users/you/weather-outliers/mlflow.db`.
2. **`mlflow-skinny` ships no UI.** It is a client: no Flask, no gunicorn.
   `backend/.venv/bin/python -c "import flask"` fails with
   `ModuleNotFoundError`, so `mlflow ui` cannot be run from the backend
   virtualenv at all. To look at traces locally, install the full distribution
   somewhere else and point it at the same file:

```bash
uv venv /tmp/mlflow-ui --python 3.13
VIRTUAL_ENV=/tmp/mlflow-ui uv pip install "mlflow==3.16.1"
/tmp/mlflow-ui/bin/mlflow ui --backend-store-uri "sqlite:///$PWD/mlflow.db"
```

`mlflow.db` and `mlruns/` are gitignored. They can hold prompt and response text
and are not part of the application.

### Railway

```
                 ┌──────────────────────────────┐
  no public      │  mlflow  (ops/mlflow)        │
  domain  ──✕──▶ │  tracking server + UI        │
                 │  --serve-artifacts           │
                 └───────┬──────────────┬───────┘
                         │              │
              backend store         artifacts
                         │              │
                 ┌───────▼──────┐  ┌────▼──────────────┐
                 │  PostgreSQL  │  │  Volume /data     │
                 │  db: mlflow  │  │  /data/mlartifacts│
                 └──────────────┘  └───────────────────┘
                         ▲
        private network  │  http://mlflow.railway.internal:5000
                         │
       ┌─────────────────┴─────────────────┐
       │ worker-daily · worker-finalize    │  ← tracing enabled here only
       │ worker-baselines                  │
       └───────────────────────────────────┘

       Weather-Outliers (website) and the public API: tracing OFF.
       No visitor request writes telemetry, and no visitor can reach MLflow.
```

Tracking metadata, including traces, goes in PostgreSQL. Artifacts — reports,
datasets, judge verdicts — go on a mounted volume and are served *through* the
tracking server (`--serve-artifacts`), so clients need no object-store credentials
of their own. GitHub holds the evaluation code and configuration; it is not a trace
store.

**Security posture, and why it is shaped this way.** MLflow's open-source server
has no authentication of its own. A generated Railway domain would therefore publish
every trace to the internet. So:

- The service has **no public domain**. It is reachable at
  `mlflow.railway.internal:5000` over Railway's private network and nowhere else.
- `ops/mlflow/entrypoint.sh` **refuses to start** unless either
  `MLFLOW_AUTH_USERNAME` + `MLFLOW_AUTH_PASSWORD` are set (HTTP basic auth) or
  `MLFLOW_ALLOW_ANONYMOUS=true` states the choice out loud. There is no silent
  default.
- MLflow gets its **own database**, not the application's. It runs its own
  migrations, and two migration histories in one schema is a bad afternoon.
- The application image installs `mlflow-skinny` but tracing stays off until
  `MLFLOW_TRACING_ENABLED=true`, and it is set on the cron workers only.

Two findings from building this, both of which would have been deploy failures:

- **MLflow 3 refuses to start its auth app without `MLFLOW_FLASK_SERVER_SECRET_KEY`.**
  The entrypoint derives a stable one from the auth password, or takes an explicit
  value.
- **MLflow 3's DNS-rebinding guard allows only localhost and private IPs by
  default, and it checks the Host header by name.** Measured against a default
  server: `Host: mlflow.railway.internal:5000` returns **200 on `/health`** and
  **403 on `/api/2.0/mlflow/experiments/search`**. The service would have passed
  its health check and rejected every client. The entrypoint therefore sets
  `--allowed-hosts`, defaulting to `*.railway.internal` plus localhost.

Verified locally end to end against the real server, with basic auth enabled:
anonymous API request → **401**, wrong password → **401**, correct credentials →
**200**; `Host: mlflow.railway.internal:5000` → **200**, `Host: evil.example.com` →
**403**; and `python -m evals.experiment --only ranking`, pointed at that server
over HTTP, logged its run and uploaded a 9,844-byte report artifact through the
proxy artifact store. One suite rather than eight there, because what was under
test was the transport and the credentials, not the evaluators.

#### Steps that need your authorization

These cannot be done from a config file — Railway has no schema for them, and they
spend money and create infrastructure:

1. **Create a database for MLflow** on the existing PostgreSQL service:
   `CREATE DATABASE mlflow;` then build its URL from the service's credentials, or
   add a second PostgreSQL service if you would rather keep them fully apart.
2. **Create the service.** New service from this repository, root directory
   `ops/mlflow`. It will read `ops/mlflow/railway.json`.
3. **Attach a volume** mounted at `/data`. Without it, artifacts are lost on every
   deploy while the run metadata survives — a failure that looks fine until someone
   clicks an artifact.
4. **Do not generate a domain.** If you ever want the UI in a browser, set basic
   auth credentials first, then add the domain and add its hostname to
   `MLFLOW_ALLOWED_HOSTS`.
5. **Set variables** on the MLflow service:
   `MLFLOW_BACKEND_STORE_URI`, `MLFLOW_ARTIFACTS_DESTINATION=/data/mlartifacts`,
   and either `MLFLOW_AUTH_USERNAME` + `MLFLOW_AUTH_PASSWORD` or
   `MLFLOW_ALLOW_ANONYMOUS=true`.
6. **Set variables on the cron workers only** — `worker-daily`, `worker-finalize`,
   `worker-baselines`:
   `MLFLOW_TRACING_ENABLED=true`,
   `MLFLOW_TRACKING_URI=http://mlflow.railway.internal:5000`,
   `MLFLOW_EXPERIMENT=weather-outliers`, plus
   `MLFLOW_TRACKING_USERNAME`/`MLFLOW_TRACKING_PASSWORD` if auth is on. Leave the
   website and public API untouched.

Local evaluation runs cannot reach a private Railway service — private networking
is between services only. They write to the local SQLite store by default, which is
the intended arrangement: local runs measure a machine, and `mlflow.db` is not a
shared record of anything.

---

## Known limitations

1. Every figure in the report was measured by the run that produced it. A suite that
   cannot measure something reports `null`.
2. The pipeline, API and published-explanation suites run against
   `synthetic_fixture_v1`, a deterministic synthetic provider. They measure whether
   the machinery is correct and reproducible — **not** the accuracy of real weather
   data.
3. Latency figures come from an in-process client over an in-memory SQLite
   database. They are a floor for the deployed system, not a measurement of it.
4. The `anomaly_score` bound is two-regime, as described above. The measured worst
   case is reported in both regimes either way.
5. Grounding accuracy is measured on a small hand-labelled case set. It quantifies
   the guards, not any language model's general reliability.
6. LLM spend is an estimate derived from configured token prices. Where no prices
   are configured it reads `0.0`, meaning "not priced" rather than "free".
7. The city registry is a curated sample of major North American cities. It is not a
   representative sample of the continent's climate, and the rankings are
   statistical outliers within that sample — **not records of any kind**.
8. `explanation_agreement`'s extractors are written against the deterministic
   template's phrasing. A live model phrasing things differently would yield fewer
   extractable claims; the coverage floor reports that rather than hiding it.
9. The optional judge's limitations are listed in its own section. They are not
   small.
10. The `ops/mlflow` service has never run on Railway. The *image* is built and
    exercised on every push — CI has a Docker daemon and the machine these notes
    were written on does not — and inside the real container it asserts: the
    entrypoint exits non-zero with no backend store, exits non-zero with a store but
    no authentication, serves `/health` in 13 s, runs as uid 10001 rather than root,
    and answers **401** anonymous, **401** on a wrong password, **200** with correct
    credentials, **200** for a `*.railway.internal` Host and **403** for an unknown
    one. What remains unverified is the platform: Railway's private DNS, the mounted
    volume, and PostgreSQL rather than the SQLite store CI uses. A full client
    round-trip including artifact upload was verified outside a container, against
    the same pinned MLflow version.

---

## Reproducing everything

```bash
# Lint and type checks
backend/.venv/bin/python -m ruff check backend evals
cd frontend && npx tsc --noEmit && npx eslint . && cd ..

# Backend tests (270)
cd backend && .venv/bin/python -m pytest -q && cd ..

# Every deterministic evaluator; writes evals/reports/latest.json
backend/.venv/bin/python -m evals.runner

# One suite at a time while iterating
backend/.venv/bin/python -m evals.runner --only ranking --skip-unit-tests

# Log a run to MLflow
backend/.venv/bin/python -m evals.experiment

# With pipeline tracing in the same experiment
backend/.venv/bin/python -m evals.experiment --trace

# Optional model-based judge. Costs money. Needs a judge key exported.
backend/.venv/bin/python -m evals.runner --judge
```

Exit codes: `0` all suites passed, `1` a suite failed, `2` the scratch world could
not publish a board (a setup failure, not an evaluation result). `evals.experiment`
returns the evaluation's own verdict — a telemetry problem never changes it.
