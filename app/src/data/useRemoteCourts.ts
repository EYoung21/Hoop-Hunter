import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Region } from 'react-native-maps';

import { loadCourtCache, saveCourtCache } from '../storage';
import type { Court, CourtFilter, LatLng } from '../types';
import { rowToCourt } from './courts';
import { type Bbox, type CourtsInBox, fetchClusters, fetchCourtsInBox, type ServerCluster } from './remote';
import {
  bboxContains,
  clusterCellDegrees,
  DETAIL_MAX_DELTA,
  pointInBbox,
  regionToBbox,
  snapBbox,
  zoomForRegion,
} from './viewport';

/** Wait this long after the map stops moving before asking the server. */
const DEBOUNCE_MS = 350;
/** Courts are fetched this far beyond the view (fraction of the span, per side) so small pans are free. */
const DETAIL_MARGIN = 0.25;
/** Most courts one detail fetch returns (4 pages of 1,000). Denser views keep the courts nearest the centre. */
const DETAIL_MAX_ROWS = 4000;
/** An area or cluster answer is reused this long before it is fetched again. */
const AREA_TTL_MS = 30 * 60_000;
const CLUSTER_TTL_MS = 10 * 60_000;
/** Memory bounds: areas remembered, courts kept, cluster answers kept. */
const MAX_AREAS = 24;
const MAX_MEMORY_COURTS = 20_000;
const MAX_CLUSTER_VIEWS = 8;
/** Offline cache on the phone: the newest areas, up to this many courts (well under AsyncStorage limits). */
const CACHE_MAX_COURTS = 4000;
const CACHE_SAVE_DELAY_MS = 2000;
const CACHE_VERSION = 1;
/** After a failed request, try again this often while the map sits still. */
const OFFLINE_RETRY_MS = 30_000;

/** A box where the server answered in full: its courts replace the bundled ones there. */
interface Area extends Bbox {
  fetchedAt: number;
}

interface ClusterView {
  zoom: number;
  statusKey: string;
  box: Bbox;
  clusters: ServerCluster[];
  fetchedAt: number;
}

export type RemoteStatus = 'loading' | 'ok' | 'offline';

export interface RemoteCourts {
  /** Courts loaded from the server (this session or the offline cache). */
  courts: readonly Court[];
  /** True where server data is known, so bundled courts there are left out (they may be stale). */
  isCovered: (point: LatLng) => boolean;
  /** Server clusters for a zoomed-out view; null when zoomed in, or offline (cluster on the phone instead). */
  clusters: readonly ServerCluster[] | null;
  status: RemoteStatus;
}

/** Statuses for court_clusters per filter. 'mine' never asks the server (your courts are on the phone). */
function clusterStatuses(filter: CourtFilter): string[] | null {
  if (filter === 'verified') return ['verified'];
  if (filter === 'candidates') return ['candidate'];
  return null;
}

/** The part of a capped answer known to be complete: inside the box and the circle every court was returned from. */
function coveredBox(box: Bbox, result: CourtsInBox): Bbox {
  if (!result.truncated) return box;
  const half = result.radius / Math.SQRT2;
  const lng = (box.west + box.east) / 2;
  const lat = (box.south + box.north) / 2;
  return {
    west: Math.max(box.west, lng - half),
    south: Math.max(box.south, lat - half),
    east: Math.min(box.east, lng + half),
    north: Math.min(box.north, lat + half),
  };
}

type CachedCourt = [string, number, number, ...unknown[]];
const CACHE_FIELDS = ['name', 'source', 'status', 'confidence', 'hoops', 'surface', 'lit', 'indoor', 'address', 'region', 'site', 'osm_id', 'updated'] as const;

function encodeCourt(court: Court): CachedCourt {
  return [court.id, court.coordinate.longitude, court.coordinate.latitude, ...CACHE_FIELDS.map((key) => court[key] ?? null)];
}

function decodeCourt(entry: unknown): Court | null {
  if (!Array.isArray(entry)) return null;
  const [id, lon, lat, ...values] = entry;
  const row: Record<string, unknown> = { id, lon, lat };
  CACHE_FIELDS.forEach((key, i) => (row[key] = values[i]));
  return rowToCourt(row);
}

