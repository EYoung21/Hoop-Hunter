import rawCourts from '../../assets/data/courts.geojson';
import type { Court, CourtCollection, CourtFeature, CourtSource, CourtStatus, LatLng } from '../types';

const SOURCES: readonly CourtSource[] = ['osm', 'nyc_parks', 'detected', 'user'];
const STATUSES: readonly CourtStatus[] = ['verified', 'candidate'];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : null;
}

function finite(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function flag(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null;
}

/** Converts one GeoJSON feature into a Court, or returns null if it is malformed. */
export function featureToCourt(feature: unknown): Court | null {
  if (!isRecord(feature) || !isRecord(feature.geometry) || !isRecord(feature.properties)) {
    return null;
  }
  const { geometry, properties: p } = feature;
  if (geometry.type !== 'Point' || !Array.isArray(geometry.coordinates)) return null;

  const longitude = finite(geometry.coordinates[0]);
  const latitude = finite(geometry.coordinates[1]);
  if (latitude === null || longitude === null || Math.abs(latitude) > 90 || Math.abs(longitude) > 180) {
    return null;
  }

  const id = text(p.id);
  const source = SOURCES.find((s) => s === p.source);
  const status = STATUSES.find((s) => s === p.status);
  if (!id || !source || !status) return null;

  return {
    id,
    name: text(p.name),
    source,
    status,
    confidence: finite(p.confidence),
    hoops: finite(p.hoops),
    surface: text(p.surface),
    lit: flag(p.lit),
    address: text(p.address),
    region: text(p.region) ?? 'unknown',
    osm_id: text(p.osm_id),
    updated: text(p.updated) ?? '',
    notes: text(p.notes),
    coordinate: { latitude, longitude },
  };
}

export function courtToFeature({ coordinate, ...properties }: Court): CourtFeature {
  return {
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [coordinate.longitude, coordinate.latitude] },
    properties,
  };
}

export function toCollection(courts: readonly Court[]): CourtCollection {
  return { type: 'FeatureCollection', features: courts.map(courtToFeature) };
}

/** Parses a FeatureCollection, skipping malformed features and duplicate ids. */
export function parseCollection(raw: unknown): Court[] {
  if (!isRecord(raw) || !Array.isArray(raw.features)) return [];
  const seen = new Set<string>();
  const courts: Court[] = [];
  for (const feature of raw.features) {
    const court = featureToCourt(feature);
    if (court && !seen.has(court.id)) {
      seen.add(court.id);
      courts.push(court);
    }
  }
  return courts;
}

/** Courts shipped with the app (assets/data/courts.geojson). */
export const BUNDLED_COURTS: readonly Court[] = parseCollection(rawCourts);

export interface NewCourtInput {
  name: string;
  hoops: number;
  lit: boolean | null;
  surface: string | null;
  notes: string;
}

export function createUserCourt(input: NewCourtInput, coordinate: LatLng): Court {
  const now = new Date();
  return {
    id: `user-${now.getTime().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
    name: text(input.name),
    source: 'user',
    status: 'verified',
    confidence: null,
    hoops: input.hoops,
    surface: input.surface,
    lit: input.lit,
    address: null,
    region: 'user',
    osm_id: null,
    updated: now.toISOString().slice(0, 10),
    notes: text(input.notes),
    coordinate,
  };
}
