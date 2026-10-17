import Ionicons from '@expo/vector-icons/Ionicons';
import { ActivityIndicator, Modal, Pressable, StyleSheet, Text, View } from 'react-native';

import { useTheme } from '../../theme';
import { Button } from '../Button';

interface Props {
  visible: boolean;
  busy: boolean;
  onAnswer: (answer: 'adult' | 'under13' | 'later') => void;
}

/** Asked once, before the first check-in, run or leaderboard. */
export function AgeGate({ visible, busy, onAnswer }: Props) {
  const theme = useTheme();
  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={() => onAnswer('later')}>
      <View style={styles.backdrop}>
        <View style={[styles.card, { backgroundColor: theme.surface }]}>
          <View style={[styles.icon, { backgroundColor: theme.surfaceMuted }]}>
            <Ionicons name="people" size={26} color={theme.accent} />
          </View>
          <Text style={[styles.title, { color: theme.text }]}>Join the hoopers</Text>
          <Text style={[styles.body, { color: theme.textMuted }]}>
            Check-ins, pickup runs and leaderboards are for players 13 and older. Other players only ever see your
            handle, XP, streak and badges.
          </Text>
          {busy ? (
            <ActivityIndicator color={theme.accent} style={styles.busy} />
          ) : (
            <>
              <View style={styles.row}>
                <Button label="I'm 13 or older" icon="checkmark" onPress={() => onAnswer('adult')} />
              </View>
              <View style={styles.row}>
                <Button label="I'm under 13" variant="secondary" onPress={() => onAnswer('under13')} />
              </View>
              <Pressable accessibilityRole="button" onPress={() => onAnswer('later')} hitSlop={10}>
                <Text style={[styles.later, { color: theme.textMuted }]}>Not now</Text>
              </Pressable>
            </>
          )}
        </View>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  backdrop: {
    flex: 1,
    backgroundColor: 'rgba(0, 0, 0, 0.45)',
    alignItems: 'center',
    justifyContent: 'center',
    padding: 24,
  },
  card: {
    width: '100%',
    maxWidth: 380,
    borderRadius: 22,
    padding: 22,
    gap: 14,
    alignItems: 'center',
  },
  icon: {
    width: 52,
    height: 52,
    borderRadius: 26,
    alignItems: 'center',
    justifyContent: 'center',
  },
  title: {
    fontSize: 20,
    fontWeight: '800',
  },
  body: {
    fontSize: 15,
    lineHeight: 21,
    textAlign: 'center',
  },
  row: {
    flexDirection: 'row',
    alignSelf: 'stretch',
  },
  later: {
    fontSize: 15,
    fontWeight: '600',
    paddingVertical: 4,
  },
  busy: {
    marginVertical: 20,
  },
});
