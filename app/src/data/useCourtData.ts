import { useCallback, useEffect, useMemo, useState } from 'react';

import { loadReviews, loadUserCourts, saveReviews, saveUserCourts } from '../storage';
import type { Court, ReviewDecision, ReviewMap } from '../types';
import { type CourtEntry, courtKind } from './classify';
import { BUNDLED_COURTS } from './courts';

function warn(what: string) {
  return (error: unknown) => console.warn(`Could not ${what}`, error);
}

/**
 * Bundled courts plus what the user saved on this phone (candidate reviews and
 * added courts). Saved data is loaded once, then written back whenever it changes.
 */
export function useCourtData() {
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

  const entries = useMemo<CourtEntry[]>(
    () => [...BUNDLED_COURTS, ...userCourts].map((court) => ({ court, kind: courtKind(court, reviews) })),
    [userCourts, reviews],
  );

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
