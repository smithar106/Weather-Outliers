# Data sources

Every number the site publishes comes from one of the sources below. This document
records what each one is, what licence it carries, what it costs, and — for the
weather data — the specific limits that shaped the code.

The order of work here was deliberate: **the provider's documentation and terms
were read before the adapter was written**, not after it broke. One of the findings
below (weighted call accounting) invalidated the obvious implementation, and
discovering it from the docs rather than from a wall of HTTP 429s is the whole
argument for doing it in that order.

---

## 1. Weather data — Open-Meteo

| | |
| --- | --- |
| Endpoint (historical) | `https://archive-api.open-meteo.com/v1/archive` |
| Endpoint (near-real-time) | `https://api.open-meteo.com/v1/forecast` |
| Underlying dataset | ERA5 / ERA5-Land reanalysis, Copernicus C3S / ECMWF |
| Model pinned as | `models=era5_seamless` |
| Data licence | CC-BY 4.0 |
| Free-tier permitted use | **Non-commercial only** |
| Attribution | Required, and shown in the site footer on every page |
| Authentication | None on the free tier |

### What the numbers physically are

**Reanalysis output, not station observations.** ERA5 assimilates observations into
a physical model and produces a gridded estimate; the value for a city is the
model's estimate at a grid point of order 10–30 km, not a reading from a
thermometer in that city. The database stores this as
`observation_type: reanalysis`, the API returns it, and the interface prints "Model
analysis estimate" rather than "observed". Nothing in this project describes
modelled data as a direct station observation.

The consequence is physical, not cosmetic: gridded reanalysis is spatially
smoothed, so it will understate a thunderstorm's peak rainfall and a canyon's peak
gust, and it will not reproduce a single station's microclimate. The city page
prints the grid coordinates actually used next to the city's own coordinates so a
reader can see the offset.

### Two tiers, because the archive lags

ERA5 publishes roughly five days behind real time, so a site whose headline is
"yesterday" cannot use it alone.

- `DataTier.FINAL` — the archive endpoint with `models=era5_seamless`. Used for the
  30-year baseline and for any date the archive has reached.
- `DataTier.PROVISIONAL` — the forecast endpoint's `past_days`, which returns the
  operational model's analysis of days that have already happened. Available
  immediately, badged **Provisional** everywhere it appears, and recomputed against
  the archive by `pipeline finalize` once the archive catches up.

Pinning the archive to an explicit `models` value matters more than it looks: the
baseline and the final daily value then come from *the same dataset*, so an anomaly
is not contaminated by a change of model between the climatology and the day being
tested. The provisional tier does not have that guarantee — it is a different
model than ERA5 — which is the honest reason for the badge.

### Licensing and the commercial-use boundary

The free API is for **non-commercial use**; the data itself is CC-BY 4.0. This
project is a public portfolio piece with no revenue, which is inside that boundary,
and the attribution obligation is met by the footer link that appears on every
page.

Setting `OPEN_METEO_API_KEY` switches to the paid customer endpoints with no other
code change (`open_meteo_customer_archive_url` /
`open_meteo_customer_forecast_url`). That is the single step required if this were
ever put to commercial use. **Do not assume the free tier covers it.**

### Rate limits: requests are not what is counted

The published free-tier allowances are **600 calls/minute, 5,000/hour and
10,000/day**. The trap is the word "calls":

> "Requests for data covering more than 10 weather variables or extending over a
> period of more than 2 weeks for a single location are considered multiple API
> calls." … "a request for 2 weeks of data with 15 weather variables will be
> calculated as 1.5 API calls, while 4 weeks of data equals 3.0 API calls."
>
> — open-meteo.com/en/pricing, retrieved 2026-09-22

So a call is a 10-variable, 2-week unit, and the cost of a request is the product
of two ratios with a floor of one:

```
weight = max(1, (variables / 10) × (days / 14))
```

`backend/app/providers/open_meteo.py` implements this as `estimate_call_weight`,
and the limiter meters **weighted calls** across three sliding windows (minute,
hour, day) rather than counting HTTP requests. What that changes in practice:

| Request | HTTP requests | Weighted calls |
| --- | --- | --- |
| One day, 5 variables, one city | 1 | 1 |
| One day, 5 variables, all 50 cities | 50 | 50 |
| One 10-year baseline chunk, 5 variables | 1 | ≈ 130.5 |
| Full 30-year baseline, one city | 3 | ≈ 391 |
| Full 30-year baseline, 50 cities | 150 | **≈ 19,566** |

This was measured, not theorised. An earlier build of the adapter counted HTTP
requests and was configured for 120 per minute — nominally a fifth of the
allowance, actually about 26× over it. Open-Meteo throttled it to exactly **five
successful archive requests per minute for six consecutive minutes** (5 × 130.5 ≈
650 ≈ the 600/minute allowance), twelve cities failed outright, and because the
build held all fifty cities in one transaction, the interruption discarded every
city it had already fetched. Both faults are fixed: the limiter is weight-aware,
and baselines commit per city.

Two consequences worth stating plainly:

**The daily pipeline is cheap.** One day of five variables for fifty cities is
about 50 weighted calls against a 10,000/day allowance — roughly 0.5%. Running
this application every day costs essentially nothing.

