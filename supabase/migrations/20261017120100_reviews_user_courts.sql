-- Hoop Hunter: player reviews and player-added courts.
--
-- Every player is a Supabase Auth user. The app signs in anonymously on first launch, so
-- "authenticated" includes anonymous users. Players can only see and change their own rows.
--
-- Re-runnable: every statement is idempotent.

-- Server-owned timestamps: clients cannot backdate created_at or freeze updated_at.
create or replace function private.touch_timestamps()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_op = 'INSERT' then
    new.created_at := now();
  else
    new.created_at := old.created_at;
  end if;
  new.updated_at := now();
  return new;
end;
$$;
revoke all on function private.touch_timestamps() from public, anon, authenticated;

-- Reviews -----------------------------------------------------------------------------------------

create table if not exists public.reviews (
  court_id    text not null references public.courts (id),
  user_id     uuid not null default auth.uid() references auth.users (id) on delete cascade,
  decision    text not null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  -- One review per player per court; changing your mind updates the row.
  primary key (court_id, user_id)
);

alter table public.reviews drop constraint if exists reviews_decision_check;
alter table public.reviews add constraint reviews_decision_check
  check (decision in ('court', 'not_court', 'home_court'));

comment on table public.reviews is
  'One decision per player per court: court | not_court | home_court (someone''s private home court).';

create index if not exists reviews_user_idx on public.reviews (user_id);

drop trigger if exists reviews_touch on public.reviews;
create trigger reviews_touch before insert or update on public.reviews
  for each row execute function private.touch_timestamps();

alter table public.reviews enable row level security;

drop policy if exists "Players read their own reviews" on public.reviews;
create policy "Players read their own reviews"
  on public.reviews for select to authenticated
  using (user_id = (select auth.uid()));

drop policy if exists "Players add their own reviews" on public.reviews;
create policy "Players add their own reviews"
  on public.reviews for insert to authenticated
  with check (user_id = (select auth.uid()));

drop policy if exists "Players change their own reviews" on public.reviews;
create policy "Players change their own reviews"
  on public.reviews for update to authenticated
  using (user_id = (select auth.uid()))
  with check (user_id = (select auth.uid()));

drop policy if exists "Players delete their own reviews" on public.reviews;
create policy "Players delete their own reviews"
  on public.reviews for delete to authenticated
  using (user_id = (select auth.uid()));

revoke all on table public.reviews from public, anon, authenticated;
grant select, insert, update, delete on table public.reviews to authenticated;
grant select, insert, update, delete on table public.reviews to service_role;

-- Player-added courts ----------------------------------------------------------------------------

