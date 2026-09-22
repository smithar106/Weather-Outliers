# City registry

`cities.json` is the only source of the city list. It is version-controlled, and
changing it is a reviewable commit rather than a database edit — which is the point:
the set of cities determines what "most unusual weather in North America" can
possibly mean, so it should be as auditable as the statistics.

**Registry version 2026.09.2 — 50 cities: 30 United States, 10 Canada, 10 Mexico.**

```jsonc
{
  "schema_version": 1,
  "registry_version": "2026.09.2",     // bumped on every change; recorded on each run
  "population_sources": { "US": "...", "CA": "...", "MX": "..." },
  "cities": [
    {
      "id": "us-new-york-ny",          // stable, never reused; appears in URLs and event ids
      "name": "New York",
      "admin": "New York",             // state / province / estado
      "country": "US",
      "region": "Northeast",
      "latitude": 40.7128,
      "longitude": -74.0060,
      "timezone": "America/New_York",  // IANA zone name, never a fixed offset
      "population": 8804190
    }
  ]
}
```

`pipeline seed-cities` syncs this file into the database. Ids are stable because
they appear in public URLs and are hashed into event ids; a renamed city keeps its
id.

## Selection criteria

Applied in this order.

1. **Three countries.** North America is the stated scope, so Canada and Mexico are
   not optional. A US-only registry would have made the project's headline false.
2. **Climate diversity over population ranking.** The registry is *not* the 50
   largest cities. Taking the top 50 by population would have produced a dense,
   climatically repetitive set — four Texas metros, no Arctic, no Atlantic
   maritime, no high desert — and a board full of the same air mass. Every
   distinctive North American climate regime the coverage allows is represented
   instead: Pacific marine (Vancouver, Seattle, San Francisco), continental
   interior (Winnipeg, Fargo, Regina), Arctic (Iqaluit, Anchorage), subtropical
   maritime (Miami, New Orleans, Veracruz), high desert (Albuquerque, Salt Lake
   City, Hermosillo), tropical (Honolulu, Cancún, Mérida), high-altitude tropical
   (Mexico City, Guadalajara), and Atlantic maritime (St. John's, Halifax).
3. **Timezone spread, deliberately awkward.** 26 distinct IANA zones across 50
   cities, chosen to include the cases that break naive date handling:
   `America/St_Johns` (−03:30), `America/Regina` and `America/Phoenix` (no DST),
   `Pacific/Honolulu` (no DST, far west), `America/Ciudad_Juarez` and
   `America/Hermosillo` (Mexican zones whose DST rules changed in 2022–2023).
   If per-city local-date resolution is wrong, this registry will expose it.
4. **Longitudinal span.** Honolulu to St. John's is about seven hours, which is
   what forces the board to analyse the most recent date complete *everywhere*
   rather than a single notion of "yesterday".
5. **Some small cities on purpose.** Iqaluit (7,429) and Billings (117,116) are
   here because unusual weather is not proportional to population, and because a
   registry that only contains large cities cannot distinguish "unusual weather"
   from "weather where lots of people live".

### The cities

| Country | Cities |
| --- | --- |
| **US** (30) | New York, Los Angeles, Chicago, Houston, Phoenix, Philadelphia, San Antonio, Dallas, Indianapolis, San Francisco, Seattle, Denver, Nashville, Oklahoma City, Boston, Las Vegas, Detroit, Louisville, Albuquerque, Kansas City, Atlanta, Miami, Minneapolis, New Orleans, Honolulu, Anchorage, Buffalo, Salt Lake City, Fargo, Billings |
| **CA** (10) | Toronto, Montréal, Calgary, Ottawa, Winnipeg, Vancouver, Halifax, Regina, St. John's, Iqaluit |
| **MX** (10) | Mexico City, Tijuana, Ciudad Juárez, Guadalajara, Monterrey, Mérida, Hermosillo, Cancún, Veracruz, Mazatlán |

## Coordinates

**City-centre reference points, not airport or weather-station coordinates.** This
matters and is disclosed on every city page: the provider returns the value at the
nearest grid point of its model, and the page prints that grid point next to these
coordinates so the offset is visible. A reader comparing against their local
airport's official reading is comparing two different things, and the interface
says so rather than letting them assume otherwise.

## Population figures

Population is displayed as context and never enters any calculation — it does not
weight, filter, rank or break ties. Sources differ by country and are attributed
per country in the file and on each city page:

| Country | Source | Unit |
| --- | --- | --- |
| US | U.S. Census Bureau, 2020 Decennial Census | incorporated place |
| CA | Statistics Canada, 2021 Census of Population | census subdivision (city proper) |
| MX | INEGI, Censo de Población y Vivienda 2020 | municipio |

These units are **not comparable across countries**, which is why the attribution
travels with the number instead of being flattened into a single "population"
column. A Mexican *municipio* can include substantial rural area; a US
incorporated place excludes suburbs that a Canadian census subdivision might
include. Ranking cities by these figures across countries would be meaningless,
so the application never does.

## Known limitations

- **This is a sample, not a census.** The board is the most unusual weather *in
  this registry*, never "in North America". The interface uses the former phrasing.
  Genuinely extraordinary weather in a city that is not listed here will not
  appear, and the site does not imply otherwise.
- **Coverage is thin where population is thin.** Nunavut has one entry, northern
  Mexico three, and there is nothing in the Canadian territories west of Iqaluit.
  Those are exactly the regions where the largest absolute anomalies occur.
- **Reanalysis grid resolution is the real spatial limit.** Adding two nearby
  cities would not add two independent measurements; they may share grid cells.
- **A wider registry costs a proportional baseline build.** Each additional city is
  about 391 weighted Open-Meteo calls to bootstrap, against a free-tier allowance of
  10,000/day. See [`docs/data-sources.md`](../docs/data-sources.md) before adding
  many at once.

## Adding or changing a city

1. Add the entry to `cities.json`, keeping the array in its existing order.
2. Bump `registry_version` — it is recorded on every pipeline run, so a board can
   be traced to the registry that produced it.
3. Use an IANA zone name. Never a fixed UTC offset, and never an abbreviation like
   `EST`.
4. Cite the population source if it is a country not already listed.
5. Run `pipeline seed-cities`, then `pipeline build-baselines` — the new city has no
   climatology until you do, and until then it is correctly excluded from rankings
   rather than silently scored against nothing.
