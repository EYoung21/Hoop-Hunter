import Ionicons from '@expo/vector-icons/Ionicons';
import { useEffect, useRef, useState } from 'react';
import { Animated, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { BADGE_INFO } from '../../social/labels';
import type { Award } from '../../social/types';
import { FLOATING_SHADOW, useTheme } from '../../theme';

const SHOW_MS = 3200;

/** "+10 XP · 6-day streak", plus any new badge, sliding in at the top for a few seconds. */
export function XpToast({ award, onDone }: { award: Award | null; onDone: () => void }) {
  const theme = useTheme();
  const insets = useSafeAreaInsets();
  const progress = useRef(new Animated.Value(0)).current;
  const [shown, setShown] = useState<Award | null>(null);
  const done = useRef(onDone);
  done.current = onDone;

  useEffect(() => {
    if (!award) return;
    setShown(award);
    progress.setValue(0);
    Animated.spring(progress, { toValue: 1, useNativeDriver: true, speed: 14, bounciness: 6 }).start();
    const timer = setTimeout(() => {
      Animated.timing(progress, { toValue: 0, duration: 220, useNativeDriver: true }).start(() => {
        setShown(null);
        done.current();
      });
    }, SHOW_MS);
    return () => clearTimeout(timer);
  }, [award, progress]);

  if (!shown) return null;
  const parts = [shown.xp > 0 ? `+${shown.xp} XP` : null, shown.streak ? `${shown.streak}-day streak` : null].filter(Boolean);
  const translateY = progress.interpolate({ inputRange: [0, 1], outputRange: [-80, 0] });

  return (
    <Animated.View
      pointerEvents="none"
      accessibilityLiveRegion="polite"
      style={[styles.wrap, { top: insets.top + 8, opacity: progress, transform: [{ translateY }] }]}
    >
      <View style={[styles.pill, { backgroundColor: theme.accent }]}>
        <Ionicons name={shown.streak ? 'flame' : 'star'} size={20} color={theme.onAccent} />
        <View>
          {parts.length > 0 ? <Text style={[styles.main, { color: theme.onAccent }]}>{parts.join(' · ')}</Text> : null}
          {shown.badges.map((badge) => (
            <Text key={badge} style={[styles.badge, { color: theme.onAccent }]}>
              New badge: {BADGE_INFO[badge].label}
            </Text>
          ))}
        </View>
      </View>
    </Animated.View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    position: 'absolute',
    left: 0,
    right: 0,
    alignItems: 'center',
  },
  pill: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 10,
    paddingHorizontal: 16,
    paddingVertical: 10,
    borderRadius: 18,
    boxShadow: FLOATING_SHADOW,
  },
  main: {
    fontSize: 16,
    fontWeight: '800',
  },
  badge: {
    fontSize: 13,
    fontWeight: '700',
  },
});
