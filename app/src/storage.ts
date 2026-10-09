import AsyncStorage from '@react-native-async-storage/async-storage';

import { parseCollection, toCollection } from './data/courts';
import type { Court, ReviewMap } from './types';

/** { [courtId]: { decision: "confirmed" | "rejected", at: ISO timestamp } } */
const REVIEWS_KEY = 'hoop-hunter:reviews:v1';
/** GeoJSON FeatureCollection of courts added on this phone (source "user"). */
const USER_COURTS_KEY = 'hoop-hunter:user-courts:v1';

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

export async function loadReviews(): Promise<ReviewMap> {
  const raw = await readJson(REVIEWS_KEY);
  const reviews: ReviewMap = {};
  if (typeof raw !== 'object' || raw === null) return reviews;
  for (const [id, value] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof value !== 'object' || value === null) continue;
    const { decision, at } = value as { decision?: unknown; at?: unknown };
    if (decision === 'confirmed' || decision === 'rejected') {
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
