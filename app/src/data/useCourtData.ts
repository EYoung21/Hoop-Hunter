import { useCallback, useEffect, useMemo, useState } from 'react';

import { loadReviews, loadUserCourts, saveReviews, saveUserCourts } from '../storage';
import { useSync } from '../sync';
import type { Court, ReviewDecision, ReviewMap } from '../types';
import { type CourtEntry, courtKind } from './classify';
import { BUNDLED_COURTS } from './courts';
import type { RemoteCourts } from './useRemoteCourts';

function warn(what: string) {
  return (error: unknown) => console.warn(`Could not ${what}`, error);
}

/**
 * Every court the map can draw, plus what the user saved on this phone (candidate reviews and added
 * courts). Saved data is loaded once, written back whenever it changes, and synced to the server.
 *
 * Precedence for the same id: your own courts, then server courts, then the bundled file. Bundled
 * courts are only used where the server has not answered (first launch, offline), so a court the
 * server has dropped does not linger from the bundled copy.
 */
export function useCourtData(remote: Pick<RemoteCourts, 'courts' | 'isCovered'>) {
  const [reviews, setReviews] = useState<ReviewMap>({});
  const [userCourts, setUserCourts] = useState<Court[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let active = true;
    Promise.all([loadReviews(), loadUserCourts()])
      .then(([savedReviews, savedCourts]) => {
        if (!active) return;
        setReviews(savedReviews);
        setUserCourts(savedCourts);
        setLoaded(true);
      })
      // Leave `loaded` false so a failed read never overwrites saved data with empty state.
      .catch(warn('load saved courts'));
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (loaded) saveReviews(reviews).catch(warn('save reviews'));
  }, [loaded, reviews]);

  useEffect(() => {
    if (loaded) saveUserCourts(userCourts).catch(warn('save your courts'));
  }, [loaded, userCourts]);

  useSync(loaded, reviews, userCourts);

  const { courts: remoteCourts, isCovered } = remote;
  const entries = useMemo<CourtEntry[]>(() => {
    const list: CourtEntry[] = [];
    const seen = new Set<string>();
    for (const court of userCourts) {
      seen.add(court.id);
      list.push({ court, kind: 'mine' });
    }
    for (const court of remoteCourts) {
      if (seen.has(court.id)) continue;
      seen.add(court.id);
      list.push({ court, kind: courtKind(court, reviews) });
    }
    for (const court of BUNDLED_COURTS) {
      if (seen.has(court.id) || isCovered(court.coordinate)) continue;
      list.push({ court, kind: courtKind(court, reviews) });
    }
    return list;
  }, [userCourts, remoteCourts, isCovered, reviews]);

  /** Records a decision on a candidate, or clears it when `decision` is null. */
  const setReview = useCallback((id: string, decision: ReviewDecision | null) => {
    setReviews((current) => {
      const next = { ...current };
      if (decision) next[id] = { decision, at: new Date().toISOString() };
      else delete next[id];
      return next;
    });
  }, []);

  const addCourt = useCallback((court: Court) => {
    setUserCourts((current) => [...current, court]);
  }, []);

  const deleteCourt = useCallback((id: string) => {
    setUserCourts((current) => current.filter((court) => court.id !== id));
  }, []);

  return { entries, setReview, addCourt, deleteCourt };
}