function decodeArea(entry: unknown): Area | null {
  if (!Array.isArray(entry) || entry.length !== 5 || !entry.every((n) => typeof n === 'number' && Number.isFinite(n))) return null;
  const [west, south, east, north, fetchedAt] = entry as number[];
  return { west, south, east, north, fetchedAt };
}

/**
 * Courts for whatever part of the map is on screen, from Supabase.
 *
 * Zoomed in (span <= DETAIL_MAX_DELTA): every court in view plus a margin, via courts_in_view, merged
 * into an in-memory cache keyed by court id. Zoomed out: grid clusters via court_clusters. Requests are
 * debounced; answers are reused while fresh; the newest areas are saved on the phone for offline use.
 * When a request fails the map keeps what it has, and the bundled courts fill in the rest.
 */
export function useRemoteCourts(region: Region, filter: CourtFilter): RemoteCourts {
  const courtsRef = useRef(new Map<string, Court>());
  const areasRef = useRef<Area[]>([]);
  const clusterViewsRef = useRef<ClusterView[]>([]);
  const requestRef = useRef(0);
  const latest = useRef({ region, filter });
  latest.current = { region, filter };
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const warnedOffline = useRef(false);

  const [version, setVersion] = useState(0);
  const [clusterView, setClusterView] = useState<ClusterView | null>(null);
  const [status, setStatus] = useState<RemoteStatus>('loading');

  const scheduleSave = useCallback(() => {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {
      const areas = [...areasRef.current].sort((a, b) => b.fetchedAt - a.fetchedAt);
      const keptAreas: Area[] = [];
      const kept = new Map<string, Court>();
      for (const area of areas) {
        const inside = [...courtsRef.current.values()].filter((court) => pointInBbox(court.coordinate, area));
        const added = inside.filter((court) => !kept.has(court.id)).length;
        if (keptAreas.length > 0 && kept.size + added > CACHE_MAX_COURTS) break;
        keptAreas.push(area);
        inside.forEach((court) => kept.set(court.id, court));
      }
      saveCourtCache({
        v: CACHE_VERSION,
        areas: keptAreas.map((a) => [a.west, a.south, a.east, a.north, a.fetchedAt]),
        courts: [...kept.values()].map(encodeCourt),
      }).catch((error: unknown) => console.warn('Could not save courts for offline use', error));
    }, CACHE_SAVE_DELAY_MS);
  }, []);

  /** Merges a full (or capped) answer for `box`: courts the server no longer returns there are dropped. */
  const applyCourts = useCallback(
    (box: Bbox, result: CourtsInBox) => {
      const map = courtsRef.current;
      const covered = coveredBox(box, result);
      const returned = new Set(result.courts.map((court) => court.id));
      for (const [id, court] of map) {
        if (!returned.has(id) && pointInBbox(court.coordinate, covered)) map.delete(id);
      }
      for (const court of result.courts) map.set(court.id, court);

      let areas = [...areasRef.current.filter((area) => !bboxContains(covered, area)), { ...covered, fetchedAt: Date.now() }];
      areas = areas.slice(-MAX_AREAS);
      // Keep memory bounded: forget the oldest areas, then any court outside the areas left.
      while (map.size > MAX_MEMORY_COURTS && areas.length > 1) {
        areas = areas.slice(1);
        for (const [id, court] of map) {
          if (!areas.some((area) => pointInBbox(court.coordinate, area))) map.delete(id);
        }
      }
      areasRef.current = areas;
      setVersion((v) => v + 1);
      scheduleSave();
    },
    [scheduleSave],
  );

  const refresh = useCallback(async () => {
    const { region: view, filter: currentFilter } = latest.current;
    const id = ++requestRef.current;
    const isLatest = () => id === requestRef.current;
    const viewBox = regionToBbox(view);
    const now = Date.now();

    const failed = (error: unknown) => {
      if (!isLatest()) return;
      setStatus('offline');
      if (!warnedOffline.current) {
        warnedOffline.current = true;
        console.warn('Could not reach the court server; showing saved courts', error);
      }
    };
    const succeeded = () => {
      warnedOffline.current = false;
      if (isLatest()) setStatus('ok');
    };

    if (view.longitudeDelta <= DETAIL_MAX_DELTA) {
      setClusterView(null);
      if (areasRef.current.some((area) => now - area.fetchedAt < AREA_TTL_MS && bboxContains(area, viewBox))) {
        if (isLatest()) setStatus('ok');
        return;
      }
      setStatus('loading');
      const box = regionToBbox(view, DETAIL_MARGIN);
      try {
        // A late answer is still good data, so it is merged even if the map has moved on.
        applyCourts(box, await fetchCourtsInBox(box, DETAIL_MAX_ROWS));
        succeeded();
      } catch (error) {
        failed(error);
      }
      return;
    }

    if (currentFilter === 'mine') {
      setClusterView(null);
      setStatus('ok');
      return;
    }
    const zoom = zoomForRegion(view);
    const statuses = clusterStatuses(currentFilter);
    const statusKey = statuses?.join(',') ?? 'all';
    const cached = clusterViewsRef.current.find(
      (c) => c.zoom === zoom && c.statusKey === statusKey && now - c.fetchedAt < CLUSTER_TTL_MS && bboxContains(c.box, viewBox),
    );
    if (cached) {
      setClusterView(cached);
      setStatus('ok');
      return;
    }
    setStatus('loading');
    const box = snapBbox(regionToBbox(view, DETAIL_MARGIN), clusterCellDegrees(zoom));
    try {
      const clusters = await fetchClusters(box, zoom, statuses);
      const answer: ClusterView = { zoom, statusKey, box, clusters, fetchedAt: Date.now() };
      clusterViewsRef.current = [...clusterViewsRef.current.slice(-(MAX_CLUSTER_VIEWS - 1)), answer];
      if (isLatest()) setClusterView(answer);
      succeeded();
    } catch (error) {
      if (isLatest()) setClusterView(null);
      failed(error);
    }
  }, [applyCourts]);

  // Offline cache: courts and areas saved by an earlier session, shown until fresh data arrives.
  useEffect(() => {
    let active = true;
    loadCourtCache()
      .then((raw) => {
        if (!active || typeof raw !== 'object' || raw === null) return;
        const cache = raw as { v?: unknown; areas?: unknown; courts?: unknown };
        if (cache.v !== CACHE_VERSION || !Array.isArray(cache.areas) || !Array.isArray(cache.courts)) return;
        const map = courtsRef.current;
        for (const entry of cache.courts) {
          const court = decodeCourt(entry);
          // Never overwrite something fetched this session.
          if (court && !map.has(court.id)) map.set(court.id, court);
        }
        const saved = cache.areas.map(decodeArea).filter((area): area is Area => area !== null);
        areasRef.current = [...saved, ...areasRef.current].slice(-MAX_AREAS);
        setVersion((v) => v + 1);
      })
      .catch((error: unknown) => console.warn('Could not read saved courts', error));
    return () => {
      active = false;
    };
  }, []);

  // Debounced fetch whenever the map settles somewhere new or the filter changes.
  useEffect(() => {
    const timer = setTimeout(() => void refresh(), DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [region, filter, refresh]);

  // While offline, retry now and then (the map may not move again).
  useEffect(() => {
    if (status !== 'offline') return;
    const timer = setTimeout(() => void refresh(), OFFLINE_RETRY_MS);
    return () => clearTimeout(timer);
  }, [status, refresh]);

  useEffect(
    () => () => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
    },
    [],
  );

  const courts = useMemo(() => [...courtsRef.current.values()], [version]); // eslint-disable-line react-hooks/exhaustive-deps
  const isCovered = useCallback(
    (point: LatLng) => areasRef.current.some((area) => pointInBbox(point, area)),
    [version], // eslint-disable-line react-hooks/exhaustive-deps
  );
  const detail = region.longitudeDelta <= DETAIL_MAX_DELTA;

  return {
    courts,
    isCovered,
    clusters: detail ? null : (clusterView?.clusters ?? null),
    status,
  };
}
