-- Hoop Hunter: social features, part 2 of 2: the RPCs the app calls.
--
-- All SECURITY DEFINER with an empty search_path, executable by `authenticated` only (anonymous sign-ins
-- count). Each one finds the caller with auth.uid() and never takes a user id as an argument, so nobody can
-- act as someone else. Other players are named by handle; their ids and friend codes never leave the database.
-- Every function except confirm_age / my_profile requires the 13+ gate (a profile with age_ok).
--
-- Errors are raised as plain messages the app maps to text (HTTP 400, code P0001):
--   not_signed_in, age_gate, bad_offset, court_not_found, bad_kind, bad_time, rate_limited, run_not_found,
--   run_closed, run_full, too_many_runs, creator_cannot_leave, not_your_run, bad_run, bad_format, bad_skill,
--   bad_limit, player_not_found, cannot_block_self, handle_invalid, handle_not_allowed, handle_taken, bad_state.
--
-- utc_offset is always minutes to ADD to UTC for the phone's local time (JavaScript: -getTimezoneOffset()).
-- Re-runnable.

-- The calling player's profile; raises when signed out or before the 13+ gate.
create or replace function private.player()
returns public.profiles
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  me public.profiles;
begin
  if auth.uid() is null then
    raise exception 'not_signed_in';
  end if;
  select * into me from public.profiles p where p.user_id = auth.uid();
  if not found or not me.age_ok then
    raise exception 'age_gate';
  end if;
  return me;
end;
$$;

-- Same, with the profile row locked: one streak / XP update at a time per player.
create or replace function private.player_locked()
returns public.profiles
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  me public.profiles;
begin
  if auth.uid() is null then
    raise exception 'not_signed_in';
  end if;
  select * into me from public.profiles p where p.user_id = auth.uid() for update;
  if not found or not me.age_ok then
    raise exception 'age_gate';
  end if;
  return me;
end;
$$;

