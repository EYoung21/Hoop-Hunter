#!/usr/bin/env bash
# Run ON the main instance after v2 training (weights/best_v2.pt), in parts so the scan can start early:
#   bash cloud/start_stage.sh job post_v2_engine bash cloud/post_train_v2.sh engine  # TensorRT engine + GPU ceiling
#   bash cloud/start_stage.sh job post_v2_ab bash cloud/post_train_v2.sh ab          # every evaluation (a then b)
#   a: TN holdout eval of v2 (+ flip TTA, + the TensorRT engine)   b: v1 evals, val_us, gold annotate, verifier,
#   v1-vs-v2 disagreement crops
set -uo pipefail
cd ~/hh/pipeline
PY=~/hhvenv/bin/python
export PYTHONPATH=.
D=datasets/us2
PART="${1:-a}"
if [ "$PART" = "engine" ]; then
  $PY - <<'EOF'
from pathlib import Path
from hoop_pipeline.common import setup_logging
from hoop_pipeline.fastdet import export_engine
setup_logging(0)
export_engine(Path("weights/best_v2.pt"), 416, 512, Path("weights/v2_416_b512.engine"), workspace_gb=8)
EOF
  rm -f weights/best_v2.onnx
  $PY cloud/bench_fastdet.py --weights weights/best_v2.pt --imgsz 416 --engine weights/v2_416_b512.engine --seconds 8 \
    --out out/bench_fastdet_v2.json
  $PY cloud/bench_fastdet.py --weights weights/best_v2.pt --imgsz 416 --seconds 8 --out out/bench_fastdet_v2.json
  echo POST_V2_ENGINE_DONE
  exit 0
fi
if [ "$PART" = "a" ] || [ "$PART" = "ab" ]; then
  $PY -m hoop_pipeline.evaluate --weights weights/best_v2.pt --data $D/data_tn.yaml --split val --imgsz 416 --fast --out out/eval_tn_v2
  $PY -m hoop_pipeline.evaluate --weights weights/best_v2.pt --data $D/data_tn.yaml --split val --imgsz 416 --fast --tta lr \
    --no-ultralytics-val --out out/eval_tn_v2_tta
  if [ -f weights/v2_416_b512.engine ]; then
    $PY -m hoop_pipeline.evaluate --weights weights/best_v2.pt --data $D/data_tn.yaml --split val --imgsz 416 --fast \
      --engine weights/v2_416_b512.engine --batch 512 --no-ultralytics-val --out out/eval_tn_v2_engine
  fi
  echo POST_V2A_DONE
fi
if [ "$PART" = "b" ] || [ "$PART" = "ab" ]; then
  $PY -m hoop_pipeline.evaluate --weights weights/best_v1.pt --data $D/data_tn.yaml --split val --imgsz 320 --fast --out out/eval_tn_v1
  $PY -m hoop_pipeline.evaluate --weights weights/best_v2.pt --data $D/data_val_us.yaml --split val --imgsz 416 --fast \
    --montage 12 --out out/eval_us_v2
  $PY -m hoop_pipeline.evaluate --weights weights/best_v1.pt --data $D/data_val_us.yaml --split val --imgsz 320 --fast \
    --montage 12 --out out/eval_us_v1
  $PY -m hoop_pipeline.gold annotate --weights weights/best_v2.pt --imgsz 416 --conf 0.15
  $PY -m hoop_pipeline.gold annotate --weights weights/best_v1.pt --imgsz 320 --conf 0.15 --preds-out data/gold/preds_v1.json
  $PY -m hoop_pipeline.gold export --tool out/label_tool --manifest $D/manifest.csv.gz datasets/us2_extra/manifest.csv.gz \
    --zip out/gold_label_data.zip
  $PY -m hoop_pipeline.cropcls eval-verifier --data $D --split val --weights weights/best_v2.pt --imgsz 416 \
    --verifier weights/verifier_v2.pt --out out/verifier_eval.json
  $PY cloud/disagree.py --a weights/best_v1.pt --a-imgsz 320 --a-thr 0.5 --b weights/best_v2.pt --b-imgsz 416 --b-thr 0.5 \
    --data $D --split val --max-crops 80 --out out/disagree_tn
  echo POST_V2B_DONE
fi
