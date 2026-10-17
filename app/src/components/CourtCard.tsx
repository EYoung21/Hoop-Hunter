import Ionicons from '@expo/vector-icons/Ionicons';
import { useEffect, useRef, useState } from 'react';
import { Alert, Animated, Linking, Platform, Pressable, ScrollView, StyleSheet, Text, useWindowDimensions, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { type CourtEntry, KIND_LABELS, siteLabel, sourceLabel } from '../data/classify';
import { fetchReviewCounts, type ReviewCounts } from '../data/remote';
import { MARKER_COLORS, type Theme, useTheme } from '../theme';
import type { LatLng, ReviewDecision } from '../types';
import { Button } from './Button';
import { MarkerGlyph } from './MarkerGlyph';
import { CourtSocial } from './social/CourtSocial';

interface Props {
  /** The court to show, or null to slide the card away. */
  entry: CourtEntry | null;
  onClose: () => void;
  onReview: (id: string, decision: ReviewDecision | null) => void;
  onDelete: (id: string) => void;
}

function openDirections({ latitude, longitude }: LatLng) {
  const destination = `${latitude},${longitude}`;
  const url =
    Platform.OS === 'ios'
      ? `https://maps.apple.com/?daddr=${destination}`
      : `https://www.google.com/maps/dir/?api=1&destination=${destination}`;
  Linking.openURL(url).catch(() => Alert.alert("Couldn't open maps", `The court is at ${destination}.`));
}

function yesNo(value: boolean | null): string {
  if (value === null) return 'Unknown';
  return value ? 'Yes' : 'No';
}

function capitalize(value: string | null): string {
  return value ? value.charAt(0).toUpperCase() + value.slice(1).replace(/_/g, ' ') : 'Unknown';
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** What other players said about a candidate (review_counts), or null when unknown / offline / none yet. */
function useReviewCounts(courtId: string | null, refreshKey: unknown): ReviewCounts | null {
  const [counts, setCounts] = useState<{ id: string; counts: ReviewCounts } | null>(null);
  useEffect(() => {
    if (!courtId) return;
    let active = true;
    fetchReviewCounts([courtId])
      .then((byId) => {
        if (active) setCounts({ id: courtId, counts: byId[courtId] ?? { court: 0, notCourt: 0, homeCourt: 0, total: 0 } });
      })
      .catch(() => {
        if (active) setCounts(null);
      });
    return () => {
      active = false;
    };
  }, [courtId, refreshKey]);
  return counts && counts.id === courtId && counts.counts.total > 0 ? counts.counts : null;
}

function countsText({ court, notCourt, homeCourt }: ReviewCounts): string {
  const parts = [
    court ? `${plural(court, 'player says', 'players say')} it's a court` : null,
    notCourt ? `${notCourt} not a court` : null,
    homeCourt ? `${homeCourt} someone's home court` : null,
  ].filter(Boolean);
  return parts.join(' · ');
}

/** Bottom card with a court's details and actions. Slides up when `entry` is set. */
export function CourtCard({ entry, onClose, onReview, onDelete }: Props) {
  const theme = useTheme();
  const insets = useSafeAreaInsets();
  const { height: windowHeight } = useWindowDimensions();
  const [shown, setShown] = useState(entry);
  const [cardHeight, setCardHeight] = useState(windowHeight);
  const progress = useRef(new Animated.Value(0)).current;

  useEffect(() => {
    if (entry) {
      setShown(entry);
      Animated.spring(progress, { toValue: 1, useNativeDriver: true, speed: 16, bounciness: 3 }).start();
    } else {
      Animated.timing(progress, { toValue: 0, duration: 180, useNativeDriver: true }).start(({ finished }) => {
        if (finished) setShown(null);
      });
    }
  }, [entry, progress]);

  const reviewable = shown !== null && shown.court.status === 'candidate' && shown.kind !== 'mine';
  const counts = useReviewCounts(reviewable ? shown.court.id : null, shown?.kind);

  if (!shown) return null;
  const { court, kind } = shown;
  const site = siteLabel(court.site);
  const translateY = progress.interpolate({ inputRange: [0, 1], outputRange: [cardHeight + 24, 0] });
  // The body scrolls once check-ins and runs make the card taller than most of the screen.
  const bodyMaxHeight = Math.max(240, windowHeight * 0.78 - insets.top - 96);

  const confirmDelete = () =>
    Alert.alert('Delete this court?', 'It will be removed from this phone and from the courts you sent in.', [
      { text: 'Cancel', style: 'cancel' },
      { text: 'Delete', style: 'destructive', onPress: () => onDelete(court.id) },
    ]);

  return (
    <Animated.View
      onLayout={(event) => setCardHeight(event.nativeEvent.layout.height)}
      style={[
        styles.card,
        {
          backgroundColor: theme.surface,
          paddingBottom: insets.bottom + 16,
          paddingLeft: 20 + insets.left,
          paddingRight: 20 + insets.right,
          transform: [{ translateY }],
        },
      ]}
    >
      <View style={[styles.grabber, { backgroundColor: theme.border }]} />

      <View style={styles.header}>
        <View style={styles.headerGlyph}>
          <MarkerGlyph kind={kind} size={20} />
        </View>
        <View style={styles.headerText}>
          <Text style={[styles.title, { color: theme.text }]} numberOfLines={2}>
            {court.name ?? 'Unnamed court'}
          </Text>
          <Text style={[styles.subtitle, { color: theme.textMuted }]}>{sourceLabel(court.source, kind)}</Text>
        </View>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Close"
          onPress={onClose}
          hitSlop={12}
          style={[styles.close, { backgroundColor: theme.surfaceMuted }]}
        >
          <Ionicons name="close" size={20} color={theme.text} />
        </Pressable>
      </View>

      <ScrollView style={{ maxHeight: bodyMaxHeight }} contentContainerStyle={styles.body} showsVerticalScrollIndicator={false}>
        {kind !== 'verified' && kind !== 'mine' ? (
          <StatusLine
            theme={theme}
            color={MARKER_COLORS[kind]}
            text={
              court.confidence !== null
                ? `${KIND_LABELS[kind]} · ${Math.round(court.confidence * 100)}% confidence`
                : KIND_LABELS[kind]
            }
          />
        ) : null}

        <View style={styles.stats}>
          <Stat theme={theme} label="Hoops" value={court.hoops !== null ? String(court.hoops) : 'Unknown'} />
          <Stat theme={theme} label="Surface" value={capitalize(court.surface)} />
          <Stat theme={theme} label="Lights" value={yesNo(court.lit)} />
        </View>

        {court.address ? <Detail theme={theme} icon="location-outline" text={court.address} /> : null}
        {site ? <Detail theme={theme} icon="business-outline" text={site} /> : null}
        {court.notes ? <Detail theme={theme} icon="document-text-outline" text={court.notes} /> : null}
        {counts ? <Detail theme={theme} icon="people-outline" text={countsText(counts)} /> : null}

        {kind === 'candidate' ? (
          <>
            <Text style={[styles.question, { color: theme.text }]}>Is this a basketball court?</Text>
            <View style={styles.actions}>
              <Button label="It's a court" icon="checkmark" color={theme.success} onPress={() => onReview(court.id, 'confirmed')} />
              <Button label="Not a court" icon="close" variant="danger" onPress={() => onReview(court.id, 'rejected')} />
            </View>
            <View style={styles.actions}>
              <Button label="Someone's home court" icon="home-outline" variant="secondary" onPress={() => onReview(court.id, 'home')} />
            </View>
          </>
        ) : null}

        <View style={styles.actions}>
          <Button label="Directions" icon="navigate" variant={kind === 'candidate' ? 'secondary' : 'primary'} onPress={() => openDirections(court.coordinate)} />
          {kind === 'confirmed' || kind === 'rejected' || kind === 'home' ? (
            <Button label="Undo review" icon="arrow-undo" variant="secondary" onPress={() => onReview(court.id, null)} />
          ) : null}
          {kind === 'mine' ? <Button label="Delete" icon="trash-outline" variant="danger" onPress={confirmDelete} /> : null}
        </View>

        {/* Check-ins and runs; not at a candidate you said isn't a public court. */}
        {kind !== 'rejected' && kind !== 'home' ? <CourtSocial key={court.id} court={court} /> : null}
      </ScrollView>
    </Animated.View>
  );
}

function StatusLine({ theme, color, text }: { theme: Theme; color: string; text: string }) {
  return (
    <View style={[styles.status, { backgroundColor: theme.surfaceMuted }]}>
      <View style={[styles.statusDot, { backgroundColor: color }]} />
      <Text style={[styles.statusText, { color: theme.text }]}>{text}</Text>
    </View>
  );
}

function Stat({ theme, label, value }: { theme: Theme; label: string; value: string }) {
  return (
    <View style={[styles.stat, { backgroundColor: theme.surfaceMuted }]}>
      <Text style={[styles.statLabel, { color: theme.textMuted }]}>{label.toUpperCase()}</Text>
      <Text style={[styles.statValue, { color: theme.text }]} numberOfLines={1} adjustsFontSizeToFit>
        {value}
      </Text>
    </View>
  );
}

type DetailIcon = 'location-outline' | 'document-text-outline' | 'business-outline' | 'people-outline';

function Detail({ theme, icon, text }: { theme: Theme; icon: DetailIcon; text: string }) {
  return (
    <View style={styles.detail}>
      <Ionicons name={icon} size={18} color={theme.textMuted} />
      <Text style={[styles.detailText, { color: theme.text }]} numberOfLines={4}>
        {text}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    position: 'absolute',
    left: 0,
    right: 0,
    bottom: 0,
    paddingTop: 8,
    borderTopLeftRadius: 22,
    borderTopRightRadius: 22,
    gap: 14,
    boxShadow: '0px -4px 16px rgba(0, 0, 0, 0.18)',
  },
  body: {
    gap: 14,
    paddingBottom: 2,
  },
  grabber: {
    alignSelf: 'center',
    width: 40,
    height: 5,
    borderRadius: 3,
  },
  header: {
    flexDirection: 'row',
    alignItems: 'flex-start',
    gap: 12,
  },
  headerGlyph: {
    paddingTop: 4,
  },
  headerText: {
    flex: 1,
    gap: 2,
  },
  title: {
    fontSize: 20,
    fontWeight: '800',
  },
  subtitle: {
    fontSize: 14,
    fontWeight: '600',
  },
  close: {
    width: 32,
    height: 32,
    borderRadius: 16,
    alignItems: 'center',
    justifyContent: 'center',
  },
  status: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
    paddingHorizontal: 12,
    paddingVertical: 8,
    borderRadius: 10,
  },
  statusDot: {
    width: 10,
    height: 10,
    borderRadius: 5,
  },
  statusText: {
    flexShrink: 1,
    fontSize: 14,
    fontWeight: '700',
  },
  stats: {
    flexDirection: 'row',
    gap: 8,
  },
  stat: {
    flex: 1,
    borderRadius: 12,
    paddingVertical: 10,
    paddingHorizontal: 10,
    gap: 2,
  },
  statLabel: {
    fontSize: 11,
    fontWeight: '800',
    letterSpacing: 0.6,
  },
  statValue: {
    fontSize: 17,
    fontWeight: '800',
  },
  detail: {
    flexDirection: 'row',
    alignItems: 'flex-start',
    gap: 8,
  },
  detailText: {
    flex: 1,
    fontSize: 15,
    lineHeight: 20,
  },
  question: {
    fontSize: 15,
    fontWeight: '700',
    marginBottom: -4,
  },
  actions: {
    flexDirection: 'row',
    gap: 10,
  },
});
