import Ionicons from '@expo/vector-icons/Ionicons';
import { ActivityIndicator, Pressable, StyleSheet, View } from 'react-native';

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
}

export function MapControls({ mapStyle, onToggleMapStyle, locating, onLocate, placing, onAdd }: Props) {
  const satellite = mapStyle === 'satellite';
  return (
    <View style={styles.column}>
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
}

function RoundButton({ icon, label, onPress, busy = false, active = false }: RoundButtonProps) {
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
});
