import { isTransientStatus } from '../data/remote';
import { getUserId, supabase } from '../supabase';
import { utcOffset } from './time';
import {
  type Award,
  BADGES,
  type BadgeId,
  type CheckinKind,
  type CourtActivity,
  type FollowLists,
  type FollowStatus,
  type LeaderEntry,
  type MyCheckin,
  type NewRun,
  type Profile,
  type PublicProfile,
  type ReportReason,
  RUN_FORMATS,
  type Run,
  type StateBoard,
} from './types';

/**
 * A failed social call. `message` is the server's code ("run_full", "age_gate", …; see labels.ts).
 * `transient` means try again later: offline, signed out, server busy.
 */
export class SocialError extends Error {
  constructor(
    message: string,
    readonly transient: boolean,
  ) {
    super(message);
  }
}

/** Calls an RPC as the signed-in player. */
async function call(name: string, args: Record<string, unknown> = {}): Promise<unknown> {
  const userId = await getUserId();
  if (!userId) throw new SocialError('signed_out', true);
  try {
    const { data, error, status } = await supabase.rpc(name, args);
    if (error) throw new SocialError(error.message || error.code || 'error', isTransientStatus(status));
    return data;
  } catch (error) {
    if (error instanceof SocialError) throw error;
    throw new SocialError(error instanceof Error ? error.message : 'network', true);
  }
}

// Defensive readers: a field missing or of the wrong type becomes a safe default instead of a crash.
type Json = Record<string, unknown>;
const obj = (v: unknown): Json => (typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Json) : {});
const list = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown): string => (typeof v === 'string' ? v : '');
const strOrNull = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null);
const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : Number(v) || 0);
const bool = (v: unknown): boolean => v === true;
const kind = (v: unknown): CheckinKind => (v === 'planned' ? 'planned' : 'here');
const badge = (v: unknown): BadgeId | null => BADGES.find((b) => b === v) ?? null;
const badges = (v: unknown): BadgeId[] => list(v).map(badge).filter((b): b is BadgeId => b !== null);

function toCheckin(v: unknown): MyCheckin | null {
  const o = obj(v);
  if (!o.court_id) return null;
  return {
    courtId: str(o.court_id),
    courtName: strOrNull(o.court_name),
    kind: kind(o.kind),
    startsAt: str(o.starts_at),
    expiresAt: str(o.expires_at),
  };
}

export function toProfile(v: unknown): Profile | null {
  const o = obj(v);
  if (!o.handle) return null;
  return {
    handle: str(o.handle),
    friendCode: str(o.friend_code),
    homeState: strOrNull(o.home_state),
    today: str(o.today),
    xpTotal: num(o.xp_total),
    xpWeek: num(o.xp_week),
    streak: num(o.streak),
    streakToday: bool(o.streak_today),
    bestStreak: num(o.best_streak),
    courtsVisited: num(o.courts_visited),
    badges: list(o.badges)
      .map((b) => ({ badge: badge(obj(b).badge), earnedAt: str(obj(b).earned_at) }))
      .filter((b): b is { badge: BadgeId; earnedAt: string } => b.badge !== null),
    following: num(o.following),
    followers: num(o.followers),
    checkin: toCheckin(o.checkin),
  };
}

function toRun(v: unknown): Run | null {
  const o = obj(v);
  const format = RUN_FORMATS.find((f) => f === o.format);
  if (!o.id || !format) return null;
  const lon = Number(o.lon);
  const lat = Number(o.lat);
  return {
    id: str(o.id),
    courtId: str(o.court_id),
    format,
    skill: o.skill === 'casual' || o.skill === 'competitive' ? o.skill : null,
    maxPlayers: o.max_players == null ? null : num(o.max_players),
    startsAt: str(o.starts_at),
    expiresAt: str(o.expires_at),
    cancelled: bool(o.cancelled),
    players: num(o.players),
    names: list(o.names).map(str).filter(Boolean),
    creator: strOrNull(o.creator),
    mine: bool(o.mine),
    joined: bool(o.joined),
    courtName: strOrNull(o.court_name),
    coordinate: o.lon != null && Number.isFinite(lon) && Number.isFinite(lat) ? { latitude: lat, longitude: lon } : null,
  };
}

const runs = (v: unknown): Run[] => list(v).map(toRun).filter((r): r is Run => r !== null);

function toAward(v: unknown): Award {
  const o = obj(v);
  return { xp: num(o.xp), streak: o.streak == null ? undefined : num(o.streak), badges: badges(o.badges) };
}

function toEntries(v: unknown): LeaderEntry[] {
  return list(v).map((e) => {
    const o = obj(e);
    return { rank: num(o.rank), handle: str(o.handle), xp: num(o.xp), me: bool(o.me) };
  });
}

// Profile -------------------------------------------------------------------------------------------

/** Your profile, or null before the 13+ question. */
export async function fetchMyProfile(): Promise<Profile | null> {
  return toProfile(await call('my_profile', { utc_offset: utcOffset() }));
}

/** "I'm 13 or older": creates your profile. */
export async function confirmAge(): Promise<Profile | null> {
  return toProfile(await call('confirm_age', { utc_offset: utcOffset() }));
}

