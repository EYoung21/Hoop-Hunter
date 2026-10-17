# More imagery: multi-year NAIP, leaf-off public orthos, random-area accuracy (v4, 2026-10-17)

This run tried to make the nationwide court results better by looking at more imagery:

1. **Multi-year NAIP.** Every candidate that matters was looked at again in 2-4 other NAIP flights (2016-2023),
   scored with the v2 detector and the verifier in each, and a new export rule was set from the result.
2. **Leaf-off and sharper public imagery.** Statewide 3-12 inch orthoimagery that states publish under open terms
   was surveyed, licence by licence, and a polite crop fetcher was built for it. A sample of candidates, known courts
   and missed official courts in the 21 covered states plus NYC was re-scored and reviewed on it.
3. **Random-area accuracy.** The 300 random gold tiles were labelled by the assistant (**assistant labels, not gold**)
   to measure recall and precision on random land.

The v2 detector (`weights/best_v2.pt`) is unchanged. The new export is `out/national/v4/app_ready/`: **11,379
detected courts** (v3: 5,924) at a measured **0.96** precision on a blind random sample. Privacy is unchanged:
every exported candidate is `public` with a context-CNN score. All labels in this document are blind
model-assistant labels (C = court markings or hoops visible, N = clearly something else, U = unmarked slab or
unclear), **not gold**.

New modules: `hoop_pipeline/multiyear.py`, `hoop_pipeline/ortho.py` (+ `ortho_sources.json`),
`hoop_pipeline/gold_assist.py`, and `tests/test_imagery.py`.

## Cost and instances

One `gpu_1x_a100_sxm4` (us-east-1, $1.99/h) as `hh-img1-x2610171423`, launched with
`cloud/launch.sh img1 gpu_1x_a100_sxm4,gpu_1x_a10 --max-hours 4`.

* The on-instance watchdog was installed and its self-test passed before any work: systemd timer active, cron
  fallback, own id listed by `GET /instances` and resolved by IP. `progress_glob` was not set; the 4 h deadline was hard.
* Work: setup 4 min, the multi-year run 38 min (two shards), merge 1 min.
* Terminated by `cloud/terminate.sh img1` at 11:12 UTC: **0.82 h, about $1.63** of the $40 budget (run
  `imagery-2026-10-17` in `cloud/last_run.json`). `GET /instances` returned 0 afterwards.
* Everything else (leaf-off fetch and scoring, reviews, gold-tile labelling) ran on this PC without a GPU.

## 1. Multi-year NAIP re-check

### Scope (`multiyear input`, `data/v4/multiyear_input.parquet`)

| group | rows | why |
|---|---|---|
| candidates | **154,985** | public or unknown privacy class, and v2 conf >= 0.3 or in-scan verifier >= 0.9. This holds every v3 export (5,924) and every candidate an earlier blind review labelled (437). Private_home / residents_only / restricted can never be exported, so they were left out. |
| known courts | 15,000 | random national detections near an OSM court with conf >= 0.3: how persistent real courts are |
| truth misses | 970 | official court-level truth records no national detection >= 0.1 matched (40 m point / 15 m polygon): does another flight show them? |

The NAIP catalog already indexes every flight from 2016 (`NAIP_MIN_YEAR`, 867,997 items). Each row got its scan
year plus up to 4 other years with an item covering the point, newest first. Most states have 4 flights in
2016-2023; the 2016/2017 flights are often 1.0 m (resampled to 0.6 m, so blurry), and a few flights are leaf-off
(FL and GA 2021 in January, VT 2018, several October/November flights).

### Run (`multiyear run`, GPU node)

* For each row and year: a 320 px (192 m) 4-band chip centred on the box from the same item choice as the scan, the
  v2 detector (416 px) and the verifier. Recorded per year: the best v2 score at the box (IoU >= 0.3 or the box
  contains the centre) and near the centre, the verifier on the matched detection, NDVI and brightness in the box and
  a ring around it, and the correlation of a 32 x 32 patch of the box with the scan year's patch.
* **683,268 chips, 99.9% read** (682,737), in 37.8 min on two shards of 96 reader threads (about 300 chips/s), then
  merged into `out/v4/multiyear/years.parquet` (30 MB, pulled as one file).
