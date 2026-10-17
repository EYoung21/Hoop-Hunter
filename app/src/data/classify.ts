import type { Court, CourtFilter, CourtKind, CourtSource, KnownCourtSource, ReviewDecision, ReviewMap } from '../types';

export interface CourtEntry {
  court: Court;
  kind: CourtKind;
}

/** Kind of a court from the bundled file or the server. Courts added on this phone are always 'mine'. */
export function courtKind(court: Court, reviews: ReviewMap): CourtKind {
  if (court.status === 'candidate') {
    const decision = reviews[court.id]?.decision;
    if (decision === 'confirmed') return 'confirmed';
    if (decision === 'rejected') return 'rejected';
    if (decision === 'home') return 'home';
    return 'candidate';
  }
  return court.indoor ? 'indoor' : 'verified';
}

/** Kinds hidden unless the "Rejected" chip is on: courts you said are not public courts. */
export const HIDDEN_KINDS: readonly CourtKind[] = ['rejected', 'home'];

const FILTER_KINDS: Record<CourtFilter, readonly CourtKind[]> = {
  all: ['verified', 'indoor', 'confirmed', 'candidate', 'mine'],
  verified: ['verified', 'indoor', 'confirmed'],
  candidates: ['candidate'],
  mine: ['mine'],
};

/** Which kinds a filter shows. Rejected candidates and home courts only appear when asked for. */
export function kindsForFilter(filter: CourtFilter, showRejected: boolean): ReadonlySet<CourtKind> {
  const kinds = new Set(FILTER_KINDS[filter]);
  if (showRejected && (filter === 'all' || filter === 'candidates')) HIDDEN_KINDS.forEach((kind) => kinds.add(kind));
  return kinds;
}

export const FILTER_LABELS: Record<CourtFilter, string> = {
  all: 'All',
  verified: 'Verified',
  candidates: 'Candidates',
  mine: 'Mine',
};

const SOURCE_LABELS: Record<KnownCourtSource, string> = {
  osm: 'OpenStreetMap',
  nyc_parks: 'NYC Parks',
  detected: 'Found by satellite scan',
  user: 'Added by a player',
  overture: 'Overture Maps',
  web: 'From a web listing',
  indoor: 'Indoor court listing',
  nces: 'School listing (NCES)',
};

export function sourceLabel(source: CourtSource, kind?: CourtKind): string {
  if (kind === 'mine') return 'Added by you';
  return SOURCE_LABELS[source as KnownCourtSource] ?? 'Other source';
}

export const KIND_LABELS: Record<CourtKind, string> = {
  verified: 'Verified court',
  indoor: 'Indoor court',
  candidate: 'Satellite candidate',
  confirmed: 'Confirmed by you',
  rejected: 'Not a court (your call)',
  home: "Someone's home court (your call)",
  mine: 'Added by you',
};

/** Where a satellite find sits (the pipeline's `site`). */
const SITE_LABELS: Record<string, string> = {
  park: 'In a park',
  school: 'At a school',
  college: 'At a college',
  church: 'At a church',
  rec: 'At a rec center',
  community: 'At a community center',
};

export function siteLabel(site: string | null | undefined): string | null {
  return site ? (SITE_LABELS[site] ?? null) : null;
}

/** Server names for review decisions (reviews.decision). */
export const SERVER_DECISIONS: Record<ReviewDecision, 'court' | 'not_court' | 'home_court'> = {
  confirmed: 'court',
  rejected: 'not_court',
  home: 'home_court',
};
