import { SafeAreaProvider } from 'react-native-safe-area-context';

import { MapScreen } from './src/components/MapScreen';

export default function App() {
  return (
    <SafeAreaProvider>
      <MapScreen />
    </SafeAreaProvider>
  );
}