* `multiyear features` turns that into per-candidate features (`years_checked`, `years_seen` (v2 >= 0.25),
  `years_seen50`, `other_seen`, conf / verifier mean and max over the other years, `first_seen_year`, `slab_appeared`
  (vegetated in the oldest flight, paved now), `slab_gone`, `corr_mean_other`, `persist`, `leafoff_seen`) and joins
  them onto v3's file: `out/national/v4/candidates_v4.parquet` (all 889,817 candidates, v3 columns + features +
  `score_v4`, `reviewed`, `export_v4`).

### What persistence says (`multiyear labelled`, `out/v4/multiyear/groups.json`)

| group | n | other years checked | seen in every other year | seen in none | v2 >= 0.5 in some other year |
|---|---|---|---|---|---|
| known OSM courts | 15,000 | 2.9 | 0.70 | 0.07 | 0.83 |
| reviewed C (court) | 227 | 2.9 | 0.77 | 0.08 | 0.82 |
| reviewed U (unsure) | 161 | 2.9 | 0.68 | 0.09 | 0.78 |
| reviewed N (not a court) | 49 | 3.0 | 0.41 | 0.14 | 0.69 |
| all candidates in scope | 154,985 | 3.0 | 0.30 | 0.46 | 0.41 |
| truth misses | 970 | 3.1 | 0.04 | 0.82 | 0.11 |

* Real courts persist (70-77% seen in every other flight), but so do most look-alikes: tennis, sand volleyball,
  parking and plain slabs are there every year too. Unsure slabs persist like courts.
* **Persistence is not a filter on its own.** Blind review of strata (`out/national/v4/review/persist_strata/`,
  40 each, shuffled together):

  | stratum | population | C / N / U | C / (C + N) |
  |---|---|---|---|
  | v3 exports seen in at least one other flight | 5,650 | 28 / 0 / 11 | 1.00 |
  | v3 exports **no other flight shows** | 273 | 33 / 2 / 5 | 0.94 |
  | not exported, verifier >= 0.95, seen in >= 2 other flights (v2 mean >= 0.4) | 7,052 | 22 / 7 / 11 | 0.76 |
  | not exported, verifier 0.9-0.95, seen in >= 2 other flights (v2 mean >= 0.5) | 6,426 | 14 / 8 / 18 | 0.64 |
  | not exported, verifier >= 0.9, no other flight shows | 1,097 | 18 / 8 / 13 | 0.69 |

  The exports no other flight shows are mostly **new courts** (built since the older flights), so dropping them would
  cost real courts. Simple persistence rules added too many look-alikes.
* **As extra evidence it helps.** A logistic model (`multiyear fit-score`, `out/national/v4/score_model.json`) on
  single-year scores plus the multi-year features, trained on all 625 blind labels then available (334 C, 74 N, 217 U;
  target C against N or U), has a 5-fold cross-validated AUC of **0.767**, against 0.717 for the same model without
  the multi-year features (0.844 against 0.818 for C against N alone). The largest multi-year weights: persistence
  (+), verifier mean over the other years (+), lower patch correlation with the scan year (+; repainted or resurfaced
  courts change, flat slabs don't), and a high v2 maximum in another flight without persistence (-). Its output is
  `score_v4`.

### Export rule (`multiyear select`)

**v4 export = public, CNN-scored, v3 >= 0.5, v2 >= 0.5, in-scan verifier >= 0.9 and `score_v4` >= 0.6; plus v3's
exports with `score_v4` >= 0.35; minus every candidate any blind review labelled N** (101 ids,
`out/national/v4/review_rejected.txt`). This keeps the per-state verifier thresholds' purpose (v3's floor was 0.9)
while letting the multi-year score decide inside the 0.9-0.98 band.

Validation on fresh candidates (not used to fit the score; `review/score_bands`, `review/score_bands2`):

| stratum (not v3-exported unless noted) | population | C / N / U | C / (C + N) |
|---|---|---|---|
| score_v4 >= 0.8 | 1,080 | 28 / 2 / 10 | 0.93 |
| score_v4 0.7-0.8 | 1,849 | 26 / 2 / 9 | 0.93 |
| score_v4 0.6-0.7 | 2,793 | 28 / 0 / 12 | 1.00 |
| score_v4 0.5-0.6 (not exported) | 3,253 | 28 / 5 / 7 | 0.85 |
| verifier 0.8-0.9, score_v4 >= 0.7 (not exported) | 468 | 26 / 4 / 8 | 0.87 |
| v3 exports with score_v4 0.35-0.45 (kept) | 489 | 21 / 3 / 14 | 0.88 |
| v3 exports with score_v4 < 0.35 (dropped) | 294 | 15 / 8 / 17 | 0.65 |

