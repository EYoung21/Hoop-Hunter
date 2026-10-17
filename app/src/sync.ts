import { useEffect, useRef } from 'react';
import { AppState } from 'react-native';

import { SERVER_DECISIONS } from './data/classify';
import { isTransientStatus } from './data/remote';
import { loadSyncState, saveSyncState, type SyncState } from './storage';
import { getUserId, supabase } from './supabase';
import type { Court, ReviewMap } from './types';

/** Upload this long after the last change, so quick changes of mind go up as one request. */
const FLUSH_DELAY_MS = 1500;
/** Retry delays after a failure (offline, signed out, server busy), growing to the last value. */
const RETRY_MS = [15_000, 60_000, 5 * 60_000, 15 * 60_000];

interface Result {
  error: { message: string; code?: string } | null;
  status: number;
}

/** Network trouble or an expired session: keep the item queued. Anything else (bad row) is dropped. */
function keepQueued({ error, status }: Result): boolean {
  return error !== null && isTransientStatus(status);
}

function userCourtRow(court: Court, userId: string) {
  return {
    id: court.id,
    user_id: userId,
    name: court.name,
    hoops: court.hoops,
    surface: court.surface,
    lit: court.lit,
    indoor: court.indoor ?? null,
    notes: court.notes ?? null,
    lon: court.coordinate.longitude,
    lat: court.coordinate.latitude,
  };
}

/**
 * Keeps the server in step with what is saved on the phone.
 *
 * The phone stays the source of truth: reviews and added courts are saved locally first (storage.ts),
 * and the ids that changed go into a queue that is also saved locally. The queue is uploaded when the
 * app has a session (anonymous sign-in on first launch) and a connection: on change, when the app
 * comes to the foreground, and on a growing retry timer. An upload sends the item's current state, so
 * a review changed three times offline is one upsert, and an undone review or deleted court is a delete.
 */
