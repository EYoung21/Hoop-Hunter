import Ionicons from '@expo/vector-icons/Ionicons';
import { StyleSheet, Text, View } from 'react-native';

import { BADGE_INFO } from '../../social/labels';
import { BADGES, type BadgeId } from '../../social/types';
import { useTheme } from '../../theme';
import type { IconName } from '../Button';

interface Tile {
  label: string;
  value: string;
  icon: IconName;
  /** Small line under the value, e.g. "Check in today to keep it". */
  hint?: string;
}

/** A row of stat tiles in the court card's style. */
export function StatTiles({ tiles }: { tiles: Tile[] }) {
  const theme = useTheme();
  return (
    <View style={styles.tiles}>
      {tiles.map((tile) => (
        <View key={tile.label} style={[styles.tile, { backgroundColor: theme.surface, borderColor: theme.border }]}>
          <View style={styles.tileTop}>
            <Ionicons name={tile.icon} size={14} color={theme.accent} />
            <Text style={[styles.tileLabel, { color: theme.textMuted }]}>{tile.label.toUpperCase()}</Text>
          </View>
          <Text style={[styles.tileValue, { color: theme.text }]} numberOfLines={1} adjustsFontSizeToFit>
            {tile.value}
          </Text>
          {tile.hint ? <Text style={[styles.tileHint, { color: theme.textMuted }]}>{tile.hint}</Text> : null}
        </View>
      ))}
    </View>
  );
}

interface BadgeGridProps {
  earned: readonly BadgeId[];
  /** Hide the ones not earned yet (other players' cards). */
  onlyEarned?: boolean;
}

/** Badges: earned ones in colour, the rest muted with how to earn them. */
export function BadgeGrid({ earned, onlyEarned = false }: BadgeGridProps) {
  const theme = useTheme();
  const have = new Set(earned);
  const shown = onlyEarned ? BADGES.filter((b) => have.has(b)) : BADGES;
  return (
    <View style={styles.grid}>
      {shown.map((badge) => {
        const got = have.has(badge);
        const info = BADGE_INFO[badge];
        return (
          <View
            key={badge}
            accessibilityLabel={`${info.label}: ${got ? 'earned' : 'not earned yet'}. ${info.description}`}
            style={[styles.badge, { backgroundColor: theme.surface, borderColor: got ? theme.accent : theme.border }]}
          >
            <View style={[styles.badgeIcon, { backgroundColor: got ? theme.accent : theme.surfaceMuted }]}>
              <Ionicons name={got ? info.icon : 'lock-closed'} size={20} color={got ? theme.onAccent : theme.textMuted} />
            </View>
            <Text style={[styles.badgeLabel, { color: got ? theme.text : theme.textMuted }]} numberOfLines={1}>
              {info.label}
            </Text>
            <Text style={[styles.badgeText, { color: theme.textMuted }]} numberOfLines={3}>
              {info.description}
            </Text>
          </View>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  tiles: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: 8,
  },
  tile: {
    flexGrow: 1,
    flexBasis: '30%',
    borderRadius: 12,
    borderWidth: 1,
    paddingVertical: 10,
    paddingHorizontal: 10,
    gap: 2,
  },
  tileTop: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
  },
  tileLabel: {
    fontSize: 11,
    fontWeight: '800',
    letterSpacing: 0.6,
  },
  tileValue: {
    fontSize: 20,
    fontWeight: '800',
  },
  tileHint: {
    fontSize: 12,
    fontWeight: '600',
  },
  grid: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: 8,
  },
  badge: {
    width: '31%',
    flexGrow: 1,
    borderRadius: 14,
    borderWidth: 1.5,
    padding: 10,
    gap: 4,
    alignItems: 'center',
  },
  badgeIcon: {
    width: 40,
    height: 40,
    borderRadius: 20,
    alignItems: 'center',
    justifyContent: 'center',
  },
  badgeLabel: {
    fontSize: 13,
    fontWeight: '800',
    textAlign: 'center',
  },
  badgeText: {
    fontSize: 11,
    lineHeight: 14,
    textAlign: 'center',
  },
});
