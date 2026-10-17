/**
 * Supabase project the app reads courts from and syncs reviews to.
 *
 * Only the project URL and the publishable key belong here: both are meant to ship inside the app.
 * Row level security on the server decides what they can do (read courts; write your own reviews and
 * courts once signed in). Never put the secret key or the database URL in the app.
 *
 * Override for another project with EXPO_PUBLIC_SUPABASE_URL / EXPO_PUBLIC_SUPABASE_PUBLISHABLE_KEY.
 */
export const SUPABASE_URL = process.env.EXPO_PUBLIC_SUPABASE_URL || 'https://bjszbpabcgdomaoumicc.supabase.co';

export const SUPABASE_PUBLISHABLE_KEY =
  process.env.EXPO_PUBLIC_SUPABASE_PUBLISHABLE_KEY || 'sb_publishable_ZZrCM8Hy7az28_TVG-LOEw_lMTgrY_c';
