import Ionicons from '@expo/vector-icons/Ionicons';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { FLOATING_SHADOW, useTheme } from '../theme';

interface Props {
  inViewCount: number;
  placing: boolean;
  onCancelPlacing: () => void;
}

/** Count of courts in view, plus the "tap to place" hint while adding a court. */
export function MapStatus({ inViewCount, placing, onCancelPlacing }: Props) {
  const theme = useTheme();
  return (
    <View style={styles.column} pointerEvents="box-none">
      <View style={[styles.pill, { backgroundColor: theme.surface }]} accessibilityLiveRegion="polite">
        <Text style={[styles.count, { color: theme.text }]}>
          {inViewCount} {inViewCount === 1 ? 'court' : 'courts'} in view
        </Text>
      </View>
      {placing ? (
        <View style={[styles.hint, { backgroundColor: theme.accent }]}>
          <Ionicons name="hand-left-outline" size={18} color={theme.onAccent} />
          <Text style={[styles.hintText, { color: theme.onAccent }]}>Tap the map where the court is</Text>
          <Pressable accessibilityRole="button" accessibilityLabel="Cancel adding a court" onPress={onCancelPlacing} hitSlop={10}>
            <Ionicons name="close" size={20} color={theme.onAccent} />
          </Pressable>
        </View>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  column: {
    flexShrink: 1,
    alignItems: 'flex-start',
    gap: 8,
  },
  pill: {
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 14,
    boxShadow: FLOATING_SHADOW,
  },
  count: {
    fontSize: 13,
    fontWeight: '700',
  },
  hint: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
    paddingLeft: 12,
    paddingRight: 10,
    paddingVertical: 10,
    borderRadius: 14,
    boxShadow: FLOATING_SHADOW,
  },
  hintText: {
    flexShrink: 1,
    fontSize: 14,
    fontWeight: '700',
  },
});
