import { useEffect, useState } from 'react';
import { ActivityIndicator, Alert, StyleSheet, Text, View } from 'react-native';

import { checkAtCourt } from '../../location';
import {
  cancelRun,
  checkIn,
  endCheckin,
  fetchCourtActivity,
  fetchCourtRuns,
  joinRun,
  leaveRun,
  postRun,
  reportRun,
} from '../../social/api';
import { errorText, FORMAT_LABELS, SKILL_LABELS } from '../../social/labels';
import { sendOrQueue } from '../../social/queue';
import { useSocial } from '../../social/SocialProvider';
import { formatTime, formatWhen, newId } from '../../social/time';
import type { Award, CourtActivity, ReportReason, Run } from '../../social/types';
import { type Theme, useTheme } from '../../theme';
import type { Court } from '../../types';
import { Button } from '../Button';
import { PlanSheet } from './PlanSheet';
import { PlayerSheet } from './PlayerSheet';
import { ReportSheet } from './ReportSheet';
import { type RunDraft, RunForm } from './RunForm';
import { confirmAction, Note, SectionLabel, SmallButton } from './ui';

const RUN_REASONS: readonly ReportReason[] = ['fake_run', 'spam', 'unsafe', 'harassment', 'other'];

function distanceText(meters: number): string {
  const miles = meters / 1609.344;
  return miles >= 0.2 ? `${miles.toFixed(miles >= 10 ? 0 : 1)} miles` : `${Math.round(meters)} m`;
}

function activityText({ here, planned }: CourtActivity): string {
  const coming = planned.reduce((sum, p) => sum + p.count, 0);
  const parts = [here > 0 ? `${here} here now` : 'Nobody checked in right now'];
  if (coming > 0) {
    const times = planned.slice(0, 3).map((p) => (p.count > 1 ? `${formatTime(p.at)} (${p.count})` : formatTime(p.at)));
    parts.push(`${coming} coming: ${times.join(', ')}${planned.length > 3 ? ', …' : ''}`);
  }
  return parts.join(' · ');
}

/**
 * Who's hooping at a court (check-ins) and its pickup runs, inside the court card. Reads fail softly
 * (offline: a note); check-ins and run actions made offline wait in the social queue.
 */
