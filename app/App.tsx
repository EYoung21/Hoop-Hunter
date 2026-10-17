import { SafeAreaProvider } from 'react-native-safe-area-context';

import { MapScreen } from './src/components/MapScreen';
import { SocialProvider } from './src/social/SocialProvider';

export default function App() {
  return (
    <SafeAreaProvider>
      <SocialProvider>
        <MapScreen />
      </SocialProvider>
    </SafeAreaProvider>
  );
}
