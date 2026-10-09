import Ionicons from '@expo/vector-icons/Ionicons';
import { Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { FILTER_LABELS } from '../data/classify';
import { FLOATING_SHADOW, useTheme } from '../theme';
import type { CourtFilter } from '../types';
import type { IconName } from './Button';

const FILTERS: readonly CourtFilter[] = ['all', 'verified', 'candidates', 'mine'];

interface Props {
  filter: CourtFilter;
  onChange: (filter: CourtFilter) => void;
  /** Candidates still waiting for a review; shown as a badge. */
  pendingCount: number;
  rejectedCount: number;
  showRejected: boolean;
  onToggleRejected: () => void;
}

export function FilterBar({ filter, onChange, pendingCount, rejectedCount, showRejected, onToggleRejected }: Props) {
  const insets = useSafeAreaInsets();
  return (
    <ScrollView
      horizontal
      showsHorizontalScrollIndicator={false}
      contentContainerStyle={[styles.row, { paddingLeft: 12 + insets.left, paddingRight: 12 + insets.right }]}
    >
      {FILTERS.map((value) => (
        <Chip
          key={value}
          label={FILTER_LABELS[value]}
          badge={value === 'candidates' && pendingCount > 0 ? pendingCount : undefined}
          active={filter === value}
          onPress={() => onChange(value)}
        />
      ))}
      {rejectedCount > 0 ? (
        <Chip
          label={`Rejected (${rejectedCount})`}
          icon={showRejected ? 'eye-outline' : 'eye-off-outline'}
          active={showRejected}
          onPress={onToggleRejected}
        />
      ) : null}
    </ScrollView>
  );
}

interface ChipProps {
  label: string;
  active: boolean;
  onPress: () => void;
  badge?: number;
  icon?: IconName;
}

function Chip({ label, active, onPress, badge, icon }: ChipProps) {
  const theme = useTheme();
  const color = active ? theme.onAccent : theme.text;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ selected: active }}
      onPress={onPress}
      style={({ pressed }) => [
        styles.chip,
        { backgroundColor: active ? theme.accent : theme.surface },
        pressed && styles.pressed,
      ]}
    >
      {icon ? <Ionicons name={icon} size={15} color={color} /> : null}
      <Text style={[styles.label, { color }]}>{label}</Text>
      {badge !== undefined ? (
        <View style={[styles.badge, { backgroundColor: active ? theme.onAccent : theme.accent }]}>
          <Text style={[styles.badgeText, { color: active ? theme.accent : theme.onAccent }]}>{badge}</Text>
        </View>
      ) : null}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  row: {
    gap: 8,
    paddingVertical: 8,
  },
  chip: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    height: 36,
    paddingHorizontal: 14,
    borderRadius: 18,
    boxShadow: FLOATING_SHADOW,
  },
  pressed: {
    opacity: 0.75,
  },
  label: {
    fontSize: 14,
    fontWeight: '700',
  },
  badge: {
    minWidth: 20,
    height: 20,
    borderRadius: 10,
    paddingHorizontal: 5,
    alignItems: 'center',
    justifyContent: 'center',
  },
  badgeText: {
    fontSize: 12,
    fontWeight: '800',
  },
});
