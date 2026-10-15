"""GPU ceiling of FastDetector (no reads): uint8 tiles already on the GPU or in pinned host memory.

Run ON the instance from ~/hh/pipeline:
    PYTHONPATH=. ~/hhvenv/bin/python cloud/bench_fastdet.py --weights weights/best_v1.pt --imgsz 320 416 --tta none lr
    PYTHONPATH=. ~/hhvenv/bin/python cloud/bench_fastdet.py --weights weights/best_v2.pt --imgsz 416 \
        --engine weights/v2_416_b512.engine
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from hoop_pipeline.common import setup_logging
from hoop_pipeline.fastdet import FastDetector


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", type=Path, required=True)
    ap.add_argument("--imgsz", type=int, nargs="+", default=[320, 416])
    ap.add_argument("--tta", nargs="+", default=["none"])
    ap.add_argument("--batch", type=int, default=512)
    ap.add_argument("--engine", type=Path)
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--out", type=Path, default=Path("out/bench_fastdet.json"))
    args = ap.parse_args()
    setup_logging(0)
    import torch

    res = []
    for sz in args.imgsz:
        for tta in args.tta:
            det = FastDetector(args.weights, imgsz=sz, engine=args.engine, tta=tta)
            for where in ("gpu", "pinned"):
                x = torch.randint(0, 255, (args.batch, 320, 320, 3), dtype=torch.uint8)
                x = x.cuda() if where == "gpu" else x.pin_memory()
                det.detect(x)
                torch.cuda.synchronize()
                n, t0 = 0, time.perf_counter()
                while time.perf_counter() - t0 < args.seconds:
                    out = det.detect(x, conf=0.05)
                    out.cpu()
                    n += args.batch
                el = time.perf_counter() - t0
                r = {"imgsz": sz, "tta": tta, "backend": det.info.backend, "input": where, "batch": args.batch,
                     "tiles_per_s": round(n / el, 1)}
                print(json.dumps(r), flush=True)
                res.append(r)
            del det
            torch.cuda.empty_cache()
    prev = json.loads(args.out.read_text()) if args.out.exists() else []
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(prev + res, indent=1))


if __name__ == "__main__":
    main()
