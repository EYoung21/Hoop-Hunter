import { AppState } from 'react-native';

import { loadSocialQueue, saveSocialQueue } from '../storage';
import { cancelRun, checkIn, endCheckin, joinRun, leaveRun, postRun, SocialError } from './api';
import type { CheckinKind, NewRun } from './types';

/**
 * Offline queue for check-ins and run actions, in the spirit of sync.ts. An action is tried right away;
 * if that fails because the phone is offline (or the session is refreshing), it waits here, saved on the
 * phone, and is sent on the next change, when the app comes to the foreground, or on a growing retry timer.
 * Actions the server refuses (run full, run over, …) are dropped. Follows, blocks, reports and profile
 * changes are online-only: they need an answer on the spot.
 */
export type QueuedAction =
  | { type: 'check_in'; courtId: string; mode: CheckinKind; startAt: string | null; at: number }
  | { type: 'end_checkin'; at: number }
  | { type: 'post_run'; run: NewRun; at: number }
  | { type: 'join_run' | 'leave_run' | 'cancel_run'; runId: string; courtId: string; at: number };

const RETRY_MS = [15_000, 60_000, 5 * 60_000, 15 * 60_000];
const HOUR = 3_600_000;
/** A "Here now" that couldn't be sent within this long is dropped: you've probably left. */
const HERE_NOW_MAX_DELAY_MS = 30 * 60_000;

/** Actions with the same key replace each other, so only the latest intent is sent. */
function keyOf(action: QueuedAction): string {
  switch (action.type) {
    case 'check_in':
    case 'end_checkin':
      return 'checkin';
    case 'post_run':
      return `post:${action.run.id}`;
    case 'cancel_run':
      return `cancel:${action.runId}`;
    default:
      return `member:${action.runId}`;
  }
}

function stale(action: QueuedAction, now: number): boolean {
  switch (action.type) {
    case 'check_in':
      return action.mode === 'here'
        ? now - action.at > HERE_NOW_MAX_DELAY_MS
        : new Date(action.startAt ?? 0).getTime() + 2 * HOUR < now;
    case 'post_run':
      return new Date(action.run.startAt).getTime() + 3 * HOUR < now;
    default:
      return now - action.at > 24 * HOUR;
  }
}

async function send(action: QueuedAction): Promise<void> {
  switch (action.type) {
    case 'check_in':
      await checkIn(action.courtId, action.mode, action.startAt);
      return;
    case 'end_checkin':
      await endCheckin();
      return;
    case 'post_run':
      await postRun(action.run);
      return;
    case 'join_run':
      await joinRun(action.runId);
      return;
    case 'leave_run':
      await leaveRun(action.runId);
      return;
    case 'cancel_run':
      await cancelRun(action.runId);
  }
}

const TYPES = new Set(['check_in', 'end_checkin', 'post_run', 'join_run', 'leave_run', 'cancel_run']);

function isAction(value: unknown): value is QueuedAction {
  return typeof value === 'object' && value !== null && TYPES.has((value as { type?: string }).type ?? '') && typeof (value as { at?: unknown }).at === 'number';
}

type Listener = (actions: readonly QueuedAction[], sent: boolean) => void;

let actions: QueuedAction[] = [];
let started = false;
let flushing = false;
let failures = 0;
let timer: ReturnType<typeof setTimeout> | null = null;
const listeners = new Set<Listener>();

function notify(sent: boolean) {
  listeners.forEach((listener) => listener(actions, sent));
}

function persist() {
  saveSocialQueue(actions).catch((error: unknown) => console.warn('Could not save the social queue', error));
}

function schedule(delay: number) {
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => void flush(), delay);
}

function remove(predicate: (action: QueuedAction) => boolean) {
  const before = actions.length;
  actions = actions.filter((action) => !predicate(action));
  if (actions.length !== before) {
    persist();
    notify(false);
  }
}

async function flush(): Promise<void> {
  if (flushing || actions.length === 0) return;
  flushing = true;
  let retry = false;
  let sent = false;
  try {
    while (actions.length > 0) {
      const action = actions[0];
      if (!stale(action, Date.now())) {
        try {
          await send(action);
          sent = true;
        } catch (error) {
          if (error instanceof SocialError && error.transient) {
            retry = true;
            break;
          }
          console.warn(`Dropped queued ${action.type}`, error);
        }
      }
      // Identity, not key: a newer action with the same key may have been queued while this one was sent.
      actions = actions.filter((a) => a !== action);
      persist();
    }
  } finally {
    flushing = false;
    if (sent || !retry) notify(sent);
    if (retry && actions.length > 0) {
      schedule(RETRY_MS[Math.min(failures, RETRY_MS.length - 1)]);
      failures += 1;
    } else {
      failures = 0;
    }
  }
}

/** Loads the saved queue once and starts sending it. Safe to call more than once. */
export function startSocialQueue(): void {
  if (started) return;
  started = true;
  loadSocialQueue()
    .then((saved) => {
      const restored = Array.isArray(saved) ? saved.filter(isAction) : [];
      actions = [...restored, ...actions];
      notify(false);
      schedule(0);
    })
    .catch((error: unknown) => console.warn('Could not load the social queue', error));
  AppState.addEventListener('change', (state) => {
    if (state === 'active') schedule(0);
  });
}

export function subscribeQueue(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function queuedActions(): readonly QueuedAction[] {
  return actions;
}

/**
 * Sends an action now. If the phone is offline it is queued instead ({ queued: true }); a refusal from the
 * server is thrown (SocialError) for the caller to show. A queued action with the same key is replaced.
 */
export async function sendOrQueue<T>(action: QueuedAction, attempt: () => Promise<T>): Promise<{ queued: true } | { queued: false; result: T }> {
  const key = keyOf(action);
  remove((a) => keyOf(a) === key);
  // Cancelling a run that never left the phone: just drop the post.
  if (action.type === 'cancel_run' && actions.some((a) => a.type === 'post_run' && a.run.id === action.runId)) {
    remove((a) => a.type === 'post_run' && a.run.id === action.runId);
    return { queued: true };
  }
  try {
    return { queued: false, result: await attempt() };
  } catch (error) {
    if (!(error instanceof SocialError && error.transient)) throw error;
    actions = [...actions, action];
    persist();
    notify(false);
    failures = 0;
    schedule(RETRY_MS[0]);
    return { queued: true };
  }
}
