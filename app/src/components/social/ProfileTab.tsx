import Ionicons from '@expo/vector-icons/Ionicons';
import { useEffect, useState } from 'react';
import { Alert, Pressable, Share, StyleSheet, Text, TextInput, View } from 'react-native';

import {
  fetchFollows,
  fetchMyRuns,
  followByCode,
  resetFriendCode,
  setHandle,
  setHomeState,
  unblockPlayer,
  unfollow,
} from '../../social/api';
import { errorText, FORMAT_LABELS, stateName } from '../../social/labels';
import { useSocial } from '../../social/SocialProvider';
import { formatTime, formatWhen } from '../../social/time';
import type { FollowLists, Run } from '../../social/types';
import { type Theme, useTheme } from '../../theme';
import type { LatLng } from '../../types';
import { PlayerSheet } from './PlayerSheet';
import { BadgeGrid, StatTiles } from './Stats';
import { StatePicker } from './StatePicker';
import { confirmAction, Note, SectionLabel, SmallButton } from './ui';

const HANDLE_PATTERN = /^[A-Za-z0-9_]{3,20}$/;

const FOLLOW_MESSAGES = {
  not_found: "No player has that code. Check it and try again.",
  own_code: "That's your own code.",
  rate_limited: 'Too many tries. Wait a while, then try again.',
  too_many: "You're following as many players as you can.",
} as const;

interface Props {
  /** Close the screen and show a court on the map. */
  onOpenCourt: (courtId: string, coordinate: LatLng | null) => void;
}

