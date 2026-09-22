# Local development

Two ways to run the whole thing. Docker is the shortest path to a working copy;
the native setup is what you want if you are going to be editing code.

Either way, the important decision is `WEATHER_PROVIDER`:

| Value | What it does | When to use it |
| --- | --- | --- |
| `fixture` | Generates deterministic synthetic weather in-process. No network, no quota, instant. Every row it produces is stamped `synthetic_fixture_v1`, and the UI labels it as such. | Working on the frontend, the API, the statistics, or the tests. |
| `open_meteo` | Real ERA5 reanalysis and forecast data. | Producing a real board. Read [data-sources.md](./data-sources.md) on the free-tier budget first — the initial baseline build is a multi-hour job. |

Start with `fixture`. Nothing in the application behaves differently except the
numbers, and you cannot exhaust a provider quota you never call.

---

## Option A: Docker

```bash
cp .env.example .env          # nothing in it needs editing for fixture mode
docker compose up --build     # db + api + web
```

Then, in a second terminal:

```bash
docker compose exec api alembic upgrade head
docker compose exec api python -m app.pipeline seed-cities
docker compose exec api python -m app.pipeline build-baselines
docker compose exec api python -m app.pipeline run
```

* Website: <http://localhost:3050>
* API: <http://localhost:8000> (docs at `/docs`)

`build-baselines` against the fixture provider takes well under a minute for all
50 cities. Against `open_meteo` it is rate-limited — see below.

---

## Option B: Native

Prerequisites: Python 3.13, Node 22, PostgreSQL 17.

### Database

```bash
createdb weather_outliers
psql weather_outliers -c "CREATE USER weather WITH PASSWORD 'weather'; GRANT ALL ON DATABASE weather_outliers TO weather;"
```

Any credentials work as long as `DATABASE_URL` matches. The driver is psycopg 3,
so the URL scheme must be `postgresql+psycopg://` — plain `postgresql://` makes
SQLAlchemy reach for psycopg2, which is not installed.

### Backend

```bash
cd backend
python3.13 -m venv .venv
.venv/bin/pip install -e ".[dev]"

cp ../.env.example ../.env     # then set DATABASE_URL
export ENVIRONMENT=development
export WEATHER_PROVIDER=fixture
export DATABASE_URL="postgresql+psycopg://weather:weather@127.0.0.1:5432/weather_outliers"

.venv/bin/alembic upgrade head
.venv/bin/python -m app.pipeline seed-cities
.venv/bin/python -m app.pipeline build-baselines
.venv/bin/python -m app.pipeline run

.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

`ENVIRONMENT` accepts `development`, `production` or `test` and nothing else; an
unrecognised value fails fast at startup rather than half-configuring the app.

### Frontend

```bash
cd frontend
npm install
API_BASE_URL=http://127.0.0.1:8000 npm run dev    # http://localhost:3050
```

`API_BASE_URL` is read **server-side only**. There is no `NEXT_PUBLIC_` variant
and that is deliberate: the browser never talks to the API directly, so no
provider or model credential can leak into a client bundle.

---

## The pipeline CLI

```bash
python -m app.pipeline <command>
```

| Command | Purpose |
| --- | --- |
| `seed-cities` | Load `data/cities.json` into the database. Idempotent; run it after editing the registry. |
| `build-baselines` | Populate the 1991–2020 climatology cache. `--city <id>` (repeatable) to limit, `--force` to rebuild. Slow against the real provider, and **resumable** — cities already built are skipped. |
| `run` | Analyse one date and publish it. No `--date` means "the most recent local calendar day that is complete in every city". `--skip-explanations` for a cheap re-rank; `--tier` to force `final`/`provisional`. |
| `backfill --start --end` | Re-run a closed date range, oldest first. Each date is independent, so one bad day does not abort the rest. |
| `finalize` | Recompute recent dates whose provisional data has since settled into the archive. `--lookback-days` (default 14). |
| `status` | Baseline coverage and the last few runs. The first thing to check when the site looks wrong. |

A failed `run` leaves the previous published board in place. That is the whole
point of the publish step, and `status` is how you notice it happened.

### Building real baselines

The provider's free tier meters *weighted* calls, and 30 years of daily history
for one city costs about 391 of a 10,000-per-day allowance. So:

```bash
WEATHER_PROVIDER=open_meteo python -m app.pipeline build-baselines
```

will self-throttle, then stop with `ProviderBudgetExhausted` when the day's
window is spent — roughly 25 cities in, from cold. Rerun it tomorrow and it
continues where it stopped, because each city is committed as it completes.
Cities without a baseline are excluded from rankings rather than scored against
nothing, so a partially built cache produces a smaller board, not a wrong one.

Full numbers in [data-sources.md](./data-sources.md).

---

## Tests, lint, types

```bash
# Backend (from backend/, with DATABASE_URL pointing at a throwaway database)
.venv/bin/python -m pytest
ruff check backend evals        # from the repo root

