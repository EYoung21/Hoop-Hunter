/**
 * Data contract for assets/data/courts.geojson (written by the pipeline) and
 * for courts the user adds on the phone (stored locally in the same shape).
 */

export type CourtSource = 'osm' | 'nyc_parks' | 'detected' | 'user';

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
  /** "chattanooga" | "nyc" | … ("user" for courts added on the phone). */
  region: string;
  osm_id: string | null;
  /** ISO date. */
  updated: string;
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

export type ReviewDecision = 'confirmed' | 'rejected';

export interface Review {
  decision: ReviewDecision;
  /** ISO timestamp of the decision. */
  at: string;
}

/** Review decisions for satellite candidates, keyed by court id. */
export type ReviewMap = Record<string, Review>;

/** How a court is drawn on the map, derived from its data plus the user's reviews. */
export type CourtKind = 'verified' | 'candidate' | 'confirmed' | 'rejected' | 'mine';

export type CourtFilter = 'all' | 'verified' | 'candidates' | 'mine';

export type MapStyle = 'standard' | 'satellite';
