# Hoop Hunter (phone app)

An Expo (React Native + TypeScript) app that shows basketball courts on a map, anywhere in the US,
including "candidate" courts found by the satellite-scan pipeline, which you confirm or reject from the app.
Courts load from the Hoop Hunter Supabase backend for whatever part of the map is on screen
(see [../supabase/README.md](../supabase/README.md)); a bundled copy covers offline use.

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

- **Courts for the area you're looking at**: pan and zoom anywhere in the US. Zoomed in to about
  a metro area (under 0.6° wide), the app loads every court in view plus a margin from the server
  (`courts_in_view`). Zoomed out further, it loads numbered groups from the server instead
  (`court_clusters`), so the whole country can be browsed without downloading 85,000 courts.
  Requests wait until the map stops moving, answers are reused while fresh, and a small spinner
  next to the court count shows loading.
- **Offline**: the courts you looked at most recently (about 4,000) are saved on the phone. With no
  connection, the map shows those plus the bundled file (`assets/data/courts.geojson`: Tennessee and
  NYC) and says "Offline: showing saved courts". It retries every 30 seconds and when you move the map.
- **Map**: opens on Chattanooga and Signal Mountain. The **globe** button switches between the
  standard map and satellite (hybrid) view so you can see the blacktop. The **target** button jumps
  to your location. The app explains why it wants your location before the system asks, and keeps
  working if you say no.
- **Markers** (see the map key in the top-left corner; tap it to collapse):
  - Orange dot: verified court (OpenStreetMap or NYC Parks).
  - Blue dot with a roof: indoor court. It only appears in the key once indoor courts are loaded;
    the current data has none yet.
  - Hollow purple ring: satellite candidate, not reviewed yet.
  - Orange dot with a check: a candidate you confirmed.
  - Green square: a court you added.
  - Grey dot with an x: a candidate you rejected. Grey dot with a roof: one you marked as someone's
    home court. Both stay hidden unless "Rejected" is switched on.
  - Orange numbered bubble: a group of courts, drawn when more than about 120 are in view or when
    zoomed out past a metro area. Tap it to zoom in.
- **Court card**: tap a marker for name, source, hoops, surface, lights, address, notes, where a
  satellite find sits (park, school, church…), confidence for candidates, and what other players
  said about it ("3 players say it's a court · 1 not a court"). **Directions** opens Apple Maps
  on iPhone or Google Maps on Android.
- **Reviewing candidates**: on a candidate's card, tap **It's a court**, **Not a court**, or
  **Someone's home court** (a private court at a house: it is hidden like a rejected one, and
  the report helps keep private courts off the map). **Undo review** reverses any of them. The
  **Candidates** filter chip shows how many loaded candidates are still waiting.
- **Adding a court**: long-press anywhere on the map, or tap **+** and then tap the map. Fill in
  the name, number of hoops, lights, surface, and notes. Courts you added can be deleted from their card.
  They are sent in for review (`user_courts`, status pending) and appear for everyone once approved.
- **Filters**: All / Verified / Candidates / Mine, plus a count of courts in view. A **Rejected**
  chip appears once you've rejected something.
- Supports light and dark mode and respects the notch and home indicator.

## Data

Courts come from the server (`courts_in_view` rows have the fields below plus `lon`, `lat`, `state`),
with `assets/data/courts.geojson` bundled as the offline fallback. For the same id, a court you
added wins, then the server's copy, then the bundled one. Bundled courts are only shown where the
server hasn't answered yet, so a court the server has dropped doesn't linger.

The pipeline (`../pipeline/`) writes the same schema for both. The national exports are loaded into
the database with `python -m hoop_pipeline.db_load` (see `../supabase/README.md`). The bundled file
is a GeoJSON `FeatureCollection` of `Point` features (`coordinates` are `[longitude, latitude]`).
Each feature has these properties:

| Property     | Type                                                | Notes                                          |
| ------------ | --------------------------------------------------- | ---------------------------------------------- |
| `id`         | string                                              | Unique: `osm-way-123`, `nyc-X159`, `det-…`, `user-…` |
| `name`       | string \| null                                      | Shown as "Unnamed court" when null             |
| `source`     | `"osm"` \| `"nyc_parks"` \| `"detected"` \| `"user"` \| `"overture"` \| `"web"` \| `"indoor"` \| `"nces"` | Server rows with a source newer than the app are shown as "Other source" |
| `status`     | `"verified"` \| `"candidate"`                       | Candidates get the review buttons              |
| `confidence` | number \| null                                      | 0–1, detected courts only                      |
| `hoops`      | number \| null                                      |                                                |
| `surface`    | string \| null                                      | e.g. `asphalt`, `concrete`                     |
| `lit`        | boolean \| null                                     | Has lights                                     |
| `address`    | string \| null                                      |                                                |
| `region`     | string                                              | `chattanooga`, `nyc`, …                        |
| `osm_id`     | string \| null                                      |                                                |
| `updated`    | string                                              | ISO date                                       |
| `indoor`     | boolean (optional)                                  | Indoor court: drawn blue with a roof mark      |
| `site`       | string (optional)                                   | Satellite finds: `park`, `school`, `college`, `church`, `rec`, `community` |