**The first baseline build is not.** At ≈19,566 weighted calls it exceeds one day's
free allowance, so a cold start takes **two to three days** on the free tier. It is
a one-time cost — baselines are cached in PostgreSQL and never re-downloaded by the
daily run — and the build is resumable: it commits each city as it completes, skips
cities that already have baselines, and stops with an actionable message when a
window is spent rather than hammering the service.

```bash
# Resume until it reports 50 cities. Safe to run repeatedly.
python -m app.pipeline build-baselines

# Or take it in explicit batches.
python -m app.pipeline build-baselines --city-ids us-new-york-ny,us-chicago-il
```

With `OPEN_METEO_API_KEY` set, the whole build completes in one pass.

Budgets are configurable and default to 15–20% below the published allowances,
because the weight is *our estimate of the provider's accounting*, not a reading of
it:

| Setting | Default | Published allowance |
| --- | --- | --- |
| `PROVIDER_MAX_CALL_WEIGHT_PER_MINUTE` | 500 | 600 |
| `PROVIDER_MAX_CALL_WEIGHT_PER_HOUR` | 4,200 | 5,000 |
| `PROVIDER_MAX_CALL_WEIGHT_PER_DAY` | 8,500 | 10,000 |

A 429 that does slip through is retried after a **full window** (honouring
`Retry-After` when sent), not after two seconds. Open-Meteo's counter resets on a
boundary, so exponential backoff from 2 s just spends the remaining attempts inside
the same exhausted minute — which is exactly how the twelve cities were lost.

### Swapping the provider

Nothing downstream of `backend/app/providers/` knows Open-Meteo exists. Adding
NOAA GHCN-Daily, Environment Canada, or a commercial feed means writing one class
that emits `DailyRecord`, registering it in `providers/__init__.py`, and changing
`WEATHER_PROVIDER`. A `fixture` provider ships for tests and offline development
and labels its output `synthetic_fixture_v1` in the database and in the UI, so
synthetic data can never be mistaken for real.

### Sources considered and not used

| Source | Why not |
| --- | --- |
| NOAA GHCN-Daily | Genuine station observations, which would be *better* than reanalysis for this purpose, and the natural next provider. Not used yet because coverage is US-centric, station records have gaps and moves that need handling before a 30-year baseline is trustworthy, and Canada/Mexico coverage would have to come from elsewhere. |
| Meteostat | Convenient blend of station and model data, but the blending is not transparent enough to label honestly on a page that distinguishes observation from model output. |
| Commercial weather APIs | Would resolve the rate-limit problem and none of the honesty problems, at a cost this project does not justify. |

### Not implemented: official record verification

No authoritative record archive (NOAA NCEI records, national meteorological
service record tables) is consulted. Therefore **nothing on this site is called a
record**. Events are "statistical outliers" or "unusual events", and
`is_verified_official_record` is `false` on every event on every endpoint so a
client cannot render one as a record by omission.

Implementing verification would mean ingesting a record archive per country,
reconciling it against reanalysis values it was not measured with, and deciding
what to do when the two disagree. That is a larger project than the one here, and
claiming records without it would be the single most misleading thing this
application could do.

---

## 2. Map tiles — Mapbox

| | |
| --- | --- |
| Style | `mapbox://styles/mapbox/light-v11` |
| Underlying data | OpenStreetMap, ODbL |
| Authentication | A public `pk.…` access token, read server-side and passed to the map as a prop |
| Attribution | Mapbox and © OpenStreetMap contributors, shown on the map and in the footer |

Chosen because it needs only a *public* access token — a `pk.…` key is designed to
sit in a browser bundle: it identifies the account for billing and style access and
cannot be used to administer it. That distinction matters against this project's
usual rule that no credential reaches the browser: the token is not a secret, and
it is read server-side at request time and handed to the client as a prop rather
than inlined into the build.

The attributions are set explicitly in `AnomalyMap.tsx` rather than left to a
default, and the map degrades to an explicit failure panel if the token is missing
or the tiles do not load — the ranked events are also listed as text on the same
page, so the map is never the only way to read the board.

---

## 3. City registry — this repository

`data/cities.json` is version-controlled and is the only source of the city list.
Population figures carry a per-country source attribution. See
[`data/README.md`](../data/README.md) for selection criteria and the limitations
of a 50-city sample.

---

## 4. Language model (optional) — Anthropic or OpenAI

| | |
| --- | --- |
| Purpose | One short written explanation per published event |
| Required? | **No.** With no key configured every explanation comes from a deterministic template |
| Where it runs | Server-side, once per event, during the scheduled pipeline |
| Cost control | Per-run call and token caps, plus a configurable monthly USD ceiling |

A visitor's page view never invokes a model: explanations are generated during the
pipeline and stored, so traffic does not create LLM spend. Keys live in the worker
and backend environments only and are never exposed to the browser.

Cost drivers are documented in [`deployment.md`](./deployment.md). Actual prices
are not reproduced here, because they change and an invented figure is worse than a
pointer to the vendor's page.

---

## 5. What is *not* a data source

- **No user data.** The site has no accounts, no login, and no analytics that
  identify a visitor.
- **No scraped content.** Nothing is taken from another weather site's pages.
- **No hand-entered weather numbers anywhere in the repository.** Every figure in
  the application comes from the provider through the ingest path, and every figure
  on the evaluation page is measured by the harness during the run that produced
  it.
