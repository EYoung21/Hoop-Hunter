# Courts from park web pages and official lists (`websources`, 2026-10-17)

The satellite detector misses courts under trees, faded courts, and courts in states where the verifier is
unsure. Park departments already list their amenities on public web pages ("2 lighted basketball courts", an
amenity icon "Basketball", an amenity chart). `hoop_pipeline/websources.py` finds those pages at scale, reads
them with rules (no model, no GPU), places each park on a map, and checks it against OSM courts and the national
satellite candidates. The result is extra evidence for detected candidates, and a list of parks whose page lists
an outdoor court that nothing has found yet: targets for a later low-threshold re-detection.

## Results (final run, 2026-10-17)

**Sources crawled.**

* Seeds: 100,698 unique park web addresses (96,683 park-specific) on 55,298 hosts, from Overture places
  (94,497 rows), OSM (24,598) and Wikidata (1,732).
* Expansion added 45,132 sibling pages: 35,659 from the sitemaps of 1,007 hosts, 6,984 from directory pages, and
  2,489 from 21 city and county park directories found by hand (`web_seeds.json`; 22 more big-city sites are
  listed there as `not_usable`, with the reason).
* Crawl: 131,686 URLs queued and 128,165 attempted on 53,831 hosts. 75,737 pages came back (16.0 GB, cached
  locally) from 32,028 hosts. The rest were not fetched:
  * 23,216 refused by robots.txt. This counts hosts whose robots.txt was unreachable (10,215), answered 403
    (1,549) or answered with an AWS WAF 202 (1,195), all treated as "disallow everything";
  * 18,762 HTTP errors, mostly dead Overture links;
  * 8,060 on blocked sites;
  * 1,615 behind bot challenges on 943 hosts (Chicago Park District after 115 pages);
  * 331 on hosts that ask for more than 30 s between requests.

**Reading and placing.** The rules read 75,641 pages. 8,174 pages mention a court, 943 only mention programs and
242 mention open gym. Amenity charts and park lists gave another 1,289 rows. 31,454 page rows were placed on
25,629 parks. Most of the rest were seeded pages that name some other place than their seed (28,998): Overture's
park "websites" are often a city home page or a league's site.

**Parks with courts: 4,227** (`parks_with_courts.parquet`)

* 4,095 are on a park polygon (OSM 4,009, official 64, PAD-US 22), and 867 state a count (1,394 courts in all).
* Indoor or outdoor: 536 say outdoor, 38 say both, 143 only mention indoor courts, and 3,510 do not say.
* They come from 1,171 hosts. The largest are Phoenix, Montgomery Parks, Minneapolis, Albuquerque, Columbus,
  Boston and Seattle.

| status | parks | meaning |
|---|---|---|
| `confirmed_osm` | 3,026 (72%) | an outdoor OSM basketball feature in the park |
| `confirmed_detected` | 477 | no OSM court; a satellite candidate with detection score >= 0.3 in the park |
| `weak_detection` | 74 | only candidates below 0.3 |
| `missing` | 507 | nothing mapped or detected (80 of them say "outdoor") |
| `indoor_only` | 143 | not matched |

On the satellite side, `candidate_evidence.parquet` has 1,270 rows: 1,256 national candidates inside parks whose
page lists a court. 833 of them score >= 0.3, and 766 of those 833 are not exported to the app today, so the park
page is independent evidence for them. `short_of_listed`: 268 parks list more courts than OSM and the detector
found together. `parks_missing_detection.parquet` holds the 507 missing parks with their search zones (polygon
+15 m: 217; small polygon +60 m: 192; 150 m circle: 98), to target a low-threshold re-detection.

By state (all states with parks; `by_state.csv`, `by_city.csv` have every row):

