import Ionicons from '@expo/vector-icons/Ionicons';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';

import { FLOATING_SHADOW, useTheme } from '../theme';
import type { MapStyle } from '../types';
import type { IconName } from './Button';

interface Props {
  mapStyle: MapStyle;
  onToggleMapStyle: () => void;
  locating: boolean;
  onLocate: () => void;
  placing: boolean;
  onAdd: () => void;
  onOpenSocial: () => void;
  /** Current streak, shown on the "You" button when above 0. */
  streak: number;
}

export function MapControls({ mapStyle, onToggleMapStyle, locating, onLocate, placing, onAdd, onOpenSocial, streak }: Props) {
  const satellite = mapStyle === 'satellite';
  return (
    <View style={styles.column}>
      <RoundButton icon="person" label="You, friends and leaderboards" onPress={onOpenSocial} badge={streak > 0 ? streak : undefined} />
      <RoundButton
        icon={satellite ? 'map-outline' : 'earth'}
        label={satellite ? 'Show standard map' : 'Show satellite map'}
        onPress={onToggleMapStyle}
      />
      <RoundButton icon="locate" label="Go to my location" onPress={onLocate} busy={locating} />
      <RoundButton icon="add" label="Add a court" onPress={onAdd} active={placing} />
    </View>
  );
}

interface RoundButtonProps {
  icon: IconName;
  label: string;
  onPress: () => void;
  busy?: boolean;
  active?: boolean;
  /** Small count in the corner (the streak). */
  badge?: number;
}

function RoundButton({ icon, label, onPress, busy = false, active = false, badge }: RoundButtonProps) {
  const theme = useTheme();
  const tint = active ? theme.onAccent : theme.text;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityState={{ busy, selected: active }}
      disabled={busy}
      onPress={onPress}
      style={({ pressed }) => [
        styles.button,
        { backgroundColor: active ? theme.accent : theme.surface },
        pressed && styles.pressed,
      ]}
    >
      {busy ? <ActivityIndicator color={theme.accent} /> : <Ionicons name={icon} size={22} color={tint} />}
      {badge !== undefined ? (
        <View style={[styles.badge, { backgroundColor: theme.accent, borderColor: theme.surface }]}>
          <Ionicons name="flame" size={10} color={theme.onAccent} />
          <Text style={[styles.badgeText, { color: theme.onAccent }]}>{badge}</Text>
        </View>
      ) : null}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  column: {
    gap: 10,
  },
  button: {
    width: 46,
    height: 46,
    borderRadius: 23,
    alignItems: 'center',
    justifyContent: 'center',
    boxShadow: FLOATING_SHADOW,
  },
  pressed: {
    opacity: 0.75,
  },
  badge: {
    position: 'absolute',
    top: -4,
    right: -6,
    minWidth: 22,
    height: 18,
    borderRadius: 9,
    borderWidth: 1.5,
    paddingHorizontal: 3,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 1,
  },
  badgeText: {
    fontSize: 10,
    fontWeight: '800',
  },
});
