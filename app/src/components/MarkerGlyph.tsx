import { StyleSheet, Text, View, type ViewStyle } from 'react-native';

import { MARKER_COLORS } from '../theme';
import type { CourtKind } from '../types';

const MARKS: Partial<Record<CourtKind, string>> = {
  confirmed: '✓',
  rejected: '×',
};

function glyphStyle(kind: CourtKind, size: number): ViewStyle {
  const outline = Math.max(2, Math.round(size * 0.12));
  const color = MARKER_COLORS[kind];
  switch (kind) {
    case 'candidate':
      // Hollow ring: the blacktop underneath stays visible in satellite view.
      return {
        borderRadius: size / 2,
        borderWidth: Math.max(3, Math.round(size * 0.2)),
        borderColor: color,
        backgroundColor: 'rgba(255,255,255,0.35)',
      };
    case 'mine':
      return { borderRadius: size * 0.28, borderWidth: outline, borderColor: '#FFFFFF', backgroundColor: color };
    case 'rejected':
      return { borderRadius: size / 2, borderWidth: outline, borderColor: '#FFFFFF', backgroundColor: color, opacity: 0.8 };
    case 'verified':
    case 'confirmed':
      return { borderRadius: size / 2, borderWidth: outline, borderColor: '#FFFFFF', backgroundColor: color };
  }
}

/** The shape drawn for each kind of court. Shared by map markers and the legend. */
export function MarkerGlyph({ kind, size }: { kind: CourtKind; size: number }) {
  const mark = MARKS[kind];
  return (
    <View style={[styles.glyph, { width: size, height: size }, glyphStyle(kind, size)]}>
      {mark ? (
        <Text style={[styles.mark, { fontSize: size * 0.55, lineHeight: size * 0.7 }]} allowFontScaling={false}>
          {mark}
        </Text>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  glyph: {
    alignItems: 'center',
    justifyContent: 'center',
  },
  mark: {
    color: '#FFFFFF',
    fontWeight: '800',
    textAlign: 'center',
    includeFontPadding: false,
  },
});
