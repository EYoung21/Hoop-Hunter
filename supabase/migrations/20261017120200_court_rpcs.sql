-- Hoop Hunter: map-area RPCs used by the app.
--
--   courts_in_view  every published court in a lon/lat box, nearest the box centre first
--   court_clusters  grid clusters (count, centroid, extent) for zoomed-out views
--
-- Both are SECURITY INVOKER, so the courts RLS policy (published statuses only) applies to callers.
-- courts_in_view uses the GiST index on (geom::geometry); court_clusters the lon / lat btrees.
-- See supabase/README.md for EXPLAIN ANALYZE output.
-- Re-runnable: create or replace + idempotent grants.

create or replace function public.courts_in_view(
  min_lon double precision,
  min_lat double precision,
  max_lon double precision,
  max_lat double precision,
  max_rows integer default 2000
)
returns table (
  id text,
  lon double precision,
  lat double precision,
  name text,
  source text,
  status text,
  confidence real,
  hoops smallint,
  surface text,
  lit boolean,
  indoor boolean,
  address text,
  region text,
  state text,
  site text,
  osm_id text,
  updated date
)
language sql
stable
parallel safe
set search_path = ''
as $$
  select
    c.id, c.lon, c.lat,
    c.name, c.source, c.status, c.confidence, c.hoops, c.surface, c.lit, c.indoor,
    c.address, c.region, c.state, c.site, c.osm_id, c.updated
  from public.courts c
  where (c.geom::extensions.geometry) operator(extensions.&&)
        extensions.st_makeenvelope(min_lon, min_lat, max_lon, max_lat, 4326)
    and c.status in ('verified', 'candidate')
  -- Nearest the centre first (index-ordered), so a capped answer is the middle of the view.
  order by (c.geom::extensions.geometry) operator(extensions.<->)
        extensions.st_setsrid(extensions.st_makepoint((min_lon + max_lon) / 2, (min_lat + max_lat) / 2), 4326)
  limit least(greatest(coalesce(max_rows, 2000), 1), 5000);
$$;

comment on function public.courts_in_view(double precision, double precision, double precision, double precision, integer) is
  'Published courts inside a lon/lat box, nearest the centre first, at most max_rows (cap 5000).';

-- Grid size by zoom: cell = 360 / 2^(zoom + 3) degrees, i.e. about 8 cells across a phone screen at
-- that web-map zoom. The cell never gets smaller than 1/64 of the box, so one call returns at most
-- 64 x 64 cells whatever the caller sends. statuses = null means both published statuses.
create or replace function public.court_clusters(
  min_lon double precision,
  min_lat double precision,
  max_lon double precision,
  max_lat double precision,
  zoom integer,
  statuses text[] default null
)
returns table (
  cell_x integer,
  cell_y integer,
  court_count integer,
  lon double precision,
  lat double precision,
  west double precision,
  south double precision,
  east double precision,
  north double precision
)
language plpgsql
stable
parallel safe
set search_path = ''
as $$
declare
  cell double precision := greatest(
    360.0 / power(2.0, least(greatest(coalesce(zoom, 4), 0), 22) + 3),
    (max_lon - min_lon) / 64.0,
    (max_lat - min_lat) / 64.0,
    1e-6
  );
begin
  -- Dynamic SQL so each call is planned for its actual box: a bitmap AND of the lon / lat btrees for a
  -- state or metro, a sequential scan + hash aggregate for the whole country. (A plain SQL function gets a
  -- generic plan that sorts all 85k rows for the national view: ~240 ms instead of ~50 ms.)
  return query execute
    'select floor(c.lon / $5)::int, floor(c.lat / $5)::int, count(*)::int,
            avg(c.lon), avg(c.lat), min(c.lon), min(c.lat), max(c.lon), max(c.lat)
     from public.courts c
     where c.lon between $1 and $3
       and c.lat between $2 and $4
       and c.status in (''verified'', ''candidate'')
       and ($6 is null or c.status = any ($6))
     group by 1, 2'
    using min_lon, min_lat, max_lon, max_lat, cell, statuses;
end;
$$;

comment on function public.court_clusters(double precision, double precision, double precision, double precision, integer, text[]) is
  'Published courts in a lon/lat box grouped on a grid of 360/2^(zoom+3) degrees: count, centroid, extent.';

revoke all on function public.courts_in_view(double precision, double precision, double precision, double precision, integer)
  from public, anon, authenticated;
grant execute on function public.courts_in_view(double precision, double precision, double precision, double precision, integer)
  to anon, authenticated, service_role;

revoke all on function public.court_clusters(double precision, double precision, double precision, double precision, integer, text[])
  from public, anon, authenticated;
grant execute on function public.court_clusters(double precision, double precision, double precision, double precision, integer, text[])
  to anon, authenticated, service_role;
