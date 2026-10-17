# Indoor courts layer (`indoor`, 2026-10-17)

Indoor courts are invisible from above, so the satellite pipeline never finds them. `hoop_pipeline/indoor.py`
builds an indoor layer from four open sources, dedupes it, and says who can use each place. No imagery, no GPU.

## Results (final run, 2026-10-17)

**Inputs.**

* OSM: 1,685 indoor basketball features.
* Overture: 31,374 places kept out of about 624,000 US places in the categories read. The rest were dropped:
  526,532 with no court signal, 59,057 below confidence 0.5, 6,967 closed. Another 121 `basketball_court` places
  were dropped because they sit on an outdoor court.
* NCES: 100,384 public and 22,385 private schools, and 6,241 IPEDS colleges.
* Park department pages: 652 rows.

After dedupe there are **159,916 places**. 929 of them come from more than one source; 1,938 are `verified`.

**By source and access class.** A merged place counts under each of its sources.

| source | public | membership | unknown | school | places |
|---|---|---|---|---|---|
| OSM | 156 | 236 | 1,174 | 57 | 1,623 |
| Overture | 701 | 9,337 | 20,139 | 271 | 30,448 |
| park department pages | 261 | 143 | 154 | 0 | 558 |
| NCES public schools | | | | 99,986 | 99,986 |
| NCES private schools | | | | 22,034 | 22,034 |
| IPEDS colleges | | | | 6,225 | 6,225 |
| **all places** | **927** | **9,454** | **21,282** | **128,253** | **159,916** |

* Likelihood: 2,861 listed, 62,633 likely, 94,422 possible.
* Kind: 121,739 school gyms, 15,750 community centres, 8,566 gyms, 6,225 college gyms, 3,343 clubs, 1,777 rec
  centres, 869 sports centres, 823 basketball venues, 285 indoor pitches, 234 fitness centres, 200 sports halls,
  99 arenas.

**Already in the app.** 1,429 of the OSM indoor features are already in the national OSM layer that the app
loaded (`out/national/v3/app_ready`). That layer exports every OSM basketball feature, indoor ones included,
with `indoor = false`. These places are left out of the GeoJSON files so the app does not get a second pin.
`osm_indoor_flag_fixes.json` lists their app ids (`osm-way-...`) so the existing rows can be set to
`indoor = true` instead.

**Files.**

| file | features | content |
|---|---|---|
| `indoor_courts_listed.geojson` | 1,260 | a source says there is a court: OSM 191, park pages 315, Overture `basketball_court` 754; access: 150 public, 237 membership, 873 unknown |
| `indoor_courts.geojson` | 14,560 | listed + likely (no schools, no arenas): Overture likely 13,097 (YMCA, JCC, Boys & Girls Clubs, LA Fitness, Life Time, rec centres ...), Overture listed 754, park pages 518, OSM 191; access: 516 public, 9,329 membership, 4,715 unknown |
| `indoor_school_gyms.geojson` | 128,199 | school and college gyms (49,333 likely, 78,786 possible, 80 listed) |

**Verification.** C = indoor court there; N = not; U = cannot tell.

| subset | C | N | U | precision | 95% (Wilson) | source of labels |
|---|---|---|---|---|---|---|
| OSM listed | 20 | 0 | 0 | 100% | 84-100% | `review/labels.json` (sample, pages read) |
| park pages, `listed` (the page says indoor / gym) | 5 | 1 | 0 | 83% | 44-97% | indoor sample + indoor rows of the web sample |
| park pages, `likely` (rec-centre page, no cue) | 7 | 1 | 4 | 88% | 53-98% | same |
| OSM + park pages listed (recommended public set) | 25 | 1 | 0 | 96% | 81-99% | |
| Overture `basketball_court` (listed) | 17 | 6 | 11 | 74% | 54-88% | sample (2/1/1) + 30 random places judged by name, address and website (`review/overture_listed_check.json`: 15/5/10) |
| Overture likely (gyms, YMCA, clubs, rec centres) | 8 | 1 | 31 | 89% | 57-98% | sample (4/0/12) + 24 branch pages fetched (`likely_check_labels.json`: 4/1/19) |
| Overture community centres, `possible` | 0 | 3 | 1 | 0% | | sample |
| whole indoor sample (52 places) | 34 | 4 | 14 | 89.5% | 76-96% | `review/scores.json` |

The errors in the Overture `basketball_court` places: a golf-club fitting studio, a day camp, a place named
"Facebook" at an arena's address, a dodgeball league, and an outdoor street court whose name the outdoor rule
missed ("4th St Basketball Court"). The unknowns are training academies and leagues whose venue is not stated.
College arenas (Mol Arena, Jack Stephens Center) are indoor courts but are not open to the public.

**Loading** (dry runs against the Supabase database, rolled back; nothing was loaded):

