"""Evaluate a court detector on a dataset split (default: the held-out state in data.yaml).

Reports Ultralytics mAP50 / mAP50-95 and, from our own box matching over the
same images, precision / recall / F1 at fixed confidence thresholds at IoU 0.5
(standard) and IoU 0.3 ("loose": OSM boxes around rotated or multi-court
polygons are loose, so a correct detection can miss IoU 0.5). It also counts
how often background-only chips fire, which is what drives false alarms in a
scan, and writes montages of false positives, misses and hits so the errors
can be looked at.

Example
-------
    python -m hoop_pipeline.evaluate --weights weights/best.pt --data datasets/us/data.yaml --out out/eval_tn
"""

from __future__ import annotations

import argparse
import logging
import random
import time
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from PIL import Image, ImageDraw

from . import config
from .common import add_common_args, setup_logging, write_json

log = logging.getLogger("evaluate")

THRESHOLDS = [0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9]


def iou_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    if not len(a) or not len(b):
        return np.zeros((len(a), len(b)))
    x0 = np.maximum(a[:, None, 0], b[None, :, 0])
    y0 = np.maximum(a[:, None, 1], b[None, :, 1])
    x1 = np.minimum(a[:, None, 2], b[None, :, 2])
    y1 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(x1 - x0, 0, None) * np.clip(y1 - y0, 0, None)
    area_a = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    area_b = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / np.maximum(area_a[:, None] + area_b[None, :] - inter, 1e-9)


def greedy_match(pred: np.ndarray, conf: np.ndarray, gt: np.ndarray, thr_iou: float) -> tuple[np.ndarray, np.ndarray]:
    """Return (pred_is_tp, gt_is_matched); predictions processed by descending confidence."""
    tp = np.zeros(len(pred), dtype=bool)
    matched = np.zeros(len(gt), dtype=bool)
    if not len(pred) or not len(gt):
        return tp, matched
    ious = iou_matrix(pred, gt)
    for i in np.argsort(-conf):
        cand = np.where(~matched & (ious[i] >= thr_iou))[0]
        if len(cand):
            j = cand[np.argmax(ious[i, cand])]
            matched[j] = True
            tp[i] = True
    return tp, matched


def load_gt(label_path: Path, size: int) -> np.ndarray:
    if not label_path.exists():
        return np.zeros((0, 4))
    rows = [ln.split() for ln in label_path.read_text().splitlines() if ln.strip()]
    if not rows:
        return np.zeros((0, 4))
    arr = np.array([[float(v) for v in r[1:5]] for r in rows])
    xc, yc, w, h = arr.T * size
    return np.column_stack([xc - w / 2, yc - h / 2, xc + w / 2, yc + h / 2])


def predict_split(model: Any, images: list[Path], imgsz: int, batch: int, device: str, conf: float) -> list[dict[str, Any]]:
    out = []
    for k in range(0, len(images), batch):
        chunk = images[k:k + batch]
        res = model.predict([str(p) for p in chunk], imgsz=imgsz, conf=conf, iou=0.5, device=device, verbose=False,
                            batch=len(chunk))
        for p, r in zip(chunk, res):
            b = r.boxes
            out.append({"path": p, "xyxy": b.xyxy.cpu().numpy() if len(b) else np.zeros((0, 4)),
                        "conf": b.conf.cpu().numpy() if len(b) else np.zeros(0)})
    return out


