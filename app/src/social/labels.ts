import type { IconName } from '../components/Button';
import type { BadgeId, ReportReason, RunFormat, RunSkill } from './types';

export const BADGE_INFO: Record<BadgeId, { label: string; description: string; icon: IconName }> = {
  court_hunter: {
    label: 'Court Hunter',
    description: 'First to add a court that got approved, or first to confirm a satellite find',
    icon: 'search',
  },
  first_checkin: { label: 'First check-in', description: 'Checked in at a court', icon: 'location' },
  streak_7: { label: '7-day streak', description: 'Checked in 7 days in a row', icon: 'flame' },
  streak_30: { label: '30-day streak', description: 'Checked in 30 days in a row', icon: 'bonfire' },
  courts_10: { label: 'Explorer', description: 'Checked in at 10 different courts', icon: 'map' },
  first_run: { label: 'First run', description: 'Posted or joined a pickup run', icon: 'basketball' },
};

export const FORMAT_LABELS: Record<RunFormat, string> = {
  '5v5': '5v5',
  '4v4': '4v4',
  '3v3': '3v3',
  '2v2': '2v2',
  '1v1': '1v1',
  shootaround: 'Shootaround',
};

export const SKILL_LABELS: Record<RunSkill, string> = {
  casual: 'Casual',
  competitive: 'Competitive',
};

export const REPORT_LABELS: Record<ReportReason, string> = {
  spam: 'Spam',
  offensive_name: 'Offensive handle',
  harassment: 'Harassment',
  fake_run: 'Fake run',
  unsafe: 'Unsafe or dangerous',
  other: 'Something else',
};

/** The server's error codes (raised messages) in plain English. */
const ERROR_TEXT: Record<string, string> = {
  age_gate: 'Answer the age question first.',
  not_signed_in: "You're not signed in yet. Try again in a moment.",
  signed_out: 'Social features need a connection and sign-in. Try again later.',
  bad_offset: "Your phone's time zone looks wrong. Check the date and time settings.",
  court_not_found: "This court isn't on the server yet. Try again once it has synced.",
  bad_kind: 'Something went wrong. Try again.',
  bad_time: 'Pick a time that is still ahead.',
  rate_limited: "That's a lot in a short time. Try again in a bit.",
  run_full: 'This run is full.',
  run_closed: 'This run is over or was cancelled.',
  run_not_found: "This run isn't available anymore.",
  too_many_runs: "You've hit the limit of upcoming runs. Cancel or leave one first.",
  creator_cannot_leave: 'You posted this run. Cancel it instead.',
  not_your_run: 'Only the player who posted a run can cancel it.',
  bad_run: 'Something went wrong with this run. Try again.',
  bad_format: 'Pick a format.',
  bad_skill: 'Pick a skill level.',
  bad_limit: 'Max players must be between 2 and 30.',
  handle_taken: 'That handle is taken.',
  handle_invalid: 'Use 3 to 20 letters, numbers or underscores.',
  handle_not_allowed: "That handle isn't allowed. Try another.",
  player_not_found: "That player isn't around anymore.",
  cannot_block_self: "You can't block yourself.",
  cannot_report_self: "You can't report yourself.",
  bad_state: 'Pick a state from the list.',
};

/** A friendly message for a failed social action. */
export function errorText(error: unknown): string {
  const code = error instanceof Error ? error.message : '';
  return ERROR_TEXT[code] ?? "That didn't work. Check your connection and try again.";
}

export const US_STATES: readonly [string, string][] = [
  ['AL', 'Alabama'],
  ['AK', 'Alaska'],
  ['AZ', 'Arizona'],
  ['AR', 'Arkansas'],
  ['CA', 'California'],
  ['CO', 'Colorado'],
  ['CT', 'Connecticut'],
  ['DE', 'Delaware'],
  ['DC', 'District of Columbia'],
  ['FL', 'Florida'],
  ['GA', 'Georgia'],
  ['HI', 'Hawaii'],
  ['ID', 'Idaho'],
  ['IL', 'Illinois'],
  ['IN', 'Indiana'],
  ['IA', 'Iowa'],
  ['KS', 'Kansas'],
  ['KY', 'Kentucky'],
  ['LA', 'Louisiana'],
  ['ME', 'Maine'],
  ['MD', 'Maryland'],
  ['MA', 'Massachusetts'],
  ['MI', 'Michigan'],
  ['MN', 'Minnesota'],
  ['MS', 'Mississippi'],
  ['MO', 'Missouri'],
  ['MT', 'Montana'],
  ['NE', 'Nebraska'],
  ['NV', 'Nevada'],
  ['NH', 'New Hampshire'],
  ['NJ', 'New Jersey'],
  ['NM', 'New Mexico'],
  ['NY', 'New York'],
  ['NC', 'North Carolina'],
  ['ND', 'North Dakota'],
  ['OH', 'Ohio'],
  ['OK', 'Oklahoma'],
  ['OR', 'Oregon'],
  ['PA', 'Pennsylvania'],
  ['PR', 'Puerto Rico'],
  ['RI', 'Rhode Island'],
  ['SC', 'South Carolina'],
  ['SD', 'South Dakota'],
  ['TN', 'Tennessee'],
  ['TX', 'Texas'],
  ['UT', 'Utah'],
  ['VT', 'Vermont'],
  ['VA', 'Virginia'],
  ['WA', 'Washington'],
  ['WV', 'West Virginia'],
  ['WI', 'Wisconsin'],
  ['WY', 'Wyoming'],
];

export function stateName(code: string | null): string | null {
  return US_STATES.find(([c]) => c === code)?.[1] ?? null;
}