/** "You": handle, streak, XP, badges, friend code, follows and blocks. */
export function ProfileTab({ onOpenCourt }: Props) {
  const theme = useTheme();
  const { profile, stale, refresh, syncCount } = useSocial();
  const [lists, setLists] = useState<FollowLists | null>(null);
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [code, setCode] = useState('');
  const [player, setPlayer] = useState<string | null>(null);
  const [pickingState, setPickingState] = useState(false);

  useEffect(() => {
    let active = true;
    void refresh();
    Promise.all([fetchFollows(), fetchMyRuns()])
      .then(([nextLists, nextRuns]) => {
        if (!active) return;
        setLists(nextLists);
        setRuns(nextRuns);
        setLoadFailed(false);
      })
      .catch(() => active && setLoadFailed(true));
    return () => {
      active = false;
    };
  }, [refresh, reload, syncCount]);

  if (!profile) return null;
  const again = () => setReload((n) => n + 1);

  const run = async (what: string, action: () => Promise<void>): Promise<boolean> => {
    setBusy(what);
    try {
      await action();
      return true;
    } catch (error) {
      Alert.alert("That didn't work", errorText(error));
      return false;
    } finally {
      setBusy(null);
    }
  };

  const saveHandle = async () => {
    const next = (draft ?? '').trim();
    if (!HANDLE_PATTERN.test(next)) {
      Alert.alert('Pick another handle', 'Use 3 to 20 letters, numbers or underscores.');
      return;
    }
    if (await run('handle', () => setHandle(next).then(() => undefined))) {
      setDraft(null);
      void refresh();
    }
  };

  const follow = async () => {
    const value = code.trim();
    if (!value) return;
    setBusy('follow');
    try {
      const result = await followByCode(value);
      if (result.status === 'followed' || result.status === 'already') {
        setCode('');
        Alert.alert(
          result.status === 'followed' ? `Following ${result.handle}` : `You already follow ${result.handle}`,
          result.mutual
            ? "You follow each other, so you'll see each other at courts."
            : "You'll see them on your friends leaderboard. If they follow you back, you'll see each other at courts.",
        );
        again();
        void refresh();
      } else {
        Alert.alert("Couldn't follow", FOLLOW_MESSAGES[result.status]);
      }
    } catch (error) {
      Alert.alert("Couldn't follow", errorText(error));
    } finally {
      setBusy(null);
    }
  };

  const shareCode = () => {
    Share.share({ message: `Follow me on Hoop Hunter: my friend code is ${profile.friendCode}` }).catch(() => undefined);
  };

  const newCode = async () => {
    const ok = await confirmAction(
      'Get a new friend code?',
      'Your old code stops working. People who already follow you keep following you (block anyone you want gone).',
      'New code',
    );
    if (ok && (await run('code', () => resetFriendCode().then(() => undefined)))) void refresh();
  };

  const unfollowPlayer = async (handle: string) => {
    if (await run(`p:${handle}`, () => unfollow(handle))) {
      again();
      void refresh();
    }
  };

  const unblock = async (handle: string) => {
    if (await run(`p:${handle}`, () => unblockPlayer(handle))) again();
  };

  const pickState = async (state: string) => {
    setPickingState(false);
    if (await run('state', () => setHomeState(state))) void refresh();
  };

  const streakHint = profile.streak > 0 && !profile.streakToday ? 'Check in today to keep it' : profile.bestStreak > 0 ? `Best ${profile.bestStreak}` : undefined;
  const checkin = profile.checkin;

  return (
    <>
      {stale ? <Note icon="cloud-offline-outline" text="Offline: showing your saved profile." theme={theme} /> : null}

      <View style={styles.header}>
        <View style={[styles.avatar, { backgroundColor: theme.accent }]}>
          <Text style={[styles.avatarText, { color: theme.onAccent }]}>{profile.handle.charAt(0).toUpperCase()}</Text>
        </View>
        {draft === null ? (
          <View style={styles.headerText}>
            <Text style={[styles.handle, { color: theme.text }]} numberOfLines={1}>
              {profile.handle}
            </Text>
            <Pressable accessibilityRole="button" onPress={() => setDraft(profile.handle)} hitSlop={8}>
              <Text style={[styles.link, { color: theme.accent }]}>Change handle</Text>
            </Pressable>
          </View>
        ) : (
          <View style={styles.headerText}>
            <TextInput
              value={draft}
              onChangeText={setDraft}
              autoCapitalize="none"
              autoCorrect={false}
              maxLength={20}
              placeholder="Your handle"
              placeholderTextColor={theme.textMuted}
              keyboardAppearance={theme.dark ? 'dark' : 'light'}
              returnKeyType="done"
              onSubmitEditing={() => void saveHandle()}
              style={[styles.input, { color: theme.text, backgroundColor: theme.surface, borderColor: theme.border }]}
            />
            <View style={styles.row}>
              <SmallButton label="Save" variant="primary" busy={busy === 'handle'} onPress={() => void saveHandle()} />
              <SmallButton label="Cancel" onPress={() => setDraft(null)} />
            </View>
          </View>
        )}
      </View>

      <StatTiles
        tiles={[
          { label: 'Streak', value: `${profile.streak} ${profile.streak === 1 ? 'day' : 'days'}`, icon: 'flame', hint: streakHint },
          { label: 'This week', value: `${profile.xpWeek} XP`, icon: 'trending-up' },
          { label: 'Total', value: `${profile.xpTotal} XP`, icon: 'star' },
          { label: 'Courts', value: profile.courtsVisited >= 10 ? '10+' : `${profile.courtsVisited}`, icon: 'location' },
        ]}
      />

      {checkin || (runs && runs.length > 0) ? (
        <View style={styles.field}>
          <SectionLabel text="Up next" theme={theme} />
          {checkin ? (
            <Row
              theme={theme}
              icon={checkin.kind === 'here' ? 'location' : 'time-outline'}
              title={checkin.courtName ?? 'A court'}
              subtitle={checkin.kind === 'here' ? `Here until ${formatTime(checkin.expiresAt)}` : `Hooping ${formatWhen(checkin.startsAt)}`}
              onPress={() => onOpenCourt(checkin.courtId, null)}
            />
          ) : null}
          {(runs ?? []).map((r) => (
            <Row
              key={r.id}
              theme={theme}
              icon="basketball"
              title={`${FORMAT_LABELS[r.format]} at ${r.courtName ?? 'a court'}`}
              subtitle={`${formatWhen(r.startsAt)} · ${r.players}${r.maxPlayers !== null ? `/${r.maxPlayers}` : ''} in${r.mine ? ' · you posted it' : ''}`}
              onPress={() => onOpenCourt(r.courtId, r.coordinate ?? null)}
            />
          ))}
        </View>
      ) : null}

      <View style={styles.field}>
        <SectionLabel text={`Badges (${profile.badges.length} of 6)`} theme={theme} />
        <BadgeGrid earned={profile.badges.map((b) => b.badge)} />
      </View>

      <View style={styles.field}>
        <SectionLabel text="Your friend code" theme={theme} />
        <View style={[styles.codeCard, { backgroundColor: theme.surface, borderColor: theme.border }]}>
          <Text selectable style={[styles.code, { color: theme.text }]} accessibilityLabel={`Friend code ${profile.friendCode.split('').join(' ')}`}>
            {profile.friendCode.slice(0, 3)} {profile.friendCode.slice(3)}
          </Text>
          <View style={styles.row}>
            <SmallButton label="Share" icon="share-outline" onPress={shareCode} />
            <SmallButton label="New code" icon="refresh" busy={busy === 'code'} onPress={() => void newCode()} />
          </View>
        </View>
        <Text style={[styles.footnote, { color: theme.textMuted }]}>
          Friends follow you by entering this code. There's no search: only people you give it to can follow you.
        </Text>
      </View>

      <View style={styles.field}>
        <SectionLabel text="Follow a friend" theme={theme} />
        <View style={styles.row}>
          <TextInput
            value={code}
            onChangeText={(value) => setCode(value.toUpperCase())}
            autoCapitalize="characters"
            autoCorrect={false}
            maxLength={9}
            placeholder="Their code, e.g. K7Q 4ZP"
            placeholderTextColor={theme.textMuted}
            keyboardAppearance={theme.dark ? 'dark' : 'light'}
            returnKeyType="done"
            onSubmitEditing={() => void follow()}
            style={[styles.input, styles.codeInput, { color: theme.text, backgroundColor: theme.surface, borderColor: theme.border }]}
          />
          <SmallButton label="Follow" icon="person-add-outline" variant="primary" busy={busy === 'follow'} onPress={() => void follow()} />
        </View>
      </View>

      {loadFailed ? <Note icon="cloud-offline-outline" text="Couldn't load your friends. You may be offline." theme={theme} /> : null}
      {lists ? (
        <>
          <People
            theme={theme}
            title={`Following (${lists.following.length})`}
            empty="You're not following anyone yet."
            people={lists.following.map((p) => ({ handle: p.handle, tag: p.mutual ? 'Friends' : null }))}
            actionLabel="Unfollow"
            busy={busy}
            onAction={(handle) => void unfollowPlayer(handle)}
            onOpen={setPlayer}
          />
          <People
            theme={theme}
            title={`Followers (${lists.followers.length})`}
            empty="Nobody follows you yet. Share your code."
            people={lists.followers.map((p) => ({ handle: p.handle, tag: p.mutual ? 'Friends' : null }))}
            onOpen={setPlayer}
          />
          {lists.blocked.length > 0 ? (
            <People
              theme={theme}
              title={`Blocked (${lists.blocked.length})`}
              empty=""
              people={lists.blocked.map((handle) => ({ handle, tag: null }))}
              actionLabel="Unblock"
              busy={busy}
              onAction={(handle) => void unblock(handle)}
            />
          ) : null}
        </>
      ) : null}

      <View style={styles.field}>
        <SectionLabel text="Your state" theme={theme} />
        <Row
          theme={theme}
          icon="flag-outline"
          title={stateName(profile.homeState) ?? 'Not set'}
          subtitle="Which state leaderboard you're on"
          onPress={() => setPickingState(true)}
        />
      </View>

      <Text style={[styles.footnote, { color: theme.textMuted }]}>
        Other players see your handle, XP, streak and badges. Check-ins show as counts; only friends who follow each other
        see names. Your location is checked on your phone when you tap "Here now" and is never sent.
      </Text>

      {player ? <PlayerSheet handle={player} onClose={() => setPlayer(null)} onChanged={again} /> : null}
      {pickingState ? <StatePicker current={profile.homeState} onCancel={() => setPickingState(false)} onPick={(s) => void pickState(s)} /> : null}
    </>
  );
}

