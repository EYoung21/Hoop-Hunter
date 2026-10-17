import { StatusBar } from 'expo-status-bar';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { BackHandler, StyleSheet, View } from 'react-native';
import MapView, { type LongPressEvent, type MapPressEvent, type Region } from 'react-native-maps';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { HIDDEN_KINDS, kindsForFilter } from '../data/classify';
import { useCourtData } from '../data/useCourtData';
import { useRemoteCourts } from '../data/useRemoteCourts';
import {
  buildMapItems,
  INITIAL_REGION,
  isInRegion,
  type MapItem,
  serverClusterItem,
  USER_ZOOM_DELTA,
} from '../data/viewport';
import { hasLocationPermission, locateUser } from '../location';
import { useSocial } from '../social/SocialProvider';
import { useTheme } from '../theme';
import type { Court, CourtFilter, LatLng, MapStyle } from '../types';
import { AddCourtForm } from './AddCourtForm';
import { ClusterMarker } from './ClusterMarker';
import { CourtCard } from './CourtCard';
import { CourtMarker } from './CourtMarker';
import { FilterBar } from './FilterBar';
import { Legend } from './Legend';
import { MapControls } from './MapControls';
import { MapStatus } from './MapStatus';
import { SocialScreen } from './social/SocialScreen';

/**
 * On iOS a tap on a marker can also reach the map's own onPress. Map presses this
 * soon after a marker press are ignored so they don't immediately close the card.
 */
const MARKER_PRESS_GUARD_MS = 400;

