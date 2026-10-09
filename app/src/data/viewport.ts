import type { Region } from 'react-native-maps';

import type { LatLng } from '../types';
import type { CourtEntry } from './classify';

/**
 * Chattanooga metro, from Signal Mountain in the west to East Brainerd in the east.
 * On a portrait phone the width (longitudeDelta) is what limits the view.
 */
export const INITIAL_REGION: Region = {
  latitude: 35.09,
  longitude: -85.25,
  latitudeDelta: 0.3,
  longitudeDelta: 0.38,
};

/** Zoom used when jumping to the user's location. */
export const USER_ZOOM_DELTA = 0.05;

/** Markers are kept mounted this far outside the visible area (fraction of the span, per side). */
const RENDER_MARGIN = 0.25;
/** Above this many markers in the render area, nearby courts are grouped into clusters. */
const MAX_MARKERS = 120;
/** Clusters are built on a grid this many cells across the visible width. */
const GRID_COLUMNS = 6;
/** Smallest span a cluster zooms to, so stacked courts still get a useful zoom. */
const MIN_CLUSTER_DELTA = 0.004;

export function isInRegion(point: LatLng, region: Region, margin = 0): boolean {
  return (
    Math.abs(point.latitude - region.latitude) <= region.latitudeDelta * (0.5 + margin) &&
    Math.abs(point.longitude - region.longitude) <= region.longitudeDelta * (0.5 + margin)
  );
}

export interface ClusterItem {
  type: 'cluster';
  key: string;
  count: number;
  coordinate: LatLng;
  /** Region that shows every court in the cluster. */
  bounds: Region;
}

export type MapItem = { type: 'court'; entry: CourtEntry } | ClusterItem;

/**
 * Picks what to draw for the current region: courts near the visible area, grouped
 * into grid clusters when there are too many to draw one by one. The selected court
 * is never hidden inside a cluster.
 */
export function buildMapItems(
  entries: readonly CourtEntry[],
  region: Region,
  selectedId: string | null,
): MapItem[] {
  const nearby = entries.filter((entry) => isInRegion(entry.court.coordinate, region, RENDER_MARGIN));
  if (nearby.length <= MAX_MARKERS) {
    return nearby.map((entry) => ({ type: 'court', entry }));
  }

  const cellLng = region.longitudeDelta / GRID_COLUMNS;
  // Keep cells roughly square on screen (Mercator draws latitude degrees taller).
  const cellLat = cellLng * Math.cos((region.latitude * Math.PI) / 180);
  const items: MapItem[] = [];
  const cells = new Map<string, CourtEntry[]>();

  for (const entry of nearby) {
    if (entry.court.id === selectedId) {
      items.push({ type: 'court', entry });
      continue;
    }
    const { latitude, longitude } = entry.court.coordinate;
    const key = `${Math.floor(latitude / cellLat)}:${Math.floor(longitude / cellLng)}`;
    const cell = cells.get(key);
    if (cell) cell.push(entry);
    else cells.set(key, [entry]);
  }

  for (const [key, members] of cells) {
    items.push(members.length === 1 ? { type: 'court', entry: members[0] } : toCluster(key, members));
  }
  return items;
}

function toCluster(key: string, members: CourtEntry[]): ClusterItem {
  let minLat = Infinity;
  let maxLat = -Infinity;
  let minLng = Infinity;
  let maxLng = -Infinity;
  let sumLat = 0;
  let sumLng = 0;
  for (const { court } of members) {
    const { latitude, longitude } = court.coordinate;
    minLat = Math.min(minLat, latitude);
    maxLat = Math.max(maxLat, latitude);
    minLng = Math.min(minLng, longitude);
    maxLng = Math.max(maxLng, longitude);
    sumLat += latitude;
    sumLng += longitude;
  }
  return {
    type: 'cluster',
    // The count is part of the key so the marker re-renders its label (Android
    // snapshots marker views once, see CourtMarker).
    key: `cluster-${key}-${members.length}`,
    count: members.length,
    coordinate: { latitude: sumLat / members.length, longitude: sumLng / members.length },
    bounds: {
      latitude: (minLat + maxLat) / 2,
      longitude: (minLng + maxLng) / 2,
      latitudeDelta: Math.max((maxLat - minLat) * 1.6, MIN_CLUSTER_DELTA),
      longitudeDelta: Math.max((maxLng - minLng) * 1.6, MIN_CLUSTER_DELTA),
    },
  };
}
