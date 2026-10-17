// URL / URLSearchParams for supabase-js on React Native. Must load before the client is created.
import 'react-native-url-polyfill/auto';

import AsyncStorage from '@react-native-async-storage/async-storage';
import { createClient, processLock } from '@supabase/supabase-js';
import { AppState, Platform } from 'react-native';

import { SUPABASE_PUBLISHABLE_KEY, SUPABASE_URL } from './config';

export const supabase = createClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, {
  auth: {
    // The anonymous session is kept on the phone so the same player id survives restarts.
    storage: AsyncStorage,
    autoRefreshToken: true,
    persistSession: true,
    detectSessionInUrl: false,
    lock: processLock,
  },
});

// Refresh the session token only while the app is in the foreground (Supabase's React Native advice).
if (Platform.OS !== 'web') {
  AppState.addEventListener('change', (state) => {
    if (state === 'active') supabase.auth.startAutoRefresh();
    else supabase.auth.stopAutoRefresh();
  });
}

let signingIn: Promise<string | null> | null = null;
let warnedSignIn = false;

/**
 * The player's id, signing in anonymously the first time. Returns null when offline or when the
 * project has anonymous sign-ins turned off; callers keep their work queued and try again later.
 */
export function getUserId(): Promise<string | null> {
  signingIn ??= (async () => {
    try {
      const { data } = await supabase.auth.getSession();
      if (data.session) return data.session.user.id;
      const { data: signedIn, error } = await supabase.auth.signInAnonymously();
      if (error) {
        if (!warnedSignIn) {
          warnedSignIn = true;
          console.warn(`Could not sign in to sync reviews (${error.code ?? error.status ?? ''}): ${error.message}`);
        }
        return null;
      }
      return signedIn.user?.id ?? null;
    } catch (error) {
      console.warn('Could not sign in to sync reviews', error);
      return null;
    } finally {
      signingIn = null;
    }
  })();
  return signingIn;
}