| command | read | would insert | skipped (50 m) |
|---|---|---|---|
| `db_load load out/indoor/indoor_courts_listed.geojson --layer indoor --only-source osm --only-source web --skip-near 50 --dry-run` (recommended) | 506 | 506 | 0 |
| `db_load load out/indoor/indoor_courts_listed.geojson --layer indoor --skip-near 50 --dry-run` | 1,260 | 1,260 | 0 |
| `db_load load out/indoor/indoor_courts.geojson --layer indoor --skip-near 50 --dry-run` | 14,560 | 14,560 | 0 |
| `db_load load out/indoor/indoor_school_gyms.geojson --no-db` (validation only; never load as public) | 128,199 valid | | |

Nothing is skipped as a near-duplicate because the database has no indoor rows yet; `--skip-near` only compares
rows with the same indoor flag. The 1,429 OSM features already loaded are why the flag fixes matter.

## Sources and licenses

| source | what is taken | license / attribution |
|---|---|---|
| OSM indoor basketball features, extract of 2026-10-15 (`data/osm_us_20261015/indoor_us.parquet`) | sports centres, sports halls, fitness centres, community centres and indoor / covered pitches tagged `sport=basketball` | **ODbL 1.0**, "© OpenStreetMap contributors"; a published database derived from it stays ODbL |
| Overture Maps places, release 2026-09-23.1 (`data/web/overture_places_us.parquet`, from `websources overture-extract`) | category `basketball_court`, `basketball_stadium`, community / civic / recreation centres, and gyms, sports clubs and youth organisations whose name points to a court | Re-checked 2026-10-17 at docs.overturemaps.org/attribution: Places has no theme-wide license line; each source carries its own: Meta, Microsoft, BrightQuery and the other data partners "Available under CDLA Permissive 2.0"; Foursquare "Copyright 2024 Foursquare Labs, Inc. All rights reserved. Available under Apache 2.0" with its NOTICE file (opensource.foursquare.com/places-notice-txt); AllThePlaces CC0 1.0. Places contain no OSM data. Overture asks for the citation "Overture Maps Foundation, overturemaps.org". CDLA-Permissive-2.0 allows use, modification and sharing of the data; when the data itself is shared, the license text must be included. Apache-2.0 rows: keep the NOTICE. Each row's `licenses` are in the extract. |
| NCES EDGE school geocodes: public 2024-25, private 2023-24 (PSS), postsecondary 2025-26 (IPEDS) | every school and college point | **Public domain** |
| Park department pages (`data/web/located_pages.parquet`, from `websources build`) | pages that state an indoor basketball court, a gym with basketball, or open-gym / drop-in basketball | facts only, with the source URL and fetch date (see `docs/web_sources.md`) |

## Rules

* **Overture categories.** `basketball_court` is taken as "the source lists basketball" (`likelihood=listed`)
  unless the name says it is an outdoor park court ("Tompkins Square Park", "Forest Park Courts") or the point
  sits within 40 m of an outdoor OSM court or a satellite candidate with conf >= 0.3: those are outdoor courts and
  are dropped. A gym, fitness, sports-club, youth or community category alone is **not** taken as a court: it
  needs a name that points to one (LA Fitness, Life Time, 24 Hour Fitness Sport / Super-Sport, YMCA, YWCA, JCC,
  Boys & Girls Club, fieldhouse, sportsplex, "basketball", "hoops", athletic club, sports center, recreation
  center, gymnasium, Police Athletic League) and is then `likely`. A bare "court" in a name is not enough (the
  pilot sample found "Durham County Teen Court"). Studios and single-purpose venues
  (Planet Fitness, CrossFit, yoga, martial arts, gymnastics, swimming, tennis, climbing, dance, skating ...), and
  offices, Head Start, child care, camps, clinics and stores are dropped. A community / civic centre with no court
  word is kept as `possible` (not in the default GeoJSON). Places with Overture confidence below 0.5, or
  permanently or temporarily closed, are dropped.
* **Schools.** Every NCES / IPEDS point is a `school_gym` or `college_gym`: "school gym (likely, not open to the
  public)". High, middle, junior-high, secondary, K-12 and academy names, and all colleges, are `likely`;
  elementary, primary and pre-K names are `possible`. Schools are **never** in the public default file.
* **Park department pages.** An indoor / gym / open-gym statement on a park department page is `listed`; a court
  on a page titled "... Recreation Center" with no indoor or outdoor cue is `likely`.
* **Access** (only when the data says so, otherwise `unknown`):
  * `school`: NCES / IPEDS, OSM `operator:type=school|university|college`, or (when nothing else says) a name
    with high / middle / elementary school, school, college, university, HS or ISD ("High school gym"). Campus
    arenas whose name does not say so (Pauley Pavilion, Cameron Indoor Stadium) stay `unknown`; they are not
    open courts either, and arenas are left out of the default file.
  * `membership`: OSM `access=private|customers|members|permit|no`; gym categories and fee-based clubs (YMCA,
    YWCA, JCC, Boys & Girls Club, LA Fitness, Life Time, athletic / sports clubs).
  * `public`: OSM `access=yes|permissive|public` or `operator:type=public|government|municipal`; a name that says a
    government runs it (City / Town / Village of, Township, County Park(s) / Rec, Park District, Parks &
    Recreation, municipal, Police Athletic League / PAL); a park department web page (.gov / .us / parks /
    recreation host).
  * A bare "Community Center" or "Recreation Center" name stays `unknown`: many are run by churches, non-profits
    or HOAs.
