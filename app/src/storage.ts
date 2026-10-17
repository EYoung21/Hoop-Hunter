import AsyncStorage from '@react-native-async-storage/async-storage';

import { parseCollection, toCollection } from './data/courts';
import type { Court, ReviewMap } from './types';

/** { [courtId]: { decision: "confirmed" | "rejected" | "home", at: ISO timestamp } } */
const REVIEWS_KEY = 'hoop-hunter:reviews:v1';
/** GeoJSON FeatureCollection of courts added on this phone (source "user"). */
const USER_COURTS_KEY = 'hoop-hunter:user-courts:v1';
/** Ids of reviews and added courts not yet uploaded (the offline queue), see sync.ts. */
const SYNC_KEY = 'hoop-hunter:sync:v1';
/** Courts last loaded from the server and the areas they cover, for offline use. See useRemoteCourts. */
const COURT_CACHE_KEY = 'hoop-hunter:court-cache:v1';

async function readJson(key: string): Promise<unknown> {
  const stored = await AsyncStorage.getItem(key);
  if (stored === null) return null;
  try {
    return JSON.parse(stored);
  } catch {
    console.warn(`Ignoring unreadable saved data under "${key}"`);
    return null;
  }
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [];
}

export async function loadReviews(): Promise<ReviewMap> {
  const raw = await readJson(REVIEWS_KEY);
  const reviews: ReviewMap = {};
  if (typeof raw !== 'object' || raw === null) return reviews;
  for (const [id, value] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof value !== 'object' || value === null) continue;
    const { decision, at } = value as { decision?: unknown; at?: unknown };
    if (decision === 'confirmed' || decision === 'rejected' || decision === 'home') {
      reviews[id] = { decision, at: typeof at === 'string' ? at : '' };
    }
  }
  return reviews;
}

export function saveReviews(reviews: ReviewMap): Promise<void> {
  return AsyncStorage.setItem(REVIEWS_KEY, JSON.stringify(reviews));
}

export async function loadUserCourts(): Promise<Court[]> {
  const courts = parseCollection(await readJson(USER_COURTS_KEY));
  return courts.filter((court) => court.source === 'user');
}

export function saveUserCourts(courts: readonly Court[]): Promise<void> {
  return AsyncStorage.setItem(USER_COURTS_KEY, JSON.stringify(toCollection(courts)));
}

export interface SyncState {
  /** False until the first launch with sync, when everything saved before is queued once. */
  initialized: boolean;
  reviews: string[];
  courts: string[];
}

export async function loadSyncState(): Promise<SyncState> {
  const raw = await readJson(SYNC_KEY);
  if (typeof raw !== 'object' || raw === null) return { initialized: false, reviews: [], courts: [] };
  const { initialized, reviews, courts } = raw as Record<string, unknown>;
  return { initialized: initialized === true, reviews: stringList(reviews), courts: stringList(courts) };
}

export function saveSyncState(state: SyncState): Promise<void> {
  return AsyncStorage.setItem(SYNC_KEY, JSON.stringify(state));
}

export function loadCourtCache(): Promise<unknown> {
  return readJson(COURT_CACHE_KEY);
}

export function saveCourtCache(cache: unknown): Promise<void> {
  return AsyncStorage.setItem(COURT_CACHE_KEY, JSON.stringify(cache));
}