# Evaluation harness (from the repo root)
backend/.venv/bin/python -m evals.runner

# Frontend (from frontend/)
npm run lint
npm run typecheck
npm run check-evals             # committed eval report matches evals/reports/latest.json
```

The `weather` role in the snippets above does not need `CREATEDB`. If you want a
clean slate, `TRUNCATE` the tables rather than dropping the database.

CI runs exactly these commands — see `.github/workflows/ci.yml`. It does not call
the weather provider or an LLM, so it cannot fail because a third party is having
a bad day.

---

## Screenshots

```bash
node frontend/scripts/capture-screenshots.mjs
```

Requires the site running at `http://localhost:3050`. Writes PNGs and a
`manifest.json` to `docs/screenshots/`. The manifest records the analysis date,
the methodology version and the `source_datasets` behind the board in the frame —
because a screenshot of `synthetic_fixture_v1` looks exactly like a screenshot of
real weather, and an image in a README is a claim.

Captures are 1x by default; `SCALE=2` for a high-resolution one-off.

Known: the `/map` capture fails in this headless setup. It waits for MapLibre to
report itself idle, which needs tiles from `tiles.openfreemap.org`, and the script
reports a timeout rather than photographing a half-drawn basemap. The map renders
in a real browser; the capture of it is not part of the committed set.

---

## Things that will waste your afternoon

* **`next start` renames its own process to `next-server`.** `pkill -f "server.js"`
  matches nothing. Use `pkill -f next-server` and confirm with
  `lsof -nP -iTCP:3050 -sTCP:LISTEN`. A stale server serving an old bundle looks
  exactly like a code change that did not work.
* **A system `python3` is not the venv.** If `field_validator` raises an
  `ImportError`, you are on a Pydantic 1.x interpreter. Use
  `backend/.venv/bin/python` explicitly.
* **`evals.runner` runs from the repo root**, not from `backend/` — it needs
  `data/` and `evals/` on the path.
* **An empty board is usually missing baselines, not a bug.** Check
  `python -m app.pipeline status` before reading any statistics code.

## One deliberate omission: no root `loading.tsx`

There used to be a `src/app/loading.tsx`. It was removed, and it should stay
removed.

With a root-level loading boundary, Next.js streams the shell immediately and
fills in the page body when the server component resolves. That is usually what
you want. Here it produced pages that rendered a visible loading skeleton and
then settled into what looked like a 404 — because a root boundary wraps *every*
route including ones whose data fetch legitimately returns nothing, and the
streamed shell had already committed a 200 before the page could respond
otherwise.

The trade-off taken: no root loading boundary, so a page waits for its data and
then renders the right thing — real content, an empty state, or a genuine 404.
The cost is that navigation shows nothing for the duration of one API call
against a local database, which is not a cost worth a class of phantom 404s. If
a per-route loading state is wanted later, add it inside that route's own
segment, with a `not-found` boundary beside it — not at the root.