export function CourtSocial({ court }: { court: Court }) {
  const theme = useTheme();
  const { status, profile, ensure, celebrate, refresh, pending, syncCount } = useSocial();
  const courtId = court.id;
  const courtName = court.name ?? 'this court';
  const [activity, setActivity] = useState<CourtActivity | null>(null);
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);
  const [planning, setPlanning] = useState(false);
  const [posting, setPosting] = useState(false);
  const [reporting, setReporting] = useState<Run | null>(null);
  const [player, setPlayer] = useState<string | null>(null);

  // The card mounts this with key={court.id}, so a new court starts from empty state.
  useEffect(() => {
    if (status !== 'ready') return;
    let active = true;
    Promise.all([fetchCourtActivity(courtId), fetchCourtRuns(courtId)])
      .then(([nextActivity, nextRuns]) => {
        if (!active) return;
        setActivity(nextActivity);
        setRuns(nextRuns);
        setLoadFailed(false);
      })
      .catch(() => active && setLoadFailed(true));
    return () => {
      active = false;
    };
  }, [courtId, status, reload, syncCount]);

  if (status === 'under13' || status === 'loading') return null;
  if (status === 'unavailable') {
    return <Note icon="cloud-offline-outline" text="Check-ins and pickup runs are unavailable right now." theme={theme} />;
  }
  if (status === 'needsAge') {
    return (
      <View style={styles.section}>
        <SectionLabel text="Who's hooping" theme={theme} />
        <View style={styles.actions}>
          <Button label="See check-ins and runs" icon="people-outline" variant="secondary" onPress={() => void ensure()} />
        </View>
      </View>
    );
  }

  const done = (award?: Award) => {
    if (award) celebrate(award);
    else void refresh();
    setReload((n) => n + 1);
  };

  /** Runs a queueable action: sends it now, or tells the player it'll go up once they're back online. */
  const act = async <T,>(what: string, attempt: () => Promise<{ queued: true } | { queued: false; result: T }>, toAward?: (result: T) => Award) => {
    setBusy(what);
    try {
      const outcome = await attempt();
      if (outcome.queued) {
        Alert.alert('Saved for later', "You're offline. This will be sent when you're back online.");
        done();
      } else {
        done(toAward ? toAward(outcome.result) : undefined);
      }
    } catch (error) {
      Alert.alert("That didn't work", errorText(error));
    } finally {
      setBusy(null);
    }
  };

  const elsewhere = profile?.checkin && profile.checkin.courtId !== courtId ? (profile.checkin.courtName ?? 'another court') : null;

  const hereNow = async () => {
    if (!(await ensure())) return;
    if (elsewhere && !(await confirmAction('Check in here?', `This ends your check-in at ${elsewhere}.`, 'Check in'))) return;
    setBusy('here');
    const presence = await checkAtCourt(court.coordinate).catch(() => ({ ok: false as const, reason: 'unavailable' as const }));
    setBusy(null);
    if (!presence.ok) {
      if (presence.reason === 'far') {
        Alert.alert(
          "You're not at this court",
          `You're about ${distanceText(presence.meters)} away. "Here now" works within 250 m of the court. Use "Hooping at…" to say when you'll be there.`,
        );
      } else if (presence.reason === 'unavailable') {
        Alert.alert("Couldn't find you", 'Make sure location services are on, then try again.');
      }
      return;
    }
    await act(
      'here',
      () => sendOrQueue({ type: 'check_in', courtId, mode: 'here', startAt: null, at: Date.now() }, () => checkIn(courtId, 'here', null)),
      (award) => award,
    );
  };

  const openPlan = async () => {
    if (await ensure()) setPlanning(true);
  };

  const plan = (startAt: Date) => {
    setPlanning(false);
    const iso = startAt.toISOString();
    void act('plan', () => sendOrQueue({ type: 'check_in', courtId, mode: 'planned', startAt: iso, at: Date.now() }, () => checkIn(courtId, 'planned', iso)));
  };

  const checkOut = () => void act('out', () => sendOrQueue({ type: 'end_checkin', at: Date.now() }, endCheckin));

  const openPost = async () => {
    if (await ensure()) setPosting(true);
  };

  const post = (draft: RunDraft) => {
    setPosting(false);
    const run = { id: newId(), courtId, ...draft };
    void act('post', () => sendOrQueue({ type: 'post_run', run, at: Date.now() }, () => postRun(run)), (award) => award);
  };

  const join = (run: Run) =>
    void act(`run:${run.id}`, () => sendOrQueue({ type: 'join_run', runId: run.id, courtId, at: Date.now() }, () => joinRun(run.id)), (award) => award);

  const leave = (run: Run) =>
    void act(`run:${run.id}`, () => sendOrQueue({ type: 'leave_run', runId: run.id, courtId, at: Date.now() }, () => leaveRun(run.id)));

  const cancel = async (run: Run) => {
    if (!(await confirmAction('Cancel this run?', 'It disappears for everyone who is in.', 'Cancel run', true))) return;
    await act(`run:${run.id}`, () => sendOrQueue({ type: 'cancel_run', runId: run.id, courtId, at: Date.now() }, () => cancelRun(run.id)));
  };

  const report = async (reason: ReportReason) => {
    if (!reporting) return;
    setBusy('report');
    try {
      await reportRun(reporting.id, reason);
      setReporting(null);
      Alert.alert('Thanks for the report', 'A moderator will take a look.');
    } catch (error) {
      Alert.alert("That didn't work", errorText(error));
    } finally {
      setBusy(null);
    }
  };

  const mine = activity?.mine ?? null;
  const waiting = pending.filter((a) => (a.type === 'post_run' ? a.run.courtId : 'courtId' in a ? a.courtId : null) === courtId);

  return (
    <>
      <View style={styles.section}>
        <SectionLabel text="Who's hooping" theme={theme} />
        {activity ? (
          <Note icon="people-outline" text={activityText(activity)} theme={theme} color={theme.text} />
        ) : loadFailed ? (
          <Note icon="cloud-offline-outline" text="Couldn't load check-ins. You may be offline." theme={theme} />
        ) : (
          <ActivityIndicator color={theme.accent} style={styles.loading} />
        )}
        {activity && activity.friends.length > 0 ? (
          <Note
            icon="heart-outline"
            theme={theme}
            color={theme.text}
            text={`Friends: ${activity.friends.map((f) => `${f.handle} (${f.kind === 'here' ? 'here' : formatTime(f.startsAt)})`).join(', ')}`}
          />
        ) : null}
        {mine ? (
          <View style={[styles.mine, { backgroundColor: theme.surfaceMuted }]}>
            <Text style={[styles.mineText, { color: theme.text }]}>
              {mine.kind === 'here' ? `You're here until ${formatTime(mine.expiresAt)}` : `You're hooping here ${formatWhen(mine.startsAt)}`}
            </Text>
            <SmallButton label={mine.kind === 'here' ? 'Check out' : 'Cancel'} busy={busy === 'out'} onPress={checkOut} />
          </View>
        ) : null}
        {waiting.length > 0 ? <Note icon="time-outline" text="Waiting for a connection to send your changes here." theme={theme} /> : null}
        {busy === 'here' || busy === 'plan' ? (
          <ActivityIndicator color={theme.accent} style={styles.loading} />
        ) : (
          <View style={styles.actions}>
            <Button label="Here now" icon="location" onPress={() => void hereNow()} />
            <Button label="Hooping at…" icon="time-outline" variant="secondary" onPress={() => void openPlan()} />
          </View>
        )}
      </View>

      <View style={styles.section}>
        <View style={styles.sectionHeader}>
          <SectionLabel text="Pickup runs" theme={theme} />
          <SmallButton label="Post a run" icon="add" busy={busy === 'post'} onPress={() => void openPost()} />
        </View>
        {runs && runs.length === 0 ? <Note icon="basketball-outline" text="No runs posted here yet." theme={theme} /> : null}
        {(runs ?? []).map((run) => (
          <RunRow
            key={run.id}
            run={run}
            theme={theme}
            me={profile?.handle ?? null}
            busy={busy === `run:${run.id}`}
            onJoin={() => join(run)}
            onLeave={() => leave(run)}
            onCancel={() => void cancel(run)}
            onReport={() => setReporting(run)}
            onPlayer={setPlayer}
          />
        ))}
      </View>

      {planning ? <PlanSheet courtName={courtName} replaces={elsewhere} onCancel={() => setPlanning(false)} onSave={plan} /> : null}
      {posting ? <RunForm courtName={courtName} onCancel={() => setPosting(false)} onSave={post} /> : null}
      {reporting ? (
        <ReportSheet title="Report this run" reasons={RUN_REASONS} busy={busy === 'report'} onCancel={() => setReporting(null)} onSubmit={(reason) => void report(reason)} />
      ) : null}
      {player ? <PlayerSheet handle={player} onClose={() => setPlayer(null)} onChanged={() => setReload((n) => n + 1)} /> : null}
    </>
  );
}

