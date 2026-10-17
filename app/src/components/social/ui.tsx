import Ionicons from '@expo/vector-icons/Ionicons';
import type { ReactNode } from 'react';
import { ActivityIndicator, Alert, KeyboardAvoidingView, Modal, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { type Theme, useTheme } from '../../theme';
import type { IconName } from '../Button';

interface SheetProps {
  title: string;
  onClose: () => void;
  /** Left toolbar label (default "Cancel"). */
  closeLabel?: string;
  /** Optional right toolbar action, e.g. Save. */
  action?: { label: string; onPress: () => void; disabled?: boolean; busy?: boolean };
  /** Wrap the content in a ScrollView (default). Pass false for a FlatList body. */
  scroll?: boolean;
  /** Replaces the title with a custom header, e.g. tabs. */
  header?: ReactNode;
  children: ReactNode;
}

/** Full-height page sheet with a toolbar, like the add-court form. */
export function Sheet({ title, onClose, closeLabel = 'Cancel', action, scroll = true, header, children }: SheetProps) {
  const theme = useTheme();
  return (
    <Modal visible animationType="slide" presentationStyle="pageSheet" onRequestClose={onClose}>
      <SafeAreaView style={[styles.screen, { backgroundColor: theme.background }]}>
        <KeyboardAvoidingView style={styles.screen} behavior="padding">
          <View style={[styles.toolbar, { borderBottomColor: theme.border }]}>
            <Pressable accessibilityRole="button" onPress={onClose} hitSlop={10} style={styles.toolbarSide}>
              <Text style={[styles.toolbarAction, { color: theme.textMuted }]}>{closeLabel}</Text>
            </Pressable>
            {header ?? <Text style={[styles.toolbarTitle, { color: theme.text }]}>{title}</Text>}
            <View style={[styles.toolbarSide, styles.toolbarRight]}>
              {action ? (
                action.busy ? (
                  <ActivityIndicator color={theme.accent} />
                ) : (
                  <Pressable accessibilityRole="button" onPress={action.onPress} disabled={action.disabled} hitSlop={10}>
                    <Text style={[styles.toolbarAction, styles.toolbarSave, { color: theme.accent, opacity: action.disabled ? 0.4 : 1 }]}>
                      {action.label}
                    </Text>
                  </Pressable>
                )
              ) : null}
            </View>
          </View>
          {scroll ? (
            <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
              {children}
            </ScrollView>
          ) : (
            children
          )}
        </KeyboardAvoidingView>
      </SafeAreaView>
    </Modal>
  );
}

export function SectionLabel({ text, theme }: { text: string; theme: Theme }) {
  return <Text style={[styles.sectionLabel, { color: theme.textMuted }]}>{text.toUpperCase()}</Text>;
}

interface ChipProps {
  label: string;
  selected: boolean;
  onPress: () => void;
  /** Stretch to share the row (segmented look). */
  grow?: boolean;
}

/** A selectable option, styled like the add-court form's segmented buttons. */
export function OptionChip({ label, selected, onPress, grow = false }: ChipProps) {
  const theme = useTheme();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ selected }}
      onPress={onPress}
      style={[
        styles.chip,
        grow && styles.grow,
        selected ? { backgroundColor: theme.accent, borderColor: theme.accent } : { backgroundColor: theme.surface, borderColor: theme.border },
      ]}
    >
      <Text style={[styles.chipText, { color: selected ? theme.onAccent : theme.text }]}>{label}</Text>
    </Pressable>
  );
}

interface SmallButtonProps {
  label: string;
  onPress: () => void;
  icon?: IconName;
  /** primary: filled accent. secondary: outlined. danger: outlined red. */
  variant?: 'primary' | 'secondary' | 'danger';
  busy?: boolean;
  accessibilityLabel?: string;
}

/** A compact button for rows (Button is full-width). */
export function SmallButton({ label, onPress, icon, variant = 'secondary', busy = false, accessibilityLabel }: SmallButtonProps) {
  const theme = useTheme();
  const filled = variant === 'primary';
  const tint = variant === 'danger' ? theme.danger : filled ? theme.onAccent : theme.text;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
      accessibilityState={{ busy }}
      disabled={busy}
      onPress={onPress}
      style={({ pressed }) => [
        styles.small,
        filled ? { backgroundColor: theme.accent } : { borderColor: variant === 'danger' ? theme.danger : theme.border, borderWidth: 1.5 },
        (pressed || busy) && styles.pressed,
      ]}
    >
      {busy ? <ActivityIndicator size="small" color={tint} /> : icon ? <Ionicons name={icon} size={15} color={tint} /> : null}
      {label ? (
        <Text style={[styles.smallText, { color: tint }]} numberOfLines={1}>
          {label}
        </Text>
      ) : null}
    </Pressable>
  );
}

/** Asks before something that can't be undone from here (block, cancel a run, new friend code). */
export function confirmAction(title: string, message: string, confirmLabel: string, destructive = false): Promise<boolean> {
  return new Promise((resolve) => {
    Alert.alert(
      title,
      message,
      [
        { text: 'Cancel', style: 'cancel', onPress: () => resolve(false) },
        { text: confirmLabel, style: destructive ? 'destructive' : 'default', onPress: () => resolve(true) },
      ],
      { cancelable: true, onDismiss: () => resolve(false) },
    );
  });
}

/** A muted line with an icon: empty states, notes, errors. */
export function Note({ icon, text, theme, color }: { icon: IconName; text: string; theme: Theme; color?: string }) {
  return (
    <View style={styles.note}>
      <Ionicons name={icon} size={16} color={color ?? theme.textMuted} />
      <Text style={[styles.noteText, { color: color ?? theme.textMuted }]}>{text}</Text>
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
    gap: 12,
  },
  toolbarSide: {
    minWidth: 64,
  },
  toolbarRight: {
    alignItems: 'flex-end',
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
  sectionLabel: {
    fontSize: 12,
    fontWeight: '800',
    letterSpacing: 0.6,
  },
  chip: {
    minHeight: 40,
    paddingHorizontal: 14,
    borderRadius: 12,
    borderWidth: 1,
    alignItems: 'center',
    justifyContent: 'center',
  },
  grow: {
    flexGrow: 1,
  },
  chipText: {
    fontSize: 15,
    fontWeight: '700',
  },
  small: {
    minHeight: 36,
    borderRadius: 10,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 5,
    paddingHorizontal: 12,
  },
  smallText: {
    fontSize: 14,
    fontWeight: '700',
  },
  pressed: {
    opacity: 0.7,
  },
  note: {
    flexDirection: 'row',
    alignItems: 'flex-start',
    gap: 8,
  },
  noteText: {
    flex: 1,
    fontSize: 14,
    lineHeight: 19,
  },
});
