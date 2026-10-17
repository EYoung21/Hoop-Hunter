# Hoop Hunter backend (Supabase)

The phone app loads courts for whatever part of the map is on screen from a Supabase Postgres + PostGIS
database, and syncs players' reviews and added courts back to it.

* Project: `https://bjszbpabcgdomaoumicc.supabase.co` (ca-central-1). PostGIS 3.3 lives in the `extensions` schema.
* Credentials live in `~/.config/hoop-hunter/supabase.env` (git-ignored; never commit it):
  * `SUPABASE_URL` and `SUPABASE_ANON_KEY` (the publishable key, `sb_publishable_…`) ship in the app (`app/src/config.ts`).
  * `SUPABASE_SERVICE_ROLE_KEY` (the secret key, `sb_secret_…`) and `SUPABASE_DB_URL` (the session-pooler URI, with the
    password) stay on your machine. The loader and the verification script read them and never print them.

```
supabase/
  migrations/
    20261017120000_courts.sql               courts table, indexes, RLS, grants
    20261017120100_reviews_user_courts.sql  reviews, user_courts, review counts, moderation helper
    20261017120200_court_rpcs.sql           courts_in_view, court_clusters
  verify_api.py                             REST / RPC / RLS checks with the publishable key
pipeline/hoop_pipeline/db_load.py           migrations + court loader (COPY + upsert)
pipeline/requirements-db.txt                psycopg 3
```

## Loaded data (2026-10-17)

85,470 published courts:

| layer (`extra.layer`) | source | courts | from |
|---|---|---|---|
| `national` | `osm` | 78,893 | `pipeline/out/national/v3/app_ready/<ST>.geojson` (49 files) |
| `national` | `detected` | 5,924 | same files (v3 operating point) |
| `app_bundle` | `nyc_parks` | 552 | `app/assets/data/courts.geojson` (553 in the file; `nyc-X039` Orchard Beach has a flipped longitude, +73.79, and is skipped) |
| `app_bundle` | `detected` | 101 | the same file: Tennessee candidates checked by eye that the v3 export doesn't have. 25 more are in v3 under the same id (v3's newer row wins); 5 are within 25 m of a v3 detection of the same court and were skipped. |

Per state, published courts (`python -m hoop_pipeline.db_load report`):

```
AL    571  AR    590  AZ  3,322  CA 17,889  CO  2,000  CT    875  DC    176  DE    215  FL  5,088  GA    974
IA    964  ID    614  IL  2,954  IN  1,678  KS    592  KY    834  LA    453  MA  2,035  MD  1,807  ME    405
MI  2,571  MN  1,838  MO  1,067  MS    243  MT    269  NC  1,412  ND    453  NE    641  NH    335  NJ  2,240
NM  1,012  NV    999  NY  5,265  OH  3,509  OK    668  OR  1,145  PA  3,856  RI    289  SC    858  SD    313
TN    785  TX  4,049  UT  1,070  VA  2,031  VT    218  WA  2,112  WI  1,587  WV    344  WY    255
```

NY includes the 552 NYC Parks courts; TN includes the 101 hand-checked candidates.

## Schema

### `courts` (public read, no client writes)

| column | type | notes |
|---|---|---|
| `id` | text PK | the app's ids: `osm-way-123`, `nyc-X159`, `det-…`, `user-…`. Never change. |
| `name`, `surface`, `address`, `region`, `site`, `osm_id` | text | as in the app schema; `site` = park / school / college / church / rec / community |
| `source` | text | `osm`, `nyc_parks`, `detected`, `user`, `overture`, `web`, `indoor`, `nces` (constraint `courts_source_check`) |
| `status` | text | `verified`, `candidate` (published); `hidden` (moderator, never undone by a load); `retired` (dropped by a newer export, `--prune`) |
| `confidence` | real | 0–1, detections only |
| `hoops` | smallint | |
| `lit`, `indoor` | boolean | `indoor` defaults to false |
| `state` | text | two-letter code, from the feature's `state`, the file name (`TN.geojson`) or `region` |
| `updated` | date | from the export; a load never replaces a row with an older one |
| `geom` | `geography(Point,4326)` | GiST index |
| `lon`, `lat` | double precision | generated from `geom` (stored), btree indexes |
| `extra` | jsonb | provenance (`layer`, `file`) plus any GeoJSON properties outside the app schema |
| `created_at`, `loaded_at` | timestamptz | |

