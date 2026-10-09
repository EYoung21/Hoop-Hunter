import Ionicons from '@expo/vector-icons/Ionicons';
import { type ReactNode, useState } from 'react';
import { KeyboardAvoidingView, Modal, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { createUserCourt } from '../data/courts';
import { type Theme, useTheme } from '../theme';
import type { Court, LatLng } from '../types';
import { Button } from './Button';

const MIN_HOOPS = 1;
const MAX_HOOPS = 20;

const LIGHT_OPTIONS = [
  { label: 'Yes', value: true },
  { label: 'No', value: false },
] as const;

const SURFACE_OPTIONS = [
  { label: 'Asphalt', value: 'asphalt' },
  { label: 'Concrete', value: 'concrete' },
  { label: 'Other', value: 'other' },
] as const;

type Surface = (typeof SURFACE_OPTIONS)[number]['value'];

interface Props {
  coordinate: LatLng;
  onCancel: () => void;
  onSave: (court: Court) => void;
}

/** Full-screen form for a court the user drops on the map. */
export function AddCourtForm({ coordinate, onCancel, onSave }: Props) {
  const theme = useTheme();
  const [name, setName] = useState('');
  const [hoops, setHoops] = useState(2);
  const [lit, setLit] = useState<boolean | null>(null);
  const [surface, setSurface] = useState<Surface | null>(null);
  const [notes, setNotes] = useState('');

  const save = () => onSave(createUserCourt({ name, hoops, lit, surface, notes }, coordinate));
  const inputStyle = [styles.input, { color: theme.text, backgroundColor: theme.surface, borderColor: theme.border }];

  return (
    <Modal visible animationType="slide" presentationStyle="pageSheet" onRequestClose={onCancel}>
      <SafeAreaView style={[styles.screen, { backgroundColor: theme.background }]}>
        <KeyboardAvoidingView style={styles.screen} behavior="padding">
          <View style={[styles.toolbar, { borderBottomColor: theme.border }]}>
            <Pressable accessibilityRole="button" onPress={onCancel} hitSlop={10}>
              <Text style={[styles.toolbarAction, { color: theme.textMuted }]}>Cancel</Text>
            </Pressable>
            <Text style={[styles.toolbarTitle, { color: theme.text }]}>Add a court</Text>
            <Pressable accessibilityRole="button" onPress={save} hitSlop={10}>
              <Text style={[styles.toolbarAction, styles.toolbarSave, { color: theme.accent }]}>Save</Text>
            </Pressable>
          </View>

          <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
            <View style={styles.pinned}>
              <Ionicons name="location" size={16} color={theme.accent} />
              <Text style={[styles.pinnedText, { color: theme.textMuted }]}>
                Pinned at {coordinate.latitude.toFixed(5)}, {coordinate.longitude.toFixed(5)}
              </Text>
            </View>

            <Field theme={theme} label="Name">
              <TextInput
                value={name}
                onChangeText={setName}
                placeholder="e.g. Rec center outdoor court"
                placeholderTextColor={theme.textMuted}
                keyboardAppearance={theme.dark ? 'dark' : 'light'}
                returnKeyType="done"
                style={inputStyle}
              />
            </Field>

            <Field theme={theme} label="Number of hoops">
              <View style={styles.stepper}>
                <StepButton theme={theme} icon="remove" label="Fewer hoops" disabled={hoops <= MIN_HOOPS} onPress={() => setHoops(hoops - 1)} />
                <Text style={[styles.stepperValue, { color: theme.text }]}>{hoops}</Text>
                <StepButton theme={theme} icon="add" label="More hoops" disabled={hoops >= MAX_HOOPS} onPress={() => setHoops(hoops + 1)} />
              </View>
            </Field>

            <Field theme={theme} label="Lights">
              <Segmented theme={theme} options={LIGHT_OPTIONS} value={lit} onChange={setLit} />
            </Field>

            <Field theme={theme} label="Surface">
              <Segmented theme={theme} options={SURFACE_OPTIONS} value={surface} onChange={setSurface} />
            </Field>

            <Field theme={theme} label="Notes">
              <TextInput
                value={notes}
                onChangeText={setNotes}
                placeholder="Nets? Double rims? Busy after 5pm?"
                placeholderTextColor={theme.textMuted}
                keyboardAppearance={theme.dark ? 'dark' : 'light'}
                multiline
                style={[inputStyle, styles.notes]}
              />
            </Field>

            <View style={styles.saveRow}>
              <Button label="Save court" icon="checkmark" onPress={save} />
            </View>
            <Text style={[styles.footnote, { color: theme.textMuted }]}>
              Saved only on this phone. Lights and surface are optional: tap a selected option again to clear it.
            </Text>
          </ScrollView>
        </KeyboardAvoidingView>
      </SafeAreaView>
    </Modal>
  );
}

function Field({ theme, label, children }: { theme: Theme; label: string; children: ReactNode }) {
  return (
    <View style={styles.field}>
      <Text style={[styles.fieldLabel, { color: theme.textMuted }]}>{label.toUpperCase()}</Text>
      {children}
    </View>
  );
}

interface StepButtonProps {
  theme: Theme;
  icon: 'add' | 'remove';
  label: string;
  disabled: boolean;
  onPress: () => void;
}

function StepButton({ theme, icon, label, disabled, onPress }: StepButtonProps) {
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

interface SegmentedProps<T> {
  theme: Theme;
  options: readonly { label: string; value: T }[];
  value: T | null;
  /** Called with null when the selected option is tapped again. */
  onChange: (value: T | null) => void;
}

function Segmented<T>({ theme, options, value, onChange }: SegmentedProps<T>) {
  return (
    <View style={styles.segmented}>
      {options.map((option) => {
        const selected = option.value === value;
        return (
          <Pressable
            key={option.label}
            accessibilityRole="button"
            accessibilityState={{ selected }}
            onPress={() => onChange(selected ? null : option.value)}
            style={[
              styles.segment,
              selected
                ? { backgroundColor: theme.accent, borderColor: theme.accent }
                : { backgroundColor: theme.surface, borderColor: theme.border },
            ]}
          >
            <Text style={[styles.segmentText, { color: selected ? theme.onAccent : theme.text }]}>{option.label}</Text>
          </Pressable>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  screen: {
    flex: 1,
  },
  toolbar: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 20,
    paddingVertical: 14,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  toolbarTitle: {
    fontSize: 17,
    fontWeight: '800',
  },
  toolbarAction: {
    fontSize: 16,
    fontWeight: '600',
  },
  toolbarSave: {
    fontWeight: '800',
  },
  content: {
    padding: 20,
    gap: 20,
  },
  pinned: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
  },
  pinnedText: {
    fontSize: 13,
    fontWeight: '600',
  },
  field: {
    gap: 8,
  },
  fieldLabel: {
    fontSize: 12,
    fontWeight: '800',
    letterSpacing: 0.6,
  },
  input: {
    borderWidth: 1,
    borderRadius: 12,
    paddingHorizontal: 14,
    paddingVertical: 12,
    fontSize: 16,
  },
  notes: {
    minHeight: 96,
    textAlignVertical: 'top',
  },
  stepper: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 18,
  },
  stepButton: {
    width: 46,
    height: 46,
    borderRadius: 23,
    borderWidth: 1,
    alignItems: 'center',
    justifyContent: 'center',
  },
  stepperValue: {
    minWidth: 32,
    textAlign: 'center',
    fontSize: 24,
    fontWeight: '800',
  },
  segmented: {
    flexDirection: 'row',
    gap: 8,
  },
  segment: {
    flex: 1,
    minHeight: 44,
    borderRadius: 12,
    borderWidth: 1,
    alignItems: 'center',
    justifyContent: 'center',
  },
  segmentText: {
    fontSize: 15,
    fontWeight: '700',
  },
  saveRow: {
    flexDirection: 'row',
    marginTop: 4,
  },
  footnote: {
    fontSize: 13,
    lineHeight: 18,
    textAlign: 'center',
  },
});