export function MapScreen() {
  const theme = useTheme();
  const insets = useSafeAreaInsets();
  const mapRef = useRef<MapView>(null);
  const lastMarkerPress = useRef(0);

  const [region, setRegion] = useState<Region>(INITIAL_REGION);
  const [filter, setFilter] = useState<CourtFilter>('all');
  const remote = useRemoteCourts(region, filter);
  const { entries, setReview, addCourt, deleteCourt } = useCourtData(remote);
  const [showRejected, setShowRejected] = useState(false);
  const [mapStyle, setMapStyle] = useState<MapStyle>('standard');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [placing, setPlacing] = useState(false);
  const [draftCoordinate, setDraftCoordinate] = useState<LatLng | null>(null);
  const [showsUserLocation, setShowsUserLocation] = useState(false);
  const [locating, setLocating] = useState(false);
  const [socialOpen, setSocialOpen] = useState(false);
  const social = useSocial();

  // Show the blue dot right away if location was allowed before; never prompts.
  useEffect(() => {
    hasLocationPermission()
      .then(setShowsUserLocation)
      .catch(() => setShowsUserLocation(false));
  }, []);

  const visibleEntries = useMemo(() => {
    const kinds = kindsForFilter(filter, showRejected);
    return entries.filter((entry) => kinds.has(entry.kind));
  }, [entries, filter, showRejected]);

  // Zoomed out with server clusters: draw those, plus your own courts and the open card's court on top.
  // Otherwise draw the loaded courts, grouped on the phone when there are too many (buildMapItems).
  const serverClusters = remote.clusters;
  const mapItems = useMemo<MapItem[]>(() => {
    if (!serverClusters) return buildMapItems(visibleEntries, region, selectedId);
    const items: MapItem[] = serverClusters.map(serverClusterItem);
    for (const entry of visibleEntries) {
      if (entry.kind === 'mine' || entry.court.id === selectedId) items.push({ type: 'court', entry });
    }
    return items;
  }, [serverClusters, visibleEntries, region, selectedId]);

  const inViewCount = useMemo(() => {
    if (!serverClusters) return visibleEntries.filter((entry) => isInRegion(entry.court.coordinate, region)).length;
    let count = 0;
    for (const cluster of serverClusters) {
      if (isInRegion({ latitude: cluster.latitude, longitude: cluster.longitude }, region)) count += cluster.count;
    }
    for (const entry of visibleEntries) {
      if (entry.kind === 'mine' && isInRegion(entry.court.coordinate, region)) count += 1;
    }
    return count;
  }, [serverClusters, visibleEntries, region]);

  const { pendingCount, rejectedCount, hasIndoor, hasHome } = useMemo(() => {
    let pending = 0;
    let rejected = 0;
    let indoor = false;
    let home = false;
    for (const { kind } of entries) {
      if (kind === 'candidate') pending += 1;
      else if (HIDDEN_KINDS.includes(kind)) rejected += 1;
      if (kind === 'indoor') indoor = true;
      if (kind === 'home') home = true;
    }
    return { pendingCount: pending, rejectedCount: rejected, hasIndoor: indoor, hasHome: home };
  }, [entries]);

  const selected = useMemo(() => entries.find((entry) => entry.court.id === selectedId) ?? null, [entries, selectedId]);

  // Android back button closes the card or cancels placing before leaving the app.
  useEffect(() => {
    if (!selectedId && !placing) return;
    const subscription = BackHandler.addEventListener('hardwareBackPress', () => {
      setSelectedId(null);
      setPlacing(false);
      return true;
    });
    return () => subscription.remove();
  }, [selectedId, placing]);

  const selectCourt = useCallback((id: string) => {
    lastMarkerPress.current = Date.now();
    setPlacing(false);
    setSelectedId(id);
  }, []);

  const zoomTo = useCallback((target: Region) => {
    lastMarkerPress.current = Date.now();
    mapRef.current?.animateToRegion(target, 350);
  }, []);

  const handleMapPress = (event: MapPressEvent) => {
    if (event.nativeEvent.action === 'marker-press') return;
    if (Date.now() - lastMarkerPress.current < MARKER_PRESS_GUARD_MS) return;
    if (placing) {
      setPlacing(false);
      setDraftCoordinate(event.nativeEvent.coordinate);
    } else {
      setSelectedId(null);
    }
  };

  const handleLongPress = (event: LongPressEvent) => {
    setPlacing(false);
    setSelectedId(null);
    setDraftCoordinate(event.nativeEvent.coordinate);
  };

  const handleLocate = async () => {
    setLocating(true);
    const position = await locateUser().catch((error: unknown) => {
      console.warn('Could not get your location', error);
      return null;
    });
    setLocating(false);
    if (!position) return;
    setShowsUserLocation(true);
    mapRef.current?.animateToRegion({ ...position, latitudeDelta: USER_ZOOM_DELTA, longitudeDelta: USER_ZOOM_DELTA }, 600);
  };

  const handleStartPlacing = () => {
    setSelectedId(null);
    setPlacing((value) => !value);
  };

  const handleSaveCourt = (court: Court) => {
    addCourt(court);
    setDraftCoordinate(null);
    if (filter === 'verified' || filter === 'candidates') setFilter('all');
    setSelectedId(court.id);
  };

  // From the You screen (your check-in, your runs): close it and show the court.
  const openCourt = (courtId: string, coordinate: LatLng | null) => {
    setSocialOpen(false);
    setPlacing(false);
    setSelectedId(courtId);
    const target = coordinate ?? entries.find((entry) => entry.court.id === courtId)?.court.coordinate;
    if (target) {
      lastMarkerPress.current = Date.now();
      mapRef.current?.animateToRegion({ ...target, latitudeDelta: USER_ZOOM_DELTA, longitudeDelta: USER_ZOOM_DELTA }, 600);
    }
  };

  const handleDelete = (id: string) => {
    deleteCourt(id);
    setSelectedId(null);
  };

  const satellite = mapStyle === 'satellite';
  const sidePadding = { paddingLeft: 12 + insets.left, paddingRight: 12 + insets.right };

  return (
    <View style={[styles.screen, { backgroundColor: theme.background }]}>
      <MapView
        ref={mapRef}
        style={StyleSheet.absoluteFill}
        initialRegion={INITIAL_REGION}
        mapType={satellite ? 'hybrid' : 'standard'}
        showsUserLocation={showsUserLocation}
        showsMyLocationButton={false}
        showsCompass={false}
        rotateEnabled={false}
        pitchEnabled={false}
        toolbarEnabled={false}
        onRegionChangeComplete={setRegion}
        onPress={handleMapPress}
        onLongPress={handleLongPress}
      >
        {mapItems.map((item) =>
          item.type === 'cluster' ? (
            <ClusterMarker key={item.key} cluster={item} onPress={zoomTo} />
          ) : (
            <CourtMarker
              key={`${item.entry.court.id}:${item.entry.kind}:${item.entry.court.id === selectedId ? 'selected' : ''}`}
              court={item.entry.court}
              kind={item.entry.kind}
              selected={item.entry.court.id === selectedId}
              onSelect={selectCourt}
            />
          ),
        )}
      </MapView>

      <View style={[styles.top, { paddingTop: insets.top + 4 }]} pointerEvents="box-none">
        <FilterBar
          filter={filter}
          onChange={setFilter}
          pendingCount={pendingCount}
          rejectedCount={rejectedCount}
          showRejected={showRejected}
          onToggleRejected={() => setShowRejected((value) => !value)}
        />
        <View style={[styles.topRow, sidePadding]} pointerEvents="box-none">
          <View style={styles.topLeft} pointerEvents="box-none">
            <MapStatus
              inViewCount={inViewCount}
              remoteStatus={remote.status}
              placing={placing}
              onCancelPlacing={() => setPlacing(false)}
            />
            {placing ? null : <Legend showRejected={showRejected} showIndoor={hasIndoor} showHome={hasHome} />}
          </View>
          <MapControls
            mapStyle={mapStyle}
            onToggleMapStyle={() => setMapStyle(satellite ? 'standard' : 'satellite')}
            locating={locating}
            onLocate={handleLocate}
            placing={placing}
            onAdd={handleStartPlacing}
            onOpenSocial={() => {
              setPlacing(false);
              setSocialOpen(true);
            }}
            streak={social.status === 'ready' ? (social.profile?.streak ?? 0) : 0}
          />
        </View>
      </View>

      <CourtCard entry={selected} onClose={() => setSelectedId(null)} onReview={setReview} onDelete={handleDelete} />

      {draftCoordinate ? (
        <AddCourtForm coordinate={draftCoordinate} onCancel={() => setDraftCoordinate(null)} onSave={handleSaveCourt} />
      ) : null}

      {socialOpen ? <SocialScreen onClose={() => setSocialOpen(false)} onOpenCourt={openCourt} /> : null}

      <StatusBar style={satellite ? 'light' : 'auto'} />
    </View>
  );
}

const styles = StyleSheet.create({
  screen: {
    flex: 1,
  },
  top: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
  },
  topRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'flex-start',
    gap: 12,
  },
  topLeft: {
    flexShrink: 1,
    alignItems: 'flex-start',
    gap: 8,
  },
});
