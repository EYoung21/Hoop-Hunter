-- Hoop Hunter: the courts table (public read, no client writes).
--
-- One row per court, loaded by pipeline/hoop_pipeline/db_load.py from the app-schema GeoJSON
-- exports. Ids are the app's ids ("osm-way-123", "nyc-X159", "det-…", "user-…") and never change.
--
-- Re-runnable: every statement is idempotent, so applying this file twice is harmless.

create schema if not exists private;
revoke all on schema private from public, anon, authenticated;

create table if not exists public.courts (
  id          text primary key check (char_length(id) between 3 and 128),
  name        text check (char_length(name) <= 200),
  -- Where the court came from. Add a value here (and to the app's CourtSource type) for a new layer.
  --   osm        OpenStreetMap, filtered by the pipeline's privacy policy
  --   nyc_parks  NYC Parks open data
  --   detected   satellite-scan candidate (status 'candidate')
  --   user       a player's submission, approved from user_courts
  --   overture   Overture Maps places
  --   web        city / county / school web sources
  --   indoor     indoor-court layer (also set indoor = true)
  --   nces       NCES / IPEDS school and college listings (e.g. school gyms from pipeline indoor.py)
  source      text not null,
  -- verified / candidate are shown in the app. hidden = removed by a moderator (the loader never
  -- un-hides). retired = dropped from a newer export (db_load --prune); a later export can revive it.
  status      text not null default 'verified',
  confidence  real check (confidence >= 0 and confidence <= 1),
  hoops       smallint check (hoops >= 0 and hoops <= 100),
  surface     text check (char_length(surface) <= 100),
  lit         boolean,
  indoor      boolean not null default false,
  address     text check (char_length(address) <= 300),
  region      text check (char_length(region) <= 64),
  state       text check (state ~ '^[A-Z]{2}$'),
  site        text check (char_length(site) <= 32),
  osm_id      text check (char_length(osm_id) <= 64),
  updated     date,
  geom        extensions.geography(Point, 4326) not null,
  -- Plain copies of the coordinates: cheap to return and to grid-cluster (no per-row cast).
  lon         double precision generated always as (extensions.st_x(geom::extensions.geometry)) stored,
  lat         double precision generated always as (extensions.st_y(geom::extensions.geometry)) stored,
  -- Provenance and anything layer-specific: layer, file, export, plus unknown GeoJSON properties.
  extra       jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now(),
  loaded_at   timestamptz not null default now()
);

-- Named constraints so a later migration can widen them with drop + add.
alter table public.courts drop constraint if exists courts_source_check;
alter table public.courts add constraint courts_source_check
  check (source in ('osm', 'nyc_parks', 'detected', 'user', 'overture', 'web', 'indoor', 'nces'));
alter table public.courts drop constraint if exists courts_status_check;
alter table public.courts add constraint courts_status_check
  check (status in ('verified', 'candidate', 'hidden', 'retired'));

comment on table public.courts is
  'Basketball courts shown in the app. Loaded by pipeline/hoop_pipeline/db_load.py; clients read only.';
comment on column public.courts.status is
  'verified | candidate (published) | hidden (moderator, sticky) | retired (dropped from a newer export)';
comment on column public.courts.extra is
  'Provenance: {"layer", "file", ...} plus GeoJSON properties outside the app schema.';

-- Spatial indexes.
--   geography GiST: distance queries in metres (ST_DWithin), e.g. db_load --skip-near
--   geometry GiST:  bounding box + nearest-first (courts_in_view)
--   lon / lat btree: grid clusters (court_clusters), combined with a bitmap AND
create index if not exists courts_geom_idx on public.courts using gist (geom);
create index if not exists courts_geom_geometry_idx on public.courts using gist ((geom::extensions.geometry));
create index if not exists courts_lon_idx on public.courts (lon);
create index if not exists courts_lat_idx on public.courts (lat);
create index if not exists courts_state_source_idx on public.courts (state, source);
create index if not exists courts_layer_idx on public.courts ((extra ->> 'layer'));

-- Row level security: anyone may read published courts; nobody may write through the API.
alter table public.courts enable row level security;

drop policy if exists "Published courts are readable" on public.courts;
create policy "Published courts are readable"
  on public.courts for select
  to anon, authenticated
  using (status in ('verified', 'candidate'));

-- The Data API does not expose new tables automatically, so grant exactly what clients need.
-- No insert / update / delete for anon or authenticated: client writes fail before RLS is even checked.
revoke all on table public.courts from public, anon, authenticated;
grant select on table public.courts to anon, authenticated;
grant select, insert, update, delete on table public.courts to service_role;