| state | pages placed | parks w/ courts | polygon | OSM | detected | weak | missing | indoor only |
|---|---|---|---|---|---|---|---|---|
| CA | 2,632 | 486 | 467 | 348 | 62 | 3 | 53 | 20 |
| TX | 1,892 | 341 | 326 | 205 | 65 | 11 | 51 | 9 |
| IL | 1,555 | 330 | 328 | 261 | 22 | 4 | 27 | 16 |
| WA | 1,683 | 273 | 268 | 208 | 16 | 1 | 32 | 16 |
| AZ | 648 | 227 | 227 | 198 | 3 | 1 | 13 | 12 |
| MD | 743 | 225 | 220 | 168 | 25 | 3 | 27 | 2 |
| FL | 1,370 | 221 | 210 | 148 | 33 | 2 | 26 | 12 |
| OH | 1,340 | 183 | 181 | 146 | 17 | 3 | 15 | 2 |
| MN | 813 | 170 | 167 | 135 | 8 | 3 | 21 | 3 |
| MO | 791 | 144 | 143 | 101 | 12 | 4 | 18 | 9 |
| CO | 702 | 134 | 128 | 90 | 13 | 0 | 26 | 5 |
| MA | 627 | 132 | 129 | 94 | 14 | 2 | 20 | 2 |
| MI | 876 | 122 | 117 | 96 | 7 | 1 | 15 | 3 |
| NY | 1,081 | 115 | 110 | 90 | 4 | 3 | 16 | 2 |
| OR | 705 | 115 | 114 | 91 | 12 | 3 | 8 | 1 |
| IN | 524 | 111 | 105 | 85 | 14 | 1 | 10 | 1 |
| NM | 191 | 80 | 80 | 55 | 14 | 2 | 8 | 1 |
| WI | 640 | 74 | 70 | 50 | 12 | 4 | 8 | 0 |
| IA | 377 | 72 | 72 | 49 | 13 | 2 | 8 | 0 |
| PA | 692 | 63 | 58 | 41 | 10 | 0 | 10 | 2 |
| NC | 592 | 62 | 60 | 39 | 9 | 0 | 10 | 4 |
| VA | 514 | 54 | 51 | 39 | 6 | 1 | 6 | 2 |
| NJ | 314 | 50 | 47 | 40 | 3 | 0 | 7 | 0 |
| KS | 288 | 48 | 47 | 29 | 12 | 0 | 6 | 1 |
| TN | 322 | 47 | 44 | 23 | 11 | 3 | 8 | 2 |
| OK | 204 | 44 | 42 | 16 | 19 | 4 | 4 | 1 |
| SD | 135 | 32 | 32 | 25 | 5 | 0 | 2 | 0 |
| MT | 173 | 28 | 27 | 18 | 3 | 2 | 5 | 0 |
| UT | 276 | 27 | 27 | 17 | 0 | 1 | 6 | 3 |
| SC | 290 | 25 | 24 | 17 | 5 | 1 | 2 | 0 |
| KY | 186 | 21 | 19 | 11 | 5 | 0 | 3 | 2 |
| AL | 224 | 20 | 15 | 3 | 0 | 4 | 10 | 3 |
| NV | 158 | 19 | 19 | 10 | 5 | 0 | 2 | 2 |
| LA | 272 | 16 | 15 | 5 | 6 | 1 | 2 | 2 |
| CT | 237 | 16 | 14 | 13 | 1 | 0 | 2 | 0 |
| GA | 448 | 14 | 11 | 8 | 1 | 1 | 4 | 0 |
| NH | 212 | 14 | 14 | 11 | 1 | 1 | 1 | 0 |
| ND | 119 | 14 | 14 | 9 | 3 | 1 | 1 | 0 |
| AR | 146 | 10 | 10 | 6 | 1 | 0 | 1 | 2 |
| ID | 184 | 10 | 9 | 6 | 3 | 0 | 1 | 0 |
| NE | 113 | 9 | 9 | 4 | 1 | 0 | 4 | 0 |
| WV | 100 | 7 | 6 | 4 | 0 | 1 | 2 | 0 |
| RI, DC, ME, VT, DE, MS, WY | 525 | 22 | 19 | 14 | 1 | 0 | 6 | 1 |

Top cities by parks with courts: Phoenix 139, Boston 83, Minneapolis 73, Albuquerque 72, Columbus 66, Seattle 64,
San Antonio 54, Fort Worth 53, Portland 53, Indianapolis 47.

**Extraction precision** (`out/web/review/labels_final.json`, `scores.json`). The final stratified sample
(`sample --per-stratum 8 --seed 7`) has 133 parks in 19 strata (match status x evidence kind). Each one was read:

| | C | N | U | precision (C / (C+N)) | 95% interval |
|---|---|---|---|---|---|
| whole sample | 124 | 4 | 5 | 96.9% | 92.2-98.8% (Wilson) |
| all parks, weighted by stratum size | | | | 98.3% | 79.9-98.1% (Jeffreys, stratum-weighted; wide because each stratum has <= 8 rows) |
| web layer strata (missing, weak, confirmed_detected) | 74 | 4 | 4 | 94.9% (weighted 93.2%) | 87.5-98.0% (Wilson) |
| `confirmed_osm` strata | 40 | 0 | 0 | 100% | 91.2-100% |
| by evidence kind: amenity chart / list page / park list / text / list item | 24/12/24/36/28 | 0/0/1/1/2 | | 100 / 100 / 96 / 97 / 93% | |

