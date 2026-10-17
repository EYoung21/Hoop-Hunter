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
    20261017140000_social_tables.sql        social: profiles, check-ins, runs, follows, blocks, reports, XP, badges
    20261017140100_social_rpcs.sql          social: the RPCs the app calls
  verify_api.py                             REST / RPC / RLS checks with the publishable key
  verify_social.py                          social features: RLS, expiry, XP / streak rules, blocking, leaderboards
pipeline/hoop_pipeline/db_load.py           migrations + court loader (COPY + upsert)
pipeline/requirements-db.txt                psycopg 3
```

## Loaded data (2026-10-17)

92,132 published courts (325 more are retired):

| layer (`extra.layer`) | source | courts | from |
|---|---|---|---|
| `national` | `osm` | 78,893 | `pipeline/out/national/v4/app_ready/<ST>.geojson` (49 files). 1,406 of them are in an indoor venue and carry `indoor: true` (`export_app.osm_indoor`). |
| `national` | `detected` | 11,379 | same files (v4 multi-year export, `pipeline/docs/imagery.md`) |
| `web` | `web` | 727 | `pipeline/out/web/web_courts_public.geojson`: parks whose own web page lists a court, placed on a satellite detection or within 150 m of the court (`pipeline/docs/web_sources.md`). Status `candidate`. 38 were within 30 m of a loaded court and skipped. |
| `indoor` | `osm`, `web` | 187 + 314 | `pipeline/out/indoor/indoor_courts_listed.geojson`, OSM and park-page rows only (`pipeline/docs/indoor.md`). Its Overture rows (74% precision) aren't loaded. |
| `app_bundle` | `nyc_parks` | 552 | `app/assets/data/courts.geojson` (553 in the file; `nyc-X039` Orchard Beach has a flipped longitude, +73.79, and is skipped) |
| `app_bundle` | `detected` | 80 | the same file: Tennessee candidates checked by eye that the national export doesn't have |

Per state, published courts (`python -m hoop_pipeline.db_load report`):

```
AL    623  AR    599  AZ  3,461  CA 21,006  CO  2,074  CT    904  DC    180  DE    224  FL  5,548  GA  1,034
IA    987  ID    623  IL  3,089  IN  1,745  KS    616  KY    898  LA    490  MA  2,130  MD  1,900  ME    439
MI  2,626  MN  1,889  MO  1,126  MS    251  MT    291  NC  1,482  ND    457  NE    647  NH    358  NJ  2,349
NM  1,031  NV  1,254  NY  5,477  OH  3,589  OK    700  OR  1,218  PA  4,197  RI    300  SC    909  SD    319
TN    825  TX  4,288  UT  1,148  VA  2,121  VT    230  WA  2,204  WI  1,649  WV    371  WY    256
```

NY includes the 552 NYC Parks courts.

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

Social features (check-ins, runs, follows, XP, streaks, badges, leaderboards): see [Social features](#social-features).

## Social features

Light, Duolingo-style features: check-ins, pickup runs, XP, streaks, badges, weekly leaderboards, follow by friend
code, block and report. They live in migrations `20261017140000_social_tables.sql` and
`20261017140100_social_rpcs.sql`. Both are additive: no existing table is altered and `courts` is untouched.
`reviews` and `user_courts` get AFTER triggers that award XP. The triggers swallow their own errors, so a review
or a submission can never fail because of XP.

Ground rules, enforced in the database:

* **No free text anywhere.** Run formats, skill levels and report reasons are presets. The handle is the only
  string a player chooses.
* **No coordinates, no location trail.**
  * A check-in is a court id, a kind and times.
  * The phone checks that it's within ~250 m of the court with a one-time location read, and never sends the
    position.
  * Check-ins and runs are deleted 2 days after they expire.
  * "Courts visited" keeps court ids only until it reaches 10 (the badge). Then the list is deleted and only the
    count stays.
  * Check-in XP keys use the check-in's row id, never the court.
* **Clients can't grant XP.** Only SECURITY DEFINER functions and triggers write `xp_events`, `user_badges`,
  `profiles` (streak, counts) and `court_firsts`. Clients have no INSERT / UPDATE / DELETE grant on any of them.
* **Handle only.** Other players are only ever named by handle. All reads of other players go through RPCs, so
  their user ids and friend codes never leave the database. There is no user search, no contact import, and no
  follower list of anyone but yourself.
* **13+ gate.** `confirm_age()` ("I'm 13 or older") creates the profile with `age_ok = true`. Every other social
  RPC raises `age_gate` until that profile exists. An under-13 answer stays on the phone, and no profile is ever
  created.

### Tables

| table | what | notes |
|---|---|---|
| `profiles` | one per player past the gate | `handle` (unique, case-insensitive), `friend_code` (6 of 31 unambiguous characters), `age_ok`, `home_state`, `tz_offset_min` (last offset the phone sent), `streak_days`, `streak_last_day`, `best_streak`, `courts_visited` |
| `checkins` | `here` / `planned` | `court_id`, `kind`, `starts_at`, `expires_at` (trigger: always `starts_at + 2 h`), `ended_at` (checked out or replaced), `local_day`, `xp` |
| `runs` | pickup runs | uuid made on the phone (idempotent retries), `court_id`, `creator_id`, `format` (5v5 … 1v1, shootaround), `skill` (casual / competitive / null), `max_players` (2–30 / null), `starts_at`, `expires_at` (trigger: `starts_at + 3 h`), `cancelled_at` |
| `run_players` | who's in | the creator is always in |
| `follows`, `blocks` | one-way follow, block | a block removes follows both ways |
| `reports` | player or run reports | reason is spam / offensive_name / harassment / fake_run / unsafe / other. A trigger sets the reporter and the time, aims run reports at the run's creator, and allows 20 a day |
| `xp_events` | XP ledger | `kind`, `points`, `local_day`, `week_start` (Monday), `dedupe_key` unique per player |
| `user_badges` | badges | court_hunter, first_checkin, streak_7, streak_30, courts_10, first_run |
| `court_firsts` | first finder per court | `added` (approved submission) or `confirmed` (first "It's a court"); never reassigned |
| `private.court_visits`, `private.follow_attempts` | bookkeeping | not exposed at all |

No foreign keys point at `courts`:

* Check-ins and runs work on any court id the app shows. That includes a player's own pending `user-…` court, which
  isn't in `courts` until it's approved (and keeps its id when it is).
* Nothing here can block a load. `db_load` never deletes a court: `--prune` only sets `status = 'retired'`, and its
  only DELETE is on its own temp `stage` table.
* Social rows at a retired court are harmless. The app stops showing the court, and the rows expire and are purged.

### XP, streaks and badges

| earns | XP | limits |
|---|---|---|
| "Here now" check-in | 10 | first per court per local day; at most 4 courts a day |
| review of a satellite candidate (existing `reviews`) | 5 | first review per court ever (changing your mind or undo + redo earns nothing); 25 a day |
| a court you added, once approved | 25 | once per court |
| post a run | 10 | once per run; 2 a day |
| join someone else's run | 5 | once per run (leave + rejoin earns nothing); 3 a day |

Planned check-ins earn nothing and don't count for streaks.

* **Local day.**
  * The phone sends `utc_offset`, the minutes to add to UTC (`-new Date().getTimezoneOffset()`). It must be between
    -720 and +840, in 15-minute steps; anything else raises `bad_offset`.
  * The server computes `local_day = (now() at time zone 'UTC' + offset)::date`.
  * Review XP has no offset of its own, so it uses the last one the phone sent (`profiles.tz_offset_min`, updated
    by `my_profile`, `check_in` and the run RPCs).
* **Streak** = consecutive local days with a "here now" check-in. It's stored as counters, with no per-day history.
  * The next day adds 1, the same day changes nothing, and a gap resets it to 1.
  * A check-in dated before the last streak day (a smaller offset after flying west) never moves it back.
  * It shows as alive through the day after its last check-in, and as 0 after that.
  * A phone could fake its offset, but real offsets span only 26 hours, so it could gain a day or so at most.
* **Badges.** Court Hunter goes to the first to add a court that gets approved, or the first to confirm a satellite
  candidate. The others are first check-in, 7- and 30-day streaks, 10 different courts, and first run (posted or
  joined).
* **Rate limits:**
  * one active check-in (a new one ends the old), and 20 check-ins an hour;
  * 3 upcoming runs posted, 10 joined, and 10 posts a day;
  * 20 friend-code tries an hour, 20 reports a day, and 500 follows.

### RPCs

All `SECURITY DEFINER`, with `set search_path = ''`, and EXECUTE for `authenticated` only. They take no user id: the
caller is `auth.uid()`. Errors are plain messages (HTTP 400, code `P0001`) that the app maps to text: `age_gate`,
`bad_offset`, `court_not_found`, `bad_time`, `rate_limited`, `run_full`, `run_closed`, `run_not_found`,
`too_many_runs`, `creator_cannot_leave`, `not_your_run`, `handle_taken`, `handle_invalid`, `handle_not_allowed`, …

| RPC | returns |
|---|---|
| `my_profile(utc_offset)` | null before the gate; else handle, friend code, home state, XP total / this week, streak (+ `streak_today`, best), courts visited, badges, following / follower counts, your active check-in |
| `confirm_age(utc_offset)` | creates the profile ("Hooper" + 4 digits, random friend code); returns `my_profile` |
| `set_handle(new_handle)`, `set_home_state(st)`, `reset_friend_code()` | the new value |
| `public_profile(player)` | handle, XP week / total, streak, best, badges, `you_follow`, `follows_you`; null if a block stands |
| `check_in(court, mode, start_at, utc_offset)` | `mode` is `here` or `planned` (today or tomorrow, local); returns the check-in, `xp`, `streak` and any new `badges` |
| `end_checkin()` | checks you out |
| `court_activity(court, hours_ahead = 6)` | `here` count, `planned` counts by time, `friends` (mutual follows only: handle, kind, time), `mine` |
| `post_run(run, court, run_format, start_at, skill_level, player_limit, utc_offset)` | the run, `xp`, `badges` |
| `join_run(run, utc_offset)`, `leave_run(run)`, `cancel_run(run)` | the run |
| `court_runs(court)`, `my_runs()` | upcoming runs: format, skill, max, times, player count, up to 12 names, creator, `mine`, `joined` (`my_runs` adds the court's name and position) |
| `follow_by_code(code)` | `{status: followed / already / not_found / own_code / rate_limited / too_many, handle, mutual}` |
| `unfollow(player)`, `my_follows()` | your following / followers (with `mutual`) / blocked lists |
| `block_player(player)`, `unblock_player(player)` | blocking removes follows both ways, and takes each player out of the runs the other hosts |
| `report_player(player, reason)`, `report_run(run, reason)` | true |
| `leaderboard_friends(utc_offset)` | you + people you follow, XP since Monday (your local week), ranked |
| `leaderboard_state(st, utc_offset)` | top 50 in a state this week + your rank; `st` null = your home state (picked, or the state of your first check-in's court) |

A block works both ways. Neither player:

* sees the other's check-ins (named or in counts), runs, names on runs, or leaderboard rows;
* can open the other's profile;
* can follow the other by code (it reports `not_found`).

Indexes cover what the RPCs query:

* active check-ins by court, and check-ins by player (for the rate limit and XP once per court per day);
* runs by court and by creator;
* follows and blocks, both directions;
* `xp_events (week_start, user_id) include (points)` for leaderboards, and `(user_id, kind, local_day)` for daily caps;
* `profiles (lower(handle))`, `(friend_code)` and `(home_state)`.

### Moderation

```sql
-- reports, newest first, with handles
select r.created_at, r.reason, rp.handle as reporter, t.handle as target, r.run_id
from public.reports r
left join public.profiles rp on rp.user_id = r.reporter_id
left join public.profiles t on t.user_id = r.target_user_id
order by r.created_at desc;

