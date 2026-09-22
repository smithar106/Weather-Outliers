# Methodology

**Version 1.0.0.** This document is the specification the code implements. Where
the two disagree, the code is the bug. Every constant named here lives in
`backend/app/stats/` or `backend/app/domain.py`, and changing any of them is a
methodology change that requires bumping `METHODOLOGY_VERSION` — because baselines
and events are keyed by that string, historical boards keep the methodology they
were computed under and the archive stays honest.

## What this project claims, and what it does not

It claims to find the **most statistically unusual** daily weather readings in its
registry of cities, measured against each city's own recent climate.

It does not claim to find records. No official record archive is consulted, so
every event is labelled a *statistical outlier*, never a city, state, national or
all-time record. The API returns `is_verified_official_record: false` on every
event, on every endpoint, so a client cannot render one as a record by omission.

It also does not claim to report station observations. The numbers are **model
output** — see [Data sources](./data-sources.md) — and the interface says so on
every page that shows one.

## 1. The reference period

Anomalies are measured against a **1991–2020** reference period, configurable via
`BASELINE_START_YEAR` / `BASELINE_END_YEAR`. Thirty years is the WMO convention
for a climate normal, and 1991–2020 is the current standard period, which means a
reader can compare these figures to published normals without rescaling.

The word "normals" is deliberately avoided in the interface. A WMO normal is a
specific published product computed to specific rules from station records; this
is a reanalysis-derived reference computed here. The API field is
`reference_period`, and the UI says "seasonal reference", not "normal".

### Seasonal window

For each city, metric and day of year, the reference sample is every day in the
reference period falling within **±7 days** of that day of year
(`BASELINE_SEASONAL_WINDOW_DAYS`). So a 30-year period yields up to 15 × 30 = 450
samples per bucket.

The window exists because a bare day-of-year sample is 30 values — far too few to
estimate a 1-in-400 tail. It is kept narrow because a wider one smears the annual
cycle: in a fast-warming shoulder season, a ±30 day window would compare early
April to mid-May and manufacture anomalies out of the calendar.

### The 365-day no-leap calendar

Day-of-year buckets are computed on a **365-day calendar with no February 29**.
February 29 maps to the February 28 bucket. Without this, day-of-year 60 means
March 1 in three years out of four and February 29 in the fourth, and every
bucket after it in a leap year is shifted by one day relative to the same bucket
in a common year — a silent one-day seasonal offset in 25% of the sample.

The window wraps across the year boundary, so January 3 draws on the previous
late December as well as early January.

### Minimum sample sizes

A bucket is usable only if it has:

| Guard | Default | Setting |
| --- | --- | --- |
| Total samples | 120 | `BASELINE_MIN_SAMPLES` |
| Distinct years | 20 | `BASELINE_MIN_YEARS` |
| Wet days (precipitation only) | 15 | `BASELINE_MIN_WET_DAYS` |

A bucket that fails a guard is stored with `sufficient: false` and produces no
ranked event. The distinct-year guard matters independently of the sample count:
450 samples drawn from three unusual years describe those years, not a climate.

## 2. What is stored, and why it is a sketch

Storing every reference sample would be 50 cities × 5 metrics × 365 days × up to
450 values ≈ 41 million floats. Storing only the mean and standard deviation
would be cheap and wrong, because precipitation is zero-inflated and gusts are
right-skewed, so normal theory misstates exactly the tail being ranked.

So each bucket stores a **quantile sketch**: a tail-dense probability grid plus
the five most extreme order statistics at each end — about 45 numbers — along with
n, mean, standard deviation, median, IQR, min, max, a wet-day fraction for
precipitation, and a 24-bin histogram for display. Percentiles and exceedance
probabilities are read off a monotone piecewise-linear empirical CDF built from
the sketch.

Conventions, both chosen to match what a statistician would expect rather than
what was convenient:

- **Plotting positions** use the Weibull rule `p_i = i / (n + 1)`, so the largest
  of n samples gets exceedance probability `1/(n+1)` rather than zero.
- **Quantile interpolation** is linear between order statistics with `h = (n−1)p`
  — NumPy's default and R's type 7.

## 3. Per-metric treatment

Five daily metrics: maximum, minimum and mean temperature, precipitation total,
and maximum wind gust.