The 4 errors left: a site search-results page (an OSM `website` tag points to a search URL); a park-list row of
"East Lake Center Park" placed on "East Lake Park"; a store selling hoops; a department home page whose name ("East
Ridge") is the prefix of a splash-pad seed. The 5 "cannot tell" rows: two placements that may be a neighbouring
park, a history paragraph, courts along a beach road, and a sportsplex whose courts are implied.

Before the final sample there were three rounds of reading, with rule fixes after each. Round 1 found 16 errors
in 136 rows:

* icons in "You May Also Like" blocks;
* "Basketball:" with "No" on the next line;
* sports-offered lists;
* playground toy hoops;
* rows about one facility assigned to the park line above them;
* "Chimborazo Playground" placed on "Chimborazo Park";
* a West Hartford list placed in Holyoke, because one stray seed stretched the host's area;
* vacation rentals and stores;
* department home pages.

Rounds 2 and 3 found 7 and 6 errors. These included "Fitness Loop" in a title making a park look indoor, a pond
seed matched to a community centre of the same name, a city park matched to a state recreation area, history
paragraphs, "taller than a basketball hoop", and "Basketball Courts (2) - 33 Sheep Hill Drive" read as 33 courts.
Each fix has a test in `tests/test_websources.py`. Labels are one reader's, made by the assistant, not gold.

**The app layer** (`out/web/web_courts.geojson`, `export`): 958 courts the app does not show yet:

* 397 at a detected candidate, where the page and the detector agree;
* 491 missing and 70 weak_detection parks, as a park point.

Same-named twins within 300 m are dropped (26). `web_courts_public.geojson` (765) keeps the 397, plus the park
points of parks small enough that the court is within 150 m (368). The other 193 sit in parks up to a few km
across: they are re-detection targets, not pins.

**Loading** (dry runs against the Supabase database, rolled back; nothing was loaded):

| command | read | would insert | skipped within 30 m of a published court |
|---|---|---|---|
| `db_load load out/web/web_courts_public.geojson --layer web --skip-near 30 --dry-run` (recommended) | 765 | 727 | 38 |
| `db_load load out/web/web_courts.geojson --layer web --skip-near 30 --dry-run` | 958 | 920 | 38 |

Every row is `status=candidate`, `source=web`, `indoor=false`, layer `web`. The web fields (`listed_courts`,
`evidence`, `source_url`, `position`, `position_uncertainty_m`, `candidate_id`, ...) go to `courts.extra`.

## Rules of the road

* **Only public pages.** No logins, no form submissions, no search boxes, no CAPTCHA or bot challenges. A challenge
  page (Cloudflare "Just a moment..." / "Attention Required", Incapsula, PerimeterX, AWS WAF, Sucuri, "are you a
  robot", "verify you are human", or a page answered with HTTP 202, which is how AWS WAF serves its challenge)
  stops the whole host, and the host is not retried on later runs (Chicago Park District is one). A plain
  reCAPTCHA on a contact form is not a challenge (an early version of the check counted it; the 108 hosts that
  answered HTTP 200 were retried after the fix). No cookies are kept.
* **robots.txt is honoured** for every host, matched per RFC 9309 with `*` and `$` wildcards and longest-match
  precedence (Python's `urllib.robotparser` does neither, so `websources.Robots` does it). Our product token is
  `HoopHunterResearch`; a group for it wins over `*`. A robots.txt that answers 401/403, 429, 5xx, a non-200
  success such as 202 (an AWS WAF challenge), or cannot be reached is treated as "disallow everything"; other 4xx
  (404) mean no rules (RFC 9309). Hosts whose robots.txt was unreachable get one retry on a later run.
* **Crawl-delay is honoured.** A host asking for more than 30 s between requests is skipped.
* **Rate.** At least 2 s (randomised up to 2.5 s) between requests to one host, one request at a time per host;
  hosts are fetched in parallel. Five 401/403 answers in a row, or a 429/503, stop or pause the host.
* **User-Agent** `HoopHunterResearch/0.1 (contact: github.com/EYoung21/Hoop-Hunter)` on every request.
* **Never fetched:** Google (Maps, Business Profile sites), Facebook and other social sites, Yelp, TripAdvisor,
  Foursquare, and third-party directories and real-estate sites (`BLOCKED_SITES`; their terms forbid automated
  collection and they are not the park's own page), tourism portals, and parked / for-sale domains (expired park
  websites). Federal land agencies (NPS, USFS, BLM, FWS, recreation.gov) are skipped because they have
  essentially no basketball courts, and expansion of state wildlife / natural-resource / tourism sites (and
  mass.gov, whose "locations" are mostly offices) stops at 150 pages per host. Hosts whose sitemap expansion
  turned out to be wedding venues, real estate, local businesses, lodges, swim schools or nature preserves were
  added to `BLOCKED_SITES` after the expansion and their pages are not read.
* **What is kept.** Facts (this park lists N basketball courts, indoor or outdoor), a short evidence snippet
  (at most 200 characters around the word "basketball"), the source URL and the fetch date. Page bodies are cached
  gzip-compressed in `data/web/cache/` (git-ignored) so the rules can be re-run offline; they are not republished.
* **Nobody is contacted.**

## Pipeline

```bash
python -m hoop_pipeline.websources osm-seeds          # Overpass, per state: OSM parks / rec grounds / sports and community centres with website|contact:website|url
python -m hoop_pipeline.websources overture-extract   # Overture places 2026-09-23.1 from the public S3 bucket (pyarrow, no DuckDB): parks, sport, community centres + websites
python -m hoop_pipeline.websources wikidata-seeds     # Wikidata US parks with an official website (P856); CC0
python -m hoop_pipeline.websources seeds              # one URL list; URLs shared by >= 3 parks (or a site root shared by 2) are "generic" department pages
python -m hoop_pipeline.websources crawl --workers 96 # polite fetch (resumable; data/web/fetch_log.jsonl, robots.jsonl)
python -m hoop_pipeline.websources expand             # sibling park pages per host: sitemaps + directory pages, and web_seeds.json directories
python -m hoop_pipeline.websources crawl --workers 96 # fetch the new pages
python -m hoop_pipeline.websources extract            # rules -> data/web/pages.parquet (resumable chunks in data/web/extract_parts/)
python -m hoop_pipeline.websources build              # locate -> out/web/parks_with_courts.parquet, data/web/located_pages.parquet
python -m hoop_pipeline.websources match              # -> candidate_evidence.parquet, parks_missing_detection.parquet, summary.json, by_state.csv, by_city.csv
python -m hoop_pipeline.websources export             # app-schema layer -> out/web/web_courts.geojson, web_courts_public.geojson
python -m hoop_pipeline.websources crawl-stats        # outcomes per URL / host / seed source -> out/web/crawl_stats.json
python -m hoop_pipeline.websources sample             # stratified spot-check sheet -> out/web/review/sample.txt
python -m hoop_pipeline.websources score --labels out/web/review/labels.json
python -m unittest tests.test_websources -v
```

### Finding park pages

1. **Seeds.** Park web addresses from three open sources:
   * OSM `leisure=park|recreation_ground|playground|sports_centre` and `amenity=community_centre` with
     `website`, `contact:website` or `url` (Overpass, one query per state; ODbL).
   * Overture places in categories park, playground, state park, community / civic centre, sports complex,
     basketball court, with their `websites` (CDLA-Permissive-2.0; Apache-2.0 for Foursquare rows).
   * Wikidata parks with an official website (CC0).
2. **Generic pages.** A URL shared by three or more differently named parks (or a site root shared by two) is a
   department page, not a park page: it is not used as evidence for any one park.
3. **Expansion (shared platform structure).** Many departments run on CivicPlus (`/Facilities/Facility/Details/
   <Name>-<id>`), Granicus/OpenCities, Drupal or WordPress park finders, where every park page sits in the same
   directory. For every host with at least two park pages that fetched fine and share a directory, `expand` reads
   the host's sitemaps (robots.txt `Sitemap:` lines, `/sitemap.xml`, sitemap indexes) and the directory page
   itself, and queues every sibling page. This recovers the parks OSM and Overture never linked, and replaces the
   many stale seed URLs (Overture's park websites are often old addresses that now return 404).
4. **Registry** (`pipeline/web_seeds.json`): park directories of large cities and counties whose seed URLs are all
   dead, found by hand: start URLs, a regex for park-page links, an optional regex for listing pages, and a bbox
   for name matching.

### Reading a page (`extract`)

* HTML to text lines with the standard-library parser. `<nav>`, `<header>`, `<footer>`, scripts, form controls
  (select, option, button, label) and elements whose class or id says nav / menu / breadcrumb / footer / cookie /
  share / filter / facet are skipped. `<form>` and `<template>` are **not** skipped: ASP.NET, DNN and OpenCities
  sites wrap the whole page in a form, and Vue sites keep amenity tabs in templates. Image `title` / `alt` text
  ("Icon of a basketball." becomes "basketball"), element titles, and class, id or `data-*` values containing
  "basketball" become lines, because many amenity lists are icons. WordPress taxonomy classes on the park post
  (`amenities-basketball-court`, `place_types-basketball-courts`, as on kcparks.org) count as a listed court.
  Table rows become one line, cells joined with ` | `. Only the document `<title>` is read (not `<svg><title>`
  icon labels).
* **Boilerplate.** Per host, a basketball line whose (previous, line, next) context appears on at least half of
  the host's pages (six pages or more) is a menu or banner and is dropped. A real amenity list differs from park
  to park, so its context differs and it survives.
* **Line classes** (only lines that mention basketball or "hoops"):
  * `court`: "basketball court(s) / hoop(s) / goal(s) / half court", "courts ... basketball".
  * `amenity_item`: a short line that is just "Basketball" (optionally "Courts", a count or "half court") with
    other amenities (playground, picnic, tennis, restrooms ...) within three lines; `icon`: the same from an icon.
  * `amenity_sentence`: "amenities include ... basketball", or a comma list of short amenity words.
  * `table`: a two-column row "Basketball | 2".
  * `program` (leagues, camps, clinics, classes, registration ...) and `open_gym` (open gym / drop-in / pick-up
    basketball) are not court evidence for the park; `open_gym` feeds the indoor layer. A "Basketball" item in a
    list of sports offered ("Fall Kickball / Winter Basketball", "Now registering") or under a "Programs /
    Leagues / Classes / Camps / Events" heading is a program, not an amenity.
  * `lone`: "Basketball" alone with no amenity list around it (usually a menu or a program category); ignored.
  * Flags: `negative` ("no basketball", "Basketball | No", "basketball court removed / replaced / converted",
    "abandoned / former basketball court") and `planned` ("future", "proposed", "will include", "master plan",
    "survey", "grant", ...) drop the line. "Hoops" next to spray / splash / water words is a splash-pad toy.
  * A page with two or more shop lines ("add to cart", "shop now", "checkout", "in stock") is a store selling hoops: no
    court. Prices alone do not count (park pages list rental fees).
* **Counts:** "two lighted basketball courts", "Basketball Courts (2)", "Basketball Court: 2", "Basketball /
  Full Courts: 1" (the count on the next line), "4 half-court basketball courts" and "Basketball Court | 1/2"
  (half courts kept apart), "6 basketball hoops" (hoops kept apart). Times ("Basketball: 9am-9pm"), ages and years
  are not counts.
* **Indoor / outdoor:** words near "basketball" (a 60-character window in long sentences): indoor / gym /
  gymnasium / fieldhouse is indoor; outdoor / outside / lighted / blacktop / asphalt / sport court is outdoor;
  both is `both`. With no cue, an "Outdoor" or "Indoor Amenities" section heading within eight lines above decides.
  A court line on a page titled "... Recreation Center / Community Center / Gym / YMCA" with no cue is
  `indoor_likely`; otherwise `unknown`. A gym schedule row ("Basketball | Monday | 6-8:30 p.m.") or "open gym /
  drop-in basketball" is `open_gym`: indoor when the line says gym, `indoor_likely` on a community-centre page.
  "Pick-up basketball" alone says nothing about indoors.
* **Pages about many parks:**
  * amenity charts: a table whose header has a basketball column and at least three rows (one record per row);
  * row tables: a park name in the first cell and "basketball" in another cell, in at least three rows;
  * park name lines each followed by an amenity line with basketball (at least three parks);
  * basketball location lists: a page titled "Basketball Courts / Basketball Court Locations / Parks with
    basketball" whose short lines name parks.
  These rows are placed by name (below); the page itself is not evidence for a single park.

### Placing a park (`build`)

Park polygons: OSM parks, playgrounds, recreation grounds, sports and community centres (from
`data/sites/sites_us.parquet`), PAD-US 4.1 public land, and the truth set's official park polygons (up to 20 km²).

* **Seeded page:** the seed's park, when the seed name and the page name agree (token overlap, one name inside
  the other, or near-identical spelling: "Fawn Ridge" / "Fawnridge"). Home pages get no exception: Overture's
  park "websites" are often a league's, a resort's or an expired domain's home page (one now sells sports
  betting). An OSM seed takes its own polygon; other seeds take the smallest park polygon containing the point
  (OSM first, then official, then PAD-US).
* Only park seeds (OSM park / recreation ground / playground, Overture park / playground / state park, Wikidata)
  and name-matched pages become `parks_with_courts` rows; community-centre, sports-centre and basketball-venue
  seeds go to the indoor layer. RV parks, campgrounds, resorts, apartments, HOAs, student housing, hotels and
  country clubs are dropped: their courts are for guests or residents.
* **Expanded or registry page, amenity-chart row:** the page's park name (h1, og:title or the first title
  segment) is matched to a named park polygon inside the host's area (the 5th-95th percentile box of the host's
  seed parks plus 0.15°, or the registry bbox). Ambiguous names (same name more than 1.5 km apart) are dropped. A
  one-word name may differ from the polygon's only by "park" / "playground" ("Krogh" = "Krogh Park", but "Youth
  Playground" is not "Youth Center").
