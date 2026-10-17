import Ionicons from '@expo/vector-icons/Ionicons';
import { useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { FORMAT_LABELS, SKILL_LABELS } from '../../social/labels';
import { formatWhen } from '../../social/time';
import { RUN_FORMATS, type RunFormat, type RunSkill } from '../../social/types';
import { type Theme, useTheme } from '../../theme';
import { Button } from '../Button';
import { TimeSlots } from './TimeSlots';
import { OptionChip, SectionLabel, Sheet } from './ui';

const MIN_PLAYERS = 2;
const MAX_PLAYERS = 30;
/** A sensible cap to start from when you switch one on. */
const DEFAULT_CAP: Record<RunFormat, number> = { '5v5': 10, '4v4': 8, '3v3': 6, '2v2': 4, '1v1': 2, shootaround: 6 };

export interface RunDraft {
  format: RunFormat;
  startAt: string;
  skill: RunSkill | null;
  maxPlayers: number | null;
}

interface Props {
  courtName: string;
  onCancel: () => void;
  onSave: (draft: RunDraft) => void;
}

/** Post a pickup run: presets only, no text. */
export function RunForm({ courtName, onCancel, onSave }: Props) {
  const theme = useTheme();
  const [format, setFormat] = useState<RunFormat>('5v5');
  const [startAt, setStartAt] = useState<Date | null>(null);
  const [skill, setSkill] = useState<RunSkill | null>(null);
  const [cap, setCap] = useState<number | null>(null);

  const save = () => {
    if (startAt) onSave({ format, startAt: startAt.toISOString(), skill, maxPlayers: cap });
  };

  return (
    <Sheet title="Post a run" onClose={onCancel} action={{ label: 'Post', onPress: save, disabled: !startAt }}>
      <Text style={[styles.court, { color: theme.text }]} numberOfLines={2}>
        {courtName}
      </Text>

      <View style={styles.field}>
        <SectionLabel text="Format" theme={theme} />
        <View style={styles.wrap}>
          {RUN_FORMATS.map((value) => (
            <View key={value} style={styles.third}>
              <OptionChip label={FORMAT_LABELS[value]} selected={format === value} onPress={() => setFormat(value)} grow />
            </View>
          ))}
        </View>
      </View>

      <View style={styles.field}>
        <SectionLabel text="Start" theme={theme} />
        <TimeSlots days={7} value={startAt} onChange={setStartAt} />
      </View>

      <View style={styles.field}>
        <SectionLabel text="Skill level (optional)" theme={theme} />
        <View style={styles.row}>
          {(['casual', 'competitive'] as const).map((value) => (
            <OptionChip key={value} label={SKILL_LABELS[value]} selected={skill === value} onPress={() => setSkill(skill === value ? null : value)} grow />
          ))}
        </View>
      </View>

      <View style={styles.field}>
        <SectionLabel text="Max players (optional)" theme={theme} />
        <View style={styles.row}>
          <OptionChip label="No limit" selected={cap === null} onPress={() => setCap(null)} grow />
          <OptionChip label="Set a limit" selected={cap !== null} onPress={() => setCap(cap ?? DEFAULT_CAP[format])} grow />
        </View>
        {cap !== null ? (
          <View style={styles.stepper}>
            <Step theme={theme} icon="remove" label="Fewer players" disabled={cap <= MIN_PLAYERS} onPress={() => setCap(cap - 1)} />
            <Text style={[styles.stepValue, { color: theme.text }]}>{cap}</Text>
            <Step theme={theme} icon="add" label="More players" disabled={cap >= MAX_PLAYERS} onPress={() => setCap(cap + 1)} />
          </View>
        ) : null}
      </View>

      {startAt ? (
        <View style={styles.row}>
          <Button label={`Post for ${formatWhen(startAt)}`} icon="basketball" onPress={save} />
        </View>
      ) : null}
      <Text style={[styles.footnote, { color: theme.textMuted }]}>
        Runs have no chat or notes: just the format, time and who's in. Players tap "I'm in" or "Leave". The run
        disappears 3 hours after it starts, and you can cancel it any time.
      </Text>
    </Sheet>
  );
}

interface StepProps {
  theme: Theme;
  icon: 'add' | 'remove';
  label: string;
  disabled: boolean;
  onPress: () => void;
}

function Step({ theme, icon, label, disabled, onPress }: StepProps) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityState={{ disabled }}
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => [
        styles.stepButton,
        { backgroundColor: theme.surface, borderColor: theme.border, opacity: disabled ? 0.4 : pressed ? 0.7 : 1 },
      ]}
    >
      <Ionicons name={icon} size={22} color={theme.text} />
    </Pressable>
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
  wrap: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: 8,
  },
  third: {
    width: '30%',
    flexGrow: 1,
  },
  row: {
    flexDirection: 'row',
    gap: 8,
  },
  stepper: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 18,
    marginTop: 4,
  },
  stepButton: {
    width: 46,
    height: 46,
    borderRadius: 23,
    borderWidth: 1,
    alignItems: 'center',
    justifyContent: 'center',
  },
  stepValue: {
    minWidth: 32,
    textAlign: 'center',
    fontSize: 24,
    fontWeight: '800',
  },
  footnote: {
    fontSize: 13,
    lineHeight: 18,
  },
});