-- Courts the app can show this player: published courts, plus the player's own submissions (not rejected).
create or replace function private.court_known(p_court text, p_user uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (select 1 from public.courts c where c.id = p_court and c.status in ('verified', 'candidate'))
      or exists (select 1 from public.user_courts u where u.id = p_court and u.user_id = p_user and u.status <> 'rejected');
$$;

-- Name, state and position of a court (public data), for "your runs" and "your check-in".
create or replace function private.court_info(p_court text, p_viewer uuid)
returns table (name text, state text, lon double precision, lat double precision)
language sql
stable
security definer
set search_path = ''
as $$
  select c.name, c.state, c.lon, c.lat from public.courts c where c.id = p_court
  union all
  select u.name, null::text, u.lon, u.lat from public.user_courts u
  where u.id = p_court and (u.user_id = p_viewer or u.status = 'approved')
    and not exists (select 1 from public.courts c2 where c2.id = p_court)
  limit 1;
$$;

create or replace function private.profile_by_handle(p_handle text)
returns public.profiles
language sql
stable
security definer
set search_path = ''
as $$
  select * from public.profiles p where lower(p.handle) = lower(p_handle) and p.age_ok;
$$;

-- A run as the app shows it to `viewer`. Player names leave out anyone blocked either way.
create or replace function private.run_json(r public.runs, viewer uuid)
returns jsonb
language sql
stable
security definer
set search_path = ''
as $$
  select jsonb_build_object(
    'id', r.id,
    'court_id', r.court_id,
    'format', r.format,
    'skill', r.skill,
    'max_players', r.max_players,
    'starts_at', r.starts_at,
    'expires_at', r.expires_at,
    'cancelled', r.cancelled_at is not null,
    'players', (select count(*) from public.run_players rp where rp.run_id = r.id),
    'names', (
      select coalesce(jsonb_agg(x.handle order by x.is_creator desc, x.joined_at), '[]'::jsonb)
      from (
        select p.handle, rp.user_id = r.creator_id as is_creator, rp.joined_at
        from public.run_players rp join public.profiles p on p.user_id = rp.user_id
        where rp.run_id = r.id and (rp.user_id = viewer or not private.blocked(viewer, rp.user_id))
        order by rp.user_id = r.creator_id desc, rp.joined_at
        limit 12
      ) x
    ),
    'creator', (select p.handle from public.profiles p where p.user_id = r.creator_id),
    'mine', r.creator_id = viewer,
    'joined', exists (select 1 from public.run_players rp where rp.run_id = r.id and rp.user_id = viewer)
  );
$$;

revoke all on function private.player() from public, anon, authenticated;
revoke all on function private.player_locked() from public, anon, authenticated;
revoke all on function private.court_known(text, uuid) from public, anon, authenticated;
revoke all on function private.court_info(text, uuid) from public, anon, authenticated;
revoke all on function private.profile_by_handle(text) from public, anon, authenticated;
revoke all on function private.run_json(public.runs, uuid) from public, anon, authenticated;

-- Profile ----------------------------------------------------------------------------------------------

-- The caller's profile, or null before the 13+ gate. Also remembers the phone's UTC offset (used to date
-- review XP, which has no offset of its own).
create or replace function public.my_profile(utc_offset integer default 0)
returns jsonb
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  uid uuid := auth.uid();
  me public.profiles;
  today date;
begin
  if uid is null then
    raise exception 'not_signed_in';
  end if;
  perform private.valid_offset(utc_offset);
  select * into me from public.profiles p where p.user_id = uid;
  if not found then
    return null;
  end if;
  if me.tz_offset_min <> utc_offset then
    update public.profiles p set tz_offset_min = utc_offset where p.user_id = uid;
  end if;
  today := private.local_day(now(), utc_offset);
  return jsonb_build_object(
    'handle', me.handle,
    'friend_code', me.friend_code,
    'home_state', me.home_state,
    'today', today,
    'xp_total', (select coalesce(sum(x.points), 0) from public.xp_events x where x.user_id = uid),
    'xp_week', (select coalesce(sum(x.points), 0) from public.xp_events x
                where x.user_id = uid and x.week_start = private.week_start(today)),
    -- A streak is alive through the day after its last check-in; after that it shows 0.
    'streak', case when me.streak_last_day >= today - 1 then me.streak_days else 0 end,
    'streak_today', coalesce(me.streak_last_day >= today, false),
    'best_streak', me.best_streak,
    'courts_visited', me.courts_visited,
    'badges', (select coalesce(jsonb_agg(jsonb_build_object('badge', b.badge, 'earned_at', b.earned_at) order by b.earned_at), '[]'::jsonb)
               from public.user_badges b where b.user_id = uid),
    'following', (select count(*) from public.follows f where f.follower_id = uid),
    'followers', (select count(*) from public.follows f where f.followee_id = uid),
    'checkin', (
      select jsonb_build_object('court_id', c.court_id, 'court_name', i.name, 'kind', c.kind,
                                'starts_at', c.starts_at, 'expires_at', c.expires_at)
      from public.checkins c left join lateral private.court_info(c.court_id, uid) i on true
      where c.user_id = uid and c.ended_at is null and c.expires_at > now()
      order by c.created_at desc
      limit 1
    )
  );
end;
$$;

-- "I'm 13 or older": creates the profile (random handle and friend code) on first use.
create or replace function public.confirm_age(utc_offset integer default 0)
returns jsonb
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  uid uuid := auth.uid();
begin
  if uid is null then
    raise exception 'not_signed_in';
  end if;
  perform private.valid_offset(utc_offset);
  insert into public.profiles as p (user_id, handle, friend_code, age_ok, age_confirmed_at, tz_offset_min)
  values (uid, private.new_handle(), private.new_friend_code(), true, now(), utc_offset)
  on conflict (user_id) do update
    set age_ok = true, age_confirmed_at = coalesce(p.age_confirmed_at, now()), updated_at = now();
  return public.my_profile(utc_offset);
end;
$$;

create or replace function public.set_handle(new_handle text)
returns text
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player();
  problem text := private.handle_problem(new_handle);
begin
  if problem is not null then
    raise exception '%', problem;
  end if;
  if exists (select 1 from public.profiles p where lower(p.handle) = lower(new_handle) and p.user_id <> me.user_id) then
    raise exception 'handle_taken';
  end if;
  begin
    update public.profiles p set handle = new_handle, updated_at = now() where p.user_id = me.user_id;
  exception when unique_violation then
    raise exception 'handle_taken';
  end;
  return new_handle;
end;
$$;

-- Two-letter state for the state leaderboard; null clears it.
create or replace function public.set_home_state(st text)
returns text
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player();
begin
  if st is not null and not private.us_state(upper(st)) then
    raise exception 'bad_state';
  end if;
  update public.profiles p set home_state = upper(st), updated_at = now() where p.user_id = me.user_id;
  return upper(st);
end;
$$;

-- A new friend code (if the old one was shared too widely). People already following you keep following.
create or replace function public.reset_friend_code()
returns text
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player();
  code text := private.new_friend_code();
begin
  update public.profiles p set friend_code = code, updated_at = now() where p.user_id = me.user_id;
  return code;
end;
$$;

-- A player's public card: handle, XP, streak and badges. Null if they don't exist or a block stands.
create or replace function public.public_profile(player text)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player();
  them public.profiles := private.profile_by_handle(player);
  today date;
begin
  if them.user_id is null or (them.user_id <> me.user_id and private.blocked(me.user_id, them.user_id)) then
    return null;
  end if;
  today := private.local_day(now(), them.tz_offset_min);
  return jsonb_build_object(
    'handle', them.handle,
    'me', them.user_id = me.user_id,
    'xp_week', (select coalesce(sum(x.points), 0) from public.xp_events x
                where x.user_id = them.user_id and x.week_start = private.week_start(today)),
    'xp_total', (select coalesce(sum(x.points), 0) from public.xp_events x where x.user_id = them.user_id),
    'streak', case when them.streak_last_day >= today - 1 then them.streak_days else 0 end,
    'best_streak', them.best_streak,
    'badges', (select coalesce(jsonb_agg(b.badge order by b.earned_at), '[]'::jsonb)
               from public.user_badges b where b.user_id = them.user_id),
    'you_follow', exists (select 1 from public.follows f where f.follower_id = me.user_id and f.followee_id = them.user_id),
    'follows_you', exists (select 1 from public.follows f where f.follower_id = them.user_id and f.followee_id = me.user_id)
  );
end;
$$;

-- Check-ins --------------------------------------------------------------------------------------------

-- mode 'here' (now; the phone has already checked you're within ~250 m) or 'planned' (start_at today or
-- tomorrow, your local time). Ends your previous check-in. "Here now" earns 10 XP for the first check-in at
-- a court each local day (at most 4 courts a day) and extends your streak.
create or replace function public.check_in(
  court text, mode text default 'here', start_at timestamptz default null, utc_offset integer default 0
)
returns jsonb
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player_locked();
  today date;
  starts timestamptz;
  new_id bigint;
  earned integer := 0;
  new_badges text[] := '{}';
  streak integer := me.streak_days;
  best integer := me.best_streak;
  last_day date := me.streak_last_day;
  visits integer := me.courts_visited;
begin
  perform private.valid_offset(utc_offset);
  if mode is null or mode not in ('here', 'planned') then
    raise exception 'bad_kind';
  end if;
  if court is null or not private.court_known(court, me.user_id) then
    raise exception 'court_not_found';
  end if;
  if (select count(*) from public.checkins c
      where c.user_id = me.user_id and c.created_at > now() - interval '1 hour') >= 20 then
    raise exception 'rate_limited';
  end if;

  today := private.local_day(now(), utc_offset);
  if mode = 'here' then
    starts := now();
  else
    if start_at is null or start_at < now() - interval '5 minutes'
       or private.local_day(start_at, utc_offset) > today + 1 then
      raise exception 'bad_time';
    end if;
    starts := start_at;
  end if;

  -- One active check-in per player: a new one ends the old one.
  update public.checkins c set ended_at = now()
  where c.user_id = me.user_id and c.ended_at is null and c.expires_at > now();

  insert into public.checkins (user_id, court_id, kind, starts_at, local_day)
  values (me.user_id, court, mode, starts, today)
  returning id into new_id;

  if mode = 'here' then
    if not exists (select 1 from public.checkins c
                   where c.user_id = me.user_id and c.court_id = court and c.local_day = today and c.xp) then
      earned := private.award_xp(me.user_id, 'checkin', 10, 'checkin:' || new_id, today, 4);
      if earned > 0 then
        update public.checkins c set xp = true where c.id = new_id;
      end if;
    end if;

    -- Streak over local days. It never moves backwards: a check-in dated before the last streak day (a
    -- smaller offset after travelling west) changes nothing.
    if last_day is null or today > last_day + 1 then
      streak := 1;
    elsif today = last_day + 1 then
      streak := streak + 1;
    end if;
    last_day := greatest(last_day, today);
    best := greatest(best, streak);

    if visits < 10 then
      insert into private.court_visits (user_id, court_id) values (me.user_id, court) on conflict do nothing;
      if found then
        visits := visits + 1;
      end if;
      if visits >= 10 then
        delete from private.court_visits v where v.user_id = me.user_id;
      end if;
    end if;

    if private.award_badge(me.user_id, 'first_checkin') then
      new_badges := array_append(new_badges, 'first_checkin');
    end if;
    if streak >= 7 then
      if private.award_badge(me.user_id, 'streak_7') then
        new_badges := array_append(new_badges, 'streak_7');
      end if;
    end if;
    if streak >= 30 then
      if private.award_badge(me.user_id, 'streak_30') then
        new_badges := array_append(new_badges, 'streak_30');
      end if;
    end if;
    if visits >= 10 then
      if private.award_badge(me.user_id, 'courts_10') then
        new_badges := array_append(new_badges, 'courts_10');
      end if;
    end if;
  end if;

  update public.profiles p set
    tz_offset_min = utc_offset,
    streak_days = streak,
    streak_last_day = last_day,
    best_streak = best,
    courts_visited = visits,
    home_state = coalesce(p.home_state, (select c.state from public.courts c where c.id = court)),
    updated_at = now()
  where p.user_id = me.user_id;

  perform private.purge_social();

  return jsonb_build_object(
    'checkin', (select jsonb_build_object('court_id', c.court_id, 'kind', c.kind, 'starts_at', c.starts_at,
                                          'expires_at', c.expires_at, 'local_day', c.local_day)
                from public.checkins c where c.id = new_id),
    'xp', earned,
    'streak', streak,
    'badges', to_jsonb(new_badges)
  );
end;
$$;

-- Check out (or drop a planned time). XP already earned stays.
create or replace function public.end_checkin()
returns void
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player();
begin
  update public.checkins c set ended_at = now()
  where c.user_id = me.user_id and c.ended_at is null and c.expires_at > now();
end;
$$;

-- How busy a court is: players here now, planned check-ins in the next hours_ahead hours grouped by time,
-- the names of mutual follows ("friends") among them, and your own check-in. Counts only for everyone else.
create or replace function public.court_activity(court text, hours_ahead integer default 6)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player();
  horizon timestamptz := now() + make_interval(hours => least(greatest(coalesce(hours_ahead, 6), 1), 48));
begin
  return (
    with active as (
      select c.user_id, c.kind, c.starts_at, c.expires_at
      from public.checkins c
      where c.court_id = court and c.ended_at is null and c.expires_at > now()
        and (c.user_id = me.user_id or not private.blocked(me.user_id, c.user_id))
    )
    select jsonb_build_object(
      'here', (select count(*) from active a where a.kind = 'here'),
      'planned', (
        select coalesce(jsonb_agg(jsonb_build_object('at', g.starts_at, 'count', g.n) order by g.starts_at), '[]'::jsonb)
        from (
          select a.starts_at, count(*) as n from active a
          where a.kind = 'planned' and a.starts_at <= horizon
          group by a.starts_at order by a.starts_at limit 12
        ) g
      ),
      'friends', (
        select coalesce(jsonb_agg(jsonb_build_object('handle', p.handle, 'kind', a.kind, 'starts_at', a.starts_at)
                                  order by a.kind, a.starts_at), '[]'::jsonb)
        from active a join public.profiles p on p.user_id = a.user_id
        where a.user_id <> me.user_id
          and exists (select 1 from public.follows f where f.follower_id = me.user_id and f.followee_id = a.user_id)
          and exists (select 1 from public.follows f where f.follower_id = a.user_id and f.followee_id = me.user_id)
      ),
      'mine', (
        select jsonb_build_object('kind', a.kind, 'starts_at', a.starts_at, 'expires_at', a.expires_at)
        from active a where a.user_id = me.user_id limit 1
      )
    )
  );
end;
$$;

-- Runs -------------------------------------------------------------------------------------------------

-- Post a run (you're in it). `run` is a uuid made on the phone, so a retry returns the same run.
-- 10 XP (at most 2 runs a day), first_run badge. At most 3 upcoming runs per player.
create or replace function public.post_run(
  run uuid, court text, run_format text, start_at timestamptz,
  skill_level text default null, player_limit integer default null, utc_offset integer default 0
)
returns jsonb
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player_locked();
  r public.runs;
  earned integer := 0;
  new_badges text[] := '{}';
begin
  perform private.valid_offset(utc_offset);
  if run is null then
    raise exception 'bad_run';
  end if;
  select * into r from public.runs x where x.id = run;
  if found then
    if r.creator_id <> me.user_id then
      raise exception 'bad_run';
    end if;
    return jsonb_build_object('run', private.run_json(r, me.user_id), 'xp', 0, 'badges', '[]'::jsonb);
  end if;
  if court is null or not private.court_known(court, me.user_id) then
    raise exception 'court_not_found';
  end if;
  if run_format is null or run_format not in ('5v5', '4v4', '3v3', '2v2', '1v1', 'shootaround') then
    raise exception 'bad_format';
  end if;
  if skill_level is not null and skill_level not in ('casual', 'competitive') then
    raise exception 'bad_skill';
  end if;
  if player_limit is not null and (player_limit < 2 or player_limit > 30) then
    raise exception 'bad_limit';
  end if;
  if start_at is null or start_at < now() - interval '15 minutes' or start_at > now() + interval '7 days' then
    raise exception 'bad_time';
  end if;
  if (select count(*) from public.runs x
      where x.creator_id = me.user_id and x.cancelled_at is null and x.expires_at > now()) >= 3 then
    raise exception 'too_many_runs';
  end if;
  if (select count(*) from public.runs x
      where x.creator_id = me.user_id and x.created_at > now() - interval '1 day') >= 10 then
    raise exception 'rate_limited';
  end if;

  insert into public.runs (id, court_id, creator_id, format, skill, max_players, starts_at)
  values (run, court, me.user_id, run_format, skill_level, player_limit, start_at)
  returning * into r;
  insert into public.run_players (run_id, user_id) values (r.id, me.user_id);

  earned := private.award_xp(me.user_id, 'run_posted', 10, 'run_posted:' || r.id,
                             private.local_day(now(), utc_offset), 2);
  if private.award_badge(me.user_id, 'first_run') then
    new_badges := array_append(new_badges, 'first_run');
  end if;
  update public.profiles p set
    tz_offset_min = utc_offset,
    home_state = coalesce(p.home_state, (select c.state from public.courts c where c.id = court)),
    updated_at = now()
  where p.user_id = me.user_id;

  perform private.purge_social();
  return jsonb_build_object('run', private.run_json(r, me.user_id), 'xp', earned, 'badges', to_jsonb(new_badges));
end;
$$;

-- "I'm in". 5 XP for someone else's run (once per run, at most 3 a day), first_run badge.
create or replace function public.join_run(run uuid, utc_offset integer default 0)
returns jsonb
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player_locked();
  r public.runs;
  earned integer := 0;
  new_badges text[] := '{}';
begin
  perform private.valid_offset(utc_offset);
  select * into r from public.runs x where x.id = run for update;
  if not found or private.blocked(me.user_id, r.creator_id) then
    raise exception 'run_not_found';
  end if;
  if r.cancelled_at is not null or r.expires_at <= now() then
    raise exception 'run_closed';
  end if;
  if exists (select 1 from public.run_players rp where rp.run_id = r.id and rp.user_id = me.user_id) then
    return jsonb_build_object('run', private.run_json(r, me.user_id), 'xp', 0, 'badges', '[]'::jsonb);
  end if;
  if r.max_players is not null
     and (select count(*) from public.run_players rp where rp.run_id = r.id) >= r.max_players then
    raise exception 'run_full';
  end if;
  if (select count(*) from public.run_players rp join public.runs x on x.id = rp.run_id
      where rp.user_id = me.user_id and x.cancelled_at is null and x.expires_at > now()) >= 10 then
    raise exception 'too_many_runs';
  end if;

  insert into public.run_players (run_id, user_id) values (r.id, me.user_id);
  if r.creator_id <> me.user_id then
    earned := private.award_xp(me.user_id, 'run_joined', 5, 'run_joined:' || r.id,
                               private.local_day(now(), utc_offset), 3);
  end if;
  if private.award_badge(me.user_id, 'first_run') then
    new_badges := array_append(new_badges, 'first_run');
  end if;
  update public.profiles p set tz_offset_min = utc_offset where p.user_id = me.user_id;
  return jsonb_build_object('run', private.run_json(r, me.user_id), 'xp', earned, 'badges', to_jsonb(new_badges));
end;
$$;

-- "Leave". The creator cancels instead.
create or replace function public.leave_run(run uuid)
returns jsonb
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player();
  r public.runs;
begin
  select * into r from public.runs x where x.id = run;
  if not found then
    raise exception 'run_not_found';
  end if;
  if r.creator_id = me.user_id then
    raise exception 'creator_cannot_leave';
  end if;
  delete from public.run_players rp where rp.run_id = r.id and rp.user_id = me.user_id;
  return private.run_json(r, me.user_id);
end;
$$;

create or replace function public.cancel_run(run uuid)
returns jsonb
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player();
  r public.runs;
begin
  select * into r from public.runs x where x.id = run for update;
  if not found then
    raise exception 'run_not_found';
  end if;
  if r.creator_id <> me.user_id then
    raise exception 'not_your_run';
  end if;
  update public.runs x set cancelled_at = coalesce(x.cancelled_at, now()) where x.id = r.id returning * into r;
  return private.run_json(r, me.user_id);
end;
$$;

-- Upcoming runs at a court (not cancelled, not expired), soonest first. Hidden when a block stands.
create or replace function public.court_runs(court text)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player();
begin
  return (
    select coalesce(jsonb_agg(t.run order by t.starts_at), '[]'::jsonb)
    from (
      select private.run_json(x, me.user_id) as run, x.starts_at
      from public.runs x
      where x.court_id = court and x.cancelled_at is null and x.expires_at > now()
        and (x.creator_id = me.user_id or not private.blocked(me.user_id, x.creator_id))
      order by x.starts_at
      limit 20
    ) t
  );
end;
$$;

-- Runs you're in, with the court's name and position so the app can take you there.
create or replace function public.my_runs()
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player();
begin
  return (
    select coalesce(jsonb_agg(
             private.run_json(r, me.user_id) || jsonb_build_object('court_name', i.name, 'lon', i.lon, 'lat', i.lat)
             order by r.starts_at), '[]'::jsonb)
    from public.run_players rp
    join public.runs r on r.id = rp.run_id
    left join lateral private.court_info(r.court_id, me.user_id) i on true
    where rp.user_id = me.user_id and r.cancelled_at is null and r.expires_at > now()
  );
end;
$$;

-- Follows ----------------------------------------------------------------------------------------------

-- Follow someone by their friend code (case, spaces and dashes ignored). Returns a status instead of raising
-- so failed attempts are counted: 20 tries an hour. status: followed | already | not_found | own_code |
-- rate_limited | too_many. A block either way looks like not_found.
create or replace function public.follow_by_code(code text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player();
  cleaned text := upper(regexp_replace(coalesce(code, ''), '[^A-Za-z0-9]', '', 'g'));
  them public.profiles;
  added boolean;
begin
  if (select count(*) from private.follow_attempts a
      where a.user_id = me.user_id and a.tried_at > now() - interval '1 hour') >= 20 then
    return jsonb_build_object('status', 'rate_limited');
  end if;
  insert into private.follow_attempts (user_id) values (me.user_id);

  select * into them from public.profiles p where p.friend_code = cleaned and p.age_ok;
  if not found or private.blocked(me.user_id, them.user_id) then
    return jsonb_build_object('status', 'not_found');
  end if;
  if them.user_id = me.user_id then
    return jsonb_build_object('status', 'own_code');
  end if;
  if (select count(*) from public.follows f where f.follower_id = me.user_id) >= 500 then
    return jsonb_build_object('status', 'too_many');
  end if;
  insert into public.follows (follower_id, followee_id) values (me.user_id, them.user_id) on conflict do nothing;
  added := found;
  return jsonb_build_object(
    'status', case when added then 'followed' else 'already' end,
    'handle', them.handle,
    'mutual', exists (select 1 from public.follows f where f.follower_id = them.user_id and f.followee_id = me.user_id)
  );
end;
$$;

create or replace function public.unfollow(player text)
returns boolean
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player();
begin
  delete from public.follows f
  where f.follower_id = me.user_id
    and f.followee_id = (select p.user_id from public.profiles p where lower(p.handle) = lower(player));
  return found;
end;
$$;

-- Your own lists only (there are no follower lists of other people): who you follow, who follows you (so you
-- can block them), and who you've blocked.
create or replace function public.my_follows()
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player();
begin
  return jsonb_build_object(
    'following', (
      select coalesce(jsonb_agg(jsonb_build_object(
               'handle', p.handle,
               'mutual', exists (select 1 from public.follows b where b.follower_id = f.followee_id and b.followee_id = me.user_id)
             ) order by lower(p.handle)), '[]'::jsonb)
      from public.follows f join public.profiles p on p.user_id = f.followee_id
      where f.follower_id = me.user_id
    ),
    'followers', (
      select coalesce(jsonb_agg(jsonb_build_object(
               'handle', p.handle,
               'mutual', exists (select 1 from public.follows b where b.follower_id = me.user_id and b.followee_id = f.follower_id)
             ) order by lower(p.handle)), '[]'::jsonb)
      from public.follows f join public.profiles p on p.user_id = f.follower_id
      where f.followee_id = me.user_id
    ),
    'blocked', (
      select coalesce(jsonb_agg(p.handle order by lower(p.handle)), '[]'::jsonb)
      from public.blocks k join public.profiles p on p.user_id = k.blocked_id
      where k.blocker_id = me.user_id
    )
  );
end;
$$;

-- Blocks and reports -----------------------------------------------------------------------------------

-- Block a player: both follows go, they leave the runs you host and you leave theirs, and from then on
-- neither sees the other's check-ins, runs or leaderboard rows.
create or replace function public.block_player(player text)
returns boolean
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player();
  them public.profiles;
begin
  select * into them from public.profiles p where lower(p.handle) = lower(player);
  if not found then
    raise exception 'player_not_found';
  end if;
  if them.user_id = me.user_id then
    raise exception 'cannot_block_self';
  end if;
  if (select count(*) from public.blocks k where k.blocker_id = me.user_id) >= 1000 then
    raise exception 'rate_limited';
  end if;
  insert into public.blocks (blocker_id, blocked_id) values (me.user_id, them.user_id) on conflict do nothing;
  delete from public.follows f
  where (f.follower_id = me.user_id and f.followee_id = them.user_id)
     or (f.follower_id = them.user_id and f.followee_id = me.user_id);
  delete from public.run_players rp using public.runs r
  where rp.run_id = r.id and r.expires_at > now()
    and ((r.creator_id = me.user_id and rp.user_id = them.user_id)
      or (r.creator_id = them.user_id and rp.user_id = me.user_id));
  return true;
end;
$$;

create or replace function public.unblock_player(player text)
returns boolean
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player();
begin
  delete from public.blocks k
  where k.blocker_id = me.user_id
    and k.blocked_id = (select p.user_id from public.profiles p where lower(p.handle) = lower(player));
  return found;
end;
$$;

-- Reports go into public.reports (insert-only for clients; private.report_guard owns the reporter, the time
-- and the 20-a-day limit). reason: spam | offensive_name | harassment | fake_run | unsafe | other.
create or replace function public.report_player(player text, reason text)
returns boolean
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player();
  them public.profiles;
begin
  select * into them from public.profiles p where lower(p.handle) = lower(player);
  if not found then
    raise exception 'player_not_found';
  end if;
  insert into public.reports (reporter_id, target_user_id, reason) values (me.user_id, them.user_id, report_player.reason);
  return true;
end;
$$;

create or replace function public.report_run(run uuid, reason text)
returns boolean
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player();
begin
  insert into public.reports (reporter_id, run_id, reason) values (me.user_id, run, report_run.reason);
  return true;
end;
$$;

-- Weekly leaderboards (XP since Monday, your local week) -----------------------------------------------

-- You and the people you follow, ranked by XP this week (zeros included).
create or replace function public.leaderboard_friends(utc_offset integer default 0)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player();
  week date := private.week_start(private.local_day(now(), private.valid_offset(utc_offset)));
begin
  return (
    with people as (
      select me.user_id as user_id
      union
      select f.followee_id from public.follows f where f.follower_id = me.user_id
    ),
    scored as (
      select p.handle, p.user_id = me.user_id as is_me,
             coalesce((select sum(x.points) from public.xp_events x
                       where x.user_id = p.user_id and x.week_start = week), 0)::integer as xp
      from people pe join public.profiles p on p.user_id = pe.user_id
      where p.age_ok and (p.user_id = me.user_id or not private.blocked(me.user_id, p.user_id))
    ),
    ranked as (
      select s.*, rank() over (order by s.xp desc) as rnk from scored s
    )
    select coalesce(jsonb_agg(jsonb_build_object('rank', r.rnk, 'handle', r.handle, 'xp', r.xp, 'me', r.is_me)
                              order by r.rnk, lower(r.handle)), '[]'::jsonb)
    from ranked r
  );
end;
$$;

-- Top 50 in a state this week (players with XP this week), plus your own rank. st null = your home state.
create or replace function public.leaderboard_state(st text default null, utc_offset integer default 0)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  me public.profiles := private.player();
  week date := private.week_start(private.local_day(now(), private.valid_offset(utc_offset)));
  target_state text := coalesce(upper(st), me.home_state);
begin
  if target_state is null then
    return jsonb_build_object('state', null, 'week_start', week, 'entries', '[]'::jsonb, 'me', null);
  end if;
  if not private.us_state(target_state) then
    raise exception 'bad_state';
  end if;
  return (
    with scored as (
      select x.user_id, sum(x.points)::integer as xp
      from public.xp_events x
      where x.week_start = week
      group by x.user_id
    ),
    ranked as (
      select p.user_id, p.handle, s.xp, rank() over (order by s.xp desc) as rnk
      from scored s join public.profiles p on p.user_id = s.user_id
      where p.home_state = target_state and p.age_ok
        and (p.user_id = me.user_id or not private.blocked(me.user_id, p.user_id))
    )
    select jsonb_build_object(
      'state', target_state,
      'week_start', week,
      'entries', (
        select coalesce(jsonb_agg(jsonb_build_object('rank', t.rnk, 'handle', t.handle, 'xp', t.xp, 'me', t.user_id = me.user_id)
                                  order by t.rnk, lower(t.handle)), '[]'::jsonb)
        from (select * from ranked order by rnk, lower(handle) limit 50) t
      ),
      'me', (select jsonb_build_object('rank', m.rnk, 'xp', m.xp) from ranked m where m.user_id = me.user_id)
    )
  );
end;
$$;

-- Grants -----------------------------------------------------------------------------------------------

do $$
declare
  fn text;
begin
  foreach fn in array array[
    'public.my_profile(integer)',
    'public.confirm_age(integer)',
    'public.set_handle(text)',
    'public.set_home_state(text)',
    'public.reset_friend_code()',
    'public.public_profile(text)',
    'public.check_in(text, text, timestamptz, integer)',
    'public.end_checkin()',
    'public.court_activity(text, integer)',
    'public.post_run(uuid, text, text, timestamptz, text, integer, integer)',
    'public.join_run(uuid, integer)',
    'public.leave_run(uuid)',
    'public.cancel_run(uuid)',
    'public.court_runs(text)',
    'public.my_runs()',
    'public.follow_by_code(text)',
    'public.unfollow(text)',
    'public.my_follows()',
    'public.block_player(text)',
    'public.unblock_player(text)',
    'public.report_player(text, text)',
    'public.report_run(uuid, text)',
    'public.leaderboard_friends(integer)',
    'public.leaderboard_state(text, integer)'
  ] loop
    execute format('revoke all on function %s from public, anon, authenticated', fn);
    execute format('grant execute on function %s to authenticated, service_role', fn);
  end loop;
end;
$$;
