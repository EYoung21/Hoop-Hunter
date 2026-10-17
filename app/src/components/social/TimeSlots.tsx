import { useMemo, useState } from 'react';
import { StyleSheet, View } from 'react-native';

import { dayLabel, dayStart, formatTime, slotsForDay } from '../../social/time';
import { useTheme } from '../../theme';
import { Note, OptionChip } from './ui';

interface Props {
  /** How many days to offer, starting today (2 = today and tomorrow). */
  days: number;
  value: Date | null;
  onChange: (value: Date) => void;
}

/** Day chips, then half-hour start times from 5:00 AM to 11:30 PM. No free typing. */
export function TimeSlots({ days, value, onChange }: Props) {
  const theme = useTheme();
  // Fixed while the picker is open, so the slots don't shift under your finger.
  const now = useMemo(() => new Date(), []);
  const offered = useMemo(() => Array.from({ length: days }, (_, i) => i).filter((i) => slotsForDay(i, now).length > 0), [days, now]);
  const initial = value ? Math.round((dayStart(0, value).getTime() - dayStart(0, now).getTime()) / 86_400_000) : offered[0] ?? 0;
  const [day, setDay] = useState(initial);
  const slots = slotsForDay(day, now);

  return (
    <View style={styles.column}>
      <View style={styles.wrap}>
        {offered.map((i) => (
          <OptionChip key={i} label={dayLabel(i, now)} selected={i === day} onPress={() => setDay(i)} />
        ))}
      </View>
      {slots.length === 0 ? (
        <Note icon="time-outline" text="No start times left today. Pick another day." theme={theme} />
      ) : (
        <View style={styles.wrap}>
          {slots.map((slot) => (
            <View key={slot.getTime()} style={styles.slot}>
              <OptionChip label={formatTime(slot)} selected={value?.getTime() === slot.getTime()} onPress={() => onChange(slot)} grow />
            </View>
          ))}
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  column: {
    gap: 12,
  },
  wrap: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: 8,
  },
  slot: {
    width: '23%',
    flexGrow: 1,
  },
});
