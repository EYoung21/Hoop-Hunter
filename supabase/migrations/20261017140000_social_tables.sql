-- Hoop Hunter: social features, part 1 of 2: tables, helpers, XP triggers.
--
-- Light and Duolingo-style on purpose:
--   * No free text anywhere. Formats, skill levels and report reasons are presets; the handle is the only
--     string a player picks (3-20 of [A-Za-z0-9_], unique, with a small blocklist).
--   * No coordinates and no location trail. A check-in is court id, kind and times, and is deleted two days
--     after it expires. The phone checks the distance to the court itself and never sends where it is.
--   * XP, streaks and badges are written only by SECURITY DEFINER functions and triggers. Clients have no
--     INSERT / UPDATE / DELETE grant on any of these tables (reports: INSERT only).
--   * Players see each other by handle only. Every read goes through an RPC (part 2), so user ids and friend
--     codes of other players never leave the database.
--
-- Additive: no existing table is altered and courts is not touched. reviews and user_courts get AFTER triggers
-- that award XP; they swallow their own errors so a review or a submission can never fail because of XP.
-- No foreign keys to courts: check-ins and runs work on any court id the app shows (including a player's own
-- pending user-... court), and db_load, which only ever retires courts, can never be blocked by them.
-- Re-runnable: every statement is idempotent.

-- Helpers ----------------------------------------------------------------------------------------------

-- The player's local calendar day. utc_offset = minutes to add to UTC (Chattanooga in summer: -240).
create or replace function private.local_day(ts timestamptz, utc_offset integer)
returns date
language sql
immutable
set search_path = ''
as $$
  select ((ts at time zone 'UTC') + make_interval(mins => utc_offset))::date;
$$;

-- Monday of the week a day falls in (weekly leaderboards).
create or replace function private.week_start(d date)
returns date
language sql
immutable
set search_path = ''
as $$
  select d - (extract(isodow from d)::integer - 1);
$$;

-- Real UTC offsets run from -12:00 to +14:00 in 15-minute steps (India +5:30, Nepal +5:45).
create or replace function private.valid_offset(utc_offset integer)
returns integer
language plpgsql
immutable
set search_path = ''
as $$
begin
  if utc_offset is null or utc_offset < -720 or utc_offset > 840 or utc_offset % 15 <> 0 then
    raise exception 'bad_offset';
  end if;
  return utc_offset;
end;
$$;

create or replace function private.us_state(st text)
returns boolean
language sql
immutable
set search_path = ''
as $$
  select coalesce(st = any (array[
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'DC', 'FL', 'GA', 'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY',
    'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND', 'OH',
    'OK', 'OR', 'PA', 'PR', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY'
  ]), false);
$$;

-- Null when a handle is fine, else 'handle_invalid' (shape) or 'handle_not_allowed' (blocklist, reserved).
create or replace function private.handle_problem(h text)
returns text
language plpgsql
immutable
set search_path = ''
as $$
declare
  -- Matched anywhere once number-for-letter swaps and underscores are undone ("f_u_c_k", "sh1t").
  anywhere constant text[] := array[
    'fuck', 'shit', 'cunt', 'bitch', 'whore', 'slut', 'nigger', 'nigga', 'faggot', 'retard', 'wetback',
    'tranny', 'nazi', 'hitler', 'porn', 'penis', 'vagina', 'pussy', 'asshole', 'bastard', 'molest', 'twat',
    'jizz', 'dildo', 'blowjob', 'kkk', 'kike', 'chink', 'beaner', 'pedophile', 'pendejo', 'mierda'
  ];
  -- Matched as a whole word only, so "Peacock", "Dickens", "Grape", "Essex" and "Therapist" stay allowed.
  whole_words constant text[] := array[
    'ass', 'arse', 'dick', 'cock', 'cum', 'sex', 'tit', 'tits', 'fag', 'fags', 'dyke', 'spic', 'coon', 'gook',
    'negro', 'homo', 'jap', 'rape', 'raped', 'rapist', 'pedo', 'hoe', 'hoes', 'nig', 'anal', 'anus', 'boob',
    'boobs', 'puta', 'puto', 'wank', 'fuk', 'fck', 'fuq', 'isis'
  ];
  reserved constant text[] := array[
    'admin', 'administrator', 'moderator', 'mod', 'mods', 'support', 'staff', 'official', 'system', 'root'
  ];
  split text;
  squashed text;
  tokens text[];
