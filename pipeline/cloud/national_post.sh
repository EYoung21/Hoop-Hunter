#!/usr/bin/env bash
# Run ON the post-processing node of the national run, detached, one part per job:
#   HH_NODE=post bash cloud/start_stage.sh job <name> bash cloud/national_post.sh PART [args]
#
#   rescan MANIFEST  scan_engine run over the items in MANIFEST (items without a done marker) with the scan's own
#                    settings (best_v2.pt, imgsz 416, conf 0.1, in-stream verifier >= 0.1; PyTorch FP16, which matched
#                    the TensorRT engine to 0.001). At most 4 passes, each capped at HH_PASS_TIMEOUT s (default 1500),
#                    because the s1 node of the scan stalled on these items.
#   prep             per-state slices of sites_us / residential_us (data/sites/by_state) and Overture buildings for
#                    every state (data/sites/buildings_<ST>.parquet; HH_BUILDING_WORKERS, default 8)
#   merge            scan_engine merge of data/scan2/US/results (done items only) -> out/national/detections.parquet:
#                    cross-file dedupe within 20 m, near_known_osm = within 40 m of an OSM basketball feature
#   phome            context-CNN p_home for every OSM basketball feature -> data/national/osm_phome.parquet
#   rescore          fresh 192 m NAIP chips for the candidates passing the screen (conf >= HH_RESCORE_MIN, default 0.3;
#                    scan verifier >= HH_RESCORE_VERIFY, default 0): v2 again (conf2), the verifier again, p_home
#   privacy          per-state privacy classes (rules + p_home where rescored) for candidates >= HH_PRIVACY_MIN
#                    (default 0.1) and every OSM feature, then summarize
#   review NAME ARGS national review (blind sheets from the rescore chips), extra args passed through
#   package          tar of everything the PC needs -> ~/hh/national_out.tar
set -uo pipefail
cd ~/hh/pipeline
PY=~/hhvenv/bin/python
export PYTHONPATH=~/hh/pipeline
PART="${1:?rescan|prep|merge|phome|rescore|privacy|review|package}"
shift
PLAN=data/scan2/US
step() { echo "== $* $(date -u +%FT%TZ)"; "$@" || { echo "STEP FAILED: $*"; return 1; }; }

case "$PART" in
  rescan)
    MAN="${1:?manifest of the items to rescan}"
    for pass in 1 2 3 4; do
      echo "== rescan pass $pass $(date -u +%FT%TZ)"
      timeout -k 60 "${HH_PASS_TIMEOUT:-1500}" $PY -m hoop_pipeline.scan_engine run --plan "$PLAN" --manifest "$MAN" \
        --weights weights/best_v2.pt --imgsz 416 --conf 0.1 --verifier weights/verifier_v2.pt --verify-min 0.1 \
        --threads "${HH_THREADS:-96}" --batch 512 || echo "pass $pass exited with $?"
      P=$($PY -m hoop_pipeline.scan_engine status --plan "$PLAN" --manifest "$MAN" | $PY -c 'import json,sys; print(json.load(sys.stdin)["pending"])')
      echo "pass $pass: $P pending"
      [ "$P" = "0" ] && { echo "RESCAN_COMPLETE"; exit 0; }
      sleep 30
    done
    echo "RESCAN_INCOMPLETE ($P pending)"; exit 1 ;;
  prep)
    step $PY -m hoop_pipeline.national split-sites || exit 1
    step $PY -m hoop_pipeline.sites buildings --states CONUS --workers "${HH_BUILDING_WORKERS:-8}" || exit 1
    ls data/sites/buildings_*.parquet | wc -l
    echo "PREP_DONE" ;;
  merge)
    step $PY -m hoop_pipeline.scan_engine merge --plan "$PLAN" --out out/national --v1-mask none || exit 1
    echo "MERGE_DONE" ;;
  phome)
    step $PY -m hoop_pipeline.national osm-phome --workers "${HH_FETCH_WORKERS:-128}" || exit 1
    echo "PHOME_DONE" ;;
  rescore)
    step $PY -m hoop_pipeline.rescore --candidates out/national/detections.parquet --min-conf "${HH_RESCORE_MIN:-0.3}" \
      --min-verify "${HH_RESCORE_VERIFY:-0}" --weights weights/best_v2.pt --imgsz 416 --verifier weights/verifier_v2.pt \
      --context-model weights/context_v2.pt --chips-dir data/national/rescore_chips --chunk 4096 --batch 256 \
      --workers "${HH_FETCH_WORKERS:-128}" --out out/national/rescore.parquet || exit 1
    echo "RESCORE_DONE" ;;
  privacy)
    step $PY -m hoop_pipeline.national privacy --rescore out/national/rescore.parquet \
      --osm-phome data/national/osm_phome.parquet --min-conf "${HH_PRIVACY_MIN:-0.1}" \
      --workers "${HH_PRIVACY_WORKERS:-16}" || exit 1
    step $PY -m hoop_pipeline.national summarize || exit 1
    echo "PRIVACY_DONE" ;;
  review)
    NAME="${1:?review name}"; shift
    step $PY -m hoop_pipeline.national review --name "$NAME" "$@" || exit 1
    echo "REVIEW_DONE" ;;
  package)
    # file lists: the outputs plus the rescanned items' result files (MANIFEST), and the rescore chips of the public
    # candidates (review / later labelling); cloud side only writes lists, the PC pulls them as tar streams
    MAN="${1:?manifest of the rescanned items}"
    $PY - "$MAN" <<'EOF'
import sys
from pathlib import Path
import pyarrow.compute as pc
import pyarrow.parquet as pq
ids = {ln.strip() for ln in open(sys.argv[1]) if ln.strip()}
plan = pq.read_table("data/scan2/US/plan.parquet", columns=["item_id", "state"]).to_pydict()
res = []
for i, s in zip(plan["item_id"], plan["state"]):
    if i in ids:
        for ext in (".parquet", ".done.json"):
            p = Path(f"data/scan2/US/results/{s}/{i}{ext}")
            if p.exists():
                res.append(str(p))
outs = [str(p) for p in Path("out/national").rglob("*") if p.is_file()]
outs += [p for p in ("data/national/osm_phome.parquet",) if Path(p).exists()]
Path("/home/ubuntu/hh/pkg_outputs.txt").write_text("\n".join(outs + res) + "\n")
t = pq.read_table("out/national/candidates_us.parquet", columns=["id", "privacy", "p_home"])
t = t.filter(pc.and_(pc.equal(t.column("privacy"), "public"), pc.invert(pc.is_nan(t.column("p_home")))))
chips = [f"data/national/rescore_chips/{i}.jpg" for i in t.column("id").to_pylist()]
chips = [c for c in chips if Path(c).exists()]
Path("/home/ubuntu/hh/pkg_chips.txt").write_text("\n".join(chips) + "\n")
print(len(outs), "output files,", len(res), "rescanned result files,", len(chips), "public chips")
EOF
    du -ch $(cat ~/hh/pkg_outputs.txt) 2>/dev/null | tail -1
    echo "PACKAGE_DONE" ;;
  *)
    echo "unknown part $PART" >&2; exit 2 ;;
esac
