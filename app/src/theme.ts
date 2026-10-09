import { useColorScheme } from 'react-native';

import type { CourtKind } from './types';

export const ACCENT = '#E0651C';

/** Marker colors stay the same in light and dark mode so the legend never changes. */
export const MARKER_COLORS: Record<CourtKind, string> = {
  verified: ACCENT,
  confirmed: ACCENT,
  candidate: '#9B5CF6',
  mine: '#16A34A',
  rejected: '#6B7280',
};

export interface Theme {
  dark: boolean;
  background: string;
  surface: string;
  surfaceMuted: string;
  text: string;
  textMuted: string;
  border: string;
  accent: string;
  onAccent: string;
  success: string;
  danger: string;
  shadow: string;
}

const light: Theme = {
  dark: false,
  background: '#F4F4F6',
  surface: '#FFFFFF',
  surfaceMuted: '#F0F0F3',
  text: '#15171A',
  textMuted: '#61666F',
  border: '#E1E2E6',
  accent: ACCENT,
  onAccent: '#FFFFFF',
  success: '#16A34A',
  danger: '#D93025',
  shadow: '#000000',
};

const dark: Theme = {
  dark: true,
  background: '#0F1012',
  surface: '#1D1E22',
  surfaceMuted: '#2A2B30',
  text: '#F3F4F6',
  textMuted: '#A3A8B1',
  border: '#34363C',
  accent: '#F07A32',
  onAccent: '#FFFFFF',
  success: '#34C26A',
  danger: '#F2685F',
  shadow: '#000000',
};

/** Shadow for controls that float over the map. */
export const FLOATING_SHADOW = '0px 2px 8px rgba(0, 0, 0, 0.22)';

export function useTheme(): Theme {
  return useColorScheme() === 'dark' ? dark : light;
}