begin
  if h is null or h !~ '^[A-Za-z0-9_]{3,20}$' then
    return 'handle_invalid';
  end if;
  -- Word breaks at underscores and lower-to-upper changes: "BigDog_23" -> "big_dog_23".
  split := lower(regexp_replace(h, '([a-z0-9])([A-Z])', '\1_\2', 'g'));
  -- Swaps undone, underscores and leftover digits dropped: "Sh1t_Talk3r" -> "shittalker".
  squashed := regexp_replace(translate(split, '01345789_', 'oieastbg'), '[0-9]', '', 'g');
  -- Words as typed ("dick69" -> dick) and with swaps undone ("d1ck" -> dick).
  tokens := regexp_split_to_array(split, '[_0-9]+')
         || regexp_split_to_array(regexp_replace(translate(split, '01345789', 'oieastbg'), '[0-9]', '', 'g'), '_+');
  if exists (select 1 from unnest(anywhere) w where strpos(squashed, w) > 0)
     or tokens && whole_words or squashed = any (whole_words)
     or tokens && reserved or strpos(squashed, 'hoophunter') > 0 then
    return 'handle_not_allowed';
  end if;
  return null;
end;
$$;

revoke all on function private.local_day(timestamptz, integer) from public, anon, authenticated;
revoke all on function private.week_start(date) from public, anon, authenticated;
revoke all on function private.valid_offset(integer) from public, anon, authenticated;
revoke all on function private.us_state(text) from public, anon, authenticated;
revoke all on function private.handle_problem(text) from public, anon, authenticated;

-- Profiles ---------------------------------------------------------------------------------------------