-- rename an offensive handle; cancel a run
update public.profiles set handle = 'Hooper' || (1000 + floor(random() * 9000))::int where lower(handle) = lower('…');
update public.runs set cancelled_at = now() where id = '…';
```

Deleting a player (`auth.users`) deletes all their social rows, because every foreign key cascades.

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
| `private.*` (timestamps trigger, `approve_user_court`, social helpers) | none | none | SQL only |
| `profiles`, `checkins`, `xp_events`, `user_badges` | none | SELECT **own rows** only | all |
| `runs`, `run_players`, `follows`, `blocks`, `court_firsts` | none | none (RPCs only) | all |
| `reports` | none | INSERT only (`target_user_id`, `run_id`, `reason`); no SELECT | all |
| social RPCs (`check_in`, `post_run`, `leaderboard_state`, …) | none | EXECUTE | EXECUTE |

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

The two social migrations were applied on 2026-10-17 the same way `db_load migrate` does it: each file runs in one
transaction and is then recorded in `schema_migrations`, so `migrate --dry-run` lists them as applied. First, both
were run twice in a transaction that was rolled back, which proved they're re-runnable. The court counts were the
same before and after (90,904 published, 325 retired).

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

What was run on 2026-10-17 (each after a `--dry-run`):

```bash
.venv/Scripts/python -m hoop_pipeline.db_load load out/web/web_courts_public.geojson --layer web --skip-near 30
.venv/Scripts/python -m hoop_pipeline.db_load load out/indoor/indoor_courts_listed.geojson --layer indoor \
    --only-source osm --only-source web --skip-near 50
