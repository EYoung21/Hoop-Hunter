import type { Court, CourtFilter, CourtKind, CourtSource, ReviewMap } from '../types';

export interface CourtEntry {
  court: Court;
  kind: CourtKind;
}

export function courtKind(court: Court, reviews: ReviewMap): CourtKind {
  if (court.source === 'user') return 'mine';
  if (court.status === 'candidate') {
    const decision = reviews[court.id]?.decision;
    if (decision === 'confirmed') return 'confirmed';
    if (decision === 'rejected') return 'rejected';
    return 'candidate';
  }
  return 'verified';
}

const FILTER_KINDS: Record<CourtFilter, readonly CourtKind[]> = {
  all: ['verified', 'confirmed', 'candidate', 'mine'],
  verified: ['verified', 'confirmed'],
  candidates: ['candidate'],
  mine: ['mine'],
};

/** Which kinds a filter shows. Rejected candidates only appear when asked for. */
export function kindsForFilter(filter: CourtFilter, showRejected: boolean): ReadonlySet<CourtKind> {
  const kinds = new Set(FILTER_KINDS[filter]);
  if (showRejected && (filter === 'all' || filter === 'candidates')) kinds.add('rejected');
  return kinds;
}

export const FILTER_LABELS: Record<CourtFilter, string> = {
  all: 'All',
  verified: 'Verified',
  candidates: 'Candidates',
  mine: 'Mine',
};

export const SOURCE_LABELS: Record<CourtSource, string> = {
  osm: 'OpenStreetMap',
  nyc_parks: 'NYC Parks',
  detected: 'Found by satellite scan',
  user: 'Added by you',
};

export const KIND_LABELS: Record<CourtKind, string> = {
  verified: 'Verified court',
  candidate: 'Satellite candidate',
  confirmed: 'Confirmed by you',
  rejected: 'Not a court (your call)',
  mine: 'Added by you',
};