create table if not exists public.profiles (
  user_id          uuid primary key references auth.users (id) on delete cascade,
  -- Public: the only thing other players ever see of you (plus XP, streak and badges).
  handle           text not null check (handle ~ '^[A-Za-z0-9_]{3,20}$'),
  -- Private: share it to let someone follow you. 6 characters, no 0/O, 1/I/L.
  friend_code      text not null check (friend_code ~ '^[2-9A-HJKMNP-Z]{6}$'),
  -- "I'm 13 or older". Profiles are made by confirm_age(), so this is true on every row; every social
  -- function still checks it. An under-13 answer stays on the phone and no profile is ever created.
  age_ok           boolean not null default false,
  age_confirmed_at timestamptz,
  -- For the state leaderboard: picked by the player, or the state of the court of their first check-in.
  home_state       text check (home_state ~ '^[A-Z]{2}$'),
  -- Minutes to add to UTC for the player's local time, as last sent by the phone.
  tz_offset_min    smallint not null default 0 check (tz_offset_min between -720 and 840),
  -- Streak = consecutive local days with a "here now" check-in. Counters only: no per-day history is kept.
  streak_days      integer not null default 0 check (streak_days >= 0),
  streak_last_day  date,
  best_streak      integer not null default 0 check (best_streak >= 0),
  -- Distinct courts checked in at, counted up to 10 (the badge); see private.court_visits.
  courts_visited   integer not null default 0 check (courts_visited >= 0),
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

comment on table public.profiles is
  'One per player who passed the 13+ gate. Public by handle only; written only by SECURITY DEFINER RPCs.';

create unique index if not exists profiles_handle_key on public.profiles (lower(handle));
create unique index if not exists profiles_friend_code_key on public.profiles (friend_code);
create index if not exists profiles_home_state_idx on public.profiles (home_state) where age_ok;

-- Random handle "Hooper" + 4 digits (6 once the 4-digit names get crowded).
create or replace function private.new_handle()
returns text
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  candidate text;
  tries integer := 0;
begin
  loop
    tries := tries + 1;
    candidate := 'Hooper' || case
      when tries <= 20 then (1000 + floor(random() * 9000))::integer::text
      else (100000 + floor(random() * 900000))::integer::text
    end;
    exit when private.handle_problem(candidate) is null
      and not exists (select 1 from public.profiles p where lower(p.handle) = lower(candidate));
  end loop;
  return candidate;
end;
$$;

-- 6 random characters from 31 that can't be misread (about 887 million codes).
create or replace function private.new_friend_code()
returns text
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  alphabet constant text := '23456789ABCDEFGHJKMNPQRSTUVWXYZ';
  bytes bytea;
  code text;
begin
  loop
    bytes := extensions.gen_random_bytes(6);
    code := '';
    for i in 0..5 loop
      code := code || substr(alphabet, 1 + get_byte(bytes, i) % 31, 1);
    end loop;
    exit when not exists (select 1 from public.profiles p where p.friend_code = code);
  end loop;
  return code;
end;
$$;

revoke all on function private.new_handle() from public, anon, authenticated;
revoke all on function private.new_friend_code() from public, anon, authenticated;

-- Check-ins --------------------------------------------------------------------------------------------

create table if not exists public.checkins (
  id          bigint generated always as identity primary key,
  user_id     uuid not null references auth.users (id) on delete cascade,
  court_id    text not null check (char_length(court_id) between 3 and 128),
  -- here = "Here now" (starts_at = check-in time); planned = "Hooping at <time>" today or tomorrow.
  kind        text not null check (kind in ('here', 'planned')),
  starts_at   timestamptz not null,
  -- Always starts_at + 2 hours (trigger below).
  expires_at  timestamptz not null,
  -- Set when the player checks out or checks in somewhere else (one active check-in per player).
  ended_at    timestamptz,
  -- The player's local day when they checked in, and whether it earned XP (once per court per day).
  local_day   date not null,
  xp          boolean not null default false,
  created_at  timestamptz not null default now()
);

comment on table public.checkins is
  'Court id, kind and times only (never coordinates). Rows are deleted 2 days after they expire.';

create index if not exists checkins_court_active_idx on public.checkins (court_id, expires_at) where ended_at is null;
create index if not exists checkins_user_created_idx on public.checkins (user_id, created_at);
create index if not exists checkins_user_court_day_idx on public.checkins (user_id, court_id, local_day);
create index if not exists checkins_expires_idx on public.checkins (expires_at);

create or replace function private.checkin_expiry()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.expires_at := new.starts_at + interval '2 hours';
  return new;
end;
$$;
revoke all on function private.checkin_expiry() from public, anon, authenticated;

drop trigger if exists checkins_expiry on public.checkins;
create trigger checkins_expiry before insert or update on public.checkins
  for each row execute function private.checkin_expiry();

-- Which courts a player has checked in at, kept only until they reach 10 (the courts_10 badge). Then the
-- list is deleted and only the count on the profile stays, so no long-term record of places is kept.
create table if not exists private.court_visits (
  user_id   uuid not null references auth.users (id) on delete cascade,
  court_id  text not null,
  primary key (user_id, court_id)
);
alter table private.court_visits enable row level security;
revoke all on table private.court_visits from public, anon, authenticated;

-- Runs -------------------------------------------------------------------------------------------------

create table if not exists public.runs (
  -- Made on the phone, so a retried post from the offline queue is idempotent.
  id            uuid primary key default gen_random_uuid(),
  court_id      text not null check (char_length(court_id) between 3 and 128),
  creator_id    uuid not null references auth.users (id) on delete cascade,
  format        text not null check (format in ('5v5', '4v4', '3v3', '2v2', '1v1', 'shootaround')),
  skill         text check (skill in ('casual', 'competitive')),
  max_players   smallint check (max_players between 2 and 30),
  starts_at     timestamptz not null,
  -- Always starts_at + 3 hours (trigger below).
  expires_at    timestamptz not null,
  cancelled_at  timestamptz,
  created_at    timestamptz not null default now()
);

comment on table public.runs is
  'Pickup runs: presets only, no text. Expire 3 h after the start; deleted 2 days after that.';

create index if not exists runs_court_active_idx on public.runs (court_id, expires_at) where cancelled_at is null;
create index if not exists runs_creator_idx on public.runs (creator_id, created_at);
create index if not exists runs_expires_idx on public.runs (expires_at);

create or replace function private.run_expiry()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.expires_at := new.starts_at + interval '3 hours';
  return new;
end;
$$;
revoke all on function private.run_expiry() from public, anon, authenticated;

drop trigger if exists runs_expiry on public.runs;
create trigger runs_expiry before insert or update on public.runs
  for each row execute function private.run_expiry();

create table if not exists public.run_players (
  run_id     uuid not null references public.runs (id) on delete cascade,
  user_id    uuid not null references auth.users (id) on delete cascade,
  joined_at  timestamptz not null default now(),
  primary key (run_id, user_id)
);

create index if not exists run_players_user_idx on public.run_players (user_id);

-- Follows and blocks -----------------------------------------------------------------------------------

create table if not exists public.follows (
  follower_id  uuid not null references auth.users (id) on delete cascade,
  followee_id  uuid not null references auth.users (id) on delete cascade,
  created_at   timestamptz not null default now(),
  primary key (follower_id, followee_id),
  check (follower_id <> followee_id)
);

create index if not exists follows_followee_idx on public.follows (followee_id, follower_id);

create table if not exists public.blocks (
  blocker_id  uuid not null references auth.users (id) on delete cascade,
  blocked_id  uuid not null references auth.users (id) on delete cascade,
  created_at  timestamptz not null default now(),
  primary key (blocker_id, blocked_id),
  check (blocker_id <> blocked_id)
);

create index if not exists blocks_blocked_idx on public.blocks (blocked_id, blocker_id);

-- A block works both ways: neither player sees the other's check-ins, runs or leaderboard rows.
create or replace function private.blocked(a uuid, b uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.blocks k
    where (k.blocker_id = a and k.blocked_id = b) or (k.blocker_id = b and k.blocked_id = a)
  );
$$;
revoke all on function private.blocked(uuid, uuid) from public, anon, authenticated;

-- follow_by_code attempts in the last hour (rate limit against guessing codes). Purged after a day.
create table if not exists private.follow_attempts (
  user_id  uuid not null references auth.users (id) on delete cascade,
  tried_at timestamptz not null default now()
);
create index if not exists follow_attempts_user_idx on private.follow_attempts (user_id, tried_at);
alter table private.follow_attempts enable row level security;
revoke all on table private.follow_attempts from public, anon, authenticated;

-- Reports (insert-only for clients) --------------------------------------------------------------------

create table if not exists public.reports (
  id              bigint generated always as identity primary key,
  reporter_id     uuid not null default auth.uid() references auth.users (id) on delete cascade,
  target_user_id  uuid references auth.users (id) on delete cascade,
  -- No foreign key: the report outlives the run (runs are deleted 2 days after they expire).
  run_id          uuid,
  reason          text not null check (reason in ('spam', 'offensive_name', 'harassment', 'fake_run', 'unsafe', 'other')),
  created_at      timestamptz not null default now()
);

comment on table public.reports is
  'Player reports of a player or a run. Clients may only insert; only the service role can read them.';

create index if not exists reports_reporter_idx on public.reports (reporter_id, created_at);
create index if not exists reports_target_idx on public.reports (target_user_id);

-- Server-owned reporter and time, the run's creator as the target, and at most 20 reports a day.
create or replace function private.report_guard()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  creator uuid;
begin
  if auth.uid() is not null then
    new.reporter_id := auth.uid();
    new.created_at := now();
    if (select count(*) from public.reports r
        where r.reporter_id = new.reporter_id and r.created_at > now() - interval '1 day') >= 20 then
      raise exception 'rate_limited';
    end if;
  end if;
  if new.run_id is not null then
    select r.creator_id into creator from public.runs r where r.id = new.run_id;
    if not found then
      raise exception 'run_not_found';
    end if;
    new.target_user_id := coalesce(new.target_user_id, creator);
  end if;
  if new.target_user_id is null then
    raise exception 'nothing_to_report';
  end if;
  if new.target_user_id = new.reporter_id then
    raise exception 'cannot_report_self';
  end if;
  return new;
end;
$$;
revoke all on function private.report_guard() from public, anon, authenticated;

drop trigger if exists reports_guard on public.reports;
create trigger reports_guard before insert on public.reports
  for each row execute function private.report_guard();

-- XP and badges (server-written only) ------------------------------------------------------------------

create table if not exists public.xp_events (
  id          bigint generated always as identity primary key,
  user_id     uuid not null references auth.users (id) on delete cascade,
  kind        text not null check (kind in ('checkin', 'review', 'court_added', 'run_posted', 'run_joined')),
  points      integer not null check (points between 1 and 100),
  -- The player's local day and its Monday (weekly leaderboards).
  local_day   date not null,
  week_start  date not null,
  -- Makes each award happen once: review:<court>, court_added:<id>, run_joined:<run>, checkin:<check-in id>.
  -- Check-in keys use the check-in's row id, not the court, so XP history never says where you were.
  dedupe_key  text not null,
  created_at  timestamptz not null default now(),
  unique (user_id, dedupe_key)
);

comment on table public.xp_events is
  'XP ledger. Written only by SECURITY DEFINER functions and triggers; players can read their own rows.';

create index if not exists xp_events_week_idx on public.xp_events (week_start, user_id) include (points);
create index if not exists xp_events_user_day_idx on public.xp_events (user_id, kind, local_day);

create table if not exists public.user_badges (
  user_id    uuid not null references auth.users (id) on delete cascade,
  badge      text not null check (badge in ('court_hunter', 'first_checkin', 'streak_7', 'streak_30', 'courts_10', 'first_run')),
  earned_at  timestamptz not null default now(),
  primary key (user_id, badge)
);

-- Who found a court first: the player whose submission was approved ('added'), or the first player to say a
-- satellite candidate is a court ('confirmed'). One row per court, never reassigned. Earns "Court Hunter".
create table if not exists public.court_firsts (
  court_id  text primary key,
  user_id   uuid not null references auth.users (id) on delete cascade,
  how       text not null check (how in ('added', 'confirmed')),
  found_at  timestamptz not null default now()
);

-- Returns the points awarded: 0 when already awarded (dedupe_key) or over the daily cap for that kind.
create or replace function private.award_xp(
  p_user uuid, p_kind text, p_points integer, p_key text, p_day date, p_daily_cap integer default null
)
returns integer
language plpgsql
volatile
security definer
set search_path = ''
as $$
begin
  if p_daily_cap is not null and (
    select count(*) from public.xp_events x where x.user_id = p_user and x.kind = p_kind and x.local_day = p_day
  ) >= p_daily_cap then
    return 0;
  end if;
  insert into public.xp_events (user_id, kind, points, local_day, week_start, dedupe_key)
  values (p_user, p_kind, p_points, p_day, private.week_start(p_day), p_key)
  on conflict (user_id, dedupe_key) do nothing;
  if found then
    return p_points;
  end if;
  return 0;
end;
$$;

-- True when the badge is new.
create or replace function private.award_badge(p_user uuid, p_badge text)
returns boolean
language plpgsql
volatile
security definer
set search_path = ''
as $$
begin
  insert into public.user_badges (user_id, badge) values (p_user, p_badge) on conflict do nothing;
  return found;
end;
$$;

revoke all on function private.award_xp(uuid, text, integer, text, date, integer) from public, anon, authenticated;
revoke all on function private.award_badge(uuid, text) from public, anon, authenticated;

-- The player's local day from the offset their phone last sent (UTC before they have a profile).
create or replace function private.player_day(p_user uuid)
returns date
language sql
stable
security definer
set search_path = ''
as $$
  select private.local_day(now(), coalesce((select p.tz_offset_min from public.profiles p where p.user_id = p_user), 0));
$$;
revoke all on function private.player_day(uuid) from public, anon, authenticated;

-- XP for reviewing a satellite candidate: 5 for the first review of each court (changing your mind or
-- undoing and redoing earns nothing more), at most 25 a day. The first player to say "It's a court" earns
-- Court Hunter. Reviews by players who never opened the social features still count once they do.
create or replace function private.review_xp()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  begin
    if exists (select 1 from public.courts c where c.id = new.court_id and c.status = 'candidate') then
      perform private.award_xp(new.user_id, 'review', 5, 'review:' || new.court_id, private.player_day(new.user_id), 25);
      if new.decision = 'court' then
        insert into public.court_firsts (court_id, user_id, how) values (new.court_id, new.user_id, 'confirmed')
        on conflict (court_id) do nothing;
        if found then
          perform private.award_badge(new.user_id, 'court_hunter');
        end if;
      end if;
    end if;
  exception when others then
    -- XP must never block a review.
    raise warning 'review XP for % failed: %', new.court_id, sqlerrm;
  end;
  return null;
end;
$$;
revoke all on function private.review_xp() from public, anon, authenticated;

drop trigger if exists reviews_xp on public.reviews;
create trigger reviews_xp after insert or update of decision on public.reviews
  for each row execute function private.review_xp();

-- XP for a player-added court once a moderator approves it (private.approve_user_court or a status update):
-- 25 XP and Court Hunter.
create or replace function private.user_court_xp()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.status = 'approved' and (tg_op = 'INSERT' or old.status is distinct from 'approved') then
    begin
      perform private.award_xp(new.user_id, 'court_added', 25, 'court_added:' || new.id, private.player_day(new.user_id));
      insert into public.court_firsts (court_id, user_id, how) values (new.id, new.user_id, 'added')
      on conflict (court_id) do nothing;
      if found then
        perform private.award_badge(new.user_id, 'court_hunter');
      end if;
    exception when others then
      raise warning 'court XP for % failed: %', new.id, sqlerrm;
    end;
  end if;
  return null;
end;
$$;
revoke all on function private.user_court_xp() from public, anon, authenticated;

drop trigger if exists user_courts_xp on public.user_courts;
create trigger user_courts_xp after insert or update of status on public.user_courts
  for each row execute function private.user_court_xp();

-- Housekeeping: check-ins and runs go 2 days after they expire, follow attempts after a day. Called by the
-- write RPCs (a few hundred rows at a time), so no cron job is needed.
create or replace function private.purge_social()
returns void
language sql
volatile
security definer
set search_path = ''
as $$
  delete from public.checkins
  where id in (select c.id from public.checkins c where c.expires_at < now() - interval '2 days' limit 500);
  delete from public.runs
  where id in (select r.id from public.runs r where r.expires_at < now() - interval '2 days' limit 200);
  delete from private.follow_attempts where tried_at < now() - interval '1 day';
$$;
revoke all on function private.purge_social() from public, anon, authenticated;

-- Row level security and grants ------------------------------------------------------------------------
-- Clients read their own profile, check-ins, XP and badges directly (RLS: own rows); everything that shows
-- other players goes through the RPCs in part 2. No client can write any of these tables, except INSERT
-- on reports.

alter table public.profiles enable row level security;
alter table public.checkins enable row level security;
alter table public.runs enable row level security;
alter table public.run_players enable row level security;
alter table public.follows enable row level security;
alter table public.blocks enable row level security;
alter table public.reports enable row level security;
alter table public.xp_events enable row level security;
alter table public.user_badges enable row level security;
alter table public.court_firsts enable row level security;

drop policy if exists "Players read their own profile" on public.profiles;
create policy "Players read their own profile"
  on public.profiles for select to authenticated
  using (user_id = (select auth.uid()));

drop policy if exists "Players read their own check-ins" on public.checkins;
create policy "Players read their own check-ins"
  on public.checkins for select to authenticated
  using (user_id = (select auth.uid()));

drop policy if exists "Players read their own XP" on public.xp_events;
create policy "Players read their own XP"
  on public.xp_events for select to authenticated
  using (user_id = (select auth.uid()));

drop policy if exists "Players read their own badges" on public.user_badges;
create policy "Players read their own badges"
  on public.user_badges for select to authenticated
  using (user_id = (select auth.uid()));

drop policy if exists "Players file their own reports" on public.reports;
create policy "Players file their own reports"
  on public.reports for insert to authenticated
  with check (reporter_id = (select auth.uid()));

-- runs, run_players, follows, blocks, court_firsts: RLS on, no policies, no client grants (RPC only).

revoke all on table public.profiles, public.checkins, public.runs, public.run_players, public.follows,
  public.blocks, public.reports, public.xp_events, public.user_badges, public.court_firsts
  from public, anon, authenticated;

grant select on table public.profiles, public.checkins, public.xp_events, public.user_badges to authenticated;
-- Column grant: a client can't set reporter_id or created_at (the trigger owns them).
grant insert (target_user_id, run_id, reason) on table public.reports to authenticated;

grant select, insert, update, delete on table public.profiles, public.checkins, public.runs, public.run_players,
  public.follows, public.blocks, public.reports, public.xp_events, public.user_badges, public.court_firsts
  to service_role;
