import { useEffect, useState } from 'react';
import { ActivityIndicator, Alert, Pressable, StyleSheet, Text, View } from 'react-native';

import { errorText, stateName } from '../../social/labels';
import { fetchFriendsBoard, fetchStateBoard, setHomeState } from '../../social/api';
import { useSocial } from '../../social/SocialProvider';
import type { LeaderEntry, StateBoard } from '../../social/types';
import { type Theme, useTheme } from '../../theme';
import { PlayerSheet } from './PlayerSheet';
import { StatePicker } from './StatePicker';
import { Note, OptionChip, SmallButton } from './ui';

type Scope = 'friends' | 'state';

/** Weekly leaderboards: XP since Monday, for the people you follow or for your state. */
export function LeaderboardTab() {
  const theme = useTheme();
  const { profile, refresh, syncCount } = useSocial();
  const [scope, setScope] = useState<Scope>('friends');
  const [friends, setFriends] = useState<LeaderEntry[] | null>(null);
  const [board, setBoard] = useState<StateBoard | null>(null);
  const [failed, setFailed] = useState(false);
  const [reload, setReload] = useState(0);
  const [picking, setPicking] = useState(false);
  const [player, setPlayer] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setFailed(false);
    const load = scope === 'friends' ? fetchFriendsBoard().then((rows) => active && setFriends(rows)) : fetchStateBoard(null).then((next) => active && setBoard(next));
    load.catch(() => active && setFailed(true));
    return () => {
      active = false;
    };
  }, [scope, reload, syncCount]);

  const pickState = async (state: string) => {
    setPicking(false);
    try {
      await setHomeState(state);
      void refresh();
      setReload((n) => n + 1);
    } catch (error) {
      Alert.alert("That didn't work", errorText(error));
    }
  };

  const entries = scope === 'friends' ? friends : (board?.entries ?? null);
  const pinned = scope === 'state' && board?.me && !board.entries.some((e) => e.me) ? board.me : null;

  return (
    <>
      <View style={styles.tabs}>
        <OptionChip label="Friends" selected={scope === 'friends'} onPress={() => setScope('friends')} grow />
        <OptionChip label={profile?.homeState ? stateName(profile.homeState) ?? 'State' : 'State'} selected={scope === 'state'} onPress={() => setScope('state')} grow />
      </View>
      <Text style={[styles.week, { color: theme.textMuted }]}>XP this week (since Monday). A new race starts every Monday.</Text>

      {scope === 'state' ? (
        <View style={styles.stateRow}>
          <Text style={[styles.stateName, { color: theme.text }]}>{stateName(board?.state ?? profile?.homeState ?? null) ?? 'No state picked'}</Text>
          <SmallButton label={board?.state ? 'Change' : 'Pick your state'} icon="flag-outline" onPress={() => setPicking(true)} />
        </View>
      ) : null}

      {failed ? (
        <Note icon="cloud-offline-outline" text="Couldn't load the leaderboard. You may be offline." theme={theme} />
      ) : entries === null ? (
        <ActivityIndicator color={theme.accent} />
      ) : scope === 'state' && !board?.state ? (
        <Note icon="flag-outline" text="Pick your state to see who's leading it this week. Your first check-in sets it too." theme={theme} />
      ) : (
        <View style={[styles.list, { backgroundColor: theme.surface, borderColor: theme.border }]}>
          {entries.map((entry, index) => (
            <EntryRow key={entry.handle} entry={entry} theme={theme} first={index === 0} onPress={entry.me ? undefined : () => setPlayer(entry.handle)} />
          ))}
          {pinned ? (
            <EntryRow entry={{ rank: pinned.rank, handle: profile?.handle ?? 'You', xp: pinned.xp, me: true }} theme={theme} first={false} />
          ) : null}
          {entries.length === 0 && !pinned ? (
            <View style={styles.empty}>
              <Note icon="basketball-outline" text="No XP here yet this week. Check in at a court to get on the board." theme={theme} />
            </View>
          ) : null}
        </View>
      )}

      {scope === 'friends' && friends && friends.length <= 1 ? (
        <Note icon="person-add-outline" text="Follow friends with their friend code (on the You tab) to race them every week." theme={theme} />
      ) : null}

      {picking ? <StatePicker current={board?.state ?? profile?.homeState ?? null} onCancel={() => setPicking(false)} onPick={(s) => void pickState(s)} /> : null}
      {player ? <PlayerSheet handle={player} onClose={() => setPlayer(null)} onChanged={() => setReload((n) => n + 1)} /> : null}
    </>
  );
}

function EntryRow({ entry, theme, first, onPress }: { entry: LeaderEntry; theme: Theme; first: boolean; onPress?: () => void }) {
  return (
    <Pressable
      accessibilityRole={onPress ? 'button' : undefined}
      disabled={!onPress}
      onPress={onPress}
      style={({ pressed }) => [
        styles.entry,
        !first && { borderTopColor: theme.border, borderTopWidth: StyleSheet.hairlineWidth },
        entry.me && { backgroundColor: theme.surfaceMuted },
        pressed && styles.pressed,
      ]}
    >
      <View style={[styles.rank, { backgroundColor: entry.rank <= 3 ? theme.accent : theme.surfaceMuted }]}>
        <Text style={[styles.rankText, { color: entry.rank <= 3 ? theme.onAccent : theme.text }]}>{entry.rank}</Text>
      </View>
      <Text style={[styles.handle, { color: theme.text }]} numberOfLines={1}>
        {entry.handle}
        {entry.me ? ' (you)' : ''}
      </Text>
      <Text style={[styles.xp, { color: theme.text }]}>{entry.xp} XP</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  tabs: {
    flexDirection: 'row',
    gap: 8,
  },
  week: {
    fontSize: 13,
    lineHeight: 18,
    marginTop: -8,
  },
  stateRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 10,
  },
  stateName: {
    flexShrink: 1,
    fontSize: 18,
    fontWeight: '800',
  },
  list: {
    borderRadius: 14,
    borderWidth: 1,
    overflow: 'hidden',
  },
  entry: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 12,
    paddingHorizontal: 14,
    paddingVertical: 12,
  },
  rank: {
    minWidth: 30,
    height: 30,
    borderRadius: 15,
    paddingHorizontal: 6,
    alignItems: 'center',
    justifyContent: 'center',
  },
  rankText: {
    fontSize: 14,
    fontWeight: '800',
  },
  handle: {
    flex: 1,
    fontSize: 16,
    fontWeight: '700',
  },
  xp: {
    fontSize: 15,
    fontWeight: '800',
  },
  empty: {
    padding: 14,
  },
  pressed: {
    opacity: 0.7,
  },
});