interface RunRowProps {
  run: Run;
  theme: Theme;
  me: string | null;
  busy: boolean;
  onJoin: () => void;
  onLeave: () => void;
  onCancel: () => void;
  onReport: () => void;
  onPlayer: (handle: string) => void;
}

function RunRow({ run, theme, me, busy, onJoin, onLeave, onCancel, onReport, onPlayer }: RunRowProps) {
  const full = run.maxPlayers !== null && run.players >= run.maxPlayers;
  const others = run.players - run.names.length;
  return (
    <View style={[styles.run, { backgroundColor: theme.surfaceMuted }]}>
      <View style={styles.runTop}>
        <Text style={[styles.runTitle, { color: theme.text }]}>
          {FORMAT_LABELS[run.format]}
          {run.skill ? ` · ${SKILL_LABELS[run.skill]}` : ''}
        </Text>
        <Text style={[styles.runWhen, { color: theme.text }]}>{formatWhen(run.startsAt)}</Text>
      </View>
      <Text style={[styles.runPlayers, { color: theme.textMuted }]}>
        {run.players}
        {run.maxPlayers !== null ? `/${run.maxPlayers}` : ''} in
        {run.names.length > 0 ? ' · ' : ''}
        {run.names.map((name, i) => (
          <Text
            key={name}
            accessibilityRole={name === me ? undefined : 'link'}
            onPress={name === me ? undefined : () => onPlayer(name)}
            style={name === me ? styles.meName : [styles.name, { color: theme.text }]}
          >
            {i > 0 ? ', ' : ''}
            {name === me ? 'you' : name}
          </Text>
        ))}
        {others > 0 ? ` +${others}` : ''}
      </Text>
      <View style={styles.runActions}>
        {run.mine ? (
          <SmallButton label="Cancel run" icon="close" variant="danger" busy={busy} onPress={onCancel} />
        ) : run.joined ? (
          <SmallButton label="Leave" icon="exit-outline" busy={busy} onPress={onLeave} />
        ) : full ? (
          <Text style={[styles.full, { color: theme.textMuted }]}>Full</Text>
        ) : (
          <SmallButton label="I'm in" icon="checkmark" variant="primary" busy={busy} onPress={onJoin} />
        )}
        {run.mine ? null : <SmallButton label="" icon="flag-outline" accessibilityLabel="Report this run" onPress={onReport} />}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  section: {
    gap: 10,
  },
  sectionHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  actions: {
    flexDirection: 'row',
    gap: 10,
  },
  loading: {
    alignSelf: 'flex-start',
  },
  mine: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 10,
    paddingHorizontal: 12,
    paddingVertical: 8,
    borderRadius: 10,
  },
  mineText: {
    flexShrink: 1,
    fontSize: 14,
    fontWeight: '700',
  },
  run: {
    borderRadius: 12,
    padding: 12,
    gap: 6,
  },
  runTop: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'baseline',
    gap: 8,
  },
  runTitle: {
    flexShrink: 1,
    fontSize: 16,
    fontWeight: '800',
  },
  runWhen: {
    fontSize: 14,
    fontWeight: '700',
  },
  runPlayers: {
    fontSize: 14,
    lineHeight: 19,
  },
  name: {
    fontWeight: '700',
  },
  meName: {
    fontWeight: '700',
  },
  runActions: {
    flexDirection: 'row',
    justifyContent: 'flex-end',
    alignItems: 'center',
    gap: 8,
  },
  full: {
    fontSize: 14,
    fontWeight: '700',
  },
});
