import Ionicons from '@expo/vector-icons/Ionicons';
import { useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { KIND_LABELS } from '../data/classify';
import { FLOATING_SHADOW, useTheme } from '../theme';
import type { CourtKind } from '../types';
import { MarkerGlyph } from './MarkerGlyph';

const KINDS: readonly CourtKind[] = ['verified', 'candidate', 'confirmed', 'mine'];

/** Map key explaining the marker shapes. Tap to collapse or expand. */
export function Legend({ showRejected }: { showRejected: boolean }) {
  const theme = useTheme();
  const [open, setOpen] = useState(true);
  const kinds: readonly CourtKind[] = showRejected ? [...KINDS, 'rejected'] : KINDS;

  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={open ? 'Hide map key' : 'Show map key'}
      onPress={() => setOpen((value) => !value)}
      style={[styles.panel, { backgroundColor: theme.surface }]}
    >
      <View style={styles.header}>
        <Text style={[styles.title, { color: theme.textMuted }]}>MAP KEY</Text>
        <Ionicons name={open ? 'chevron-down' : 'chevron-up'} size={14} color={theme.textMuted} />
      </View>
      {open
        ? kinds.map((kind) => (
            <View key={kind} style={styles.row}>
              <View style={styles.glyph}>
                <MarkerGlyph kind={kind} size={16} />
              </View>
              <Text style={[styles.label, { color: theme.text }]}>{KIND_LABELS[kind]}</Text>
            </View>
          ))
        : null}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  panel: {
    borderRadius: 14,
    paddingHorizontal: 12,
    paddingVertical: 10,
    gap: 6,
    boxShadow: FLOATING_SHADOW,
  },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 8,
  },
  title: {
    fontSize: 11,
    fontWeight: '800',
    letterSpacing: 0.8,
  },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
  },
  glyph: {
    width: 18,
    alignItems: 'center',
  },
  label: {
    fontSize: 13,
    fontWeight: '600',
  },
});