```

Not loaded, on purpose:

* the other 193 rows of `out/web/web_courts.geojson`: courts in large parks, where a park-level pin could be far from
  the court
* the 754 Overture rows of `indoor_courts_listed.geojson` (74% precision) and the 14,560 of
  `out/indoor/indoor_courts.geojson`, which are mostly unverified "likely" gyms. Publishing Overture rows also needs
  per-row CDLA-Permissive-2.0 attribution.
* `out/indoor/indoor_school_gyms.geojson`: 128,199 school gyms. School gyms usually aren't open to the public.

The indoor files leave out OSM courts the national layer already has. Those get `indoor: true` from the national
export instead.

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
* The social features need a session too. With sign-in off, the app's "You" screen and the court card say social
  features are unavailable, and everything else keeps working.
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

```bash
pipeline/.venv/Scripts/python supabase/verify_social.py
```

100 checks of the social features (100/100 on 2026-10-17). It uses five throwaway password players, created with
the secret key and deleted at the end; the script then confirms no rows of theirs are left in any social table.

* **RLS and grants.** No session can't call anything. Players read only their own profile, check-ins, XP and
  badges. They can't write any of those, grant XP or badges, insert check-ins (or as someone else), read runs /
  follows / blocks / reports / court_firsts, or call private helpers. Reports are insert-only, with a server-set
  reporter.
* **13+ gate, handles.** Before the gate, every social RPC answers `age_gate`. The script also covers generated
  handles and friend codes, custom handles (shape, case-insensitive uniqueness, blocklist with leet and
  CamelCase, reserved names), and innocent words that contain blocked ones (Peacock, Dickens, Therapist).
* **Check-ins.**
  * XP once per court per local day, and 4 courts a day. One active check-in.
  * "Here now" expires after 2 h. A planned check-in must be today or tomorrow, and expires 2 h after its time.
  * No coordinate columns. XP keys don't name courts.
  * Works at your own pending court, not at someone else's.
  * Purged 2 days after expiry. 20 an hour.
* **Streaks and UTC offsets.** The offsets are chosen from the server's clock so the player's local time is 23:5x
  and then 00:0x the next day.
  * The streak extends across local midnight, and the same court earns XP again on the new day.
  * It never moves back. It's alive through the next day, then shows 0. A gap resets it, and the best is kept.
  * The -12:00 and +14:00 days are checked, and bad offsets are rejected.
  * The streak_7 and courts_10 badges.
* **Runs.**
  * Presets only. Posting is idempotent, and someone else's id is refused.
  * Join / leave / rejoin earns XP once. A full run can't be joined. The creator can't leave; only the creator
    can cancel.
  * Cancelled runs are hidden. Expiry is 3 h after the start.
  * Caps: 2 XP posts a day, 3 upcoming runs.
* **XP from existing features.** The first review of a candidate earns 5 once; the first confirmer gets Court
  Hunter, the second doesn't. A verified court earns nothing. An approved user court earns 25 + Court Hunter, and
  only once.
* **Follows, leaderboards, blocking, reports.**
  * Follow by messy code: already / own / unknown codes, mutual follows, own lists only, and the 20-try limit.
  * Friends' names at a court (mutual only); public profiles show the handle only.
  * Friends and state leaderboards with ranks and a Monday week.
  * Blocking hides runs, check-ins and leaderboard rows both ways, removes follows and run spots, and stops
    re-follows; unblocking undoes the hiding.
  * Report reasons are presets only, and the service role can read reports.