* A seeded page whose seed lies outside that host's area is dropped (an Iowa park in Overture pointing at a Texas
  city's page of the same name). The host's area ignores stray seeds: points beyond 3 x the interquartile range
  (+0.5°) of the host's seeds do not stretch it (one Wisconsin seed on a Connecticut town's site had put a West
  Hartford court list in Holyoke, MA).
* Seeds named like a department ("East Ridge Parks and Rec", "... Park District", "Parks Department") are not
  parks. A seed whose name has a kind word the page lacks (pond, lake, pool, trail, skate, school, beach, ...)
  is another place ("Meadowbrook Pond" is not "Meadowbrook Community Center"), and a city park is not the state
  recreation area of the same name. On a site's home page the h1 (often a carousel headline) does not count as
  the page naming the park.
* The same park placed twice (a seed point next to its own polygon, or two same-named OSM polygons) is merged
  when the names agree within 1 km (point next to polygon) and the export keeps one row per name within 300 m.
* State from the Census state file; city from the Census 2023 places file (`cb_2023_us_place_500k`, public
  domain), downloaded once to `data/census/`.
* Several pages for one park are merged; the best evidence (a stated count, a `court` line) is kept and every URL
  is listed in `source_urls`.

### Matching (`match`)

The zone of a park is its polygon plus 15 m (plus 60 m for polygons under 2 ha, which are often only the play
area; polygons up to 20 km²), else a 150 m circle around the point.

