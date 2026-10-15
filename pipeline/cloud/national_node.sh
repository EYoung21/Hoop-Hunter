#!/usr/bin/env bash
# Run ON a scan node, detached (cloud/national.sh up starts it as the "node" job):
#   bash cloud/national_node.sh PLAN_DIR MANIFEST NODE
# 1. setup (venv, TensorRT 10 + export deps; setup_remote.sh re-checks the watchdog first)
# 2. TensorRT FP16 engine for this node's GPU/driver (post_train_v2.sh engine: build + GPU ceiling benchmark),
#    then a smoke test; if either fails the scan runs PyTorch FP16 instead
# 3. the scan (national_scan.sh: retry passes + per-state packs)
set -uo pipefail
cd ~/hh/pipeline
PY=~/hhvenv/bin/python
export PYTHONPATH=~/hh/pipeline
PLAN="${1:?plan dir}"; MANIFEST="${2:?manifest}"; NODE="${3:?node name}"
echo "== setup $(date -u +%FT%TZ)"
HH_TRT_EXPORT=1 bash cloud/setup_remote.sh || { echo "SETUP FAILED"; exit 1; }
ENGINE=weights/v2_416_b512.engine
if [ "${HH_NO_TRT:-0}" != "1" ]; then
  echo "== engine $(date -u +%FT%TZ)"
  rm -f "$ENGINE"
  bash cloud/post_train_v2.sh engine || echo "engine build failed"
fi
ARGS=(--weights weights/best_v2.pt --imgsz 416 --threads "${HH_THREADS:-160}" --batch 512 --conf "${HH_CONF:-0.1}"
      --verifier weights/verifier_v2.pt --verify-min 0.1)
if [ -s "$ENGINE" ] && $PY - "$ENGINE" <<'EOF'
import sys
import numpy as np
import torch
from hoop_pipeline.fastdet import FastDetector
rng = np.random.default_rng(0)
x = torch.from_numpy(rng.integers(0, 255, (512, 320, 320, 3), dtype=np.uint8))
a = FastDetector("weights/best_v2.pt", imgsz=416, engine=sys.argv[1]).detect(x, conf=0.05)
b = FastDetector("weights/best_v2.pt", imgsz=416).detect(x, conf=0.05)
print("engine smoke test: trt", tuple(a.shape), "torch", tuple(b.shape))
assert abs(len(a) - len(b)) <= max(3, 0.05 * len(b)), "TensorRT and PyTorch disagree"
EOF
then
  ARGS+=(--engine "$ENGINE")
  echo "backend: tensorrt ($ENGINE)"
else
  echo "backend: torch-fp16 (TensorRT unavailable or failed its smoke test)"
fi
echo "== scan $(date -u +%FT%TZ): ${ARGS[*]}"
bash cloud/national_scan.sh "$PLAN" "$MANIFEST" "$NODE" "${ARGS[@]}"
