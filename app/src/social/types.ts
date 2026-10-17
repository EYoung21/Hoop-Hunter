/**
 * Social features (check-ins, runs, XP, streaks, badges, follows, leaderboards). Shapes of what the
 * Supabase RPCs return (supabase/migrations/20261017140100_social_rpcs.sql), parsed in api.ts.
 */
import type { LatLng } from '../types';

export const BADGES = ['court_hunter', 'first_checkin', 'streak_7', 'streak_30', 'courts_10', 'first_run'] as const;
export type BadgeId = (typeof BADGES)[number];

export const RUN_FORMATS = ['5v5', '4v4', '3v3', '2v2', '1v1', 'shootaround'] as const;
export type RunFormat = (typeof RUN_FORMATS)[number];

export type RunSkill = 'casual' | 'competitive';

export const REPORT_REASONS = ['spam', 'offensive_name', 'harassment', 'fake_run', 'unsafe', 'other'] as const;
export type ReportReason = (typeof REPORT_REASONS)[number];

/** here = "Here now"; planned = "Hooping at <time>" (today or tomorrow). */
export type CheckinKind = 'here' | 'planned';

export interface MyCheckin {
  courtId: string;
  courtName: string | null;
  kind: CheckinKind;
  startsAt: string;
  expiresAt: string;
}

export interface Profile {
  handle: string;
  /** Private: share it so friends can follow you. */
  friendCode: string;
  homeState: string | null;
  /** Your local day (YYYY-MM-DD) as the server computed it. */
  today: string;
  xpTotal: number;
  xpWeek: number;
  /** 0 once a day has been missed. */
  streak: number;
  /** True when today already counts for the streak. */
  streakToday: boolean;
  bestStreak: number;
  /** Different courts checked in at; counting stops at 10 (the badge). */
  courtsVisited: number;
  badges: { badge: BadgeId; earnedAt: string }[];
  following: number;
  followers: number;
  checkin: MyCheckin | null;
}

export interface CourtActivity {
  here: number;
  /** Planned check-ins in the next few hours, grouped by start time. */
  planned: { at: string; count: number }[];
  /** Mutual follows at this court (the only names shown). */
  friends: { handle: string; kind: CheckinKind; startsAt: string }[];
  mine: { kind: CheckinKind; startsAt: string; expiresAt: string } | null;
}

export interface Run {
  id: string;
  courtId: string;
  format: RunFormat;
  skill: RunSkill | null;
  maxPlayers: number | null;
  startsAt: string;
  expiresAt: string;
  cancelled: boolean;
  players: number;
  /** Up to 12 handles, creator first. Players you blocked (or who blocked you) are left out. */
  names: string[];
  creator: string | null;
  mine: boolean;
  joined: boolean;
  /** Only from my_runs: where the run is. */
  courtName?: string | null;
  coordinate?: LatLng | null;
}

/** What an action earned. */
export interface Award {
  xp: number;
  streak?: number;
  badges: BadgeId[];
}

export interface LeaderEntry {
  rank: number;
  handle: string;
  xp: number;
  me: boolean;
}

export interface StateBoard {
  state: string | null;
  weekStart: string;
  entries: LeaderEntry[];
  me: { rank: number; xp: number } | null;
}

export interface FollowLists {
  following: { handle: string; mutual: boolean }[];
  followers: { handle: string; mutual: boolean }[];
  blocked: string[];
}

export interface PublicProfile {
  handle: string;
  me: boolean;
  xpWeek: number;
  xpTotal: number;
  streak: number;
  bestStreak: number;
  badges: BadgeId[];
  youFollow: boolean;
  followsYou: boolean;
}

export type FollowStatus = 'followed' | 'already' | 'not_found' | 'own_code' | 'rate_limited' | 'too_many';

export interface NewRun {
  /** Made on the phone so a retried upload is the same run. */
  id: string;
  courtId: string;
  format: RunFormat;
  startAt: string;
  skill: RunSkill | null;
  maxPlayers: number | null;
}