* OSM basketball features from `data/osm_us_20261015/courts_us.parquet` (the 2026-10-15 extract).
* Satellite candidates from `out/national/v3/candidates_us.parquet` (889,817; none is within 40 m of an OSM court).
* Status of a park whose page lists an outdoor or unspecified court:
  * `confirmed_osm`: an outdoor OSM basketball feature in the zone.
  * `confirmed_detected`: no OSM court, but a candidate with detection score >= 0.3 (the better of the v2 score
    that produced the candidate and the v3 re-score; v3 scores 0 on many v2 detections).
  * `weak_detection`: only candidates below 0.3.
  * `missing`: nothing mapped or detected.
  * `indoor_only`: the page only mentions an indoor court; not matched.
* `short_of_listed`: the page states more courts than OSM courts plus candidates >= 0.3 found in the zone.

### App layer (`export`)

`out/web/web_courts.geojson` holds only the courts the app does not show yet, in the app schema (`id, name,
source, status, confidence, hoops, surface, lit, address, region, osm_id, updated`, then `site, indoor, state` and
the web fields), with `source=web`, `status=candidate`, `site=park`, `indoor=false`:

* skipped: `confirmed_osm` parks (the OSM layer has the court), `indoor_only` parks, and any park with an
  exported satellite candidate inside (the national layer shows it; the page is extra evidence for it in
  `candidate_evidence.parquet`);
