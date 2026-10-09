import Ionicons from '@expo/vector-icons/Ionicons';
import type { ComponentProps } from 'react';
import { Pressable, StyleSheet, Text } from 'react-native';

import { useTheme } from '../theme';

export type IconName = ComponentProps<typeof Ionicons>['name'];

interface Props {
  label: string;
  onPress: () => void;
  icon?: IconName;
  /** primary: filled accent. secondary: outlined. danger: outlined red. */
  variant?: 'primary' | 'secondary' | 'danger';
  /** Overrides the fill color of a primary button. */
  color?: string;
}

export function Button({ label, onPress, icon, variant = 'primary', color }: Props) {
  const theme = useTheme();
  const filled = variant === 'primary';
  const tint = variant === 'danger' ? theme.danger : filled ? theme.onAccent : theme.text;
  return (
    <Pressable
      accessibilityRole="button"
      onPress={onPress}
      style={({ pressed }) => [
        styles.button,
        filled
          ? { backgroundColor: color ?? theme.accent }
          : { borderColor: variant === 'danger' ? theme.danger : theme.border, borderWidth: 1.5 },
        pressed && styles.pressed,
      ]}
    >
      {icon ? <Ionicons name={icon} size={18} color={tint} /> : null}
      <Text style={[styles.label, { color: tint }]} numberOfLines={1}>
        {label}
      </Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  button: {
    flex: 1,
    minHeight: 46,
    borderRadius: 12,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 6,
    paddingHorizontal: 12,
  },
  pressed: {
    opacity: 0.7,
  },
  label: {
    fontSize: 15,
    fontWeight: '700',
  },
});
