# Hoop Hunter

Find every basketball court, including the ones that aren't on any map. Hoop Hunter combines courts already mapped in OpenStreetMap with new courts found by scanning free aerial photos, and lets people confirm the finds from a phone app.

| Folder | What it is |
|---|---|
| [`app/`](app/) | The phone app (Expo / React Native, TypeScript, iOS and Android). Map of courts, satellite view, court details and directions, a review queue for satellite finds, and adding your own courts. See [app/README.md](app/README.md). |
| [`pipeline/`](pipeline/) | The satellite court finder (Python). It pulls every US court from OpenStreetMap, cuts USDA NAIP aerial photo tiles, trains a YOLO detector on a rented GPU, scans for courts that aren't mapped yet, and exports them to the app. See [pipeline/README.md](pipeline/README.md). |
| `lib/`, `android/`, `ios/`, `web/`, `test/` | The original Flutter prototype (a Google Map of NYC courts), kept for reference. |
| `assets/NYCcourts.json` | NYC Parks basketball courts, used by the app's NYC layer. |

## Status (October 2026)

- **App:** runs in Expo Go. It has 1,306 courts:
  - 622 mapped Tennessee courts that pass the privacy policy (home and residents-only courts removed)
  - 553 NYC Parks courts
  - 131 new satellite finds in Tennessee, each one at a park, school, church, rec center or college and checked by eye
- **Detector:** trained on every mapped court in the lower 48 (about 94,000 usable labels from 101,098 OpenStreetMap courts) at 0.6 m per pixel. On Tennessee, which was held out of training, it scores mAP50 0.77.
- **Nationwide scan (Oct 15–16, 2026):**
  - Scanned every NAIP pixel of the lower 48: 219,198 photo files on 8 rented GPUs, about $157.
  - Found 889,817 possible courts that aren't in OpenStreetMap, 159,140 of them at confidence 0.5 or higher.
  - After the privacy filter and the second-check model: 5,431 high-precision new public courts, plus 78,893 OpenStreetMap courts that pass the privacy policy. They're exported per state in `pipeline/out/national/app_ready/` (local only) for loading into a database.
- **Accuracy against official city court lists:**
  - Finds 87% of official courts of 150 m² or more at a low confidence setting, and 77% at 0.5.
  - It's weaker on courts OpenStreetMap lacks: 47% at 0.5.
- **Safety:** every rented GPU machine shuts itself down at a deadline or after 30 idle minutes. See the watchdog section in [pipeline/README.md](pipeline/README.md).
- **Next:**
  - the nationwide scan
  - a server database, so the app can load courts for the whole country by map area
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
