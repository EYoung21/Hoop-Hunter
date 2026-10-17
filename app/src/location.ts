import * as Location from 'expo-location';
import { Alert, Linking } from 'react-native';

import type { LatLng } from './types';

export async function hasLocationPermission(): Promise<boolean> {
  const { granted } = await Location.getForegroundPermissionsAsync();
  return granted;
}

function ask(title: string, message: string, confirmLabel: string): Promise<boolean> {
  return new Promise((resolve) => {
    Alert.alert(
      title,
      message,
      [
        { text: 'Not now', style: 'cancel', onPress: () => resolve(false) },
        { text: confirmLabel, onPress: () => resolve(true) },
      ],
      { cancelable: true, onDismiss: () => resolve(false) },
    );
  });
}

/** A "Here now" check-in needs the phone within this distance of the court. */
export const CHECK_IN_RADIUS_M = 250;
/** GPS error allowed on top of the radius, at most. */
const CHECK_IN_SLACK_M = 75;

/** Great-circle distance in metres. */
export function distanceMeters(a: LatLng, b: LatLng): number {
  const rad = Math.PI / 180;
  const dLat = (b.latitude - a.latitude) * rad;
  const dLng = (b.longitude - a.longitude) * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.latitude * rad) * Math.cos(b.latitude * rad) * Math.sin(dLng / 2) ** 2;
  return 2 * 6_371_000 * Math.asin(Math.min(1, Math.sqrt(h)));
}

export type PresenceCheck =
  | { ok: true }
  | { ok: false; reason: 'declined' | 'unavailable' }
  | { ok: false; reason: 'far'; meters: number };

/**
 * One location read to confirm the player is at a court before a "Here now" check-in. Only the yes / no
 * leaves this function: the position is not sent, saved or kept, and nothing keeps tracking afterwards.
 */
export async function checkAtCourt(court: LatLng): Promise<PresenceCheck> {
  const permission = await Location.getForegroundPermissionsAsync();
  if (!permission.granted) {
    if (!permission.canAskAgain) {
      if (
        await ask(
          'Location is off for Hoop Hunter',
          'To check in, Hoop Hunter checks once that you are at the court. Allow location access in Settings to use "Here now".',
          'Open Settings',
        )
      ) {
        await Linking.openSettings();
      }
      return { ok: false, reason: 'declined' };
    }
    const proceed = await ask(
      'Check in at this court?',
      'Hoop Hunter checks once that you are within 250 m of the court. Your location stays on your phone: it is not sent or saved.',
      'Continue',
    );
    if (!proceed) return { ok: false, reason: 'declined' };
    const request = await Location.requestForegroundPermissionsAsync();
    if (!request.granted) return { ok: false, reason: 'declined' };
  }

  try {
    const { coords } = await Location.getCurrentPositionAsync({ accuracy: Location.Accuracy.High });
    const meters = distanceMeters(court, { latitude: coords.latitude, longitude: coords.longitude });
    const slack = Math.min(coords.accuracy ?? 0, CHECK_IN_SLACK_M);
    return meters - slack <= CHECK_IN_RADIUS_M ? { ok: true } : { ok: false, reason: 'far', meters };
  } catch {
    return { ok: false, reason: 'unavailable' };
  }
}

/**
 * Asks for location access (explaining why first) and returns the user's position,
 * or null if they decline or the position can't be found.
 */
export async function locateUser(): Promise<LatLng | null> {
  const permission = await Location.getForegroundPermissionsAsync();

  if (!permission.granted) {
    if (!permission.canAskAgain) {
      if (
        await ask(
          'Location is off for Hoop Hunter',
          'To show courts near you, allow location access in Settings. You can keep browsing the map without it.',
          'Open Settings',
        )
      ) {
        await Linking.openSettings();
      }
      return null;
    }

    const proceed = await ask(
      'Find courts near you?',
      'Hoop Hunter uses your location only to center the map on you. It is not saved or shared.',
      'Continue',
    );
    if (!proceed) return null;

    const request = await Location.requestForegroundPermissionsAsync();
    if (!request.granted) {
      Alert.alert('No problem', 'You can still pan and zoom the map to find courts.');
      return null;
    }
  }

  try {
    const position = await Location.getCurrentPositionAsync({ accuracy: Location.Accuracy.Balanced });
    return { latitude: position.coords.latitude, longitude: position.coords.longitude };
  } catch {
    Alert.alert("Couldn't find you", 'Make sure location services are turned on, then try again.');
    return null;
  }
}