| Metric | Distribution assumption | Tail | z-score |
| --- | --- | --- | --- |
| `temp_max`, `temp_min`, `temp_mean` | approximately symmetric | two-tailed | reported, `z_valid: true` |
| `precipitation` | zero-inflated, strongly right-skewed | upper only | **withheld entirely** |
| `wind_gust` | right-skewed, strictly positive | upper only | reported, `z_valid: false` |

Three deliberate consequences:

**A z-score is never presented as universally comparable.** Every event carries a
`z_valid` flag and, when false, the reason. The score bar on a city page prints
"Withheld" with an explanation rather than a number the reader would be entitled
to compare against a temperature z-score.

**Rainfall is scored as a zero-inflated mixture.** Most days in most cities are
dry, so the reference sample has a point mass at zero and normal theory applied
to it is wrong by orders of magnitude. The upper tail is computed as

```
P(X ≥ x) = P(X > 0) · P(X ≥ x | X > 0)
```

with the conditional tail read off the sketch of wet days only. A day with no
rain is not an event in either direction: a dry day in a dry season is not news,
and "0 mm" cannot be extreme in the lower tail of a variable bounded below by
zero.

**Cold and heat are both events.** Temperature is two-tailed, so an
extraordinarily cold morning ranks against an extraordinarily hot afternoon on the
same scale.

## 4. The anomaly score

Four figures are computed for every city-metric-day. Only the third is used to
rank.

1. **Absolute anomaly** — `value − seasonal median`, in the metric's own units.
   Shown for interpretability. Never ranks.
2. **Standardised anomaly (z-score)** — `(value − mean) / sd`, reported only where
   the distribution justifies it, and never used to rank.
3. **Empirical percentile and tail probability** — the reading's position in its
   own bucket, read off the sketch.
4. **Robust deviation** — `(value − median) / IQR`. Always defined, degrades
   gracefully on skewed samples, used only as a tie-break.

The ranking statistic is **surprisal**:

```
surprisal = −log₁₀(p_tail)
```

This is the single most important choice in the project. Raw magnitude cannot
rank across cities (a 40 °C day is unremarkable in Phoenix and unprecedented in
Vancouver) and cannot rank across metrics at all (is 60 mm of rain more unusual
than a 95 km/h gust?). A tail probability is dimensionless and has one meaning in
every city and every unit, so a 1-in-400 rainfall total and a 1-in-400 cold
morning sit at the same height. Taking `−log₁₀` turns it into a score that grows
by 1 for every additional factor of ten of rarity.

### The out-of-sample margin term

With at most 450 samples, the smallest resolvable tail probability is about
1/451. Every reading beyond the reference-period extreme therefore lands on that
same floor, and a value that beats the old extreme by 0.1 °C would tie with one
that beats it by 8 °C. Those results are flagged `tail_probability_is_bounded:
true` — the probability is a **lower bound on rarity**, not an estimate — and a
small term separates them:

```
margin_bonus = 0.5 · log₁₀(1 + margin / IQR)
anomaly_score = surprisal + margin_bonus
```

where `margin` is the distance beyond the most extreme reference value.
`MARGIN_WEIGHT = 0.5` is chosen so that a reading a full IQR past the old extreme
adds `0.5 · log₁₀(2) ≈ 0.15` — enough to order genuinely unprecedented values,
far too small to let magnitude overturn rarity. For any reading inside the
reference sample the term is exactly zero.

### Return period

`return_period_years = 1 / (p_tail · 365.25)` is displayed as context and is
explicitly not a design standard. It is an estimate from a 30-year sample of what
a stationary climate would produce; the climate is not stationary, and the
document says so on the page.

## 5. Ranking

Candidates from every city and metric are pooled and sorted by a **total order**,
so two runs over identical inputs produce an identical board even in the presence
of exact ties:

1. higher `anomaly_score`
2. lower (rarer) `tail_probability`
3. larger `|robust_deviation|`
4. fixed metric precedence
5. `city_id` ascending

Step 5 guarantees totality, since city ids are unique. Scores and probabilities
are rounded to 6 decimals before comparison (`COMPARISON_DECIMALS`) so
floating-point noise cannot reorder two mathematically equal events. Nothing in
the chain depends on dictionary or database iteration order.

