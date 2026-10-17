import { useState } from 'react';
import { StyleSheet, Text, View } from 'react-native';

import { formatWhen } from '../../social/time';
import { useTheme } from '../../theme';
import { Button } from '../Button';
import { TimeSlots } from './TimeSlots';
import { SectionLabel, Sheet } from './ui';

interface Props {
  courtName: string;
  /** Your current check-in elsewhere, which this one replaces. */
  replaces: string | null;
  onCancel: () => void;
  onSave: (startAt: Date) => void;
}

/** "Hooping at <time>": a planned check-in for later today or tomorrow. */
export function PlanSheet({ courtName, replaces, onCancel, onSave }: Props) {
  const theme = useTheme();
  const [startAt, setStartAt] = useState<Date | null>(null);
  const save = () => {
    if (startAt) onSave(startAt);
  };

  return (
    <Sheet title="Hooping at…" onClose={onCancel} action={{ label: 'Save', onPress: save, disabled: !startAt }}>
      <Text style={[styles.court, { color: theme.text }]} numberOfLines={2}>
        {courtName}
      </Text>
      <View style={styles.field}>
        <SectionLabel text="When" theme={theme} />
        <TimeSlots days={2} value={startAt} onChange={setStartAt} />
      </View>
      {startAt ? (
        <View style={styles.saveRow}>
          <Button label={`Hooping at ${formatWhen(startAt)}`} icon="time-outline" onPress={save} />
        </View>
      ) : null}
      <Text style={[styles.footnote, { color: theme.textMuted }]}>
        Other players see how many are coming and when, not who. Friends who follow each other see your handle. It
        disappears 2 hours after the time you pick
        {replaces ? `, and it replaces your check-in at ${replaces}` : ''}.
      </Text>
    </Sheet>
  );
}

const styles = StyleSheet.create({
  court: {
    fontSize: 18,
    fontWeight: '800',
  },
  field: {
    gap: 8,
  },
  saveRow: {
    flexDirection: 'row',
  },
  footnote: {
    fontSize: 13,
    lineHeight: 18,
  },
});