export function useSync(loaded: boolean, reviews: ReviewMap, userCourts: readonly Court[]): void {
  const state = useRef({ reviews, userCourts });
  state.current = { reviews, userCourts };
  const queue = useRef<SyncState | null>(null);
  const previous = useRef<{ reviews: ReviewMap; userCourts: readonly Court[] } | null>(null);
  const flushing = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const failures = useRef(0);
  const flushRef = useRef<() => Promise<void>>(async () => {});

  const schedule = (delay: number) => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => void flushRef.current(), delay);
  };

  const persist = () => {
    if (queue.current) saveSyncState(queue.current).catch((error: unknown) => console.warn('Could not save the sync queue', error));
  };

  flushRef.current = async () => {
    const pending = queue.current;
    if (!pending || flushing.current || (pending.reviews.length === 0 && pending.courts.length === 0)) return;
    flushing.current = true;
    let retry = false;
    try {
      const userId = await getUserId();
      if (!userId) {
        retry = true;
        return;
      }
      const { reviews: currentReviews, userCourts: currentCourts } = state.current;
      const done = new Set<string>();

      // Reviews: one batched upsert, then deletes for reviews that were undone.
      const reviewIds = [...pending.reviews];
      const upserts = reviewIds.filter((id) => currentReviews[id]);
      const deletes = reviewIds.filter((id) => !currentReviews[id]);
      if (upserts.length > 0) {
        const row = (id: string) => ({ court_id: id, user_id: userId, decision: SERVER_DECISIONS[currentReviews[id].decision] });
        const batch: Result = await supabase.from('reviews').upsert(upserts.map(row), { onConflict: 'court_id,user_id' });
        if (!batch.error) upserts.forEach((id) => done.add(`r:${id}`));
        else if (keepQueued(batch)) retry = true;
        else {
          // One bad row (e.g. a court the server does not have) fails the batch; retry one by one.
          for (const id of upserts) {
            const one: Result = await supabase.from('reviews').upsert(row(id), { onConflict: 'court_id,user_id' });
            if (keepQueued(one)) retry = true;
            else {
              if (one.error) console.warn(`Dropped review of ${id}: ${one.error.message}`);
              done.add(`r:${id}`);
            }
          }
        }
      }
      if (deletes.length > 0) {
        const res: Result = await supabase.from('reviews').delete().eq('user_id', userId).in('court_id', deletes);
        if (keepQueued(res)) retry = true;
        else deletes.forEach((id) => done.add(`r:${id}`));
      }

      // Added courts: upsert the ones still on the phone, delete the ones removed from it.
      for (const id of [...pending.courts]) {
        const court = currentCourts.find((c) => c.id === id);
        const res: Result = court
          ? await supabase.from('user_courts').upsert(userCourtRow(court, userId), { onConflict: 'id' })
          : await supabase.from('user_courts').delete().eq('id', id);
        if (keepQueued(res)) retry = true;
        else {
          if (res.error) console.warn(`Dropped upload of court ${id}: ${res.error.message}`);
          done.add(`c:${id}`);
        }
      }

      // Dequeue what was uploaded, unless it changed again while the upload ran (then it goes up next time).
      const findCourt = (list: readonly Court[], id: string) => list.find((c) => c.id === id);
      const uploaded = {
        review: (id: string) => done.has(`r:${id}`) && state.current.reviews[id] === currentReviews[id],
        court: (id: string) => done.has(`c:${id}`) && findCourt(state.current.userCourts, id) === findCourt(currentCourts, id),
      };
      if (queue.current) {
        queue.current = {
          ...queue.current,
          reviews: queue.current.reviews.filter((id) => !uploaded.review(id)),
          courts: queue.current.courts.filter((id) => !uploaded.court(id)),
        };
        persist();
      }
    } catch (error) {
      console.warn('Sync failed', error);
      retry = true;
    } finally {
      flushing.current = false;
      const left = queue.current ? queue.current.reviews.length + queue.current.courts.length : 0;
      if (retry && left > 0) {
        schedule(RETRY_MS[Math.min(failures.current, RETRY_MS.length - 1)]);
        failures.current += 1;
      } else {
        failures.current = 0;
        if (left > 0) schedule(FLUSH_DELAY_MS);
      }
    }
  };

  // Load the queue, and on the first launch with sync queue everything saved before it existed.
  useEffect(() => {
    if (!loaded || queue.current) return;
    let active = true;
    loadSyncState()
      .then((saved) => {
        if (!active) return;
        const { reviews: r, userCourts: c } = state.current;
        queue.current = saved.initialized
          ? saved
          : { initialized: true, reviews: [...new Set([...saved.reviews, ...Object.keys(r)])], courts: [...new Set([...saved.courts, ...c.map((x) => x.id)])] };
        previous.current = { reviews: r, userCourts: c };
        persist();
        schedule(0);
      })
      .catch((error: unknown) => console.warn('Could not load the sync queue', error));
    return () => {
      active = false;
    };
  }, [loaded]);

  // Queue whatever changed since the last render.
  useEffect(() => {
    const before = previous.current;
    const pending = queue.current;
    if (!before || !pending) return;
    previous.current = { reviews, userCourts };
    const changedReviews = new Set([...Object.keys(before.reviews), ...Object.keys(reviews)]);
    const reviewIds = [...changedReviews].filter((id) => before.reviews[id] !== reviews[id]);
    const beforeCourts = new Map(before.userCourts.map((c) => [c.id, c]));
    const nowCourts = new Map(userCourts.map((c) => [c.id, c]));
    const courtIds = [...new Set([...beforeCourts.keys(), ...nowCourts.keys()])].filter((id) => beforeCourts.get(id) !== nowCourts.get(id));
    if (reviewIds.length === 0 && courtIds.length === 0) return;
    queue.current = {
      ...pending,
      reviews: [...new Set([...pending.reviews, ...reviewIds])],
      courts: [...new Set([...pending.courts, ...courtIds])],
    };
    persist();
    failures.current = 0;
    schedule(FLUSH_DELAY_MS);
  }, [reviews, userCourts]);

  // Coming back to the app is a good moment to retry.
  useEffect(() => {
    const subscription = AppState.addEventListener('change', (next) => {
      if (next === 'active') schedule(0);
    });
    return () => {
      subscription.remove();
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);
}
