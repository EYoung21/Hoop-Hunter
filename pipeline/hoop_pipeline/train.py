"""Train a small Ultralytics YOLO court detector on a build_dataset output.

CPU works (slowly); a CUDA GPU is used automatically when torch sees one.
The best checkpoint is copied to ``weights/best.pt`` (what ``scan`` uses by
default) together with ``weights/best.json`` describing how it was trained.

Examples
--------
    # smoke test (minutes on CPU, useless model)
    python -m hoop_pipeline.train --data datasets/courts_smoke/data.yaml --epochs 2 --imgsz 320 --batch 8

    # real run on a GPU
    python -m hoop_pipeline.train --data datasets/courts/data.yaml --epochs 100 --imgsz 640 --batch 32 --device 0

Aerial-specific augmentation defaults: vertical + horizontal flips (top-down
imagery has no "up"), little scale jitter (every chip is resampled to the same
0.6 m/px, so object scale is known), no rotation (it would inflate axis-aligned
boxes; use an OBB model for rotation, see README).
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from . import config
from .common import add_common_args, setup_logging, write_json

log = logging.getLogger("train")


def resolve_model(model: str) -> str:
    """Keep downloaded base weights in weights/ instead of the current directory."""
    p = Path(model)
    if p.exists() or p.parent != Path("."):
        return str(p)
    config.WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    return str(config.WEIGHTS_DIR / p.name)


def pick_device(requested: str | None) -> str:
    import torch

    if requested:
        return requested
    if torch.cuda.is_available():
        log.info("CUDA available: %s", torch.cuda.get_device_name(0))
        return "0"
    log.info("No CUDA device visible to torch (torch %s); training on CPU", torch.__version__)
    return "cpu"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=config.DATASETS_DIR / "courts" / "data.yaml")
    ap.add_argument("--model", default=config.DEFAULT_MODEL,
                    help="base weights (yolo11n.pt, yolov8n.pt, yolo11s.pt, ...) or a .yaml to train from scratch")
    ap.add_argument("--epochs", type=int, default=100)
    ap.add_argument("--imgsz", type=int, default=config.CHIP_SIZE_PX)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--device", help="'cpu', '0', '0,1' ... (default: GPU 0 if available else cpu)")
    ap.add_argument("--workers", type=int, default=2, help="dataloader workers (keep low on Windows)")
    ap.add_argument("--patience", type=int, default=25, help="early-stopping patience (epochs)")
    ap.add_argument("--name", default=None, help="run name under runs/ (default: courts-<timestamp>)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cache", choices=["ram", "disk", "none"], default="none")
    ap.add_argument("--fraction", type=float, default=1.0, help="train on this fraction of the train split")
    ap.add_argument("--no-copy", action="store_true", help="don't copy best.pt to weights/best.pt")
    ap.add_argument("--set", nargs="*", default=[], metavar="KEY=VALUE",
                    help="extra Ultralytics train args, e.g. --set deterministic=False close_mosaic=5 (YAML values)")
    add_common_args(ap)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)

    if not args.data.exists():
        raise SystemExit(f"{args.data} not found; run build_dataset first")

    config.init_ultralytics()
    from ultralytics import YOLO  # heavy import; keep --help fast

    device = pick_device(args.device)
    name = args.name or "courts-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    model_path = resolve_model(args.model)
    log.info("training %s on %s for %d epochs, imgsz=%d, batch=%d, device=%s",
             model_path, args.data, args.epochs, args.imgsz, args.batch, device)

    t0 = time.monotonic()
    model = YOLO(model_path)
    train_kwargs: dict[str, Any] = dict(
        data=str(args.data.resolve()),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=device,
        workers=args.workers,
        project=str(config.RUNS_DIR),
        name=name,
        exist_ok=True,
        seed=args.seed,
        patience=args.patience,
        fraction=args.fraction,
        cache=False if args.cache == "none" else args.cache,
        amp=device != "cpu",
        plots=True,
        # aerial augmentation
        fliplr=0.5,
        flipud=0.5,
        degrees=0.0,
        scale=0.2,
        translate=0.1,
        mosaic=1.0,
        hsv_h=0.01,
        hsv_s=0.5,
        hsv_v=0.3,
    )
    if args.set:
        import yaml

        for item in args.set:
            key, _, value = item.partition("=")
            train_kwargs[key.strip()] = yaml.safe_load(value)
        log.info("extra train args: %s", {k: train_kwargs[k] for k in (s.partition("=")[0].strip() for s in args.set)})
    model.train(**train_kwargs)
    elapsed = time.monotonic() - t0

    trainer = model.trainer
    if trainer is None:
        raise RuntimeError("Ultralytics returned no trainer; training did not run")
    best = Path(trainer.best) if Path(trainer.best).exists() else Path(trainer.last)
    metrics = {k: round(float(v), 4) for k, v in (trainer.metrics or {}).items()}
    log.info("finished in %.1f min; best weights %s", elapsed / 60, best)
    log.info("val metrics: %s", metrics)

    summary = {
        "weights": str(best),
        "run_dir": str(trainer.save_dir),
        "data": str(args.data.resolve()),
        "base_model": args.model,
        "epochs": args.epochs,
        "imgsz": args.imgsz,
        "batch": args.batch,
        "device": device,
        "extra_args": args.set,
        "seconds": round(elapsed, 1),
        "metrics": metrics,
        "trained": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    if not args.no_copy:
        config.WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(best, config.DEFAULT_WEIGHTS)
        write_json(config.DEFAULT_WEIGHTS.with_suffix(".json"), summary)
        log.info("copied to %s", config.DEFAULT_WEIGHTS)
    write_json(Path(trainer.save_dir) / "hoop_summary.json", summary)


if __name__ == "__main__":
    main()
