import { useState } from 'react';
import { ActivityIndicator, StyleSheet, Text, View } from 'react-native';

import { useSocial } from '../../social/SocialProvider';
import { useTheme } from '../../theme';
import type { LatLng } from '../../types';
import { Button } from '../Button';
import { LeaderboardTab } from './LeaderboardTab';
import { ProfileTab } from './ProfileTab';
import { Note, OptionChip, Sheet } from './ui';

type Tab = 'you' | 'leaderboard';

interface Props {
  onClose: () => void;
  onOpenCourt: (courtId: string, coordinate: LatLng | null) => void;
}

/** The social screen: "You" (profile, badges, friends) and weekly leaderboards. */
export function SocialScreen({ onClose, onOpenCourt }: Props) {
  const theme = useTheme();
  const { status, refresh, ensure } = useSocial();
  const [tab, setTab] = useState<Tab>('you');
  const [retrying, setRetrying] = useState(false);

  const retry = async () => {
    setRetrying(true);
    await refresh();
    setRetrying(false);
  };

  const tabs = (
    <View style={styles.tabs}>
      <OptionChip label="You" selected={tab === 'you'} onPress={() => setTab('you')} />
      <OptionChip label="Leaderboard" selected={tab === 'leaderboard'} onPress={() => setTab('leaderboard')} />
    </View>
  );

  let body;
  if (status === 'ready') {
    body = tab === 'you' ? <ProfileTab onOpenCourt={onOpenCourt} /> : <LeaderboardTab />;
  } else if (status === 'under13') {
    body = (
      <Note
        icon="lock-closed-outline"
        text="Social features are off on this phone. Check-ins, runs and leaderboards are for players 13 and older. The map, reviews and adding courts work as usual."
        theme={theme}
      />
    );
  } else if (status === 'needsAge') {
    body = (
      <>
        <Text style={[styles.title, { color: theme.text }]}>Hoop with friends</Text>
        <Text style={[styles.body, { color: theme.textMuted }]}>
          Check in at courts, post pickup runs, keep a streak going, earn XP and badges, and race your friends and your
          state on weekly leaderboards. Friends follow each other with a code: there's no search and no chat.
        </Text>
        <View style={styles.row}>
          <Button label="Get started" icon="arrow-forward" onPress={() => void ensure()} />
        </View>
      </>
    );
  } else if (status === 'loading' || retrying) {
    body = <ActivityIndicator color={theme.accent} />;
  } else {
    body = (
      <>
        <Note
          icon="cloud-offline-outline"
          text="Social features are unavailable right now. They need a connection and sign-in. The map keeps working."
          theme={theme}
        />
        <View style={styles.row}>
          <Button label="Try again" icon="refresh" variant="secondary" onPress={() => void retry()} />
        </View>
      </>
    );
  }

  return (
    <Sheet title="You" onClose={onClose} closeLabel="Done" header={status === 'ready' ? tabs : undefined}>
      {body}
    </Sheet>
  );
}

const styles = StyleSheet.create({
  tabs: {
    flexDirection: 'row',
    gap: 6,
  },
  title: {
    fontSize: 22,
    fontWeight: '800',
  },
  body: {
    fontSize: 15,
    lineHeight: 21,
  },
  row: {
    flexDirection: 'row',
  },
});
