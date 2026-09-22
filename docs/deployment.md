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
| Root directory | `/` — the default |
| Builder | *(nothing to set: `./Dockerfile` is detected, and `railway.json` pins it)* |
| Start command | *(leave empty — the image's `CMD` reads `$PORT`)* |
| Health check path | `/health` |
| Public domain | generate one, or attach a custom domain |

> ### How the builder is decided, and why it took three tries
>
> Railway's rule is that it *"will always build with a Dockerfile if it finds one"* —
> one at the root of the **build context**. The only per-service setting that matters
> here is therefore **Root directory**, because that is what sets the context:
>
> | Service | Root directory | Dockerfile it finds |
> | --- | --- | --- |
> | Backend API | `/` | `./Dockerfile` |
> | Workers | `/` | `./Dockerfile` (same image) |
> | Website | `frontend` | `frontend/Dockerfile` |
>
> Nothing else needs configuring. `railway.json` at the repository root pins
> `builder: DOCKERFILE` for the first two, and `frontend/railway.json` does the same
> for the website; config-as-code takes precedence over the dashboard, so a builder
> left pinned to Railpack by an earlier attempt cannot override it.
>
> **The history, because the failure mode is worth recognising.** The repository
> originally kept its Dockerfiles at `backend/Dockerfile` and `frontend/Dockerfile`,
> and three deploys failed with:
>
> ```
> Railpack could not determine how to build the app.
> ```
>
> That message means no Dockerfile was found at the context root, so Railway fell
> back to language autodetection and could not classify a root holding a Python
> service beside a Node one. Two fixes were tried and did not work: a `railway.json`
> inside `backend/` (Railway reads config-as-code from the root, so it was ignored),
> and the `RAILWAY_DOCKERFILE_PATH` variable (documented, and genuinely the right
> tool — but an explicitly pinned builder takes precedence over it, and by then the
> services had one). The layout above needs neither, which is why it is the layout.
>
> `RAILWAY_DOCKERFILE_PATH` is still a valid escape hatch if you would rather not
> move a root directory — set it to `Dockerfile` or `frontend/Dockerfile` — but with
> the files where they are now, it should not be necessary.
>
> **Redeploy explicitly after changing any of this.** A settings or variable change
> does not always trigger a rebuild, and the build log you are re-reading may be the
> old one. Check its timestamp before concluding a fix did not work.

Variables:

```
ENVIRONMENT=production
DATABASE_URL=postgresql+psycopg://...        # as above
WEATHER_PROVIDER=open_meteo
CORS_ALLOW_ORIGINS=https://<your-frontend-domain>
PORT=8000
LOG_LEVEL=INFO
```

**Set `PORT` explicitly here even though the image defaults to it.** The website
reaches this service over private networking at a fixed port, and private
networking does no port mapping — whatever the process listens on is the port you
must dial. Leaving `PORT` to be injected means the address in the website's
`API_BASE_URL` is a guess about what was injected. Pinning it makes `:8000`
true by construction.

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
| Root directory | **`frontend`** — this is the one setting that matters; see the box in §2 |
| Builder | *(nothing to set: `frontend/Dockerfile` is detected there)* |
| Start command | *(empty — image `CMD` reads `$PORT`)* |
| Health check path | `/` |
| Public domain | required; this is the website |

Build config in version control: `frontend/railway.json`, which Railway reads because the service's root is `frontend`.

Variables:

```
API_BASE_URL=http://${{<backend-service-name>.RAILWAY_PRIVATE_DOMAIN}}:8000
```

Use the reference form rather than typing the host. A service's private domain is
derived from **its service name**, so `backend.railway.internal` is only correct if
the backend service is literally named `backend`; name it `weather-outliers-api` and
the hostname changes with it. The reference is resolved by Railway, so it cannot be
stale or misspelled — and it is why `PORT=8000` is pinned on the backend, since the
port half of this address is not a reference and has to be true.

Two more things about that value:

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

**[AUTHORIZATION REQUIRED]** Create a third service from the same repo. It builds
the root `Dockerfile` — the same image as the API — and differs only in command.

Railway cron services run the start command on a schedule and exit. Two
schedules are needed, which means two services (Railway allows one cron
expression per service):

### 4a. `worker-daily`

| Setting | Value |
| --- | --- |
| Root directory | `/` — the default |
| Builder | *(nothing to set)* |
| Start command | `python -m app.pipeline run` |
| Cron schedule | `30 9 * * *` (09:30 UTC) |
| Health check | none — this service is not a server |

Set **Restart policy → Never** on both workers. A cron run that fails should wait
for tomorrow rather than immediately retry against the same exhausted provider
quota, and a failed run cannot corrupt the site: it never reaches the publish step,
so the last successful board stays up. The root `railway.json` deliberately carries
no `deploy` block, so it does not impose the API's health check on a service that
exits by design.

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

**Using DeepSeek (or another OpenAI-compatible endpoint).** The `openai` provider
is a plain Chat Completions client, so a third-party gateway needs no code — four
variables on **both worker services** and nothing anywhere else:

```
LLM_PROVIDER=openai
OPENAI_API_KEY=<your key>
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_MODEL=deepseek-flash             # confirm the current name on their dashboard
OPENAI_MAX_TOKENS_PARAM=max_tokens      # DeepSeek's field name; see below
```

Two things to get right:

* **`OPENAI_MAX_TOKENS_PARAM`.** OpenAI renamed the output cap to
  `max_completion_tokens` and its reasoning models reject the old name; DeepSeek
  accepts `max_tokens` only. That is the default's one wrong case, and the failure
  is silent — a lenient endpoint ignores the field it does not know, the 1,200-token
  cap stops applying, and nothing errors. Set it to `max_tokens` for DeepSeek.
* **The model must support tool calling.** The investigator hands the model five
  tools and a `submit_explanation` schema; a model without function calling cannot
  answer in the required shape, and every event falls back to the deterministic
  template. Reasoning-only models are the usual offenders.

`AGENT_USD_PER_MTOK_INPUT` / `_OUTPUT` stay 0.00 until you copy the current
figures off your provider's pricing page — until then `AGENT_MONTHLY_MAX_LLM_CALLS`
is the ceiling that actually binds, which is why it exists. At ≤10 investigations a
day the call count is bounded at roughly 300–600/month before the cap is near.

To confirm the key is actually being used rather than silently falling back, run
`python -m app.pipeline run` on the worker and read the report: `model-written`
explanations mean the model answered, `deterministic` means it did not.

---

## 5. First-run commands

**[AUTHORIZATION REQUIRED]** Run these once, after the backend service is deployed
and running. `railway ssh` executes them **inside the container**, which is what
makes them work at all — substitute your own backend service name:

```bash
railway link                       # once, to select the project + environment
railway ssh --service weather-outliers-api alembic upgrade head
railway ssh --service weather-outliers-api python -m app.pipeline seed-cities
railway ssh --service weather-outliers-api python -m app.pipeline build-baselines
railway ssh --service weather-outliers-api python -m app.pipeline run
railway ssh --service weather-outliers-api python -m app.pipeline status
```

Not `railway run`: that runs the command on **your machine** with the service's
variables injected, and `DATABASE_URL` points at `${{Postgres.RAILWAY_PRIVATE_DOMAIN}}`
— a hostname that exists only inside Railway's network. It would need the backend's
dependencies installed locally too. `railway ssh` needs neither.

If you would rather run them locally against the deployed database — to watch the
baseline build in your own terminal, say — use the Postgres service's **public** TCP
proxy instead, and expect it to be slower and to be billed as egress:

```bash
cd backend
DATABASE_URL="postgresql+psycopg://…@<proxy-host>:<proxy-port>/railway" \
  .venv/bin/python -m app.pipeline build-baselines
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
| `ENVIRONMENT` | – | ✅ `production` | ✅ `production` |
| `DATABASE_URL` | – | ✅ | ✅ |
| `API_BASE_URL` | ✅ private address | – | – |
| `CORS_ALLOW_ORIGINS` | – | ✅ frontend origin | – |
| `WEATHER_PROVIDER` | – | ✅ | ✅ |
| `OPEN_METEO_API_KEY` | – | ❌ never | optional |
| `PROVIDER_MAX_CALL_WEIGHT_PER_*` | – | – | optional |
| `LLM_PROVIDER` | – | – | ✅ |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | ❌ never | ❌ never | optional |
| `OPENAI_BASE_URL` / `OPENAI_MODEL` / `OPENAI_MAX_TOKENS_PARAM` | – | – | with `openai` |
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
| Build fails with `Railpack could not determine how to build the app`, listing the repo's top-level directories | No Dockerfile at the root of that service's build context. For the website it means the root directory is not `frontend`. | Set the root directory per the table in §2, then **redeploy explicitly** — a settings change does not always rebuild. |
| Same Railpack error after fixing the root directory | The log is the old build, or the service has a builder explicitly pinned to Railpack from an earlier attempt | Compare the build timestamp to when you saved the change. A pinned builder is overridden by `railway.json`, so a genuinely new build cannot still be on Railpack — if it is, you are reading the old log. |
| Build succeeds, then the container restart-loops on `Error loading ASGI app. Could not import module "app.main"` | Something installed a regular `app` package into site-packages. `app` is an implicit namespace package here, and a regular package of the same name wins the import *regardless of `sys.path` order*, so it shadows `/app/app`. This was a stub left behind by the dependency layer; fixed, with a `find_spec` assertion in the Dockerfile so the **build** fails instead of the deploy. | If it reappears: `python -c "import app; print(app.__path__)"` inside the container. It must print `/app/app`. Anything under `site-packages` is the shadow — delete it in the same layer that creates it. |
| `ProviderBudgetExhausted` in a baseline build | Free-tier window spent | Expected. Rerun later; it resumes. |
| 429s with `Hourly API request limit exceeded` | The provider's real counter is ahead of ours (e.g. two runs in one hour) | Wait for the hour to roll over. Lower `PROVIDER_MAX_CALL_WEIGHT_PER_HOUR` if it recurs. |
| Board has fewer than 10 events | Fewer than 10 cities have baselines | Continue `build-baselines`. This is correct behaviour, not a bug. |
| Explanations are templated, not narrative | No LLM key, or a budget ceiling reached | Intended fallback. Check `LLM_PROVIDER` and the monthly counters. |
| The custom domain returns `{"name":"Weather Outliers API",...}` | The domain is attached to the **backend** service. That JSON is the API's root route, so the API is healthy — it is just not the website. | Attach the domain to the website service instead (root directory `frontend`). The API does not need a public domain at all. |
| API up, frontend shows an error | `API_BASE_URL` wrong. Usually the host: the private domain follows the **service name**, so `backend.railway.internal` is wrong unless the service is named `backend`. Otherwise the port, if `PORT` was left unpinned on the backend. | Use `http://${{<service>.RAILWAY_PRIVATE_DOMAIN}}:8000` and set `PORT=8000` on the backend. Confirm with `curl` from a backend shell. |
| Migration errors on deploy | Schema and code out of step | `alembic upgrade head`; migrations are never automatic. |
| Everything is labelled `synthetic_fixture_v1` | `WEATHER_PROVIDER=fixture` in production | Set it to `open_meteo` and re-run. |

---

## Deployment checklist

Do not describe this deployment as complete until every line is checked, by
observation and not by assumption:

- [ ] Website service root directory is `frontend`; the other services are at `/`
- [ ] Every build log shows a Docker build, not Railpack
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
