import Ionicons from '@expo/vector-icons/Ionicons';
import { useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { REPORT_LABELS } from '../../social/labels';
import { REPORT_REASONS, type ReportReason } from '../../social/types';
import { useTheme } from '../../theme';

import { Sheet } from './ui';

interface Props {
  /** "Report Hooper4821" or "Report this run". */
  title: string;
  /** Reasons that fit (a run can be fake; a handle can be offensive). */
  reasons?: readonly ReportReason[];
  busy: boolean;
  onCancel: () => void;
  onSubmit: (reason: ReportReason) => void;
}

/** Pick a preset reason. Reports go to the moderators only; nobody else sees them. */
export function ReportSheet({ title, reasons = REPORT_REASONS, busy, onCancel, onSubmit }: Props) {
  const theme = useTheme();
  const [reason, setReason] = useState<ReportReason | null>(null);
  return (
    <Sheet
      title={title}
      onClose={onCancel}
      action={{ label: 'Send', onPress: () => reason && onSubmit(reason), disabled: !reason, busy }}
    >
      <View style={[styles.list, { backgroundColor: theme.surface, borderColor: theme.border }]}>
        {reasons.map((value, index) => {
          const selected = value === reason;
          return (
            <Pressable
              key={value}
              accessibilityRole="radio"
              accessibilityState={{ selected }}
              onPress={() => setReason(value)}
              style={[styles.row, index > 0 && { borderTopColor: theme.border, borderTopWidth: StyleSheet.hairlineWidth }]}
            >
              <Text style={[styles.label, { color: theme.text }]}>{REPORT_LABELS[value]}</Text>
              <Ionicons name={selected ? 'radio-button-on' : 'radio-button-off'} size={22} color={selected ? theme.accent : theme.textMuted} />
            </Pressable>
          );
        })}
      </View>
      <Text style={[styles.footnote, { color: theme.textMuted }]}>
        Reports go to Hoop Hunter's moderators only. The player isn't told who reported them. To stop seeing someone,
        block them.
      </Text>
    </Sheet>
  );
}

const styles = StyleSheet.create({
  list: {
    borderRadius: 14,
    borderWidth: 1,
  },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 16,
    paddingVertical: 14,
  },
  label: {
    fontSize: 16,
    fontWeight: '600',
  },
  footnote: {
    fontSize: 13,
    lineHeight: 18,
  },
});