The TypeScript types are in `src/types.ts`. The app checks every feature and server row when it
loads and skips any that are malformed (bad coordinates, unknown `status`, missing or duplicate `id`;
an unknown `source` too, for the bundled file) instead of crashing. The file can have zero candidates.

Metro doesn't bundle `.geojson` files out of the box. `metro.config.js` adds the extension and
`metro.transformer.js` turns the file into a JS module. To ship new data, replace the file and
restart Expo. No code changes are needed.

## Server and what is sent

The app talks to Supabase with the project URL and the **publishable** key in `src/config.ts`
(both are meant to ship; override with `EXPO_PUBLIC_SUPABASE_URL` and
`EXPO_PUBLIC_SUPABASE_PUBLISHABLE_KEY`). Row level security decides what the key can do: read
published courts, and write only your own reviews and courts once signed in. The secret key and the
database password never go in the app.

- **Sign-in**: on first launch the app signs in **anonymously** (no email, no name). The session is
  kept in AsyncStorage, so you stay the same player across restarts. This needs "Allow anonymous
  sign-ins" turned on in the Supabase dashboard. Until then everything still works on the phone,
  and uploads wait in the queue.
- **Sent**:
  - the box of map you're looking at, to load courts;
  - your reviews (`court` / `not_court` / `home_court`);
  - courts you add (name, hoops, lights, surface, notes, position).
- **Not sent**: your location. It's used only to center the map. The map box you then load
  courts for is of course near you.

## What's stored on the phone

Saved in AsyncStorage:

- `hoop-hunter:reviews:v1`: your candidate decisions, keyed by court id:
  `{ "det-123": { "decision": "confirmed" | "rejected" | "home", "at": "<ISO timestamp>" } }`.
- `hoop-hunter:user-courts:v1`: courts you added, saved as a GeoJSON `FeatureCollection` with the
  same schema (`source: "user"`, `status: "verified"`, `region: "user"`, plus an optional `notes`).
- `hoop-hunter:sync:v1`: the offline queue, the ids of reviews and courts not uploaded yet. Your
  phone stays the source of truth. A change is saved locally first, then uploaded 1.5 s later,
  when the app comes to the foreground, or on a retry timer (15 s growing to 15 min). Reviews
  and courts saved before sync existed are uploaded once on the first launch with it.
- `hoop-hunter:court-cache:v1`: the courts last loaded from the server and the areas they cover,
  for offline use (newest areas, at most about 4,000 courts).
- The Supabase session (`sb-…-auth-token`).

Deleting Expo Go (or the app) removes this data. The reviews and courts you already uploaded stay
on the server under an anonymous id that nothing links to you.

## Code layout

```
App.tsx                      Root: safe-area provider + map screen
index.ts                     Expo entry point
metro.config.js              Adds .geojson support
metro.transformer.js         Turns .geojson into a JS module at bundle time
assets/data/courts.geojson   Bundled court data (written by the pipeline), the offline fallback
src/types.ts                 Data contract and app types
src/config.ts                Supabase URL and publishable key
src/supabase.ts              Supabase client (AsyncStorage session, URL polyfill), anonymous sign-in
src/sync.ts                  Offline queue: uploads reviews and added courts when signed in and online
src/storage.ts               AsyncStorage load/save: reviews, added courts, sync queue, court cache
src/location.ts              Polite location permission flow and "find me"
src/theme.ts                 Light/dark colors and marker colors
src/data/courts.ts           Validates bundled GeoJSON and server rows; creates user courts
src/data/remote.ts           RPC calls: courts_in_view (paged), court_clusters, review_counts
src/data/useRemoteCourts.ts  Courts for the visible area: debounce, cache, clusters, offline cache
src/data/classify.ts         Marker kind per court, filters, plain-English labels
src/data/viewport.ts         Starting region, in-view checks, marker clustering, server boxes and zoom
src/data/useCourtData.ts     Hook merging your courts, server courts and bundled courts, with reviews
src/components/              Map screen, markers, legend, filter chips, controls, court card, add form
```

## Checks

```bash
npm run typecheck                    # tsc --noEmit
npx expo export --platform android   # proves the bundle (and the .geojson import) builds; delete dist/ afterwards
npx expo-doctor
../pipeline/.venv/Scripts/python ../supabase/verify_api.py   # the server side: RPCs, RLS, grants (35 checks)
```

## Later: store builds

Expo Go needs no map API keys. A standalone Android build (EAS) will need a Google Maps API key
set via the `react-native-maps` config plugin. See https://docs.expo.dev/versions/latest/sdk/map-view/.