create table if not exists public.user_courts (
  -- Made on the phone ("user-<time>-<random>") so a retried upload is an idempotent upsert.
  id          text primary key check (id ~ '^user-[a-z0-9-]{4,64}$'),
  user_id     uuid not null default auth.uid() references auth.users (id) on delete cascade,
  name        text check (char_length(name) <= 120),
  hoops       smallint check (hoops >= 0 and hoops <= 50),
  surface     text check (char_length(surface) <= 40),
  lit         boolean,
  indoor      boolean,
  notes       text check (char_length(notes) <= 1000),
  lon         double precision not null check (lon >= -180 and lon <= 180),
  lat         double precision not null check (lat >= -90 and lat <= 90),
  geom        extensions.geography(Point, 4326)
              generated always as (extensions.st_setsrid(extensions.st_makepoint(lon, lat), 4326)::extensions.geography) stored,
  -- pending until a moderator approves (copied into courts as source 'user') or rejects it.
  status      text not null default 'pending',
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

alter table public.user_courts drop constraint if exists user_courts_status_check;
alter table public.user_courts add constraint user_courts_status_check
  check (status in ('pending', 'approved', 'rejected'));

comment on table public.user_courts is
  'Courts players add on the phone. pending -> approved (copied into courts) | rejected.';

create index if not exists user_courts_user_idx on public.user_courts (user_id);
create index if not exists user_courts_status_idx on public.user_courts (status);
create index if not exists user_courts_geom_idx on public.user_courts using gist (geom);

drop trigger if exists user_courts_touch on public.user_courts;
create trigger user_courts_touch before insert or update on public.user_courts
  for each row execute function private.touch_timestamps();

alter table public.user_courts enable row level security;

drop policy if exists "Players read their own courts" on public.user_courts;
create policy "Players read their own courts"
  on public.user_courts for select to authenticated
  using (user_id = (select auth.uid()));

drop policy if exists "Players add their own courts" on public.user_courts;
create policy "Players add their own courts"
  on public.user_courts for insert to authenticated
  with check (user_id = (select auth.uid()) and status = 'pending');

-- Only while pending, and a player can never approve their own court.
drop policy if exists "Players edit their own pending courts" on public.user_courts;
create policy "Players edit their own pending courts"
  on public.user_courts for update to authenticated
  using (user_id = (select auth.uid()) and status = 'pending')
  with check (user_id = (select auth.uid()) and status = 'pending');

drop policy if exists "Players delete their own courts" on public.user_courts;
create policy "Players delete their own courts"
  on public.user_courts for delete to authenticated
  using (user_id = (select auth.uid()));

revoke all on table public.user_courts from public, anon, authenticated;
grant select, insert, update, delete on table public.user_courts to authenticated;
grant select, insert, update, delete on table public.user_courts to service_role;

-- Review counts ----------------------------------------------------------------------------------

-- Per-court totals for moderators and SQL (not exposed to clients; they use review_counts()).
create or replace view public.court_review_counts
with (security_invoker = true)
as
select
  r.court_id,
  count(*) filter (where r.decision = 'court')::int      as court,
  count(*) filter (where r.decision = 'not_court')::int  as not_court,
  count(*) filter (where r.decision = 'home_court')::int as home_court,
  count(*)::int                                          as total,
  max(r.updated_at)                                      as last_review_at
from public.reviews r
group by r.court_id;

revoke all on table public.court_review_counts from public, anon, authenticated;
grant select on table public.court_review_counts to service_role;

-- Totals for up to 200 courts, readable by anyone. Runs as the owner so it can count every
-- player's review, but returns only counts, never who reviewed.
create or replace function public.review_counts(court_ids text[])
returns table (court_id text, court integer, not_court integer, home_court integer, total integer)
language sql
stable
security definer
set search_path = ''
as $$
  select
    r.court_id,
    count(*) filter (where r.decision = 'court')::int,
    count(*) filter (where r.decision = 'not_court')::int,
    count(*) filter (where r.decision = 'home_court')::int,
    count(*)::int
  from public.reviews r
  where r.court_id = any ((court_ids)[1:200])
  group by r.court_id;
$$;

revoke all on function public.review_counts(text[]) from public, anon, authenticated;
grant execute on function public.review_counts(text[]) to anon, authenticated, service_role;

-- Moderation helper (SQL editor / service role only) ----------------------------------------------

-- Copies a pending submission into courts as a published court (source 'user') and marks it
-- approved. Usage: select private.approve_user_court('user-…');
create or replace function private.approve_user_court(submission_id text)
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
  s public.user_courts;
begin
  select * into s from public.user_courts where id = submission_id for update;
  if not found then
    raise exception 'No user court with id %', submission_id;
  end if;

  insert into public.courts as c (id, name, source, status, hoops, surface, lit, indoor, region, geom, updated, extra)
  values (s.id, s.name, 'user', 'verified', s.hoops, s.surface, s.lit, coalesce(s.indoor, false), 'user', s.geom,
          current_date, jsonb_build_object('layer', 'user_courts', 'notes', s.notes))
  on conflict (id) do update
    set name = excluded.name, hoops = excluded.hoops, surface = excluded.surface, lit = excluded.lit,
        indoor = excluded.indoor, geom = excluded.geom, updated = excluded.updated,
        extra = c.extra || excluded.extra, loaded_at = now(),
        status = case when c.status = 'hidden' then 'hidden' else 'verified' end;

  update public.user_courts set status = 'approved' where id = submission_id;
  return s.id;
end;
$$;

revoke all on function private.approve_user_court(text) from public, anon, authenticated;
