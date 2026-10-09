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