* **Dedupe** (union-find over pairs within 150 m): same-named places (core-name overlap >= 0.5) merge within
  150 m; places within 40 m merge when they share a name token or one has no name; a school only merges with a
  same-named feature; two NCES points merge only when nearly identically named within 40 m. A merged place keeps
  the most specific access (school > membership / public > unknown; membership and public together fall back to
  the OSM or web value), the strongest likelihood, every source id (`source_ids`) and the first website.
* **Status:** `verified` when OSM maps it or a park department page lists the indoor court; `candidate` otherwise.

## Verification

`indoor sample` draws a few places per stratum (primary source x kind x likelihood, schools left out unless
`--include-schools`), fetches each place's own website through the polite fetcher (robots.txt honoured), and
records whether the page mentions basketball, a court or a gym. Each place was read and labelled:

* **C**: an indoor basketball court is there (OSM tags say so, the page says so, or it is a known gym / arena).
* **N**: no indoor court (an office, a food bank, an outdoor court, a program with no venue).
* **U**: cannot tell (chain home pages and YMCA pages often do not list amenities in their HTML).

Because most chain and YMCA home pages list no amenities in their HTML, a second, targeted check fetched the
branch page (not the home page) of 24 random Overture `likely` places (`review/likely_check.json`,
`likely_check_labels.json`): 4 pages list basketball or a gym with basketball (Highland County YMCA, West Essex
YMCA, LA Fitness Huntington Station, Sanibel Recreation Center), 1 lists amenities without basketball (a TruFit
club), and 19 could not be read (robots.txt refusals, bot challenges, dead links, amenities loaded by
JavaScript). So `likely` is mostly unverified by reading; where a branch page can be read it usually agrees.

A pilot round before the final run found: "Durham County Teen Court" taken as a court (bare "court" no longer
counts); a food bank and a school learning center classed as community centres (still `possible`, so not in the
default file); "County" making a place public (now only "County Park(s) / Rec"); campus arenas and a
"High school gym" left `unknown` (now `school` from the name); a park page whose basketball court sits under an
"Outdoor" heading taken as indoor (now outdoor and dropped); an outdoor court called indoor because the page also
advertised pick-up games (no longer).

## Outputs (`out/indoor/`, git-ignored)

* `indoor_courts.parquet` (GeoParquet, points): `id`, `name`, `kind` (sports_centre, sports_hall, fitness_centre,
  indoor_pitch, rec_center, community_center, gym, club, basketball_venue, arena, school_gym, college_gym),
  `likelihood` (listed / likely / possible), `access` (public / membership / school / unknown), `status`, `state`,
  `region`, `site`, `address`, `primary_source`, `sources`, `n_sources`, `n_records`, `source_ids`, `osm_ref`,
  `url`, `evidence`, `lon`, `lat`, `updated`, `indoor` (always true).
* `indoor_courts.geojson`: the default layer for the app: access public, membership or unknown, likelihood
  listed or likely, no arenas, no schools, nothing the app's OSM layer already has.
* `indoor_courts_listed.geojson`: its `listed` subset (a source says there is a court).
* `indoor_school_gyms.geojson`: school and college gyms, kept apart so no "public" default can include them.
* `osm_indoor_flag_fixes.json`: app ids of OSM features the national layer already shows as outdoor that OSM tags
  as indoor (`build --app-osm` points at the app's national GeoJSON; default `out/national/v3/app_ready/*.geojson`).
  `in_app_osm` marks them in the parquet.
* Both GeoJSON files use the app schema (`id, name, source, status, confidence, hoops, surface, lit, address,
  region, osm_id, updated`) followed by `site, indoor, state, access, kind, likelihood, provenance`. `source` is
  the primary source: `osm`, `overture`, `web` or `nces`, all allowed by the `courts_source_check` constraint and
  `db_load.SOURCES` (`export_app.SOURCES`, the older file validator, knows only the first). `db_load` keeps
  `access, kind, likelihood, provenance` in `courts.extra`. `indoor.validate` checks the files.
* `summary.json`: counts by source, access, kind, likelihood, status and state.
* `review/`: the verification sample (`sample.txt`, `sample.json`), labels and scores.

```bash
python -m hoop_pipeline.websources overture-extract        # once (also used by websources)
python -m hoop_pipeline.websources build                   # optional: park pages for the web source
python -m hoop_pipeline.indoor build
python -m hoop_pipeline.indoor sample --per-stratum 4      # fetches each sampled place's website politely
python -m hoop_pipeline.indoor score --labels out/indoor/review/labels.json
python -m unittest tests.test_indoor -v
python -m hoop_pipeline.db_load load out/indoor/indoor_courts_listed.geojson --layer indoor --only-source osm --only-source web --skip-near 50 --dry-run
```