* `confirmed_detected` with no exported candidate: the point is the best public candidate with detection score
  >= 0.3 (`position=detected_candidate`, `candidate_id`); the page says there is a court, the detector says where;
* `missing` and `weak_detection`: the court is somewhere in the park, so the point is the park polygon's
  representative point, or the park point, with `position=park` and `position_uncertainty_m` (1.5 x the radius
  of a circle of the polygon's area, at most 2 km; 150 m for a point).

Web fields: `listed_courts`, `half_courts`, `indoor_outdoor`, `park_status`, `position`, `position_uncertainty_m`,
`candidate_id`, `evidence` (the snippet), `source_url`, `mention_kind`, `polygon_source`, `seed_source`. `osm_id` is
the OSM park polygon when there is one (not a court). `db_load` keeps every non-app field in `courts.extra`.

### Spot check

`sample` draws up to N parks per stratum (match status x evidence kind: `text` = a court sentence or line,
`list` = an amenity list item or icon, `matrix`, `park_list`, `list_page`), and prints the evidence with the
three lines around it, the page title and URL, and the polygon the park was placed on. Each row was read and
labelled:

* **C**: the page says this park has a basketball court now, and the park was placed on the right polygon / point.
* **N**: wrong: no court (program, schedule, a "No" cell, planned or removed court, another park's court, a menu
  item), or placed on the wrong park.
* **U**: cannot tell from the page.

Precision is C / (C + N) with a Wilson 95% interval (`score`). Three pilot rounds (about 200 rows read) before
the final run changed the rules: icons need other amenities around them; "Basketball | No" cells are negative;
amenity-chart cells must say yes, a number or basketball; "related locations" blocks are skipped; open gym no
longer makes a park indoor; section headings decide indoor / outdoor; `<form>` / `<template>` pages are parsed;
sports-offered lists and "Programs" sections are programs; home pages need a name match; one-word seed names do
not match longer page names; resorts, RV parks and housing are dropped; shops selling hoops are dropped. The
pilots found these error sources mostly among Overture-seeded pages, whose "website" is often a league,
business or expired domain rather than the park's own page.

## Outputs (git-ignored)

* `out/web/parks_with_courts.parquet` (GeoParquet, park point): `park_id`, `park_name`, `state`, `city`, `lon`,
  `lat`, `polygon_wkb` + `polygon_source` (osm / padus / official) / `polygon_ref` / `polygon_name` /
  `polygon_area_m2`, `location_basis`, `court_count`, `half_court_count`, `indoor_court_count`, `indoor_outdoor`,
  `mention_kind`, `evidence` (snippet), `source_url`, `source_urls`, `n_pages`, `fetch_date`, `page_title`,
  `seed_source`, `seed_id`, `host`, and from `match`: `status`, `n_osm_courts`, `n_candidates*`,
  `best_candidate_conf`, `listed_courts`, `short_of_listed`.
* `out/web/candidate_evidence.parquet`: one row per (satellite candidate, park) for candidates inside a park whose
  page lists a basketball court: candidate scores, privacy class, export flag, park id / name / status, source
  URL, snippet, fetch date, `evidence_kind=park_web_page_lists_basketball`.
* `out/web/parks_missing_detection.parquet` (GeoParquet, the search zone polygon): parks whose page lists an
  outdoor or unspecified court with nothing mapped or detected; `outdoor_stated` marks the ones whose page says
  outdoor.
* `out/web/web_courts.geojson`: the app layer (above); `web_courts_public.geojson`: its subset placed on a
  detection or in a park small enough that the pin is within `--public-max-uncertainty` (150 m) of the court.
* `out/web/summary.json`, `by_state.csv`, `by_city.csv`, `crawl_stats.json`; `out/web/review/` (sample, labels,
  scores).
* `data/web/`: seeds, `overture_places_us.parquet` (+ `.json` with license and release), fetch and robots logs,
  page cache, `pages.parquet`, `located_pages.parquet`.

## Caveats

* **Coverage follows the seeds.** A park is only found if OSM, Overture or Wikidata links a page for it, its
  department's site exposes sibling pages (sitemap or directory), or the registry lists its directory. Big cities
  that refuse automated clients (New York, Los Angeles, San Jose, San Antonio, Houston, Atlanta, Baltimore,
  Louisville, Omaha, Henderson) or render their park lists in JavaScript (Miami-Dade, New Orleans, Bakersfield)
  are thin or absent. Florida, the largest gap in OSM, is covered better by the Florida inventory (FORI) in the
  truth set than by web pages.
* **Pages are not always current.** Some pages describe courts that were since removed or planned courts written
  as built. `negative` / `planned` catch the explicit cases only.
* **"Missing" is a target list, not a list of courts OSM lacks.** A park can be "missing" because the court is
  under trees, indoors without saying so, on a part of a large park outside the zone, or because the page is
  wrong. Each needs a look at imagery (or the low-threshold re-detection it is meant for).
* **Placement by name** can pick a same-named park elsewhere inside the host's area; ambiguous names are dropped,
  but a unique wrong match is possible ("East Lake Center Park" on "East Lake Park"). Seeded pages are placed by
  the seed's own coordinates.
* **Indoor / outdoor** is the weakest field: "inside the play area" reads as indoor (5 of 345 indoor calls rest on
  "inside" alone), and a recreation-centre page with an outdoor court and no cue is `indoor_likely`, so some
  outdoor courts sit in `indoor_only` (not matched, and offered to the indoor layer as `likely`).
* Some seeds are third-party pages (a watershed group, a city wiki, a personal park-review site, RecPlanet).
  Their facts are usually right, but they are not the department's own page.
* **Precision** comes from one reviewer's reading of a stratified sample (model-assistant labels, not gold).

## Commands as run (2026-10-17, one Windows PC, no GPU)

```bash
python -m hoop_pipeline.websources overture-extract          # 12.6 min, 624,051 US places
python -m hoop_pipeline.websources wikidata-seeds            # 1,842 parks
python -m hoop_pipeline.websources osm-seeds --sleep 30      # Overpass, one state at a time (slow: 502s from the public servers; VT retried)
python -m hoop_pipeline.websources seeds
python -m hoop_pipeline.websources crawl --workers 160 --max-per-host 1500   # resumable; ten runs in all
python -m hoop_pipeline.websources expand --max-per-template 1500            # 27 min for 1,058 hosts; then incremental (128 new hosts)
python -m hoop_pipeline.websources crawl --workers 160 --max-per-host 1500   # last run: 42,244 pages in 78 min
python -m hoop_pipeline.websources extract --workers 4      # 25 chunks of ~3,000 pages, about 1 min each
python -m hoop_pipeline.websources build
python -m hoop_pipeline.websources match
python -m hoop_pipeline.websources export
python -m hoop_pipeline.websources sample --per-stratum 8 --seed 7
python -m hoop_pipeline.websources score --labels out/web/review/labels.json
python -m hoop_pipeline.db_load load out/web/web_courts_public.geojson --layer web --skip-near 30 --dry-run
```

`extract` writes one part file per chunk of whole hosts (`data/web/extract_parts/`), keyed by the rule code and
the chunk's pages, so a crash (the first final run died with Windows "insufficient system resources" while
another job held the memory) resumes where it stopped; a fresh process pool per chunk releases worker memory.

