import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, AppState, StyleSheet, View } from 'react-native';

import { AgeGate } from '../components/social/AgeGate';
import { XpToast } from '../components/social/XpToast';
import { loadSocialProfile, loadUnder13, saveSocialProfile, saveUnder13 } from '../storage';
import { confirmAge, fetchMyProfile } from './api';
import { errorText } from './labels';
import { type QueuedAction, queuedActions, startSocialQueue, subscribeQueue } from './queue';
import type { Award, Profile } from './types';

/**
 * loading: first answer pending. unavailable: no session or no connection (and nothing saved).
 * needsAge: signed in, the 13+ question not answered yet. under13: social stays off on this phone.
 * ready: profile loaded (possibly the saved copy while offline; see `stale`).
 */
export type SocialStatus = 'loading' | 'unavailable' | 'needsAge' | 'under13' | 'ready';

interface SocialValue {
  status: SocialStatus;
  profile: Profile | null;
  /** The profile shown is the copy saved on the phone (the server couldn't be reached). */
  stale: boolean;
  /** Social actions waiting for a connection. */
  pending: readonly QueuedAction[];
  /** Goes up whenever queued actions reach the server, so open screens can reload. */
  syncCount: number;
  refresh: () => Promise<SocialStatus>;
  /** True once social features can be used, asking the 13+ question first if needed. */
  ensure: () => Promise<boolean>;
  /** Shows what an action earned, and reloads the profile. */
  celebrate: (award: Award) => void;
}

const SocialContext = createContext<SocialValue | null>(null);

export function useSocial(): SocialValue {
  const value = useContext(SocialContext);
  if (!value) throw new Error('useSocial must be used inside SocialProvider');
  return value;
}

/** Foreground refreshes at most this often. */
const REFRESH_MIN_MS = 30_000;

function warn(what: string) {
  return (error: unknown) => console.warn(`Could not ${what}`, error);
}

function savedProfile(raw: unknown): Profile | null {
  if (typeof raw !== 'object' || raw === null) return null;
  const p = raw as Partial<Profile>;
  return typeof p.handle === 'string' && typeof p.friendCode === 'string' && Array.isArray(p.badges) ? (p as Profile) : null;
}

/**
 * Social state for the whole app: who you are (profile), whether social features are usable, the 13+
 * question and the "+10 XP" toast. Everything degrades softly: with no sign-in or no connection the
 * social parts of the app say they're unavailable and the map keeps working.
 */
export function SocialProvider({ children }: { children: ReactNode }) {
  const [status, setStatusState] = useState<SocialStatus>('loading');
  const [profile, setProfileState] = useState<Profile | null>(null);
  const [stale, setStale] = useState(false);
  const [pending, setPending] = useState<readonly QueuedAction[]>([]);
  const [syncCount, setSyncCount] = useState(0);
  const [gate, setGate] = useState<{ resolve: (ok: boolean) => void } | null>(null);
  const [gateBusy, setGateBusy] = useState(false);
  const [toast, setToast] = useState<Award | null>(null);
  // Refs mirror state so async flows see the latest value without waiting for a render.
  const statusRef = useRef<SocialStatus>('loading');
  const profileRef = useRef<Profile | null>(null);
  const refreshing = useRef<Promise<SocialStatus> | null>(null);
  const lastRefresh = useRef(0);

  const setStatus = useCallback((next: SocialStatus) => {
    statusRef.current = next;
    setStatusState(next);
  }, []);

  const setProfile = useCallback((next: Profile | null) => {
    profileRef.current = next;
    setProfileState(next);
    saveSocialProfile(next).catch(warn('save your profile'));
  }, []);

  const refresh = useCallback((): Promise<SocialStatus> => {
    if (statusRef.current === 'under13') return Promise.resolve('under13');
    refreshing.current ??= (async () => {
      lastRefresh.current = Date.now();
      try {
        const next = await fetchMyProfile();
        setStale(false);
        setProfile(next);
        setStatus(next ? 'ready' : 'needsAge');
      } catch {
        // Offline or signed out: keep showing the saved profile, if there is one.
        setStale(true);
        if (!profileRef.current) setStatus('unavailable');
      } finally {
        refreshing.current = null;
      }
      return statusRef.current;
    })();
    return refreshing.current;
  }, [setProfile, setStatus]);

  useEffect(() => {
    let active = true;
    startSocialQueue();
    setPending(queuedActions());
    const unsubscribe = subscribeQueue((actions, sent) => {
      setPending(actions);
      if (sent) {
        setSyncCount((count) => count + 1);
        void refresh();
      }
    });
    (async () => {
      if (await loadUnder13().catch(() => false)) {
        if (active) setStatus('under13');
        return;
      }
      const saved = savedProfile(await loadSocialProfile().catch(() => null));
      if (active && saved) {
        profileRef.current = saved;
        setProfileState(saved);
        setStale(true);
        setStatus('ready');
      }
      if (active) await refresh();
    })();
    const subscription = AppState.addEventListener('change', (state) => {
      if (state === 'active' && Date.now() - lastRefresh.current > REFRESH_MIN_MS) void refresh();
    });
    return () => {
      active = false;
      unsubscribe();
      subscription.remove();
    };
  }, [refresh, setStatus]);

  const ensure = useCallback(async (): Promise<boolean> => {
    let current = statusRef.current;
    if (current === 'loading' || current === 'unavailable') current = await refresh();
    if (current === 'ready') return true;
    if (current === 'under13') return false;
    if (current === 'needsAge') return new Promise<boolean>((resolve) => setGate({ resolve }));
    Alert.alert(
      'Social features are unavailable',
      'Check-ins, runs and leaderboards need a connection and sign-in. The map works as usual; try again later.',
    );
    return false;
  }, [refresh]);

  const answerGate = async (answer: 'adult' | 'under13' | 'later') => {
    if (!gate) return;
    if (answer === 'later') {
      setGate(null);
      gate.resolve(false);
      return;
    }
    if (answer === 'under13') {
      await saveUnder13().catch(warn('save your answer'));
      setStatus('under13');
      setProfile(null);
      setGate(null);
      gate.resolve(false);
      Alert.alert('Social features stay off', 'Check-ins, runs and leaderboards are off on this phone. Everything else works as usual.');
      return;
    }
    setGateBusy(true);
    try {
      const next = await confirmAge();
      setProfile(next);
      setStale(false);
      setStatus(next ? 'ready' : 'needsAge');
      setGate(null);
      gate.resolve(next !== null);
    } catch (error) {
      Alert.alert("Couldn't finish", errorText(error));
    } finally {
      setGateBusy(false);
    }
  };

  const celebrate = useCallback(
    (award: Award) => {
      if (award.xp > 0 || award.badges.length > 0) setToast(award);
      void refresh();
    },
    [refresh],
  );

  const value = useMemo<SocialValue>(
    () => ({ status, profile, stale, pending, syncCount, refresh, ensure, celebrate }),
    [status, profile, stale, pending, syncCount, refresh, ensure, celebrate],
  );

  return (
    <SocialContext.Provider value={value}>
      <View style={styles.root}>
        {children}
        <XpToast award={toast} onDone={() => setToast(null)} />
        <AgeGate visible={gate !== null} busy={gateBusy} onAnswer={answerGate} />
      </View>
    </SocialContext.Provider>
  );
}

const styles = StyleSheet.create({
  root: {
    flex: 1,
  },
});
