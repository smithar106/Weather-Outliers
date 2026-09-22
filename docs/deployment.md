# Deployment (Railway)

Status, stated plainly: **this document describes a deployment that has not been
performed.** The application has been built and verified locally; the Railway
project, its services, its database and its cron schedules do not exist yet
because creating them requires account access this repository does not have. Every
step below that needs a human is marked **[AUTHORIZATION REQUIRED]**.

Nothing here claims a live URL, a passing production health check, or a working
schedule. Those claims belong in this file only after someone has run the
checklist at the bottom and seen it pass.

---

## Topology

Four services in one Railway project:

```
                         ┌────────────────────────────┐
   public internet ──────▶ Frontend  (Next.js)        │
                         │  next start, port $PORT    │
                         └─────────────┬──────────────┘
                                       │ private network
                                       │ http://backend.railway.internal:8000
                         ┌─────────────▼──────────────┐
                         │ Backend   (FastAPI)        │
   public internet ──────▶  read-only JSON API        │
                         └─────────────┬──────────────┘
                                       │ private network
                         ┌─────────────▼──────────────┐
                         │ PostgreSQL (Railway plugin)│
                         │  no public proxy           │
                         └─────────────▲──────────────┘
                                       │ private network
                         ┌─────────────┴──────────────┐
                         │ Worker (cron only)         │
                         │  python -m app.pipeline    │
                         └────────────────────────────┘
```

Why this shape:

* **The worker is a separate service from the API** even though it is the *same
  image*. A daily run fetches 50 cities, computes statistics and may call an LLM;
  running that inside the web process means a long CPU-bound job competing with
  request handling, and a deploy that restarts mid-run. Separating them also means
  the worker can hold the provider and LLM credentials while the API holds none.
* **The frontend is the only service that strictly needs to be public.** The API is
  public as well, because a documented read-only API is part of the point of the
  project — but it is public *by choice*, and it can be made private by removing
  its domain without changing any code.
* **The database has no public endpoint.** Only the three services reach it, over
  Railway's private network.

---

## Prerequisites

* **[AUTHORIZATION REQUIRED]** A Railway account and a new empty project.
* **[AUTHORIZATION REQUIRED]** GitHub authorization for Railway to read
  `smithar106/Weather-Outliers`.
* Optional: an Anthropic or OpenAI API key. The application runs fully without
  one and falls back to deterministic explanations.
* Optional: an Open-Meteo commercial subscription key. Without it the free tier
  applies, which is non-commercial-use only and makes the first baseline build a
  multi-day job (see [data-sources.md](./data-sources.md)).

---

## 1. Database

**[AUTHORIZATION REQUIRED]** Add the PostgreSQL plugin to the project.

Then:

* Do **not** enable the public TCP proxy. If it is on by default, turn it off.
  With it off, the database is reachable only from services in the project.
* Note the private connection string Railway exposes as
  `${{Postgres.DATABASE_URL}}`. Use the *variable reference*, never a pasted
  literal — a rotated password should not require editing three services.

**One change is required to the URL.** Railway hands out
`postgresql://...`, and this application uses psycopg 3, which SQLAlchemy only
selects when the scheme says so. Set each service's `DATABASE_URL` to:

```
postgresql+psycopg://${{Postgres.PGUSER}}:${{Postgres.PGPASSWORD}}@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}:${{Postgres.PGPORT}}/${{Postgres.PGDATABASE}}
```

Using `RAILWAY_PRIVATE_DOMAIN` rather than the public host keeps the traffic on
the private network and off the metered egress path.

---

## 2. Backend service

**[AUTHORIZATION REQUIRED]** Create a service from the GitHub repo.