Indexes: GiST on `geom` (metre distances, `ST_DWithin`), GiST on `(geom::geometry)` (box + nearest-first), btree on
`lon` and `lat` (grid clusters), `(state, source)`, `(extra->>'layer')`.

### `reviews`

`court_id` → `courts.id`, `user_id` → `auth.users` (default `auth.uid()`, cascade on delete), `decision` in
`court | not_court | home_court`, `created_at`, `updated_at`. Primary key `(court_id, user_id)`: one review per player
per court; changing your mind is an upsert. A trigger owns the timestamps (a client can't backdate `created_at`).

### `user_courts`

Player submissions: `id` (`user-…`, made on the phone so retries are idempotent), `user_id`, `name`, `hoops`,
`surface`, `lit`, `indoor`, `notes`, `lon`, `lat`, generated `geom`, `status` in `pending | approved | rejected`,
timestamps.

### Review counts

* `court_review_counts` view: per court `court`, `not_court`, `home_court`, `total`, `last_review_at`. For SQL and
  moderation; not granted to clients.
* `review_counts(court_ids text[])` RPC: the same counts for up to 200 courts, callable by anyone. It is
  `SECURITY DEFINER` so it can count every player's review, and it returns counts only, never who reviewed.

Social features (check-ins, runs, streaks) can hang off `courts.id` and `auth.users.id` the same way `reviews` does.

## Security: RLS and grants

The Data API is on, but new tables aren't exposed automatically. Each migration revokes everything from
`public`/`anon`/`authenticated` (including the default TRUNCATE / REFERENCES / TRIGGER), then grants only this:

| object | anon (publishable key, no session) | authenticated (incl. anonymous sign-in) | service_role |
|---|---|---|---|
| `courts` | SELECT, rows with status verified / candidate | same | all |
| `reviews` | none | SELECT / INSERT / UPDATE / DELETE **own rows** (`user_id = auth.uid()`) | all |
| `user_courts` | none | SELECT / DELETE own; INSERT own with `status = 'pending'`; UPDATE own while pending, and it must stay pending | all |
| `court_review_counts` (view) | none | none | SELECT |
| `courts_in_view`, `court_clusters`, `review_counts` | EXECUTE | EXECUTE | EXECUTE |
| `private.*` (timestamps trigger, `approve_user_court`) | none | none | SQL only |

No client can write `courts`: there's no INSERT/UPDATE/DELETE grant, so the API returns `42501 permission denied`
before RLS is even checked. `courts_in_view` and `court_clusters` are `SECURITY INVOKER`, so the courts policy applies
to them too: `hidden` and `retired` courts never leave the database.

## RPCs

### `courts_in_view(min_lon, min_lat, max_lon, max_lat, max_rows = 2000)`

Returns `id, lon, lat, name, source, status, confidence, hoops, surface, lit, indoor, address, region, state, site,
osm_id, updated` for every published court in the box, **nearest the box centre first**, at most `max_rows` (capped at
5000). The ordering comes straight off the GiST index, so a capped answer is the middle of the view, not a random
subset.

The API returns at most **1,000 rows per request** (Supabase's default "Max rows" setting), so page with
`?offset=&limit=` (supabase-js `.rpc(...).range(from, to)`; a `Range` header is ignored on POST):

```bash
curl -s "$SUPABASE_URL/rest/v1/rpc/courts_in_view?offset=0&limit=1000" \
  -H "apikey: $SUPABASE_ANON_KEY" -H "Content-Type: application/json" \
  -d '{"min_lon":-85.44,"min_lat":34.94,"max_lon":-85.06,"max_lat":35.24,"max_rows":3000}'
```

### `court_clusters(min_lon, min_lat, max_lon, max_lat, zoom, statuses = null)`

Groups published courts on a grid of `360 / 2^(zoom + 3)` degrees (about 8 cells across a phone screen at that web-map
zoom) and returns `cell_x, cell_y, court_count, lon, lat` (centroid) and `west, south, east, north` (extent, for a
tap-to-zoom). The cell is never smaller than 1/64 of the box, so one call returns at most 64 × 64 cells. `statuses`
narrows to `{verified}` or `{candidate}`. The whole lower 48 at zoom 4 is 143 cells and 23 KB of JSON.

It's PL/pgSQL with `EXECUTE … USING` so each call is planned for its actual box. A plain SQL function gets a generic
plan that sorts all 85k rows for the national view: about 240 ms instead of 50 ms.

### Measured (`EXPLAIN ANALYZE`, 85,470 courts, 2026-10-17)

| call | rows | time | plan |
|---|---|---|---|
| `courts_in_view` Chattanooga (0.38° × 0.3°) | 28 | 2.4 ms | Index Scan using `courts_geom_geometry_idx`, Index Cond `&&`, Order By `<->` |
| `courts_in_view` Los Angeles (0.6° × 0.9°) | 2,887 | 9.8 ms | same, no sort |
| `court_clusters` Tennessee box, zoom 7 | 185 cells | 3.5 ms | BitmapAnd of `courts_lat_idx` and `courts_lon_idx` |
| `court_clusters` LA metro, zoom 9, candidates | 82 cells | 3.8 ms | same |
| `court_clusters` lower 48, zoom 4 | 143 cells | 47 ms | Seq Scan + HashAggregate (every row is in the box) |

```sql
explain (analyze, buffers)
select c.id from public.courts c
where (c.geom::extensions.geometry) operator(extensions.&&) extensions.st_makeenvelope(-118.6, 33.6, -118.0, 34.5, 4326)
  and c.status in ('verified', 'candidate')
order by (c.geom::extensions.geometry) operator(extensions.<->) extensions.st_setsrid(extensions.st_makepoint(-118.3, 34.05), 4326)
limit 5000;
explain analyze select * from public.court_clusters(-125, 24, -66, 50, 4);
```

Run outside a function, the LA query sometimes picks a parallel bitmap scan whose worker start-up costs about 60–230 ms
on this instance. Inside the function it uses the index-ordered scan (about 10 ms).

## Applying the schema

The migrations are ordered (timestamp prefix) and re-runnable (`if not exists`, `create or replace`, drop-and-create
policies, idempotent grants).

```bash
cd pipeline
.venv/Scripts/python -m pip install -r requirements-db.txt      # psycopg 3, once
.venv/Scripts/python -m hoop_pipeline.db_load migrate --dry-run  # list applied / pending
.venv/Scripts/python -m hoop_pipeline.db_load migrate            # apply pending
.venv/Scripts/python -m hoop_pipeline.db_load migrate --reapply  # run all again (safe)
```

Applied versions are recorded in `supabase_migrations.schema_migrations`, the table the Supabase CLI uses, so
`supabase db push` would treat them as applied if you switch to the CLI later. A schema change is a new file with a
later timestamp. To widen an enum, drop and re-add the named constraint, for example `courts_source_check`.

## Loading and re-loading courts

```bash
cd pipeline
# what was run on 2026-10-17
.venv/Scripts/python -m hoop_pipeline.db_load load 'out/national/v3/app_ready/*.geojson'
.venv/Scripts/python -m hoop_pipeline.db_load load ../app/assets/data/courts.geojson --layer app_bundle --only-source nyc_parks --skip-invalid
.venv/Scripts/python -m hoop_pipeline.db_load load ../app/assets/data/courts.geojson --layer app_bundle --only-source detected --skip-near 25
.venv/Scripts/python -m hoop_pipeline.db_load report
```

How a load works, in one transaction:

1. **Validate** every feature: Point geometry inside the US (lower 48, AK, HI, PR/VI, Guam), id ≤ 128 chars and unique
   across all inputs, `source`/`status` enums, `confidence` 0–1, whole `hoops`, booleans, ISO `updated`. Any problem
   aborts the whole load with a list; `--skip-invalid` loads the rest and reports what it left out.
2. **COPY** into a temp table, then **upsert** by id. A new id is inserted. An existing row is updated only if
   something changed and the file's `updated` isn't older (`--force` overrides the date check). `hidden` is never
   undone. Re-running the same files reports `unchanged` for everything.
3. Optional:
   * `--skip-near M` drops **new** ids within M metres of an existing published court with the same `indoor` flag.
     This dedupes a new layer against what's loaded.
   * `--prune` sets `status = 'retired'` on rows of the same layer, states and sources that the input no longer
     has, for example a court a newer export dropped after a privacy re-check. Without `--prune` nothing is ever
     removed.
4. **Report** counts by state and source: input, inserted, updated, unchanged, kept-newer, skipped, retired, then
   the database totals. `--dry-run` does all of it and rolls back. `--no-db` validates without connecting.

Inputs are files, directories or quoted globs. `--layer` names the provenance stored in `extra.layer` (default:
`national` for files in an `app_ready` folder, otherwise the file stem).

### A newer national export (v4)

```bash
.venv/Scripts/python -m hoop_pipeline.db_load load 'out/national/v4/app_ready/*.geojson' --prune --dry-run   # review
.venv/Scripts/python -m hoop_pipeline.db_load load 'out/national/v4/app_ready/*.geojson' --prune
```

Same ids update in place (reviews stay attached). Courts v4 dropped are retired, so they disappear from the app.

### Indoor, web and Overture layers

Write app-schema GeoJSON (the `app/README.md` "Data file" properties, Point features), one file per state or one big
file, then load it with its own layer name:

* **`id`**: stable and prefixed per source, for example `web-<hash of url + name>`, `ovt-<overture id>`,
  `osm-way-123` for OSM indoor courts.
* **`source`**: `web`, `overture`, `indoor`, `nces` (school listings) or `osm`.
* **`status`**: `verified` for listings you trust, `candidate` to have players confirm them in the app.
* **`indoor: true`** for gyms and rec centres. The app draws them blue with a roof mark. `source: "indoor"` implies it.
* **`state`**: set it if the file isn't named `<ST>.geojson` and `region` isn't a state name.
* **Anything else** (`url`, `source_name`, `hours`, `phone`, `fee`, …) is kept in `extra` with no schema change. The
  app doesn't read `extra` yet.

```bash
.venv/Scripts/python -m hoop_pipeline.db_load load 'out/web/app_ready/*.geojson' --layer web --skip-near 30 --dry-run
.venv/Scripts/python -m hoop_pipeline.db_load load out/indoor/indoor_courts.geojson --layer indoor --skip-near 30 --dry-run
```

Checked on 2026-10-17 against what `hoop_pipeline/indoor.py` had written so far (not loaded):

* `out/indoor/indoor_courts.geojson`: 18,032 features (16,459 Overture candidates, 1,573 OSM verified). They all
  validate, the states come from `region`, and a dry run would insert all 18,032.
* `out/indoor/indoor_school_gyms.geojson`: 127,952 school gyms, almost all `source: "nces"`. They validate too. School
  gyms usually aren't open to the public, so decide on the policy before loading them. The `access` property
  (public / membership / unknown / school) is kept in `extra`.

`--skip-near` compares like with like (indoor against indoor, outdoor against outdoor), so a gym next to a park
court isn't dropped. A new source value, for example `official`, needs a migration that widens
`courts_source_check`, plus a label in `app/src/data/classify.ts`. Older app versions still show such courts, labelled
"Other source".

## Moderation (SQL editor, or `psql "$SUPABASE_DB_URL"`)

```sql
-- hide a court (for example reported as someone's home court); loads never un-hide it
update public.courts set status = 'hidden' where id = 'det-…';

-- candidates players disagree with, or report as private
select c.id, c.name, c.state, r.*
from public.court_review_counts r join public.courts c on c.id = r.court_id
where r.home_court > 0 or r.not_court > r.court
order by r.home_court desc, r.total desc;

-- player submissions
select id, name, hoops, lon, lat, created_at from public.user_courts where status = 'pending' order by created_at;
select private.approve_user_court('user-…');                                  -- copy into courts as source 'user'
update public.user_courts set status = 'rejected' where id = 'user-…';

-- take the hand-checked TN candidates back out, if you'd rather show v3 only
update public.courts set status = 'retired' where extra->>'layer' = 'app_bundle' and source = 'detected';
```

## Auth settings the app relies on

* **Anonymous sign-ins must be on**: Authentication → Sign In / Providers → "Allow anonymous sign-ins". As of
  2026-10-17 it's **off**, and `/auth/v1/signup` answers `422 anonymous_provider_disabled`. Until it's on, the app
  still loads courts (they need no session) and queues reviews and added courts on the phone, uploading them once
  sign-in works.
* Recommended with it: CAPTCHA (Turnstile or hCaptcha) on sign-ups, or keep the default per-IP rate limit (30
  anonymous sign-ins per hour). Anonymous users accumulate in `auth.users`; deleting one deletes their reviews and
  submissions (cascade).
* "Max rows" (API settings) is 1000. The app pages around it, so leave it.

## Verifying

```bash
pipeline/.venv/Scripts/python supabase/verify_api.py
```

35 checks with the publishable key:

* anon can read courts and call all three RPCs (Chattanooga, LA paged past 1,000 rows, San Francisco, whole-US
  clusters summing to every court).
* anon can't insert, update or delete courts, or read or write reviews, user_courts or the counts view.
* A throwaway authenticated user (created with the secret key, deleted at the end) can:
  * upsert and delete its own review (one per court, server-owned timestamps);
  * add and edit its own pending court.
* That user can't:
  * review as someone else, or self-approve a court;
  * write courts.

It also reports whether anonymous sign-in is enabled.
