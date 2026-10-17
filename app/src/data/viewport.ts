import type { Region } from 'react-native-maps';

import type { LatLng } from '../types';
import type { CourtEntry } from './classify';
import type { Bbox, ServerCluster } from './remote';

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

// --------------------------------------------------------------------------- server data

/**
 * Views narrower than this (degrees of longitude, about a metro area) load every court in view
 * (courts_in_view). Wider views load grid clusters from the server instead (court_clusters), so
 * the whole country can be browsed without downloading 85,000 courts.
 */
export const DETAIL_MAX_DELTA = 0.6;

/** The box a region shows, grown by `margin` of its span on every side. */
export function regionToBbox(region: Region, margin = 0): Bbox {
  const halfLat = region.latitudeDelta * (0.5 + margin);
  const halfLng = region.longitudeDelta * (0.5 + margin);
  return {
    west: Math.max(-180, region.longitude - halfLng),
    south: Math.max(-90, region.latitude - halfLat),
    east: Math.min(180, region.longitude + halfLng),
    north: Math.min(90, region.latitude + halfLat),
  };
}

export function bboxContains(outer: Bbox, inner: Bbox): boolean {
  return inner.west >= outer.west && inner.east <= outer.east && inner.south >= outer.south && inner.north <= outer.north;
}

export function pointInBbox({ latitude, longitude }: LatLng, box: Bbox): boolean {
  return longitude >= box.west && longitude <= box.east && latitude >= box.south && latitude <= box.north;
}

/** Web-map zoom level for a region (0 = whole world across the screen). */
export function zoomForRegion(region: Region): number {
  const zoom = Math.round(Math.log2(360 / Math.max(region.longitudeDelta, 1e-6)));
  return Math.min(20, Math.max(0, zoom));
}

/** Grid cell size the server uses at a zoom (see court_clusters in supabase/migrations). */
export function clusterCellDegrees(zoom: number): number {
  return 360 / 2 ** (zoom + 3);
}

/** Grows a box outward to whole grid cells, so cached clusters line up with fresh ones. */
export function snapBbox(box: Bbox, cell: number): Bbox {
  return {
    west: Math.max(-180, Math.floor(box.west / cell) * cell),
    south: Math.max(-90, Math.floor(box.south / cell) * cell),
    east: Math.min(180, Math.ceil(box.east / cell) * cell),
    north: Math.min(90, Math.ceil(box.north / cell) * cell),
  };
}

/** Smallest span a server cluster zooms to; a lone court then shows in its neighbourhood. */
const MIN_SERVER_CLUSTER_DELTA = 0.02;

export function serverClusterItem(cluster: ServerCluster): ClusterItem {
  const { west, south, east, north } = cluster.bounds;
  return {
    type: 'cluster',
    key: `srv-${cluster.key}`,
    count: cluster.count,
    coordinate: { latitude: cluster.latitude, longitude: cluster.longitude },
    bounds: {
      latitude: (south + north) / 2,
      longitude: (west + east) / 2,
      latitudeDelta: Math.max((north - south) * 1.6, MIN_SERVER_CLUSTER_DELTA),
      longitudeDelta: Math.max((east - west) * 1.6, MIN_SERVER_CLUSTER_DELTA),
    },
  };
}
