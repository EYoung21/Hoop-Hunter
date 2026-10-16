#!/usr/bin/env bash
# Run ON the v3 node (README "v3"), one part per job, detached:
#   HH_NODE=v3a bash cloud/start_stage.sh job <name> bash cloud/v3_node.sh PART [args]
#
#   all          everything below in order, unattended: osm, dataset, (prefetch + held-out scans of v1/v2 in the
#                background), train, held-out scan of v3, TN evals, rescore, package. Each finished part writes
#                ~/hh/logs/v3_<part>.ok, and `all` skips parts that already have one, so it can be restarted.
#   osm          Geofabrik US extract (md5-checked) + osm_extract -> data/osm_us/ (fresh courts + context layers)
#   dataset      build_dataset_us v2 negative mix + the v3 extra layer -> datasets/us3 (train_v3.txt, data_v3.yaml)
#   prefetch     cache a 192 m NAIP chip for every national candidate (rescore --fetch-only)
#   train        fine-tune v2 -> weights/best_v3.pt (HH_EPOCHS, HH_BATCH, HH_LR0)
#   heldout M    scan the held-out truth items with model M (v1 | v2 | v3), then merge -> out/v3/heldout/M/
#   tn_eval M    evaluate.py --fast on the Tennessee holdout chips (+ val_us) -> out/v3/eval_tn_M/, out/v3/eval_us_M/
#   rescore      v3 + verifier + context CNN on every cached candidate chip -> out/v3/rescore_v3.parquet
#   v3b          ablation: the same dataset relabelled with v2-style hard negatives (every detection >= 0.1 that is
#                not a likely court; data/v3/extra_courts_b.parquet, build_dataset_us --resume, so only labels and the
#                list change), trained like v3 -> weights/best_v3b.pt, then held-out scan + TN eval as "v3b"
#   package      file list of the outputs the PC pulls (~/hh/v3_outputs.txt)
#
# Inputs pushed from the PC (README "v3: commands"): weights best_v1/best_v2/verifier_v2/context_v2, data/v3/*,
# out/clean/{excluded,removed}_labels.txt, data/osm_us_1006/courts_us.parquet (the extract the truth set and the
# national candidates were matched against), data/scan2/US/plan.parquet, the NAIP catalog, data/truth/*.parquet.
#
# The whole script is one function called on its last line, so bash parses it completely before running anything
# and copying a new version over it while a part runs is safe (README, run_remote.sh gotcha).
main() {
set -uo pipefail
cd ~/hh/pipeline
PY=~/hhvenv/bin/python
export PYTHONPATH=~/hh/pipeline
PART="${1:?all|osm|dataset|prefetch|train|heldout|tn_eval|rescore|package}"
shift
LOGS=~/hh/logs
PLAN=data/scan2/US
step() { echo "== $* $(date -u +%FT%TZ)"; "$@" || { echo "STEP FAILED: $*"; return 1; }; }
done_mark() { date -u +%FT%TZ > "$LOGS/v3_$1.ok"; echo "V3_PART_DONE $1"; }
weights_of() { case "$1" in v1) echo "weights/best_v1.pt 320" ;; v2) echo "weights/best_v2.pt 416" ;; v3) echo "weights/best_v3.pt 416" ;;
                v3b) echo "weights/best_v3b.pt 416" ;; *) return 1 ;; esac; }
NEG_MIX="pitch_tennis=0.22,pitch_pickleball=0.08,pitch_volleyball=0.05,pitch_other=0.05,parking=0.12,pool=0.07,playground=0.04,park=0.09,school=0.09,residential=0.07,apartments=0.04,random_land=0.08"
gpu_mem_gb() { nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1 | awk '{print int($1/1024)}'; }