**One event per city** reaches the published top 10 by default
(`RANKING_ONE_EVENT_PER_CITY`). A single heat dome can produce a genuine top-10
sweep across one metro area; that is meteorologically real and makes for a useless
daily board. Every other event is still computed, still stored, and still
queryable through the API — the constraint applies to presentation, not to the
dataset. If fewer than ten cities qualify, remaining slots are backfilled with the
next-highest-scoring events rather than leaving the board thin.

## 6. Provisional and final data

ERA5 reanalysis publishes with a delay of about five days, so a product whose
headline is "yesterday" cannot use it alone. Two tiers are therefore published,
and the difference is disclosed everywhere:

| Tier | Source | Latency | Label |
| --- | --- | --- | --- |
| `final` | ERA5 / ERA5-Land reanalysis | ~5 days | no badge |
| `provisional` | operational model analysis of a past day | immediate | **Provisional** badge |

`pipeline finalize` recomputes provisional dates against the archive once it
catches up. Because event identity is a pure function of date, city, metric and
methodology version, that is an update of existing rows, not a new set — the
archive shows the settled number, and the run history records that it was revised.

The two tiers come from different models, which is the honest reason for the
badge: the baseline is built from ERA5, so a final-tier anomaly compares like with
like, and a provisional one does not.

## 7. Date handling

Each city is analysed over **its own local calendar day**, resolved in its own
IANA timezone (`America/New_York`, not a fixed −05:00 offset). A fixed offset is
wrong twice a year, and wrong in a way that silently mixes 23- and 25-hour days
into a daily maximum.

A single board must be internally comparable, so the analysis date is the most
recent local date that is **complete in every city in the registry** — the minimum
across roughly seven hours of longitude. A day still in progress anywhere is not
analysed, rather than analysed partially.

## 8. Explanations

Each published event gets a short written explanation. This is the one part of the
system that involves a language model, and it is bounded on every axis:

- **One agent, five tools.** A single investigation agent with read-only access to
  the event's own calculation, the city's baseline for that day, the city's recent
  history, the same day across other cities, and the registry entry. There is no
  multi-agent system, because nothing here needs one.
- **Grounded output only.** Output is validated against a Pydantic schema and then
  against guards that reject any numeric claim not present in the supplied
  evidence, any causal claim, and any record claim. A rejected explanation falls
  back to the deterministic template.
- **No LLM required.** With no API key configured, every explanation is generated
  from a deterministic template over the same evidence, and the interface labels
  it "Deterministic template". The application is fully functional this way.
- **Bounded cost.** Per-run call and token caps plus a configurable monthly USD
  ceiling; explanations are generated once during the scheduled pipeline and
  cached, so a page view never invokes a model.

Guard accuracy is measured, not asserted — see the evaluation page and
`evals/README.md`.

## 9. Publication

A run writes as it goes but nothing is visible until the last statement of the
transaction sets `published = true`. The API only reads published, successful
runs. **A failed run therefore never replaces a good one**: a crash at step four
leaves the previous board serving unchanged, with its own timestamps intact, and
the failure recorded in the run history.

Reruns are idempotent. Event identity is `hash(date, city, metric,
methodology_version)`, so a rerun updates rows rather than inserting duplicates,
and the previous ranking for that date is replaced wholesale inside the same
transaction that publishes the new one.

## 10. Known limitations

Stated here because a portfolio piece that hides them is worth less than one that
does not.

- **Model data, not station data.** Reanalysis interpolates to a grid cell of
  order 10–30 km. It is spatially smoothed: it will understate a thunderstorm's
  peak rainfall and a canyon's peak gust. The city page prints the grid point
  actually used alongside the city's own coordinates.
- **No official record verification.** See the top of this document.
- **A 30-year reference period in a warming climate.** 1991–2020 is warmer than
  1961–1990, so a hot day is measured against a warm baseline and scores as less
  unusual than it would against an earlier period. This is the standard
  convention, and it makes the board conservative about heat.
- **50 cities is a sample, not a census.** The board is the most unusual weather
  *in the registry*, not in North America. See [`data/README.md`](../data/README.md).
- **Tail probabilities beyond about 1-in-450 are lower bounds**, flagged as such.
- **The provisional tier is a different model than the baseline.** Badged.
- **No spatial or temporal dependence modelling.** Neighbouring cities in one air
  mass are scored independently, and the one-event-per-city rule is a presentation
  fix for that, not a statistical one.
