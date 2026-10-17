# Hoop Hunter

Find every basketball court, including the ones that aren't on any map. Hoop Hunter combines courts already mapped in OpenStreetMap with new courts found by scanning free aerial photos, and lets people confirm the finds from a phone app.

| Folder | What it is |
|---|---|
| [`app/`](app/) | The phone app (Expo / React Native, TypeScript, iOS and Android). Map of courts, satellite view, court details and directions, a review queue for satellite finds, and adding your own courts. See [app/README.md](app/README.md). |
| [`pipeline/`](pipeline/) | The satellite court finder (Python). It pulls every US court from OpenStreetMap, cuts USDA NAIP aerial photo tiles, trains a YOLO detector on a rented GPU, scans for courts that aren't mapped yet, and exports them to the app. See [pipeline/README.md](pipeline/README.md). |
| `lib/`, `android/`, `ios/`, `web/`, `test/` | The original Flutter prototype (a Google Map of NYC courts), kept for reference. |
| `assets/NYCcourts.json` | NYC Parks basketball courts, used by the app's NYC layer. |

## Status (October 2026)

- **App:** runs in Expo Go. It loads the courts in the visible map area from the Supabase database, and keeps the newest areas on the phone for offline use. When the server can't be reached it falls back to 1,306 bundled courts (Tennessee and NYC). Reviews and added courts use an anonymous sign-in and wait in a queue while the phone is offline. See [supabase/README.md](supabase/README.md).
- **Database (Supabase, loaded Oct 17, 2026):** 90,904 courts across the lower 48:
  - 78,893 OpenStreetMap courts that pass the privacy policy
  - 552 NYC Parks courts
  - 11,459 satellite finds, all at a public site: mostly schools and parks, plus churches, rec centers and colleges. They're marked "candidate" until people confirm them.
- **Detector:** trained on every mapped court in the lower 48 (about 94,000 usable labels from 101,098 OpenStreetMap courts) at 0.6 m per pixel. On Tennessee, which was held out of training, it scores mAP50 0.77.
- **Nationwide scan (Oct 15–16, 2026):**
  - Scanned every NAIP pixel of the lower 48: 219,198 photo files on 8 rented GPUs, about $157.
  - Found 889,817 possible courts that aren't in OpenStreetMap, 159,140 of them at confidence 0.5 or higher.
- **Satellite finds, v4 export** (Oct 17, 2026, about $1.63 of GPU):
  - Each find is re-checked against older NAIP years before export. 11,379 public courts made it, up from 5,431 in the first export.
  - A blind sample of 100 exports was 96% courts. That number leaves out the 22% of crops that were too unclear to call; counting those as misses, it's 74%.
  - Details are in [pipeline/docs/imagery.md](pipeline/docs/imagery.md).
- **Accuracy against official city court lists:**
  - The raw scan finds 87% of official courts of 150 m² or more at a low confidence setting, and 77% at 0.5.
  - It's weaker on courts OpenStreetMap lacks: 47% at 0.5. The strict export keeps 17% of those large courts, choosing precision over recall.
  - On 300 random 0.15 km² tiles, the assistant labels found 14 courts. OpenStreetMap had 6 of them, and the scan detected 6 at confidence 0.5. None of the exports fell inside those tiles. The sample is small, so treat these numbers as rough.
- **Safety:** every rented GPU machine shuts itself down when it stops making progress, or after 30 idle minutes. See the watchdog section in [pipeline/README.md](pipeline/README.md).
- **Next:**
  - park and school websites, plus indoor gyms from public sources (in progress)
  - Mapillary street-level photos, which need a free Mapillary token
  - scanning parks and schools again on leaf-off imagery, to find courts hidden under trees
  - hand-labeled accuracy checks using the gold labeling tool
  - light social features

## Quick start

```bash
# Phone app (then scan the QR code with Expo Go)
cd app && npm install && npx expo start

# Pipeline (Python 3.12, see pipeline/README.md for every step)
cd pipeline && python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
```

## Data and credits

- Court data from [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors (ODbL; attribution required).
- Aerial imagery: USDA NAIP via Microsoft Planetary Computer (public domain).
- NYC courts from NYC Parks open data.
