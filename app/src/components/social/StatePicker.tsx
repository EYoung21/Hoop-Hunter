import Ionicons from '@expo/vector-icons/Ionicons';
import { FlatList, Pressable, StyleSheet, Text } from 'react-native';

import { US_STATES } from '../../social/labels';
import { useTheme } from '../../theme';
import { Sheet } from './ui';

interface Props {
  current: string | null;
  onCancel: () => void;
  onPick: (state: string) => void;
}

/** Your state, for the state leaderboard. */
export function StatePicker({ current, onCancel, onPick }: Props) {
  const theme = useTheme();
  return (
    <Sheet title="Your state" onClose={onCancel} scroll={false}>
      <FlatList
        data={US_STATES}
        keyExtractor={([code]) => code}
        contentContainerStyle={styles.list}
        renderItem={({ item: [code, name] }) => {
          const selected = code === current;
          return (
            <Pressable
              accessibilityRole="button"
              accessibilityState={{ selected }}
              onPress={() => onPick(code)}
              style={({ pressed }) => [styles.row, { borderBottomColor: theme.border }, pressed && styles.pressed]}
            >
              <Text style={[styles.name, { color: theme.text }]}>{name}</Text>
              {selected ? <Ionicons name="checkmark" size={20} color={theme.accent} /> : null}
            </Pressable>
          );
        }}
      />
    </Sheet>
  );
}

const styles = StyleSheet.create({
  list: {
    paddingHorizontal: 20,
    paddingBottom: 24,
  },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingVertical: 14,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  name: {
    fontSize: 16,
    fontWeight: '600',
  },
  pressed: {
    opacity: 0.6,
  },
});
