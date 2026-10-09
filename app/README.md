# Hoop Hunter (phone app)

An Expo (React Native + TypeScript) app that shows basketball courts on a map, including
"candidate" courts found by the satellite-scan pipeline, which you confirm or reject from the app.

Built on Expo SDK 57. It runs in **Expo Go**, so you don't need an App Store or Play Store build.

## Run it on your phone

1. Install **Expo Go** on your phone from the App Store (iPhone) or Google Play (Android).
   It needs to be a version that supports SDK 57 (the current store version).
2. On your computer (Node 20.19+, 22.13+, or 24.3+):
   ```bash
   cd app
   npm install
   npx expo start
   ```
3. Scan the QR code shown in the terminal:
   - **iPhone:** use the Camera app, then tap the banner to open it in Expo Go.
   - **Android:** open Expo Go and tap "Scan QR code".
4. The phone and computer must be on the **same Wi-Fi network**. If they can't be (or the QR
   code times out, which can happen on campus or office Wi-Fi), use a tunnel:
   ```bash
   npx expo start --tunnel
   ```

If the map shows old data after the data file changes, restart with `npx expo start --clear`.

## What it does

- **Map**: opens on Chattanooga and Signal Mountain. The **globe** button switches between the
  standard map and satellite (hybrid) view so you can see the blacktop. The **target** button jumps
  to your location. The app explains why it wants your location before the system asks, and keeps
  working if you say no.
- **Markers** (see the map key in the top-left corner; tap it to collapse):
  - Orange dot: verified court (OpenStreetMap or NYC Parks).
  - Hollow purple ring: satellite candidate, not reviewed yet.
  - Orange dot with a check: a candidate you confirmed.
  - Green square: a court you added.
  - Grey dot with an x: a candidate you rejected (hidden unless "Rejected" is switched on).
  - Orange numbered bubble: a group of courts, drawn when more than about 120 are in view. Tap it to zoom in.
- **Court card**: tap a marker for name, source, hoops, surface, lights, address, notes, and
  confidence for candidates. **Directions** opens Apple Maps on iPhone or Google Maps on Android.
- **Reviewing candidates**: on a candidate's card, tap **It's a court** or **Not a court**.
  **Undo review** reverses either decision. The **Candidates** filter chip shows how many are
  still waiting.
- **Adding a court**: long-press anywhere on the map, or tap **+** and then tap the map. Fill in
  the name, number of hoops, lights, surface, and notes. Courts you added can be deleted from their card.
- **Filters**: All / Verified / Candidates / Mine, plus a count of courts in view. A **Rejected**
  chip appears once you've rejected something.
- Supports light and dark mode and respects the notch and home indicator.

## Data file

The courts come from `assets/data/courts.geojson`, which is bundled into the app.
The pipeline (`../pipeline/`) regenerates this file. It's a GeoJSON `FeatureCollection` of
`Point` features (`coordinates` are `[longitude, latitude]`). Each feature has these properties:

| Property     | Type                                                | Notes                                          |
| ------------ | --------------------------------------------------- | ---------------------------------------------- |
| `id`         | string                                              | Unique: `osm-way-123`, `nyc-X159`, `det-…`, `user-…` |
| `name`       | string \| null                                      | Shown as "Unnamed court" when null             |
| `source`     | `"osm"` \| `"nyc_parks"` \| `"detected"` \| `"user"` |                                                |
| `status`     | `"verified"` \| `"candidate"`                       | Candidates get the review buttons              |
| `confidence` | number \| null                                      | 0–1, detected courts only                      |
| `hoops`      | number \| null                                      |                                                |
| `surface`    | string \| null                                      | e.g. `asphalt`, `concrete`                     |
| `lit`        | boolean \| null                                     | Has lights                                     |
| `address`    | string \| null                                      |                                                |
| `region`     | string                                              | `chattanooga`, `nyc`, …                        |
| `osm_id`     | string \| null                                      |                                                |
| `updated`    | string                                              | ISO date                                       |

The TypeScript types are in `src/types.ts`. The app checks every feature when it loads and skips
any that are malformed (bad coordinates, unknown `source`/`status`, missing or duplicate `id`)
instead of crashing. The file can have zero candidates.

Metro doesn't bundle `.geojson` files out of the box. `metro.config.js` adds the extension and
`metro.transformer.js` turns the file into a JS module. To ship new data, replace the file and
restart Expo. No code changes are needed.

## What's stored on the phone

Nothing is sent anywhere. Two keys are saved in AsyncStorage, on the device only:

- `hoop-hunter:reviews:v1`: your candidate decisions, keyed by court id:
  `{ "det-123": { "decision": "confirmed" | "rejected", "at": "<ISO timestamp>" } }`.
- `hoop-hunter:user-courts:v1`: courts you added, saved as a GeoJSON `FeatureCollection` with the
  same schema (`source: "user"`, `status: "verified"`, `region: "user"`, plus an optional `notes`).

Deleting Expo Go (or the app) removes this data. Your location is used only to center the map
and is never stored.

## Code layout

```
App.tsx                      Root: safe-area provider + map screen
index.ts                     Expo entry point
metro.config.js              Adds .geojson support
metro.transformer.js         Turns .geojson into a JS module at bundle time
assets/data/courts.geojson   Court data (written by the pipeline)
src/types.ts                 Data contract and app types
src/storage.ts               AsyncStorage load/save for reviews and added courts
src/location.ts              Polite location permission flow and "find me"
src/theme.ts                 Light/dark colors and marker colors
src/data/courts.ts           Loads and validates the bundled GeoJSON; creates user courts
src/data/classify.ts         Marker kind per court, filters, plain-English labels
src/data/viewport.ts         Starting region, in-view checks, marker clustering
src/data/useCourtData.ts     Hook combining bundled data with saved reviews and courts
src/components/              Map screen, markers, legend, filter chips, controls, court card, add form
```

## Checks

```bash
npm run typecheck                    # tsc --noEmit
npx expo export --platform android   # proves the bundle (and the .geojson import) builds; delete dist/ afterwards
npx expo-doctor
```

## Later: store builds

Expo Go needs no map API keys. A standalone Android build (EAS) will need a Google Maps API key
set via the `react-native-maps` config plugin. See https://docs.expo.dev/versions/latest/sdk/map-view/.