Rules compared (`multiyear grid`, `out/national/v4/grid.json`; held-out evidence as in v3: candidates in held-out
covered parks matching an official court, DC + Ramsey candidates matching a traced court, recall on the 203 held-out
courts OSM lacks; the "labelled" column is in-sample for score rules):

| rule | selected | + / - vs v3 | CA share | TX / MO / OH | held-out park | DC + Ramsey | held-out recall, OSM lacks >= 150 m² |
|---|---|---|---|---|---|---|---|
| v3 export (per-state verifier thresholds) | 5,924 | | 0.49 | 157 / 50 / 76 | 13 / 14 | 5 / 6 | 0.108 |
| v3 minus exports no other flight shows | 5,650 | -274 | 0.50 | 143 / 46 / 70 | 13 / 14 | 4 / 5 | 0.103 |
| verifier >= 0.95, seen in >= 2 other flights | 12,085 | +7,052 / -891 | 0.47 | 294 / 73 / 165 | 18 / 19 | 14 / 15 | 0.167 |
| verifier >= 0.9, seen in >= 2 other flights | 17,337 | +12,775 / -1,362 | 0.46 | 584 / 147 / 265 | 21 / 22 | 20 / 23 | 0.202 |
| verifier >= 0.9, score >= 0.5, v3 kept at >= 0.35 | 14,680 | +9,070 / -314 | 0.49 | 354 / 74 / 177 | 16 / 18 | 15 / 17 | 0.158 |
| **verifier >= 0.9, score >= 0.6, v3 kept at >= 0.35 (v4)** | **11,399** (11,379 after the N list) | +5,789 / -314 | 0.52 | 258 / 65 / 116 | 14 / 16 | 9 / 11 | 0.123 |
| verifier >= 0.9, score >= 0.7, v3 kept at >= 0.35 | 8,573 | +2,963 / -314 | 0.52 | 207 / 50 / 85 | 13 / 14 | 5 / 6 | 0.099 |

The held-out counts are too small to choose between rules; the choice rests on the blind bands above (0.6 is the
lowest band that held >= 0.93).

### Result

* **11,379 exported** (v3: 5,924): +5,780 new, -325 dropped (314 v3 exports with `score_v4` < 0.35, 11 more by the
  N list; the N list also removed 9 new ones).
* **Blind random sample of 100 v4 exports** (`review/exported_sample`, 98 crops, no overlap with any training label):
  **73 C, 3 N, 22 U: precision 0.96 (Wilson 95% CI 0.89-0.99)**, 0.74 (0.65-0.82) with unsure counted as not.
  The 55 that v3 did not export: 38 C / 1 N / 16 U (0.97). The 3 N: a dirt pad in a field, a pale roof or slab under trees, and a parking lot.
  Unsure is 22%, as in v3's sample (23%).
* **Official truth set** (`out/national/v4/truth_export_compare.json`), exported candidates only:

  | | v3 export | v4 export |
  |---|---|---|
  | recall on the 1,006 official courts OSM lacks | 0.051 | **0.094** |
  | ... at least 150 m² (516) | 0.091 | **0.172** |
  | exports inside covered parks matching an official court | 34 / 35 (0.97) | 68 / 73 (0.93) |