export async function setHandle(handle: string): Promise<string> {
  return str(await call('set_handle', { new_handle: handle }));
}

export async function setHomeState(state: string | null): Promise<void> {
  await call('set_home_state', { st: state });
}

export async function resetFriendCode(): Promise<string> {
  return str(await call('reset_friend_code'));
}

export async function fetchPublicProfile(handle: string): Promise<PublicProfile | null> {
  const o = obj(await call('public_profile', { player: handle }));
  if (!o.handle) return null;
  return {
    handle: str(o.handle),
    me: bool(o.me),
    xpWeek: num(o.xp_week),
    xpTotal: num(o.xp_total),
    streak: num(o.streak),
    bestStreak: num(o.best_streak),
    badges: badges(o.badges),
    youFollow: bool(o.you_follow),
    followsYou: bool(o.follows_you),
  };
}

// Check-ins -----------------------------------------------------------------------------------------

/** "Here now" (the phone has already checked you're at the court) or "Hooping at <startAt>". */
export async function checkIn(courtId: string, mode: CheckinKind, startAt: string | null): Promise<Award> {
  return toAward(await call('check_in', { court: courtId, mode, start_at: startAt, utc_offset: utcOffset() }));
}

export async function endCheckin(): Promise<void> {
  await call('end_checkin');
}

export async function fetchCourtActivity(courtId: string): Promise<CourtActivity> {
  const o = obj(await call('court_activity', { court: courtId }));
  const mine = obj(o.mine);
  return {
    here: num(o.here),
    planned: list(o.planned).map((p) => ({ at: str(obj(p).at), count: num(obj(p).count) })),
    friends: list(o.friends).map((f) => ({ handle: str(obj(f).handle), kind: kind(obj(f).kind), startsAt: str(obj(f).starts_at) })),
    mine: mine.kind ? { kind: kind(mine.kind), startsAt: str(mine.starts_at), expiresAt: str(mine.expires_at) } : null,
  };
}

// Runs ----------------------------------------------------------------------------------------------

export async function postRun(run: NewRun): Promise<Award> {
  return toAward(
    await call('post_run', {
      run: run.id,
      court: run.courtId,
      run_format: run.format,
      start_at: run.startAt,
      skill_level: run.skill,
      player_limit: run.maxPlayers,
      utc_offset: utcOffset(),
    }),
  );
}

export async function joinRun(runId: string): Promise<Award> {
  return toAward(await call('join_run', { run: runId, utc_offset: utcOffset() }));
}

export async function leaveRun(runId: string): Promise<void> {
  await call('leave_run', { run: runId });
}

export async function cancelRun(runId: string): Promise<void> {
  await call('cancel_run', { run: runId });
}

export async function fetchCourtRuns(courtId: string): Promise<Run[]> {
  return runs(await call('court_runs', { court: courtId }));
}

export async function fetchMyRuns(): Promise<Run[]> {
  return runs(await call('my_runs'));
}

// Follows, blocks, reports ----------------------------------------------------------------------------

export async function followByCode(code: string): Promise<{ status: FollowStatus; handle: string | null; mutual: boolean }> {
  const o = obj(await call('follow_by_code', { code }));
  const statuses: FollowStatus[] = ['followed', 'already', 'not_found', 'own_code', 'rate_limited', 'too_many'];
  return { status: statuses.find((s) => s === o.status) ?? 'not_found', handle: strOrNull(o.handle), mutual: bool(o.mutual) };
}

export async function unfollow(handle: string): Promise<void> {
  await call('unfollow', { player: handle });
}

export async function fetchFollows(): Promise<FollowLists> {
  const o = obj(await call('my_follows'));
  const people = (v: unknown) => list(v).map((p) => ({ handle: str(obj(p).handle), mutual: bool(obj(p).mutual) }));
  return { following: people(o.following), followers: people(o.followers), blocked: list(o.blocked).map(str) };
}

export async function blockPlayer(handle: string): Promise<void> {
  await call('block_player', { player: handle });
}

export async function unblockPlayer(handle: string): Promise<void> {
  await call('unblock_player', { player: handle });
}

export async function reportPlayer(handle: string, reason: ReportReason): Promise<void> {
  await call('report_player', { player: handle, reason });
}

export async function reportRun(runId: string, reason: ReportReason): Promise<void> {
  await call('report_run', { run: runId, reason });
}

// Leaderboards --------------------------------------------------------------------------------------

export async function fetchFriendsBoard(): Promise<LeaderEntry[]> {
  return toEntries(await call('leaderboard_friends', { utc_offset: utcOffset() }));
}

/** state null = your home state. */
export async function fetchStateBoard(state: string | null): Promise<StateBoard> {
  const o = obj(await call('leaderboard_state', { st: state, utc_offset: utcOffset() }));
  const me = obj(o.me);
  return {
    state: strOrNull(o.state),
    weekStart: str(o.week_start),
    entries: toEntries(o.entries),
    me: me.rank != null ? { rank: num(me.rank), xp: num(me.xp) } : null,
  };
}
