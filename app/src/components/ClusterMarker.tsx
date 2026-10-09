import { StyleSheet, Text, View } from 'react-native';
import { Marker, type Region } from 'react-native-maps';

import type { ClusterItem } from '../data/viewport';
import { ACCENT } from '../theme';

const CENTER = { x: 0.5, y: 0.5 };

interface Props {
  cluster: ClusterItem;
  onPress: (bounds: Region) => void;
}

/**
 * A group of nearby courts, shown when too many are in view. Tapping zooms in.
 * Keyed by cell + count (see buildMapItems) so a new count remounts and redraws it.
 */
export function ClusterMarker({ cluster, onPress }: Props) {
  const size = cluster.count < 10 ? 34 : cluster.count < 100 ? 40 : 48;
  return (
    <Marker
      coordinate={cluster.coordinate}
      anchor={CENTER}
      tracksViewChanges={false}
      stopPropagation
      zIndex={50}
      onPress={() => onPress(cluster.bounds)}
      accessibilityLabel={`${cluster.count} courts. Tap to zoom in.`}
    >
      <View collapsable={false} style={[styles.bubble, { width: size, height: size, borderRadius: size / 2 }]}>
        <Text style={styles.count} allowFontScaling={false}>
          {cluster.count}
        </Text>
      </View>
    </Marker>
  );
}

const styles = StyleSheet.create({
  bubble: {
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: ACCENT,
    borderWidth: 3,
    borderColor: '#FFFFFF',
  },
  count: {
    color: '#FFFFFF',
    fontWeight: '800',
    fontSize: 14,
    includeFontPadding: false,
  },
});