* Per state (v3 -> v4): CA 2,899 -> 5,878 · FL 381 -> 772 · NV 302 -> 552 · PA 96 -> 406 · AZ 249 -> 372 · NY 106 -> 280 ·
  TX 157 -> 257 · NJ 106 -> 204 · NC 101 -> 142 · IL 65 -> 128 · MA 63 -> 128 · UT 56 -> 125 · OH 76 -> 116 · MI 79 -> 114 ·
  SC 70 -> 112 · GA 61 -> 111 · VA 47 -> 111 · MD 61 -> 108 · TN 62 -> 100 · CO 57 -> 91 · IN 51 -> 91 · LA 61 -> 90 ·
  KY 36 -> 82 · MN 50 -> 81 · WI 38 -> 81 · OK 78 -> 79 · AL 40 -> 71 · MO 50 -> 65 · WA 36 -> 58 · WV 29 -> 54 · AR 48 -> 50 ·
  CT 18 -> 44 · ME 13 -> 43 · OR 17 -> 43 · ID 35 -> 41 · KS 30 -> 39 · NM 40 -> 33 · RI 23 -> 33 · IA 29 -> 32 · MS 22 -> 28 ·
  MT 13 -> 27 · NH 10 -> 25 · VT 10 -> 21 · NE 15 -> 16 · DE 7 -> 13 · SD 8 -> 10 · WY 9 -> 10 · ND 12 -> 8 · DC 2 -> 4.
  CA's share rises from 49% to 52%: the flat 0.9 verifier floor favours states whose flights the verifier likes, as
  v2's 0.98 did. By site: school 8,524, park 2,125, church 300, rec 281, college 106, community 43.
* **Files:** `out/national/v4/app_ready/<ST>.geojson` (49) + `index.json` + `exported_candidates.parquet`. OSM features
  are copied from v3's files unchanged (78,893). Detected features keep v3's schema (`site` optional) and add three
  optional evidence fields: `years_checked`, `years_seen` and `first_seen_year` (-1 = no flight shows it). The core
  schema validates with `export_app.validate` (evidence fields stripped first; their types are checked separately) and
  ids are unique across files (90,272). `export_app.py` was not changed: its `validate` rejects unknown keys, so a
  consumer that validates against it must strip or allow the three fields.
* **Truth misses in other flights.** 114 of the 970 missed official courts score v2 >= 0.5 in some flight (98 in an
  older one; 66 also with verifier >= 0.9). A look at 20 of them (`out/v4/multiyear/truth_miss_other_year_sheet.jpg`)
  found about 12 with a court visible in that flight (later shaded, resurfaced, built over or under canopy), about 6
  blurry 1 m 2017 hits and 1 non-court.

## 2. Leaf-off and sharper public imagery

### Survey (2026-10-17)

Two research passes looked for free, openly licensed imagery that is leaf-off or sharper than NAIP and can be fetched
as small crops by a script. Every service in the registry was fetched and the image looked at. Licences were read at
the URL given; this run re-read OH, IN, DC, DE, CO, MO, KY and NC from the live service metadata or the AWS Open Data
registry.