| Setting | Value |
| --- | --- |
| Root directory | `/` (the Dockerfile copies `data/`, which sits outside `backend/`, so the build context must be the repo root) |
| **Builder** | set by `RAILWAY_DOCKERFILE_PATH` below — **read the box first** |
| Start command | *(leave empty — the image's `CMD` reads `$PORT`)* |
| Health check path | `/health` |
| Public domain | generate one, or attach a custom domain |

> ### Read this before the first deploy, or it fails
>
> Railway's documented rule is that it *"will always build with a Dockerfile if it
> finds one"* — meaning one at the root of the build context. This is a monorepo:
> the root holds `backend/` (Python) and `frontend/` (Node) and no Dockerfile, so
> Railway finds nothing to detect, falls back to Railpack, and stops with
>
> ```
> Railpack could not determine how to build the app.
> ```
>
> That is a configuration message, not a code fault. The fix is **one variable per
> service** — add it in the service's **Variables** tab with the rest:
>
> ```
> RAILWAY_DOCKERFILE_PATH=backend/Dockerfile
> ```
>
> Setting it switches the service to the Dockerfile builder and names the file, in
> one step, with no build-settings hunting. Use `frontend/Dockerfile` for the
> website service and `backend/Dockerfile` for both workers.
>
> **A variable change alone may not rebuild.** After adding it, trigger a fresh
> deploy (**Deployments → ⋯ → Redeploy**) rather than waiting: a cached failed build
> will otherwise be what you keep looking at.
>
> The `railway.json` files in `backend/` and `frontend/` record the same intent in
> version control — builder, Dockerfile path, health check, restart policy — but
> Railway reads config-as-code from the **repository root**, and nothing in its
> reference documents a way to point a service at a config file in a subdirectory.
> So treat them as the documented intent, and `RAILWAY_DOCKERFILE_PATH` as the
> mechanism that actually takes effect.

Variables:

```
RAILWAY_DOCKERFILE_PATH=backend/Dockerfile   # required; see the box above
ENVIRONMENT=production
DATABASE_URL=postgresql+psycopg://...        # as above
WEATHER_PROVIDER=open_meteo
CORS_ALLOW_ORIGINS=https://<your-frontend-domain>
LOG_LEVEL=INFO
```

The API service deliberately gets **no** `ANTHROPIC_API_KEY`, no
`OPENAI_API_KEY`, and no `OPEN_METEO_API_KEY`. It never calls either service: it
reads published rows out of PostgreSQL. A credential a process cannot use is a
credential that cannot leak from it.

`CORS_ALLOW_ORIGINS` is a comma-separated allowlist. It is not `*`, and page data
does not depend on it — server-side rendering fetches over the private network —
so the only thing it governs is direct browser access to the public API.

---

## 3. Frontend service

**[AUTHORIZATION REQUIRED]** Create a second service from the same repo.

| Setting | Value |
| --- | --- |
| Root directory | `/` |
| **Builder** | set by `RAILWAY_DOCKERFILE_PATH` below — same caveat as the backend |
| Start command | *(empty — image `CMD` reads `$PORT`)* |
| Health check path | `/` |
| Public domain | required; this is the website |

Documented intent in version control: `frontend/railway.json`.

Variables:

```
RAILWAY_DOCKERFILE_PATH=frontend/Dockerfile  # required
API_BASE_URL=http://backend.railway.internal:8000
```

Two things about that value:

* It is the **private** address. Page rendering therefore does not leave
  Railway's network, does not pay egress, and does not depend on the API's public
  domain existing.
* There is no `NEXT_PUBLIC_API_BASE_URL`. `API_BASE_URL` is read in server
  components only. Nothing the browser downloads contains an API address, a key,
  or a model name.

No build-time variables are needed: every data page is `force-dynamic`, so the
image builds without a reachable backend. That is checked in CI.

---

## 4. Worker service (the schedule)

**[AUTHORIZATION REQUIRED]** Create a third service from the same repo, using
`backend/Dockerfile` again — the same image as the API, different command.

Railway cron services run the start command on a schedule and exit. Two
schedules are needed, which means two services (Railway allows one cron
expression per service):

### 4a. `worker-daily`

| Setting | Value |
| --- | --- |
| Root directory | `/` |
| **Builder** | set by `RAILWAY_DOCKERFILE_PATH` in the variables below |
| Start command | `python -m app.pipeline run` |
| Cron schedule | `30 9 * * *` (09:30 UTC) |
| Health check | none — this service is not a server |

Config as code: `backend/worker.railway.json` (it sets the start command for the
daily run and `restartPolicyType: NEVER`; override the start command in the UI for
`worker-finalize`).

Why 09:30 UTC: the pipeline analyses the most recent local calendar day that has
finished in *every* city in the registry. The registry's westernmost zone is
`Pacific/Honolulu` (UTC−10), so a day ends there at 10:00 UTC. Running at 09:30
UTC analyses the day before that — which is the correct, complete day everywhere,
including the −03:30 offset in St. John's. Running "at midnight" would silently
analyse a partial day for a third of the map.

### 4b. `worker-finalize`

| Setting | Value |
| --- | --- |
| Start command | `python -m app.pipeline finalize --lookback-days 14` |
| Cron schedule | `0 11 * * *` (11:00 UTC, after the daily run) |

Yesterday's data is a near-real-time model product; the ERA5 archive settles
about five days later. `finalize` re-runs those dates once the final data exists
and replaces the provisional board. Without this service the site is still
correct — provisional events are labelled as provisional — but it never upgrades
them.

Variables for **both** worker services:

```
RAILWAY_DOCKERFILE_PATH=backend/Dockerfile   # required; same image as the API
ENVIRONMENT=production
DATABASE_URL=postgresql+psycopg://...        # same reference as the backend
WEATHER_PROVIDER=open_meteo
OPEN_METEO_API_KEY=                          # optional; commercial tier
LLM_PROVIDER=none                            # or anthropic / openai
ANTHROPIC_API_KEY=                           # only if LLM_PROVIDER=anthropic
AGENT_MONTHLY_USD_BUDGET=5.00
AGENT_MONTHLY_MAX_LLM_CALLS=2000
AGENT_INVESTIGATE_TOP_N=10
LOG_LEVEL=INFO
```

This is the only place in the deployment where an LLM key exists. Explanations
are generated here, once, during the run, and stored. A page view never triggers
a model call, so traffic does not affect the AI bill.

---

## 5. First-run commands

**[AUTHORIZATION REQUIRED]** Run these once, after the services deploy, from
`railway run` against the backend service (or a one-off shell):

```bash
railway run --service backend alembic upgrade head
railway run --service backend python -m app.pipeline seed-cities
railway run --service backend python -m app.pipeline build-baselines
railway run --service backend python -m app.pipeline run
railway run --service backend python -m app.pipeline status
```

Migrations are **not** run automatically on boot. An automatic migration on
container start means every replica races to alter the same schema during a
deploy, and a bad migration takes the API down instead of failing one command.
Running them deliberately is worth the extra step.

### The cold-start caveat, measured

`build-baselines` fetches 30 years of daily history for each of 50 cities. On the
Open-Meteo free tier this costs about **391 weighted calls per city**, roughly
**19,600 in total**, against a published allowance of 10,000 per day and 5,000 per
hour.

Measured on 2026-09-22: the client self-throttles correctly and completes about
one city per 58 seconds until the hourly window closes, then stops. It committed
8–10 cities per window before `ProviderBudgetExhausted`. A full 50-city cache is
therefore a **2–3 day resumable job** on the free tier, not a single command.

This is not a failure mode to work around. Rerun it; each city is committed as it
finishes, cities already built are skipped, and cities without a baseline are
excluded from rankings rather than scored against nothing. The board simply grows
as the cache fills. With a commercial `OPEN_METEO_API_KEY` the whole build is a
single uninterrupted run.

The **daily** pipeline is a different order of magnitude: about 50 weighted calls,
0.5% of the free daily allowance. The quota is a setup cost, not an operating one.

---

## 6. Automatic deployments

**[AUTHORIZATION REQUIRED]** For each of the four services, connect it to the
GitHub repository and set the deploy branch to `main`.

Worth setting per service, if the plan allows it:

* **Watch paths.** `backend/**` for the backend and workers, `frontend/**` for the
  frontend. Otherwise a CSS change rebuilds and restarts the cron workers for no
  reason.
* **Wait for CI.** The CI workflow in this repo runs lint, types, the full test
  suite, the migration path and the evaluation harness. Gating deploys on it is
  the difference between "main is deployable" and "main is deployed".

---

## Environment variable reference

`.env.example` is the authoritative list with inline commentary. What matters for
deployment is *which service gets what* — the least-privilege split is the point:

| Variable | Frontend | Backend | Workers |
| --- | :---: | :---: | :---: |
| `RAILWAY_DOCKERFILE_PATH` | ✅ `frontend/Dockerfile` | ✅ `backend/Dockerfile` | ✅ `backend/Dockerfile` |
| `ENVIRONMENT` | – | ✅ `production` | ✅ `production` |
| `DATABASE_URL` | – | ✅ | ✅ |
| `API_BASE_URL` | ✅ private address | – | – |
| `CORS_ALLOW_ORIGINS` | – | ✅ frontend origin | – |
| `WEATHER_PROVIDER` | – | ✅ | ✅ |
| `OPEN_METEO_API_KEY` | – | ❌ never | optional |
| `PROVIDER_MAX_CALL_WEIGHT_PER_*` | – | – | optional |
| `LLM_PROVIDER` | – | – | ✅ |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | ❌ never | ❌ never | optional |
| `AGENT_MONTHLY_USD_BUDGET` | – | – | ✅ |
| `AGENT_MONTHLY_MAX_LLM_CALLS` | – | – | ✅ |
| `BASELINE_*`, `RANKING_*` | – | ✅ read by the API for `/api/methodology` | ✅ |
| `LOG_LEVEL` | – | optional | optional |

A `–` means the service ignores it. A ❌ means setting it there would be a
mistake, not merely unnecessary.

---

## Cost drivers

No prices here. Railway's and the model providers' pricing changes, and inventing
a monthly figure would be exactly the kind of unverified number this project
refuses to publish elsewhere. What is worth knowing is *what the meter responds
to*:

| Driver | Shape | Notes |
| --- | --- | --- |
| Frontend + backend uptime | Fixed per month | Two always-on containers. Both are small; the backend does arithmetic on cached rows, not modelling. |
| PostgreSQL | Fixed + storage | Storage grows with history: roughly 1,825 baseline rows per city (≈91k rows once all 50 are built, then static) plus ~50 observation rows and ≤10 published events per day. Kilobytes per day, not megabytes. |
| Worker compute | Per run | Two short cron executions a day. Idle between runs; billed for minutes, not hours. |
| Weather provider | Per weighted call | ~50/day steady state. The 19,600-call baseline build is once, ever — unless you change the reference period. |
| LLM | Per investigated event | ≤10 events/day × one bounded investigation each (6 tool calls, 1,200 output tokens max). Traffic does **not** affect this: explanations are precomputed and stored. Capped twice, by `AGENT_MONTHLY_USD_BUDGET` and by `AGENT_MONTHLY_MAX_LLM_CALLS`. |
| Egress | Per GB | Private networking between services is not metered as public egress; using `backend.railway.internal` rather than the public domain matters here. |

The expensive-looking part of the project — 30 years of climatology for 50 cities
— is a one-time cost that then answers every query from cache.

---

## When the pipeline fails

The design assumption is that it will, occasionally: a provider outage, a quota
window, a bad deploy.

* **A failed run does not overwrite the published board.** Analysis writes to a
  new run row and only the final publish step moves the pointer. If the run dies
  anywhere before that, the site keeps serving the last successful board, with its
  own analysis date and publication timestamp displayed.
* **The site shows how stale it is.** Every page displays the analysis date and
  publication time, so "yesterday's board is still up" is visible rather than
  silent.

Triage, in order:

```bash
railway logs --service worker-daily
railway run --service backend python -m app.pipeline status
```

| Symptom | Cause | Action |
| --- | --- | --- |
| Build fails with `Railpack could not determine how to build the app`, listing the repo's top-level directories | The service has no Dockerfile at the root of its build context, so Railway fell back to Railpack. Expected on a first deploy of this monorepo. | Set `RAILWAY_DOCKERFILE_PATH` on that service (`backend/Dockerfile` or `frontend/Dockerfile`), then **redeploy** — a variable change may not rebuild on its own. |
| Same Railpack error *after* setting `RAILWAY_DOCKERFILE_PATH` | Either the log is from the earlier build, the variable landed on a different service, or the path has a leading `./` or a typo | Check the build's timestamp against when the variable was saved; confirm the variable is on the failing service; the value is repo-root-relative with no leading slash or dot. |
| `ProviderBudgetExhausted` in a baseline build | Free-tier window spent | Expected. Rerun later; it resumes. |
| 429s with `Hourly API request limit exceeded` | The provider's real counter is ahead of ours (e.g. two runs in one hour) | Wait for the hour to roll over. Lower `PROVIDER_MAX_CALL_WEIGHT_PER_HOUR` if it recurs. |
| Board has fewer than 10 events | Fewer than 10 cities have baselines | Continue `build-baselines`. This is correct behaviour, not a bug. |
| Explanations are templated, not narrative | No LLM key, or a budget ceiling reached | Intended fallback. Check `LLM_PROVIDER` and the monthly counters. |
| API up, frontend shows an error | `API_BASE_URL` wrong, or the private domain misspelled | `curl` the private address from a backend shell. |
| Migration errors on deploy | Schema and code out of step | `alembic upgrade head`; migrations are never automatic. |
| Everything is labelled `synthetic_fixture_v1` | `WEATHER_PROVIDER=fixture` in production | Set it to `open_meteo` and re-run. |

---

## Deployment checklist

Do not describe this deployment as complete until every line is checked, by
observation and not by assumption:

- [ ] `RAILWAY_DOCKERFILE_PATH` set on all three app services; each build log shows a Docker build, not Railpack
- [ ] PostgreSQL provisioned; public TCP proxy **off**
- [ ] `DATABASE_URL` on all three app services uses `postgresql+psycopg://` and the private domain
- [ ] `alembic upgrade head` applied
- [ ] `seed-cities` reports 50 cities
- [ ] `build-baselines` has covered at least 10 cities (full coverage may take days on the free tier)
- [ ] `python -m app.pipeline run` completed and `status` shows a published run
- [ ] `GET /health` on the backend returns healthy
- [ ] `GET /api/rankings/latest` returns the published board with a real `source_dataset`
- [ ] Frontend home page renders that board, with analysis date and publication timestamp visible
- [ ] Frontend loads with no authentication
- [ ] No API key appears in any client bundle (`curl` the page source and grep)
- [ ] `worker-daily` has executed on schedule at least once, in the logs
- [ ] `worker-finalize` has executed on schedule at least once
- [ ] A deliberately failed run leaves the previous board published
- [ ] Attribution to Open-Meteo and OpenStreetMap is visible in the footer
- [ ] `/methodology` shows the methodology version and reference period