Throughput notes: requests re-read the CA bundle for every new TLS connection (about 1 s of CPU each on Windows)
and copied a cookie jar shared by thousands of hosts on every request; the fetcher now shares one TLS context
(urllib3's own defaults, so the TLS handshake looks like a normal requests client), keeps no cookies and skips
netrc / proxy-registry lookups. That took the crawl from about 16 to 40-60 pages per second across hosts; the
per-host delay is unchanged.

## Official court lists: license re-check (2026-10-17)

Every disabled source in `truth_sources.json` was re-checked, the "found but not used" list in the README was
re-searched, and ArcGIS Online was searched for openly licensed US layers with a basketball field (7,523 park /
recreation / court items; 356 US items with an open license; 20 layers with a basketball field, several of them
in Canada or already used). The truth set stays an internal evaluation set: nothing from it is shipped in the
app.

| source | decision | why |
|---|---|---|
| `fl_fori`: Florida Outdoor Recreation Inventory (FDEP), 2,697 sites / 4,958 outdoor courts | **enabled** (`public_record_no_copyright`) | Item licenseInfo still says only "intended to be used for informational and planning purposes", but the FDEP GIS Data Disclaimer it links disclaims liability for "use of provided data (including redistribution and reproduction)", the item is published by the FDEP Open Data Portal, and under Florida's public-records law an agency cannot claim copyright in a public record without statutory authorization (Microdecisions, Inc. v. Skinner, 889 So. 2d 871, Fla. 2d DCA 2004; review denied); no statute covers this inventory. Commercial and club sites stay `public=false`. This rests on Florida law, not on a named license: flip `enabled` back if you want only explicit licenses. |
| `tn_statewide_rec_areas`: Tennessee Statewide Recreation Areas, 445 parks / 587 courts | **stays disabled** | Item licenseInfo is unchanged: a liability disclaimer with no grant of reuse. Tennessee has no rule like Florida's that state records carry no copyright, STS GIS sells some of its data, and the compilation credits the Trust for Public Land (a private source with its own terms). Needs a reuse statement from the State. |
| `vt_anr_ori`: Vermont ANR Outdoor Recreation Sites Inventory, 241 sites | **added**, `tier=weak` | CC BY-SA (item licenseInfo). Survey dates are 1994-1997, so it is kept out of official recall. Share-alike binds only published derivatives. |
| `guilford_county_parks` (NC), 3 parks | **added** | "This data is considered to be in the public domain." County parks only. |
| `ne_ngpc_parks`: Nebraska Game and Parks, 3 areas | **added** | "There are no restrictions on the use and distribution of data provided in this map or service." |
| `manteca_parks` (CA), 31 parks + 111 park polygons | **added** | CC BY 4.0. |
| `tucker_parks` (GA), 2 parks | **added** | CC BY. |
| Montgomery Parks MD (courts), Fairfax County (recreational features), Boise BPR courts | still not used | licenseInfo is still an "as is" disclaimer or a link to one. |
| Baltimore, Arlington VA, Madison, Portland, Sacramento, Mecklenburg, BREC court points, Tucson, Tacoma, Eugene, Oakland | still not used | no new license statement found on re-search. |
| Milton GA park facilities | not used | CC BY-NC (non-commercial). |
| Sacramento County parks (CC0), Miami City Parks (CC BY 4.0), Fort Worth parks (CC BY 4.0), DC parks `BBCOURT` (CC BY 4.0) | not added | open, but no basketball field (Sacramento County, Miami, Fort Worth), or already covered by DC planimetrics. |

`truth refresh --sources fl_fori vt_anr_ori guilford_county_parks ne_ngpc_parks manteca_parks tucker_parks`, then
`truth osm-compare` (OSM = `data/osm_us/courts_us.parquet`, the 2026-10-06 extract, as before):

| | before | after |
|---|---|---|
| official records (live, visible) | 5,216 (about 5,733 courts) | 7,952 (about 10,730 courts) |
| with an OSM basketball feature within 40 m | 3,526 (0.68) | 3,907 (0.49) |
| with an OSM basketball feature in the same park (or within 250 m) | 0.78 | 0.66 |
| court-level records only | 4,232, 0.76 within 40 m | unchanged (all new sources are park-level) |

Per new source (park-level, so "same park" is the meaningful column): FL FORI 2,697 sites, 0.44 with an OSM
basketball feature within 250 m (median nearest 199 m); Vermont 0.37; Manteca 0.65; Guilford 2 of 3; Nebraska 0
of 3; Tucker 1 of 2. Every existing source's numbers are unchanged. **Florida is the big gap**: 56% of FORI sites
that list outdoor courts have no OSM basketball feature within 250 m.