def pr_table(preds: list[dict[str, Any]], gts: list[np.ndarray], thresholds: list[float], thr_iou: float) -> list[dict[str, Any]]:
    rows = []
    for t in thresholds:
        tp = fp = fn = 0
        bg_imgs = bg_fire = 0
        for p, g in zip(preds, gts):
            keep = p["conf"] >= t
            pt, gm = greedy_match(p["xyxy"][keep], p["conf"][keep], g, thr_iou)
            tp += int(pt.sum())
            fp += int((~pt).sum())
            fn += int((~gm).sum())
            if not len(g):
                bg_imgs += 1
                bg_fire += int(keep.any())
        prec = tp / max(tp + fp, 1)
        rec = tp / max(tp + fn, 1)
        rows.append({"conf": t, "tp": tp, "fp": fp, "fn": fn, "precision": round(prec, 4), "recall": round(rec, 4),
                     "f1": round(2 * prec * rec / max(prec + rec, 1e-9), 4),
                     "background_chips_firing": round(bg_fire / max(bg_imgs, 1), 4)})
    return rows


def draw(path: Path, gt: np.ndarray, pred: np.ndarray, conf: np.ndarray, title: str) -> Image.Image:
    im = Image.open(path).convert("RGB")
    d = ImageDraw.Draw(im)
    for x0, y0, x1, y1 in gt:
        d.rectangle([x0, y0, x1, y1], outline=(0, 255, 0), width=2)
    for (x0, y0, x1, y1), c in zip(pred, conf):
        d.rectangle([x0, y0, x1, y1], outline=(255, 0, 255), width=2)
        d.text((x0 + 2, max(0, y0 - 11)), f"{c:.2f}", fill=(255, 0, 255))
    d.rectangle([0, 0, im.width, 13], fill=(0, 0, 0))
    d.text((3, 1), title[:58], fill=(255, 255, 0))
    return im


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--weights", type=Path, default=config.DEFAULT_WEIGHTS)
    ap.add_argument("--data", type=Path, default=config.DATASETS_DIR / "us" / "data.yaml")
    ap.add_argument("--split", default="val")
    ap.add_argument("--out", type=Path, default=config.OUT_DIR / "eval_tn")
    ap.add_argument("--imgsz", type=int, default=config.CHIP_SIZE_PX)
    ap.add_argument("--batch", type=int, default=128)
    ap.add_argument("--device")
    ap.add_argument("--recommend-precision", type=float, default=0.85,
                    help="recommended threshold = lowest conf whose precision (IoU 0.3) reaches this")
    ap.add_argument("--montage", type=int, default=24, help="images per error montage")
    ap.add_argument("--no-ultralytics-val", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    add_common_args(ap)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    import torch

    config.init_ultralytics()
    from ultralytics import YOLO

    from .build_dataset_us import montage

    t0 = time.monotonic()
    device = args.device or ("0" if torch.cuda.is_available() else "cpu")
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_load(args.data.read_text(encoding="utf-8"))
    root = Path(data["path"])
    img_dir = root / data[args.split]
    images = sorted(p for p in img_dir.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    label_dir = root / "labels" / Path(data[args.split]).name  # images/<split> <-> labels/<split>
    model = YOLO(str(args.weights))
    summary: dict[str, Any] = {"weights": str(args.weights), "data": str(args.data), "split": args.split,
                               "images": len(images)}

    if not args.no_ultralytics_val:
        m = model.val(data=str(args.data), split=args.split, imgsz=args.imgsz, batch=args.batch, device=device,
                      conf=0.001, iou=0.6, plots=True, project=str(out), name="ultralytics_val", exist_ok=True,
                      verbose=False)
        box = m.box
        px = np.asarray(box.px) if hasattr(box, "px") else np.linspace(0, 1, 1000)
        p_curve = np.asarray(box.p_curve)[0] if len(getattr(box, "p_curve", [])) else None
        r_curve = np.asarray(box.r_curve)[0] if len(getattr(box, "r_curve", [])) else None
        summary["ultralytics"] = {"mAP50": round(float(box.map50), 4), "mAP50_95": round(float(box.map), 4),
                                  "precision": round(float(box.mp), 4), "recall": round(float(box.mr), 4)}
        if p_curve is not None and r_curve is not None:
            summary["ultralytics"]["pr_at_conf"] = {
                f"{t:.2f}": {"precision": round(float(np.interp(t, px, p_curve)), 4),
                             "recall": round(float(np.interp(t, px, r_curve)), 4)} for t in THRESHOLDS}
        log.info("ultralytics %s: %s", args.split, {k: v for k, v in summary["ultralytics"].items() if k != "pr_at_conf"})

    preds = predict_split(model, images, args.imgsz, args.batch, device, conf=min(THRESHOLDS))
    gts = [load_gt(label_dir / (p["path"].stem + ".txt"), args.imgsz) for p in preds]
    strict = pr_table(preds, gts, THRESHOLDS, 0.5)
    loose = pr_table(preds, gts, THRESHOLDS, 0.3)
    summary["pr_iou50"] = strict
    summary["pr_iou30"] = loose
    best_f1 = max(strict, key=lambda r: r["f1"])
    reach = [r for r in loose if r["precision"] >= args.recommend_precision]
    rec = min(reach, key=lambda r: r["conf"]) if reach else max(loose, key=lambda r: r["f1"])
    summary["best_f1_iou50"] = best_f1
    summary["recommended"] = {"conf": rec["conf"], "rule": f"lowest conf with precision(IoU0.3) >= {args.recommend_precision}",
                              "iou30": rec, "iou50": next(r for r in strict if r["conf"] == rec["conf"])}
    n_gt = sum(len(g) for g in gts)
    summary["gt_boxes"] = n_gt
    summary["background_chips"] = sum(1 for g in gts if not len(g))
    log.info("%-6s %-22s %-22s %s", "conf", "IoU0.5 P/R/F1", "IoU0.3 P/R/F1", "bg chips firing")
    for s, lo in zip(strict, loose):
        log.info("%-6.2f %.3f/%.3f/%.3f    %.3f/%.3f/%.3f    %.3f", s["conf"], s["precision"], s["recall"], s["f1"],
                 lo["precision"], lo["recall"], lo["f1"], s["background_chips_firing"])
    log.info("recommended threshold %.2f (%s)", rec["conf"], summary["recommended"]["rule"])

    # ---- error montages at the recommended threshold (IoU 0.3 matching)
    t = rec["conf"]
    fps, fns, tps = [], [], []
    for p, g in zip(preds, gts):
        keep = p["conf"] >= t
        xy, cf = p["xyxy"][keep], p["conf"][keep]
        pt, gm = greedy_match(xy, cf, g, 0.3)
        for k in np.flatnonzero(~pt):
            fps.append((float(cf[k]), p["path"], g, xy, cf))
        if (~gm).any():
            fns.append((float((~gm).sum()), p["path"], g, xy, cf))
        if pt.any():
            tps.append((float(cf[pt].max()), p["path"], g, xy, cf))
    rng = random.Random(args.seed)
    fps.sort(key=lambda r: -r[0])
    seen: set[Path] = set()
    fp_rows = []
    for r in fps:
        if r[1] not in seen:
            seen.add(r[1])
            fp_rows.append(r)
    for name, rows in (("false_positives", fp_rows[: args.montage]),
                       ("misses", rng.sample(fns, min(len(fns), args.montage))),
                       ("hits", rng.sample(tps, min(len(tps), args.montage)))):
        ims = [draw(path, g, xy, cf, f"{path.stem} max {score:.2f}" if name != "misses" else f"{path.stem} missed {int(score)}")
               for score, path, g, xy, cf in rows]
        for start in range(0, len(ims), 12):
            montage(ims[start:start + 12], 4, out / f"{name}_{start // 12:02d}.jpg")
    summary["counts_at_recommended"] = {"false_positive_boxes": len(fps), "images_with_misses": len(fns),
                                        "images_with_hits": len(tps)}
    summary["seconds"] = round(time.monotonic() - t0, 1)
    write_json(out / "metrics.json", summary)
    log.info("wrote %s", out / "metrics.json")


if __name__ == "__main__":
    main()