run_part() {
  local part="$1"; shift
  case "$part" in
    osm)
      step bash cloud/run_remote.sh pbf || return 1
      step bash cloud/run_remote.sh osm || return 1 ;;
    dataset)
      step $PY -m hoop_pipeline.build_dataset_us --out datasets/us3 --overwrite --workers "${HH_DATASET_WORKERS:-96}" \
        --neg-ratio 2.0 \
        --neg-mix "pitch_tennis=0.22,pitch_pickleball=0.08,pitch_volleyball=0.05,pitch_other=0.05,parking=0.12,pool=0.07,playground=0.04,park=0.09,school=0.09,residential=0.07,apartments=0.04,random_land=0.08" \
        --exclude-labels out/clean/excluded_labels.txt --remove-labels out/clean/removed_labels.txt \
        --extra-courts data/v3/extra_courts.parquet --holdout-zones data/v3/holdout_zones.geojson \
        --extra-offsets "${HH_EXTRA_OFFSETS:-3}" --extra-repeat "${HH_EXTRA_REPEAT:-4}" --hard-repeat 2 || return 1 ;;
    prefetch)
      step $PY -m hoop_pipeline.rescore --candidates data/v3/rescore_input.parquet --min-conf 0.1 --fetch-only \
        --chips-dir data/v3/rescore_chips --chunk 8192 --workers "${HH_FETCH_WORKERS:-128}" --out out/v3/unused.parquet || return 1 ;;
    train)
      local mem batch
      mem=$(gpu_mem_gb)
      batch="${HH_BATCH:-$([ "$mem" -ge 38 ] && echo 192 || echo 96)}"
      echo "GPU memory ${mem} GB -> batch $batch"
      step $PY -m hoop_pipeline.train --data datasets/us3/data_v3.yaml --model weights/best_v2.pt \
        --epochs "${HH_EPOCHS:-14}" --imgsz 416 --batch "$batch" --workers "${HH_TRAIN_WORKERS:-20}" \
        --patience "${HH_PATIENCE:-4}" --name us3-y11s-416 --copy-to weights/best_v3.pt \
        --set deterministic=False close_mosaic=3 optimizer=SGD lr0="${HH_LR0:-0.005}" warmup_epochs=1 || return 1 ;;
    heldout)
      local m="${1:?model v1|v2|v3}" w sz
      read -r w sz < <(weights_of "$m")
      [ -s "$w" ] || { echo "missing $w"; return 1; }
      local res=data/v3/heldout/$m/results
      for pass in 1 2 3; do
        timeout -k 60 3600 $PY -m hoop_pipeline.scan_engine run --plan "$PLAN" --manifest data/v3/heldout_items.txt \
          --results "$res" --weights "$w" --imgsz "$sz" --conf 0.05 --verifier weights/verifier_v2.pt --verify-min 0.05 \
          --threads "${HH_THREADS:-128}" --batch 256 || echo "heldout $m pass $pass exited with $?"
        P=$($PY -m hoop_pipeline.scan_engine status --plan "$PLAN" --results "$res" --manifest data/v3/heldout_items.txt \
            | $PY -c 'import json,sys; print(json.load(sys.stdin)["pending"])')
        echo "heldout $m pass $pass: $P pending"
        [ "$P" = "0" ] && break
      done
      step $PY -m hoop_pipeline.scan_engine merge --plan "$PLAN" --results "$res" --out "out/v3/heldout/$m" \
        --courts data/osm_us_1006/courts_us.parquet --v1-mask none || return 1 ;;
    tn_eval)
      local m="${1:?model v1|v2|v3}" w sz
      read -r w sz < <(weights_of "$m")
      step $PY -m hoop_pipeline.evaluate --weights "$w" --imgsz "$sz" --fast --data datasets/us3/data_tn.yaml --split val \
        --out "out/v3/eval_tn_$m" --batch 256 || return 1
      step $PY -m hoop_pipeline.evaluate --weights "$w" --imgsz "$sz" --fast --data datasets/us3/data_val_us.yaml \
        --split val --out "out/v3/eval_us_$m" --batch 256 --montage 12 || return 1 ;;
    rescore)
      step $PY -m hoop_pipeline.rescore --candidates data/v3/rescore_input.parquet --min-conf 0.1 \
        --weights weights/best_v3.pt --imgsz 416 --verifier weights/verifier_v2.pt --context-model weights/context_v2.pt \
        --chips-dir data/v3/rescore_chips --chunk 8192 --batch 512 --workers "${HH_FETCH_WORKERS:-128}" \
        --score-col conf3 --out out/v3/rescore_v3.parquet || return 1 ;;
    v3b)
      # keep v3's records, drop label files that still hold an official box rejected by the second snap review
      for f in manifest.csv.gz stats.json data_v3.yaml; do cp -n "datasets/us3/$f" "datasets/us3/v3a_$f"; done
      $PY - <<'EOF' || return 1
import csv, gzip
from pathlib import Path
gone = {l.strip() for l in open("data/v3/official_dropped_r2.txt") if l.strip()}
n = 0
for r in csv.DictReader(gzip.open("datasets/us3/v3a_manifest.csv.gz", "rt")):
    refs = set(r["label_refs"].split(";")) if r["label_refs"] else set()
    if refs & gone or r["source"] in gone:
        p = Path("datasets/us3/labels") / r["split"] / f"{r['name']}.txt"
        if p.exists():
            p.unlink()
            n += 1
