import { useEffect, useState } from 'react';
import { ActivityIndicator, Alert, StyleSheet, Text, View } from 'react-native';

import { blockPlayer, fetchPublicProfile, reportPlayer, unfollow } from '../../social/api';
import { BADGE_INFO, errorText } from '../../social/labels';
import type { PublicProfile, ReportReason } from '../../social/types';
import { useTheme } from '../../theme';
import { BadgeGrid, StatTiles } from './Stats';
import { ReportSheet } from './ReportSheet';
import { confirmAction, Note, SectionLabel, Sheet, SmallButton } from './ui';

const PLAYER_REASONS: readonly ReportReason[] = ['offensive_name', 'harassment', 'spam', 'unsafe', 'other'];

interface Props {
  handle: string;
  onClose: () => void;
  /** Called after an unfollow or a block, so lists can reload. */
  onChanged?: () => void;
}

/** Another player's public card (handle, streak, XP, badges) with unfollow, block and report. */
export function PlayerSheet({ handle, onClose, onChanged }: Props) {
  const theme = useTheme();
  const [player, setPlayer] = useState<PublicProfile | null | undefined>(undefined);
  const [busy, setBusy] = useState<string | null>(null);
  const [reporting, setReporting] = useState(false);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let active = true;
    fetchPublicProfile(handle)
      .then((value) => active && setPlayer(value))
      .catch(() => active && setPlayer(null));
    return () => {
      active = false;
    };
  }, [handle, reload]);

  const run = async (what: string, action: () => Promise<void>) => {
    setBusy(what);
    try {
      await action();
      onChanged?.();
      return true;
    } catch (error) {
      Alert.alert("That didn't work", errorText(error));
      return false;
    } finally {
      setBusy(null);
    }
  };

  const handleUnfollow = async () => {
    if (await run('unfollow', () => unfollow(handle))) setReload((n) => n + 1);
  };

  const handleBlock = async () => {
    const ok = await confirmAction(
      `Block ${handle}?`,
      "You won't see each other's check-ins, runs or leaderboard spots, and neither of you will follow the other. They aren't told.",
      'Block',
      true,
    );
    if (ok && (await run('block', () => blockPlayer(handle)))) {
      Alert.alert('Blocked', `You won't see ${handle} anymore. You can unblock them from your profile.`);
      onClose();
    }
  };

  const handleReport = async (reason: ReportReason) => {
    if (await run('report', () => reportPlayer(handle, reason))) {
      setReporting(false);
      Alert.alert('Thanks for the report', 'A moderator will take a look. You can also block this player.');
    }
  };

  return (
    <Sheet title="Player" onClose={onClose} closeLabel="Close">
      {player === undefined ? (
        <ActivityIndicator color={theme.accent} />
      ) : player === null ? (
        <Note icon="cloud-offline-outline" text="This player isn't available. You may be offline." theme={theme} />
      ) : (
        <>
          <View style={styles.header}>
            <View style={[styles.avatar, { backgroundColor: theme.accent }]}>
              <Text style={[styles.avatarText, { color: theme.onAccent }]}>{player.handle.charAt(0).toUpperCase()}</Text>
            </View>
            <View style={styles.headerText}>
              <Text style={[styles.handle, { color: theme.text }]}>{player.handle}</Text>
              <Text style={[styles.tag, { color: theme.textMuted }]}>
                {player.me
                  ? 'You'
                  : player.youFollow && player.followsYou
                    ? 'Friends: you follow each other'
                    : player.youFollow
                      ? 'You follow them'
                      : player.followsYou
                        ? 'Follows you'
                        : 'Player'}
              </Text>
            </View>
          </View>
          <StatTiles
            tiles={[
              { label: 'Streak', value: `${player.streak}`, icon: 'flame' },
              { label: 'This week', value: `${player.xpWeek} XP`, icon: 'trending-up' },
              { label: 'Total', value: `${player.xpTotal} XP`, icon: 'star' },
            ]}
          />
          <View style={styles.field}>
            <SectionLabel text={`Badges (${player.badges.length})`} theme={theme} />
            {player.badges.length > 0 ? (
              <BadgeGrid earned={player.badges} onlyEarned />
            ) : (
              <Note icon="ribbon-outline" text="No badges yet." theme={theme} />
            )}
          </View>
          {player.me ? null : (
            <View style={styles.actions}>
              {player.youFollow ? <SmallButton label="Unfollow" icon="person-remove-outline" busy={busy === 'unfollow'} onPress={handleUnfollow} /> : null}
              <SmallButton label="Block" icon="hand-left-outline" variant="danger" busy={busy === 'block'} onPress={handleBlock} />
              <SmallButton label="Report" icon="flag-outline" onPress={() => setReporting(true)} />
            </View>
          )}
          <Text style={[styles.footnote, { color: theme.textMuted }]}>
            {player.youFollow ? '' : 'To follow someone, ask for their friend code. '}
            Profiles show a handle, XP, streak and badges ({Object.keys(BADGE_INFO).length} to earn), nothing else.
          </Text>
        </>
      )}
      {reporting ? (
        <ReportSheet
          title={`Report ${handle}`}
          reasons={PLAYER_REASONS}
          busy={busy === 'report'}
          onCancel={() => setReporting(false)}
          onSubmit={handleReport}
        />
      ) : null}
    </Sheet>
  );
}

const styles = StyleSheet.create({
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 14,
  },
  avatar: {
    width: 56,
    height: 56,
    borderRadius: 28,
    alignItems: 'center',
    justifyContent: 'center',
  },
  avatarText: {
    fontSize: 24,
    fontWeight: '800',
  },
  headerText: {
    flex: 1,
    gap: 2,
  },
  handle: {
    fontSize: 22,
    fontWeight: '800',
  },
  tag: {
    fontSize: 14,
    fontWeight: '600',
  },
  field: {
    gap: 8,
  },
  actions: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: 8,
  },
  footnote: {
    fontSize: 13,
    lineHeight: 18,
  },
});