**Federal: nothing usable.**
* **USGS High Resolution Orthoimagery (HRO)** was retired on 2017-09-29 ("Access to standard HRO data products and HRO
  services through the National Map will be discontinued"). The 2000-2016 archive is on EarthExplorer only, behind an
  ERS login, and the M2M API needs an approved access request. Not scriptable without an account, so not used.
* `imagery.nationalmap.gov/.../USGSNAIPPlus/ImageServer` advertises 0.3 m but every `exportImage` timed out (HTTP 504
  after 90 s, 3 tries). `USGSNAIPImagery` is ordinary NAIP; `basemap.nationalmap.gov` `USGSImageryOnly` stops at z16
  (about 2.4 m). USGS data itself is public domain.

**In `ortho_sources.json` (verdict USE).** The bbox is sent in EPSG:3857.

| key | area | type | native res | years | season | licence (short) |
|---|---|---|---|---|---|---|
| nc_onemap_latest | NC statewide | ImageServer | 0.15 m | 2021-2025, quarter of the state per year | leaf-off | NC OneMap "free and unrestricted use policy" |
| va_vbmp_recent | VA statewide | MapServer export | 0.30 m (0.15 / 0.076 m in some localities) | 2022, 2023, 2025 | leaf-off | free; published depictions of improvements must carry the Va. Code 54.1-402(C) notice |
| oh_osip | OH statewide | ImageServer | 0.15 m (0.076 m in places) | 2017-2025, most current | leaf-off | "available for public use ... as is" |
| in_current | IN statewide | ImageServer | 0.15 / 0.076 m | 2022-2025 | leaf-off | CC0 |
| ky_phase3 | KY statewide | ImageServer | 0.076 m | 2022-2024 | leaf-off | public domain with attribution (CC BY 4.0 in the catalog) |
| mo_msdis_6in | MO statewide | ImageServer | 0.15 m | 2023 (N), 2024 (S) | leaf-off | CC BY 4.0 |
| ar_adop_2023 | AR statewide | ImageServer | 0.20 m | 2023 | leaf-off | public-domain mark |
| mn_met25 | MN, Twin Cities 7 counties | WMS 1.3 | 0.30 m | 2025 | leaf-off | public domain (Minn. Stat. ch. 13) |
| tx_etcog24 | TX, 12 East Texas counties | ImageServer | 0.15 m | 2024 | leaf-off | CC0 |
| co_drapp2022 | CO, Denver region | ImageServer | 0.15 m | 2022 | leaf-off | CC BY 3.0 |
| or_osip2024 | OR statewide | ImageServer | 0.30 m | 2024 | **leaf-on** (sharper only) | use constraints "None" |
| ny_orthos_latest | NY statewide | MapServer export | ~0.3 m as served | 2022-2025 | leaf-off | free service; NYS asks large batch users to notify them first, so capped at 300 requests |
| nyc_oti_2024 | New York City | XYZ tiles (z19) | ~0.15 m | 2024 | leaf-off | CC BY 4.0 |
| pa_pema_cycle2 | PA statewide | MapServer export | 0.15 m | 2021-2023 | leaf-off | "No restrictions apply to these data. Use_Constraints: None" |
| nj_2020 | NJ statewide | WMS 1.1.1 | 0.30 m | 2020 | leaf-off | AccessConstraints none; acknowledgement appreciated |
| ma_2025 | MA statewide | XYZ tiles (z19) | 0.15 m | 2025 | leaf-off | "No restrictions apply to these data" |
| ct_2023 | CT statewide | ImageServer | 0.076 m | 2023 | leaf-off | "No restrictions apply to these data" |
| md_sixinch | MD statewide | ImageServer | 0.15 m | 2023-2025 by region | leaf-off | freely distributable; derived data must acknowledge the State of Maryland in its metadata |
| de_2024 | DE statewide | ImageServer | 0.15 m | 2024 | leaf-off | use constraints "None" |
| vt_best | VT statewide | ImageServer | 0.15-0.30 m | 2021-2024 | leaf-off | "freely available to the public and unrestricted in use" |
| ri_2026_spring | RI statewide | ImageServer | 0.076 m (not orthorectified) | Mar 2026 | mostly leaf-off | CC BY 4.0 |
| dc_2025 | DC | ImageServer | 0.08 m | Mar 2025 | leaf-off | CC BY 4.0 |

Attribution owed where derived court points are published: MSDIS (MO), KyFromAbove (KY), DRCOG DRAPP (CO), NYC OTI,
URI EDC / RIGIS (RI), DC OCTO, the State of Maryland (in the metadata of derived data), and the Va. Code 54.1-402(C)
notice for Virginia; courtesy credits are requested by NC OneMap, VGIN, PASDA/PEMA, NJ OGIS, MassGIS, CT ECO, VCGI,
Indiana GIO, the Arkansas GIS Office, the Met Council and TxGIO. **No point in the v4 export came from this imagery**
(it was used for evaluation and review only), so none of these obligations applies to the current files yet.

**Not used.**
* **MAYBE** (no licence text found, or vendor-sourced): Tennessee TDOT/TNMap leaf-off (ask tnmap.gis@tn.gov), South
  Carolina RFA 2023 6-inch tiles (no reuse terms; mapping@rfa.sc.gov), Iowa ISU county imagery, Hennepin County 2022
  (Nearmap-sourced), Wisconsin WROC 2020 (public domain but download-only), New Hampshire GRANIT 2021-22
  ("provisional", no terms found), Maine regional per-year services (usable, not wired up).
* **NO:** West Virginia leaf-off mosaic ("for viewing only"), Tennessee Vexcel (government only), Texas Imagery Service
  (subscription), Georgia GIO (Google-licensed), Michigan MiSAIL (view-only) and SEMCOG (paid Nearmap), Washington
  statewide ("not for public distribution"), Utah Google 6-inch (licensed users only), Cook County IL (redistribution
  needs written permission, per secondary sources).
* No open statewide leaf-off or sub-0.3 m program was found for FL, AL, CA, AZ, GA or the rest of the West.

### Fetcher (`ortho.py`)

* `test` fetched one crop from each of the 22 sources: all returned real imagery (`out/v4/ortho/test/`).
* `fetch` takes a 192 m crop at 0.3 m (640 px) around each point. Per source, a `Host` limits requests in flight
  (`max_concurrent`, 1-4), spaces request starts (`min_interval_s`, 0.1-1 s), retries with back-off and honours
  Retry-After, and sends the pipeline User-Agent. XYZ tile sources are stitched and cropped. Blank or no-data crops are
  flagged. `--skip-uncached` stops asking a stalled server.
* `score` resamples each crop to 0.6 m (the detector's training GSD) and runs v2 + the verifier, next to the same rows'
  NAIP scores from the multi-year run. `review` makes blind sheets, optionally with the NAIP crop beside the sharp one.

### Re-scoring (`data/v4/ortho_input.parquet` -> `out/v4/ortho/scores.parquet`, `summary.json`)

4,035 rows in covered areas: every reviewed candidate (92), samples of exports (943), not-exported public slabs
(v2 >= 0.5, verifier >= 0.8; 1,633) and known OSM courts (847), and up to 150 truth misses per source (520).
**3,790 crops** were fetched; the Jefferson County (CO, 25 of 182) and TxGIO (TX, 42 of 103) servers answered in
30-60 s per request and stalled, so they were cut short.

**The detector does worse on this imagery than on NAIP.** On known courts, v2 >= 0.5 on 72% of sharp crops against
82% of the same courts on NAIP (exports: 80% against 96%); per source (about 40 known courts each) from 0.50 (AR) to
0.88 (DE). The model was trained on
0.6 m summer NAIP; brown grass, different colour balance and resampling shift it. A detector fine-tuned on this imagery
would be needed to use it for scoring, not just viewing.

**For people, it settles most unsure slabs.** Blind re-labelling on the sharp crop (96 m at 0.3 m):

| reviewed candidates (NAIP label) | sharp: C | N | U |
|---|---|---|---|
| C (43) | 38 | 3 | 2 |
| U (35) | **18** | **4** | 13 |
| N (12) | 4 | 5 | 3 |

* 22 of 35 NAIP-unsure slabs (63%) get a definite answer, mostly "court" (18). Three NAIP "courts" are not courts on
  the sharper image.
* Not-exported public slabs (48 sampled from the pool above): 26 C / 4 N / 18 U on the sharp crop.

**Courts under canopy.** Of the 503 truth misses with a sharp crop, v2 scores >= 0.25 on **56 (11%)** of them on the
sharp crop against 8 (1.6%) on NAIP. Side by side with NAIP (`review/truth_miss_hi`), 45 of the 56 show a court:
**20 hidden by leaves on NAIP and visible leaf-off**, 14 built or repainted after the NAIP flight, 5 in building
shadow or lean on NAIP, 5 faint, 1 small; 9 stay unclear (canopy) and 2 are not courts. A random 36 of the other 447
(`review/truth_miss_rand`) show 1 court, 18 unclear (small backyard pads, canopy) and 17 non-courts (roofs, nothing,
changed). So roughly 57 of the 503 misses (11%) are visible courts on leaf-off imagery, and about 40% of those were
hidden by canopy. Most of these misses are in NYC, DC and the Twin Cities, where the truth sources are.

## 3. Random-area accuracy (assistant labels on the 300 gold tiles)

**These are assistant labels, not gold.** They are in `out/v4/gold_assist/assistant_labels.json`; the labelling page's
data and any human label file were not touched (none existed).

* **How.** Every urban and suburban tile was viewed at 2x with a pixel grid, rural tiles as 2 x 2 sheets at 1x, and
  anything court-like was zoomed to 3x, blind to model boxes. Then every OSM feature, national detection and
  pre-annotation in a tile was checked (`gold_assist context`). A court is a basketball court with lines, keys or hoops
  visible, an unmarked court-sized slab with corner marks at a school or park, or an OSM basketball feature hidden by
  canopy. 19 court-sized slabs without markings are recorded as `label: unsure` and not counted.
* **Courts found: 14** with their centre in a tile (15 boxes; one public court's centre is just outside its tile): 10
  in urban tiles, 4 suburban, 0 rural. Access: 4 public, 2 public under canopy (OSM), 4 home, 2 residents-only
  (apartments), 2 unsure.
* **Recall on random land** (`gold_assist score`, `out/v4/gold_assist/score.json`; Wilson 95% CI):

  | found by | courts | rate | CI | CONUS-weighted |
  |---|---|---|---|---|
  | OSM | 6 / 14 | 0.43 | 0.21-0.67 | 0.31 (bootstrap 0.08-0.61) |
  | any national detection >= 0.1 | 9 / 14 | 0.64 | 0.39-0.84 | 0.59 |
  | ... >= 0.5 | 6 / 14 | 0.43 | 0.21-0.67 | 0.38 |
  | OSM or a detection >= 0.5 | 8 / 14 | 0.57 | | |
  | v3 export / v4 export | 0 / 14 | 0 | 0-0.22 | |

* **Precision on random land.** No exported candidate (v3 or v4) falls in any of the 300 tiles (44 km²; the exports
  average about one per 700 km²), so export precision cannot be measured here; the blind export samples above are the
  measure. Candidates (detections not near OSM) inside the tiles: at conf >= 0.5, 2 of 14 match a court (0.14,
  CI 0.04-0.40); at >= 0.1, 4 of 36 (0.11). Public candidates at >= 0.5: 1 of 10. Most were school play yards, parking
  strips and slabs, i.e. what the export's verifier and score are there to reject.
* **What is missed.**
  * no detection at all (5): two small half-court pads in back yards, a half court with a painted key beside a
    community building, and two OSM park courts under full canopy;
  * detection 0.1-0.5 (3): an apartment full court next to an OSM court (so it would be dropped as near OSM anyway), a
    backyard half court, and a small acrylic half-court pad behind a daycare-like building;
  * the 4 private courts found (home, residents) are correctly never exported.
  * Density: about 0.018 courts per km² CONUS-weighted (bootstrap 0.008-0.029); with 14 courts the CIs are wide.

## Commands as run

```bash
# local
python -m hoop_pipeline.multiyear input                      # data/v4/multiyear_input.parquet (170,955 rows)
python cloud/lastrun.py new-run imagery-2026-10-17 --usd 40 --hours 20
bash cloud/launch.sh img1 gpu_1x_a100_sxm4,gpu_1x_a10 --max-hours 4   # watchdog installed + self-test passed
HH_NODE=img1 bash cloud/sync_code.sh && HH_NODE=img1 bash cloud/start_stage.sh setup
# upload single files: data/v4/multiyear_input.parquet, the NAIP catalog parquet, best_v2.pt, verifier_v2.pt
HH_NODE=img1 bash cloud/start_stage.sh job my0 python -m hoop_pipeline.multiyear run --shard 0/2 --workers 96
HH_NODE=img1 bash cloud/start_stage.sh job my1 python -m hoop_pipeline.multiyear run --shard 1/2 --workers 96
# node: python -m hoop_pipeline.multiyear merge; pull out/v4/multiyear/years.parquet
bash cloud/terminate.sh img1                                  # GET /instances -> 0
python -m hoop_pipeline.multiyear labelled
python -m hoop_pipeline.multiyear features                    # out/national/v4/candidates_v4.parquet
python -m hoop_pipeline.multiyear review --name persist_strata --n 40 --strata "NAME=EXPR" ...
python -m hoop_pipeline.multiyear fit-score                   # score_v4, score_model.json
python -m hoop_pipeline.multiyear review --name score_bands ... ; ... --name score_bands2 ...
python -m hoop_pipeline.multiyear grid --rules "conf=0.5,v2=0.5,verify=states" "conf=0.5,v2=0.5,verify=0.9,score=0.6,v3_score=0.35" ...
python -m hoop_pipeline.multiyear select --rule "conf=0.5,v2=0.5,verify=0.9,score=0.6,v3_score=0.35" \
    --exclude out/national/v4/review_rejected.txt
python -m hoop_pipeline.multiyear review --name exported_sample --n 100 --seed 31 --strata "all=export_v4 & ~reviewed"
python -m hoop_pipeline.multiyear app-ready --updated 2026-10-17
python -m hoop_pipeline.ortho test
python -m hoop_pipeline.ortho select --candidates out/national/v3/candidates_us.parquet
python -m hoop_pipeline.ortho fetch [--skip-uncached co_drapp2022 tx_etcog24]
python -m hoop_pipeline.ortho score
python -m hoop_pipeline.ortho review --name reviewed --where "group=='reviewed'" --n 100
python -m hoop_pipeline.ortho review --name truth_miss_hi --pair-naip --where "(group=='truth_miss') & (ortho_conf>=0.25)" --n 60
python -m hoop_pipeline.gold_assist views && python -m hoop_pipeline.gold_assist context --candidates out/national/v4/candidates_v4.parquet
python -m hoop_pipeline.gold_assist score
python -m unittest tests.test_imagery -v
```

## Outputs (git-ignored)

* `data/v4/`: `multiyear_input.parquet` (+ `.json`), `ortho_input.parquet` (+ `.json`), `ortho_fetch.parquet`
  (+ `.json`), `ortho_chips/<source>/` (3,790 sharp crops, kept for internal review only).
* `out/v4/multiyear/`: `years.parquet` (683,268 id x year rows), `features.parquet`, `groups.json`, node logs, the
  truth-miss sheet.
* `out/national/v4/`: `candidates_v4.parquet`, `score_model.json`, `grid.json`, `selection.json`,
  `review_rejected.txt`, `truth_export_compare.json`, `review/` (5 blind sets with `sample.json` + `labels.json`),
  `app_ready/`.
* `out/v4/ortho/`: `test/`, `scores.parquet`, `summary.json`, `review/` (reviewed, slab_pool, truth_miss_hi,
  truth_miss_rand, each with `labels.json`).
* `out/v4/gold_assist/`: `assistant_labels.json`, `context.json`, `score.json`, `per_tile.json`, `views/`, `check/`,
  `zoom/`.

## What is not verified

* **Every precision number rests on blind model-assistant labels**, by the same reviewer as v2's and v3's samples,
  not gold. U (unmarked slab) is 22% of the v4 export sample; "precision" counts it as neither.
* **The score was fitted on the same reviewer's labels.** Its bands were checked on fresh candidates, but a reviewer
  bias (for example, calling hoop-pole dots "C") would be learned and confirmed by the same eyes.
* **The rule was chosen after looking at those bands**, so the bands are partly validation; the final random export
  sample was drawn after the rule was fixed.
* **Per-state precision** was not measured. The flat 0.9 verifier floor replaced v3's per-state thresholds inside
  the rule, and CA's share rose to 52%.
* **Privacy** is unchanged and still rests on maps + the context CNN; no exported crop in this run's samples was on a
  home lot, but the known leak pattern (a home court near a point-only church/school) still applies.
* **Leaf-off licences** were read from service metadata, portal items and the AWS registry; none was confirmed with
  the publisher. NY was kept to 300 requests because NYS asks to be told about large batches.
* **Random-tile results** come from 14 courts; the CIs are wide and the canopy courts were counted only because OSM
  has them.
* The 114 truth misses seen in another NAIP flight were not all checked; a sample of 20 suggests about 60% are courts.

## Recommendations

1. **Use `out/national/v4/app_ready/`.** It roughly doubles the exported courts (11,379) and the recall on official
   courts OSM lacks (0.09 -> 0.17 at >= 150 m²) at the same measured precision (0.96 vs v3's 0.95). The three evidence
   fields are optional; strip them if the database loader validates with `export_app.validate`.
2. **Show sharp imagery to reviewers in the 21 covered areas.** It turns most NAIP-unsure slabs into a definite yes or
   no (22 of 35). A review queue of the not-exported pool (score_v4 0.5-0.6 and verifier 0.8-0.9 bands, ~0.85 by
   themselves) with leaf-off crops is the cheapest next gain.
3. **Fine-tune the detector on leaf-off imagery before scoring with it.** As-is it underfires (72% vs 82% on known
   courts). Labels can come from OSM courts on these sources (CC0 / CC BY / public domain).
4. **Scan parks and schools on leaf-off imagery for recall.** About 11% of the official courts the NAIP scan missed
   are visible leaf-off, 40% of them hidden by canopy on NAIP. A targeted scan of park and school polygons in the
   covered states is limited by the servers' politeness limits (wall time), not by GPU cost; tell NYS ITS first for NY.
5. **Settle the MAYBE licences by email** (Tennessee, South Carolina) before adding them; both are statewide leaf-off
   6-12 inch.
6. **Label the gold tiles for real.** The assistant pass found 14 courts in 300 tiles: the page needs a few hundred
   more urban and suburban tiles to give recall CIs narrower than +- 0.25.
