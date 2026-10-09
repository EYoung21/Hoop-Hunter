import { StatusBar } from 'expo-status-bar';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { BackHandler, StyleSheet, View } from 'react-native';
import MapView, { type LongPressEvent, type MapPressEvent, type Region } from 'react-native-maps';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { kindsForFilter } from '../data/classify';
import { useCourtData } from '../data/useCourtData';
import { buildMapItems, INITIAL_REGION, isInRegion, USER_ZOOM_DELTA } from '../data/viewport';
import { hasLocationPermission, locateUser } from '../location';
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
  const { entries, setReview, addCourt, deleteCourt } = useCourtData();

  const [region, setRegion] = useState<Region>(INITIAL_REGION);
  const [filter, setFilter] = useState<CourtFilter>('all');
  const [showRejected, setShowRejected] = useState(false);
  const [mapStyle, setMapStyle] = useState<MapStyle>('standard');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [placing, setPlacing] = useState(false);
  const [draftCoordinate, setDraftCoordinate] = useState<LatLng | null>(null);
  const [showsUserLocation, setShowsUserLocation] = useState(false);
  const [locating, setLocating] = useState(false);

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

  const inViewCount = useMemo(
    () => visibleEntries.filter((entry) => isInRegion(entry.court.coordinate, region)).length,
    [visibleEntries, region],
  );

  const mapItems = useMemo(() => buildMapItems(visibleEntries, region, selectedId), [visibleEntries, region, selectedId]);

  const { pendingCount, rejectedCount } = useMemo(() => {
    let pending = 0;
    let rejected = 0;
    for (const { kind } of entries) {
      if (kind === 'candidate') pending += 1;
      else if (kind === 'rejected') rejected += 1;
    }
    return { pendingCount: pending, rejectedCount: rejected };
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
            <MapStatus inViewCount={inViewCount} placing={placing} onCancelPlacing={() => setPlacing(false)} />
            {placing ? null : <Legend showRejected={showRejected} />}
          </View>
          <MapControls
            mapStyle={mapStyle}
            onToggleMapStyle={() => setMapStyle(satellite ? 'standard' : 'satellite')}
            locating={locating}
            onLocate={handleLocate}
            placing={placing}
            onAdd={handleStartPlacing}
          />
        </View>
      </View>

      <CourtCard entry={selected} onClose={() => setSelectedId(null)} onReview={setReview} onDelete={handleDelete} />

      {draftCoordinate ? (
        <AddCourtForm coordinate={draftCoordinate} onCancel={() => setDraftCoordinate(null)} onSave={handleSaveCourt} />
      ) : null}

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
