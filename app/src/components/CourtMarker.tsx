import { memo } from 'react';
import { StyleSheet, View } from 'react-native';
import { Marker } from 'react-native-maps';

import type { Court, CourtKind } from '../types';
import { MarkerGlyph } from './MarkerGlyph';

const SIZE = 22;
const SELECTED_SIZE = 32;
/** Transparent padding around the glyph so small markers are easier to tap. */
const TOUCH_PADDING = 8;
const CENTER = { x: 0.5, y: 0.5 };

const Z_INDEX: Record<CourtKind, number> = {
  rejected: 0,
  verified: 1,
  confirmed: 2,
  candidate: 2,
  mine: 3,
};

interface Props {
  court: Court;
  kind: CourtKind;
  selected: boolean;
  onSelect: (id: string) => void;
}

/**
 * tracksViewChanges is off for performance: Android draws the marker view to a
 * bitmap once. The parent therefore keys this component by id + kind + selection,
 * so any visual change remounts the marker and it is redrawn.
 */
function CourtMarkerView({ court, kind, selected, onSelect }: Props) {
  const size = selected ? SELECTED_SIZE : SIZE;
  return (
    <Marker
      coordinate={court.coordinate}
      anchor={CENTER}
      tracksViewChanges={false}
      stopPropagation
      zIndex={selected ? 100 : Z_INDEX[kind]}
      onPress={() => onSelect(court.id)}
      accessibilityLabel={court.name ?? 'Unnamed court'}
    >
      <View collapsable={false} style={[styles.touchArea, { width: size + TOUCH_PADDING * 2, height: size + TOUCH_PADDING * 2 }]}>
        <MarkerGlyph kind={kind} size={size} />
      </View>
    </Marker>
  );
}

export const CourtMarker = memo(CourtMarkerView);

const styles = StyleSheet.create({
  touchArea: {
    alignItems: 'center',
    justifyContent: 'center',
  },
});