interface RowProps {
  theme: Theme;
  icon: 'location' | 'time-outline' | 'basketball' | 'flag-outline';
  title: string;
  subtitle: string;
  onPress: () => void;
}

function Row({ theme, icon, title, subtitle, onPress }: RowProps) {
  return (
    <Pressable
      accessibilityRole="button"
      onPress={onPress}
      style={({ pressed }) => [styles.rowCard, { backgroundColor: theme.surface, borderColor: theme.border }, pressed && styles.pressed]}
    >
      <Ionicons name={icon} size={20} color={theme.accent} />
      <View style={styles.rowText}>
        <Text style={[styles.rowTitle, { color: theme.text }]} numberOfLines={1}>
          {title}
        </Text>
        <Text style={[styles.rowSubtitle, { color: theme.textMuted }]} numberOfLines={1}>
          {subtitle}
        </Text>
      </View>
      <Ionicons name="chevron-forward" size={18} color={theme.textMuted} />
    </Pressable>
  );
}

interface PeopleProps {
  theme: Theme;
  title: string;
  empty: string;
  people: { handle: string; tag: string | null }[];
  actionLabel?: string;
  busy?: string | null;
  onAction?: (handle: string) => void;
  onOpen?: (handle: string) => void;
}

function People({ theme, title, empty, people, actionLabel, busy, onAction, onOpen }: PeopleProps) {
  return (
    <View style={styles.field}>
      <SectionLabel text={title} theme={theme} />
      {people.length === 0 ? <Note icon="people-outline" text={empty} theme={theme} /> : null}
      {people.map(({ handle, tag }) => (
        <View key={handle} style={[styles.person, { borderBottomColor: theme.border }]}>
          <Pressable accessibilityRole="button" disabled={!onOpen} onPress={() => onOpen?.(handle)} style={styles.personName}>
            <Text style={[styles.personHandle, { color: theme.text }]} numberOfLines={1}>
              {handle}
            </Text>
            {tag ? <Text style={[styles.personTag, { color: theme.accent }]}>{tag}</Text> : null}
          </Pressable>
          {actionLabel && onAction ? <SmallButton label={actionLabel} busy={busy === `p:${handle}`} onPress={() => onAction(handle)} /> : null}
        </View>
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 14,
  },
  avatar: {
    width: 60,
    height: 60,
    borderRadius: 30,
    alignItems: 'center',
    justifyContent: 'center',
  },
  avatarText: {
    fontSize: 26,
    fontWeight: '800',
  },
  headerText: {
    flex: 1,
    gap: 6,
  },
  handle: {
    fontSize: 24,
    fontWeight: '800',
  },
  link: {
    fontSize: 14,
    fontWeight: '700',
  },
  input: {
    borderWidth: 1,
    borderRadius: 12,
    paddingHorizontal: 14,
    paddingVertical: 10,
    fontSize: 16,
  },
  codeInput: {
    flex: 1,
    letterSpacing: 1,
  },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
  },
  field: {
    gap: 8,
  },
  codeCard: {
    borderWidth: 1,
    borderRadius: 14,
    padding: 14,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 10,
  },
  code: {
    fontSize: 26,
    fontWeight: '800',
    letterSpacing: 3,
  },
  footnote: {
    fontSize: 13,
    lineHeight: 18,
  },
  rowCard: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 12,
    borderWidth: 1,
    borderRadius: 12,
    paddingHorizontal: 12,
    paddingVertical: 10,
  },
  rowText: {
    flex: 1,
    gap: 2,
  },
  rowTitle: {
    fontSize: 15,
    fontWeight: '700',
  },
  rowSubtitle: {
    fontSize: 13,
  },
  pressed: {
    opacity: 0.7,
  },
  person: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 10,
    paddingVertical: 8,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  personName: {
    flex: 1,
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
  },
  personHandle: {
    flexShrink: 1,
    fontSize: 16,
    fontWeight: '700',
  },
  personTag: {
    fontSize: 12,
    fontWeight: '800',
  },
});
