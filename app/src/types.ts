/**
 * Data contract for assets/data/courts.geojson (written by the pipeline), for courts loaded from
 * the server (Supabase RPC `courts_in_view`, same fields), and for courts the user adds on the phone.
 */

/** Sources the app knows how to label. Server rows may carry a newer one; see sourceLabel(). */
export const KNOWN_SOURCES = ['osm', 'nyc_parks', 'detected', 'user', 'overture', 'web', 'indoor', 'nces'] as const;

export type KnownCourtSource = (typeof KNOWN_SOURCES)[number];

/** A known source, or (server data only) a source added after this app version. */
export type CourtSource = KnownCourtSource | (string & {});

export type CourtStatus = 'verified' | 'candidate';

export interface CourtProperties {
  /** Unique, e.g. "osm-way-123", "nyc-X159", "det-…", "user-…". */
  id: string;
  name: string | null;
  source: CourtSource;
  status: CourtStatus;
  /** 0–1, only set for satellite detections. */
  confidence: number | null;
  hoops: number | null;
  surface: string | null;
  lit: boolean | null;
  address: string | null;
  /** "chattanooga" | "nyc" | "tennessee" | … ("user" for courts added on the phone). */
  region: string;
  osm_id: string | null;
  /** ISO date. */
  updated: string;
  /** Indoor court (gym, rec center). Optional in the file; false or missing means outdoor. */
  indoor?: boolean | null;
  /** What the court sits on, for satellite finds: park, school, college, church, rec, community. */
  site?: string | null;
  /** Free-text notes. Only courts added on the phone have these; not part of the pipeline file. */
  notes?: string | null;
}

export interface CourtFeature {
  type: 'Feature';
  geometry: {
    type: 'Point';
    /** GeoJSON order: [longitude, latitude]. */
    coordinates: [number, number];
  };
  properties: CourtProperties;
}

export interface CourtCollection {
  type: 'FeatureCollection';
  features: CourtFeature[];
}

export interface LatLng {
  latitude: number;
  longitude: number;
}

/** A court flattened for use in the app. */
export interface Court extends CourtProperties {
  coordinate: LatLng;
}

/** On the phone: confirmed = "It's a court", rejected = "Not a court", home = "Someone's home court". */
export type ReviewDecision = 'confirmed' | 'rejected' | 'home';

export interface Review {
  decision: ReviewDecision;
  /** ISO timestamp of the decision. */
  at: string;
}

/** Review decisions for satellite candidates, keyed by court id. */
export type ReviewMap = Record<string, Review>;

/** How a court is drawn on the map, derived from its data plus the user's reviews. */
export type CourtKind = 'verified' | 'indoor' | 'candidate' | 'confirmed' | 'rejected' | 'home' | 'mine';

export type CourtFilter = 'all' | 'verified' | 'candidates' | 'mine';

export type MapStyle = 'standard' | 'satellite';