print("stale label files removed:", n)
EOF
      step $PY -m hoop_pipeline.build_dataset_us --out datasets/us3 --resume --workers "${HH_DATASET_WORKERS:-96}" \
        --neg-ratio 2.0 --neg-mix "$NEG_MIX" \
        --exclude-labels out/clean/excluded_labels.txt --remove-labels out/clean/removed_labels.txt \
        --extra-courts data/v3/extra_courts_b.parquet --holdout-zones data/v3/holdout_zones.geojson \
        --extra-offsets 3 --extra-repeat 4 --hard-repeat 2 --train-list train_v3b.txt || return 1
      cp datasets/us3/data_v3.yaml datasets/us3/data_v3b.yaml
      cp datasets/us3/v3a_data_v3.yaml datasets/us3/data_v3.yaml
      for c in datasets/us3/labels/*.cache; do rm -f "$c"; done
      while [ ! -e "$LOGS/v3_rescore.ok" ]; do sleep 30; done   # the GPU is busy with the v3 rescore until then
      step $PY -m hoop_pipeline.train --data datasets/us3/data_v3b.yaml --model weights/best_v2.pt \
        --epochs "${HH_EPOCHS:-14}" --imgsz 416 --batch "${HH_BATCH:-192}" --workers "${HH_TRAIN_WORKERS:-20}" \
        --patience "${HH_PATIENCE:-4}" --name us3b-y11s-416 --copy-to weights/best_v3b.pt \
        --set deterministic=False close_mosaic=3 optimizer=SGD lr0="${HH_LR0:-0.005}" warmup_epochs=1 || return 1
      run_part heldout v3b || return 1
      run_part tn_eval v3b || return 1 ;;
    package)
      ls -d weights/best_v3.pt weights/best_v3.json runs/us3-y11s-416/results.csv runs/us3-y11s-416/args.yaml \
        runs/us3-y11s-416/results.png datasets/us3/stats.json datasets/us3/manifest.csv.gz datasets/us3/previews \
        weights/best_v3b.pt weights/best_v3b.json runs/us3b-y11s-416/results.csv runs/us3-y11s-416_r1/results.csv \
        datasets/us3/v3a_stats.json datasets/us3/v3a_manifest.csv.gz \
        out/v3 data/osm_us/summary.json 2>/dev/null | grep -v "out/v3/unused" > ~/hh/v3_outputs.txt
      cat ~/hh/v3_outputs.txt
      du -ch $(cat ~/hh/v3_outputs.txt) 2>/dev/null | tail -1 ;;
    *)
      echo "unknown part $part" >&2; return 2 ;;
  esac
}

if [ "$PART" != "all" ]; then
  run_part "$PART" "$@" && done_mark "${PART}${1:+_$1}" && exit 0
  exit 1
fi

# ---- all: the whole chain, skipping parts already marked done
once() { local name="$1"; shift; [ -e "$LOGS/v3_$name.ok" ] && { echo "skip $name (done)"; return 0; }; run_part "$@" && done_mark "$name"; }
sudo -n python3 /opt/hoophunter/watchdog_instance.py status 2>/dev/null | grep -E "reason|deadline" || true
once osm osm || exit 1
once dataset dataset || exit 1
# network-bound work alongside the GPU-bound training (skipped when already done or started as its own job)
running() { pgrep -f "$1" > /dev/null; }
PREFETCH=""
if [ ! -e "$LOGS/v3_prefetch.ok" ] && ! running "[f]etch-only"; then
  ( once prefetch prefetch > "$LOGS/v3_prefetch.log" 2>&1 ) &
  PREFETCH=$!
fi
HELD=""
if ! running "[s]can_engine run.*heldout"; then
  ( once heldout_v1 heldout v1 && once heldout_v2 heldout v2 ) > "$LOGS/v3_heldout_v1v2.log" 2>&1 &
  HELD=$!
fi
once train train || { echo "TRAIN FAILED"; wait; exit 1; }
[ -n "$HELD" ] && { wait "$HELD" || echo "held-out v1/v2 scans failed (see v3_heldout_v1v2.log)"; }
while running "[s]can_engine run.*heldout"; do sleep 30; done
once heldout_v3 heldout v3 || echo "held-out v3 scan failed"
for m in v1 v2 v3; do once "tn_eval_$m" tn_eval "$m" || echo "tn_eval $m failed"; done
[ -n "$PREFETCH" ] && { wait "$PREFETCH" || echo "prefetch exited non-zero; rescore fetches what is missing"; }
while running "[f]etch-only"; do sleep 30; done
once rescore rescore || { echo "RESCORE FAILED"; exit 1; }
run_part package
echo "V3_ALL_DONE $(date -u +%FT%TZ)"
}
main "$@"; exit $?
