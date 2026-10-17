import { supabase } from '../supabase';
import type { Court } from '../types';
import { rowToCourt } from './courts';

/** A lon/lat box. */
export interface Bbox {
  west: number;
  south: number;
  east: number;
  north: number;
}

/** One grid cell from the court_clusters RPC. */
export interface ServerCluster {
  key: string;
  count: number;
  longitude: number;
  latitude: number;
  bounds: Bbox;
}

export interface ReviewCounts {
  court: number;
  notCourt: number;
  homeCourt: number;
  total: number;
}

/** The API answers at most this many rows per request (Supabase's default "Max rows"). */
const PAGE_SIZE = 1000;

/** Thrown for any failed request. `transient` means retry later (offline, timeout, server busy). */
export class RemoteError extends Error {
  constructor(
    message: string,
    readonly transient: boolean,
  ) {
    super(message);
  }
}

/** HTTP status 0 is a network failure (no connection, DNS, timeout). */
export function isTransientStatus(status: number): boolean {
  return status === 0 || status === 401 || status === 408 || status === 429 || status >= 500;
}

function fail(what: string, status: number, error: { message: string; code?: string }): never {
  throw new RemoteError(`${what} failed (${status}${error.code ? ` ${error.code}` : ''}): ${error.message}`, isTransientStatus(status));
}

export interface CourtsInBox {
  courts: Court[];
  /**
   * True when the server had more than maxRows courts. Results come nearest the centre first, so
   * every court within `radius` degrees of the centre is included.
   */
  truncated: boolean;
  radius: number;
}

/** Every published court in a box (courts_in_view), paged 1,000 at a time up to maxRows. */
export async function fetchCourtsInBox(box: Bbox, maxRows: number): Promise<CourtsInBox> {
  const args = { min_lon: box.west, min_lat: box.south, max_lon: box.east, max_lat: box.north, max_rows: maxRows };
  const page = async (from: number) => {
    const { data, error, status } = await supabase.rpc('courts_in_view', args).range(from, from + PAGE_SIZE - 1);
    if (error) fail('Loading courts', status, error);
    return (data ?? []) as unknown[];
  };

  let rows = await page(0);
  if (rows.length === PAGE_SIZE && maxRows > PAGE_SIZE) {
    const offsets: number[] = [];
    for (let from = PAGE_SIZE; from < maxRows; from += PAGE_SIZE) offsets.push(from);
    const rest = await Promise.all(offsets.map(page));
    rows = rows.concat(...rest);
  }

  const courts: Court[] = [];
  for (const row of rows) {
    const court = rowToCourt(row);
    if (court) courts.push(court);
  }
  const truncated = rows.length >= maxRows;
  const centerLng = (box.west + box.east) / 2;
  const centerLat = (box.south + box.north) / 2;
  let radius = Infinity;
  if (truncated) {
    const last = courts[courts.length - 1];
    radius = last ? Math.hypot(last.coordinate.longitude - centerLng, last.coordinate.latitude - centerLat) : 0;
  }
  return { courts, truncated, radius };
}

/** Grid clusters for a zoomed-out view (court_clusters). statuses null = verified and candidates. */
export async function fetchClusters(box: Bbox, zoom: number, statuses: string[] | null): Promise<ServerCluster[]> {
  const { data, error, status } = await supabase.rpc('court_clusters', {
    min_lon: box.west,
    min_lat: box.south,
    max_lon: box.east,
    max_lat: box.north,
    zoom,
    statuses,
  });
  if (error) fail('Loading clusters', status, error);
  const clusters: ServerCluster[] = [];
  for (const row of (data ?? []) as Record<string, unknown>[]) {
    const count = Number(row.court_count);
    const longitude = Number(row.lon);
    const latitude = Number(row.lat);
    if (!(count > 0) || !Number.isFinite(longitude) || !Number.isFinite(latitude)) continue;
    clusters.push({
      key: `${zoom}:${row.cell_x}:${row.cell_y}:${count}`,
      count,
      longitude,
      latitude,
      bounds: { west: Number(row.west), south: Number(row.south), east: Number(row.east), north: Number(row.north) },
    });
  }
  return clusters;
}

/** How many players said court / not a court / home court, for up to 200 courts (review_counts). */
export async function fetchReviewCounts(ids: readonly string[]): Promise<Record<string, ReviewCounts>> {
  const { data, error, status } = await supabase.rpc('review_counts', { court_ids: ids.slice(0, 200) });
  if (error) fail('Loading review counts', status, error);
  const counts: Record<string, ReviewCounts> = {};
  for (const row of (data ?? []) as Record<string, unknown>[]) {
    counts[String(row.court_id)] = {
      court: Number(row.court) || 0,
      notCourt: Number(row.not_court) || 0,
      homeCourt: Number(row.home_court) || 0,
      total: Number(row.total) || 0,
    };
  }
  return counts;
}
