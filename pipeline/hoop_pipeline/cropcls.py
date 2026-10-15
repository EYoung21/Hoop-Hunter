"""Small crop classifiers (torchvision ResNet-18) used as second stages.

Two uses, one trainer:

* **verifier** (``mode=box``): is this detection a basketball court? The crop is the
  detection box grown by ``context`` (1.6x, at least ``min_side_px``), made square and
  resampled to ``input`` px with ``roi_align`` from the *same* preprocessed tensor the
  detector saw, so training crops and in-scan crops are produced identically
  (``crop_boxes``). Training data comes from dataset chips: label boxes (jittered) are
  positives, detector boxes that match no label are negatives (``verifier-data``).
* **context** (``mode=point``): is this court on a single-family home lot? A wide
  ``point_m`` (160 m) square around the court, read from NAIP, resized to ``input`` px
  (``context-data`` in privacy.py builds it from OSM-labelled courts).

``train`` keeps all crops in RAM as uint8, augments on the GPU (flips, 90-degree
rotations, brightness/contrast), and selects the epoch with the best validation AUC.
The saved ``.pt`` holds the state dict plus ``meta`` (mode, input size, context, mean/std,
validation metrics), which ``CropClassifier.load`` uses.

Example
-------
    python -m hoop_pipeline.cropcls verifier-data --data datasets/us2 --preds out/clean/preds_v1.parquet \\
        --out data/crops/verifier.npz
    python -m hoop_pipeline.cropcls train --crops data/crops/verifier.npz --out weights/verifier_v2.pt
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import time
from pathlib import Path
from typing import Any

import numpy as np

from . import config
from .common import add_common_args, setup_logging, write_json

log = logging.getLogger("cropcls")
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)
DEFAULT_META: dict[str, Any] = {"arch": "resnet18", "mode": "box", "input": 112, "context": 1.6, "min_side_px": 40.0,
                                "point_m": 160.0}


# --------------------------------------------------------------------------- crops
def box_rois(dets: Any, tile_px: int, img_px: int, context: float, min_side_px: float) -> Any:
    """(K, 6) detections [tile, x0, y0, x1, y1, ...] in tile px -> (K, 5) square ROIs in model-input px."""
    torch = __import__("torch")
    s = img_px / float(tile_px)
    cx = (dets[:, 1] + dets[:, 3]) / 2 * s
    cy = (dets[:, 2] + dets[:, 4]) / 2 * s
    side = torch.maximum((dets[:, 3] - dets[:, 1]), (dets[:, 4] - dets[:, 2])) * context
    side = torch.clamp(side, min=min_side_px) * s
    return torch.stack([dets[:, 0], cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2], 1).float()


def crop_boxes(x_pre: Any, dets: Any, tile_px: int, meta: dict[str, Any]) -> Any:
    """Crops (K, 3, input, input) float in [0, 1] around detections, from the detector's input tensor."""
    import torchvision

    rois = box_rois(dets, tile_px, int(x_pre.shape[-1]), float(meta["context"]), float(meta["min_side_px"]))
    n = int(meta["input"])
    return torchvision.ops.roi_align(x_pre.float(), rois, output_size=(n, n), spatial_scale=1.0, sampling_ratio=2,
                                     aligned=True)


def build_model(arch: str, pretrained: bool = True) -> Any:
    import torch
    import torchvision

    if arch == "resnet18":
        w = torchvision.models.ResNet18_Weights.IMAGENET1K_V1 if pretrained else None
        m = torchvision.models.resnet18(weights=w)
    elif arch == "resnet34":
        w = torchvision.models.ResNet34_Weights.IMAGENET1K_V1 if pretrained else None
        m = torchvision.models.resnet34(weights=w)
    else:
        raise ValueError(f"unknown arch {arch}")
    m.fc = torch.nn.Linear(m.fc.in_features, 1)
    return m


class CropClassifier:
    def __init__(self, model: Any, meta: dict[str, Any], device: Any) -> None:
        import torch

        self.torch = torch
        self.model = model.to(device).eval().half() if device.type == "cuda" else model.to(device).eval()
        self.meta = meta
        self.device = device
        self.mean = torch.tensor(meta.get("mean", IMAGENET_MEAN), device=device).view(1, 3, 1, 1)
        self.std = torch.tensor(meta.get("std", IMAGENET_STD), device=device).view(1, 3, 1, 1)

    @classmethod
    def load(cls, path: str | Path, device: Any = None) -> CropClassifier:
        import torch

        device = device or torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        ckpt = torch.load(str(path), map_location="cpu", weights_only=False)
        meta = {**DEFAULT_META, **ckpt["meta"]}
        m = build_model(meta["arch"], pretrained=False)
        m.load_state_dict(ckpt["state_dict"])
        return cls(m, meta, device)

    def _forward(self, x: Any) -> Any:
        torch = self.torch
        x = (x - self.mean) / self.std
        with torch.inference_mode():
            y = self.model(x.half() if self.device.type == "cuda" else x)
        return torch.sigmoid(y.float()[:, 0])

    def score_boxes(self, x_pre: Any, dets: Any, tile_px: int, batch: int = 1024) -> Any:
        """Verifier probability per detection; x_pre is the detector input (N, 3, S, S) in [0, 1]."""
        torch = self.torch
        outs = []
        for k in range(0, len(dets), batch):
            outs.append(self._forward(crop_boxes(x_pre, dets[k:k + batch].float(), tile_px, self.meta)))
        return torch.cat(outs) if outs else torch.zeros(0, device=self.device)

    def score_images(self, imgs: np.ndarray, batch: int = 256) -> np.ndarray:
        """Probability per uint8 (K, H, W, 3) image (context crops), resized to the model input."""
        torch = self.torch
        n = int(self.meta["input"])
        out = []
        for k in range(0, len(imgs), batch):
            x = torch.from_numpy(np.ascontiguousarray(imgs[k:k + batch])).to(self.device).permute(0, 3, 1, 2).float() / 255
            if x.shape[-1] != n:
                x = torch.nn.functional.interpolate(x, size=(n, n), mode="bilinear", align_corners=False, antialias=True)
            out.append(self._forward(x).cpu().numpy())
        return np.concatenate(out) if out else np.zeros(0)


# --------------------------------------------------------------------------- verifier data
def cmd_verifier_data(args: argparse.Namespace) -> None:
    """Crops around label boxes (positives) and unmatched detector boxes (negatives) from dataset chips."""
    import pyarrow.parquet as pq
    import torch

    from .evaluate import iou_matrix
    from .label_clean import iter_images, load_label_boxes

    meta = {**DEFAULT_META, "mode": "box", "input": args.input, "context": args.context, "min_side_px": args.min_side_px,
            "imgsz": args.imgsz}
    preds = pq.read_table(args.preds).to_pydict()
    by_chip: dict[str, list[int]] = {}
    for i, c in enumerate(preds["chip"]):
        by_chip.setdefault(c, []).append(i)
    pbox = np.column_stack([np.asarray(preds[k], dtype=float) for k in ("x0", "y0", "x1", "y1")])
    if not preds["chip"]:
        pbox = np.zeros((0, 4))
    pconf = np.asarray(preds["conf"], dtype=float)
    rng = random.Random(args.seed)
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    crops: list[np.ndarray] = []
    labels: list[int] = []
    splits: list[str] = []
    confs: list[float] = []
    kinds: list[str] = []
    t0 = time.monotonic()
    for split in args.splits:
        paths = sorted((args.data / "images" / split).glob("*.jpg"))
        if args.max_chips and len(paths) > args.max_chips:
            paths = sorted(rng.sample(paths, args.max_chips))
        for chunk, arr in iter_images(paths, 256, 24):
            rows = []  # (chip index in batch, x0, y0, x1, y1, label, conf, kind)
            for bi, p in enumerate(chunk):
                gt = load_label_boxes(args.data / "labels" / split / f"{p.stem}.txt", args.tile)
                di = by_chip.get(p.stem, [])
                db = pbox[di] if di else np.zeros((0, 4))
                dc = pconf[di] if di else np.zeros(0)
                for g in gt:
                    if (g[2] - g[0]) < 6 or (g[3] - g[1]) < 6:
                        continue
                    for _ in range(args.pos_jitter_copies):
                        w, h = g[2] - g[0], g[3] - g[1]
                        dx, dy = rng.uniform(-0.12, 0.12) * w, rng.uniform(-0.12, 0.12) * h
                        sc = rng.uniform(0.85, 1.15)
                        cx, cy = (g[0] + g[2]) / 2 + dx, (g[1] + g[3]) / 2 + dy
                        rows.append((bi, cx - w * sc / 2, cy - h * sc / 2, cx + w * sc / 2, cy + h * sc / 2, 1, -1.0, "gt"))
                if len(db):
                    iou = iou_matrix(db, gt) if len(gt) else np.zeros((len(db), 0))
                    best = iou.max(1) if iou.shape[1] else np.zeros(len(db))
                    for k in np.flatnonzero(dc >= args.min_conf):
                        if best[k] >= 0.5:
                            rows.append((bi, *db[k], 1, float(dc[k]), "tp"))
                        elif best[k] < 0.1:
                            rows.append((bi, *db[k], 0, float(dc[k]), "fp"))
            if not rows:
                continue
            x = torch.from_numpy(arr).to(device).permute(0, 3, 1, 2).float() / 255
            if args.imgsz != args.tile:
                x = torch.nn.functional.interpolate(x, size=(args.imgsz, args.imgsz), mode="bilinear", align_corners=False)
            d = torch.tensor([[r[0], r[1], r[2], r[3], r[4], 0.0] for r in rows], dtype=torch.float32, device=device)
            c = crop_boxes(x, d, args.tile, meta)
            u8 = (c.clamp(0, 1) * 255).round().to(torch.uint8).permute(0, 2, 3, 1).cpu().numpy()
            crops.append(u8)
            labels += [r[5] for r in rows]
            confs += [r[6] for r in rows]
            kinds += [r[7] for r in rows]
            splits += [split] * len(rows)
        log.info("%s: %d crops so far (%.0f s)", split, len(labels), time.monotonic() - t0)
    X = np.concatenate(crops) if crops else np.zeros((0, args.input, args.input, 3), np.uint8)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    np.savez(args.out, x=X, y=np.array(labels, np.int8), split=np.array(splits), conf=np.array(confs, np.float32),
             kind=np.array(kinds), meta=json.dumps(meta))
    log.info("wrote %s: %d crops (%d positive, %d negative)", args.out, len(labels), int(np.sum(labels)),
             len(labels) - int(np.sum(labels)))


# --------------------------------------------------------------------------- training
def auc(y: np.ndarray, p: np.ndarray) -> float:
    order = np.argsort(p)
    ranks = np.empty(len(p))
    ranks[order] = np.arange(1, len(p) + 1)
    pos = y == 1
    n_pos, n_neg = int(pos.sum()), int((~pos).sum())
    if not n_pos or not n_neg:
        return float("nan")
    return float((ranks[pos].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def cmd_train(args: argparse.Namespace) -> None:
    import torch

    t0 = time.monotonic()
    z = np.load(args.crops, allow_pickle=False)
    X, y, split = z["x"], z["y"].astype(np.float32), z["split"].astype(str)
    meta = {**DEFAULT_META, **json.loads(str(z["meta"]))}
    meta["arch"] = args.arch
    tr = np.isin(split, args.train_splits)
    va = np.isin(split, args.val_splits)
    log.info("crops %s: train %d (%.1f%% pos), val %d (%.1f%% pos)", X.shape, int(tr.sum()), 100 * y[tr].mean(),
             int(va.sum()), 100 * y[va].mean() if va.any() else float("nan"))
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    model = build_model(args.arch).to(device).to(memory_format=torch.channels_last)
    n = int(meta["input"])
    mean = torch.tensor(IMAGENET_MEAN, device=device).view(1, 3, 1, 1)
    std = torch.tensor(IMAGENET_STD, device=device).view(1, 3, 1, 1)
    Xt = torch.from_numpy(X)  # uint8 in host RAM
    yt = torch.from_numpy(y)
    tr_idx = np.flatnonzero(tr)
    va_idx = np.flatnonzero(va)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    steps = args.epochs * max(1, len(tr_idx) // args.batch)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=args.lr, total_steps=steps, pct_start=0.15)
    pos_w = torch.tensor([(1 - y[tr].mean()) / max(1e-6, y[tr].mean())], device=device) if args.balance else None
    lossf = torch.nn.BCEWithLogitsLoss(pos_weight=pos_w)
    scaler = torch.amp.GradScaler("cuda", enabled=device.type == "cuda")

    def prep(idx: np.ndarray, train: bool) -> Any:
        x = Xt[idx].to(device, non_blocking=True).permute(0, 3, 1, 2).float() / 255
        if x.shape[-1] != n:
            x = torch.nn.functional.interpolate(x, size=(n, n), mode="bilinear", align_corners=False, antialias=True)
        if train:
            if random.random() < 0.5:
                x = torch.flip(x, (3,))
            if random.random() < 0.5:
                x = torch.flip(x, (2,))
            k = random.randint(0, 3)
            if k:
                x = torch.rot90(x, k, (2, 3))
            b = (torch.rand(len(x), 1, 1, 1, device=device) - 0.5) * 0.2
            c = 1 + (torch.rand(len(x), 1, 1, 1, device=device) - 0.5) * 0.3
            x = ((x - 0.5) * c + 0.5 + b).clamp(0, 1)
        return ((x - mean) / std).contiguous(memory_format=torch.channels_last)

    def predict(idx: np.ndarray) -> np.ndarray:
        model.eval()
        out = []
        with torch.inference_mode(), torch.autocast(device.type, dtype=torch.float16, enabled=device.type == "cuda"):
            for k in range(0, len(idx), 1024):
                out.append(torch.sigmoid(model(prep(idx[k:k + 1024], False)).float()[:, 0]).cpu().numpy())
        model.train()
        return np.concatenate(out) if out else np.zeros(0)

    best_auc, best_state, hist = -1.0, None, []
    for ep in range(args.epochs):
        model.train()
        perm = np.random.default_rng(args.seed + ep).permutation(tr_idx)
        tl, nb = 0.0, 0
        for k in range(0, len(perm) - args.batch + 1, args.batch):
            idx = perm[k:k + args.batch]
            x = prep(idx, True)
            t = yt[idx].to(device)
            with torch.autocast(device.type, dtype=torch.float16, enabled=device.type == "cuda"):
                loss = lossf(model(x)[:, 0].float(), t)
            opt.zero_grad(set_to_none=True)
            scaler.scale(loss).backward()
            scaler.step(opt)
            scaler.update()
            sched.step()
            tl += float(loss)
            nb += 1
        pv = predict(va_idx) if len(va_idx) else np.zeros(0)
        a = auc(y[va_idx], pv) if len(va_idx) else float("nan")
        hist.append({"epoch": ep + 1, "loss": round(tl / max(nb, 1), 4), "val_auc": round(a, 4)})
        log.info("epoch %d: loss %.4f val AUC %.4f (%.0f s)", ep + 1, tl / max(nb, 1), a, time.monotonic() - t0)
        if not np.isfinite(a) or a > best_auc:
            best_auc = a
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
    meta.update({"val_auc": round(best_auc, 4), "history": hist, "train_crops": int(len(tr_idx)), "val_crops": int(len(va_idx)),
                 "mean": IMAGENET_MEAN, "std": IMAGENET_STD, "trained_seconds": round(time.monotonic() - t0, 1)})
    args.out.parent.mkdir(parents=True, exist_ok=True)
    torch.save({"state_dict": best_state, "meta": meta}, str(args.out))
    write_json(args.out.with_suffix(".json"), meta)
    log.info("saved %s (val AUC %.4f)", args.out, best_auc)


# --------------------------------------------------------------------------- evaluation
def pr_at_recall(score: np.ndarray, tp: np.ndarray, n_gt: int, recalls: list[float]) -> dict[str, Any]:
    """Precision (and the score threshold) at which each target recall is first reached."""
    order = np.argsort(-score)
    ctp = np.cumsum(tp[order])
    cfp = np.cumsum(~tp[order])
    rec = ctp / max(1, n_gt)
    prec = ctp / np.maximum(1, ctp + cfp)
    out = {}
    for r in recalls:
        k = np.flatnonzero(rec >= r)
        if len(k):
            out[f"{r:.2f}"] = {"precision": round(float(prec[k[0]]), 4), "threshold": round(float(score[order][k[0]]), 4)}
    return out


def cmd_eval_verifier(args: argparse.Namespace) -> None:
    """Detector alone vs detector x verifier on a labelled split (default the held-out state)."""
    import torch

    from .evaluate import greedy_match
    from .fastdet import FastDetector
    from .label_clean import iter_images, load_label_boxes

    det = FastDetector(args.weights, imgsz=args.imgsz)
    ver = CropClassifier.load(args.verifier, device=det.device)
    paths = sorted((args.data / "images" / args.split).glob("*.jpg"))
    confs: list[float] = []
    vers: list[float] = []
    tps: list[bool] = []
    n_gt = 0
    for chunk, arr in iter_images(paths, 256, 16):
        x = torch.from_numpy(arr)
        xp = det.preprocess(x)
        d = det.detect(x, conf=args.conf, x_pre=xp)
        v = ver.score_boxes(xp, d, tile_px=arr.shape[1]).cpu().numpy() if len(d) else np.zeros(0)
        d = d.cpu().numpy()
        for i, p in enumerate(chunk):
            gt = load_label_boxes(args.data / "labels" / args.split / f"{p.stem}.txt", arr.shape[1])
            n_gt += len(gt)
            m = d[:, 0] == i
            tp, _ = greedy_match(d[m, 1:5], d[m, 5], gt, 0.3)
            confs += d[m, 5].tolist()
            vers += v[m].tolist()
            tps += tp.tolist()
    c, vv, t = np.array(confs), np.array(vers), np.array(tps, bool)
    recalls = [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8]
    res: dict[str, Any] = {"weights": str(args.weights), "verifier": str(args.verifier), "split": args.split,
                           "gt_boxes": n_gt, "detections": len(c), "note": "IoU 0.3 matching against OSM labels"}
    res["detector"] = pr_at_recall(c, t, n_gt, recalls)
    for w in (0.5, 1.0, 2.0):
        res[f"conf_x_verify^{w}"] = pr_at_recall(c * np.power(np.clip(vv, 1e-6, 1), w), t, n_gt, recalls)
    write_json(args.out, res)
    log.info("verifier eval: %s", json.dumps(res))


def cmd_eval_crops(args: argparse.Namespace) -> None:
    """AUC and precision / recall of the positive class at thresholds, on one split of a crops file."""
    z = np.load(args.crops, allow_pickle=False)
    m = np.isin(z["split"].astype(str), args.splits)
    clf = CropClassifier.load(args.weights)
    p = clf.score_images(z["x"][m])
    y = z["y"][m].astype(int)
    res: dict[str, Any] = {"weights": str(args.weights), "splits": args.splits, "n": int(m.sum()), "positives": int(y.sum()),
                           "auc": round(auc(y, p), 4), "thresholds": {}}
    for t in args.thresholds:
        f = p >= t
        res["thresholds"][f"{t:.2f}"] = {"flagged": int(f.sum()),
                                         "precision": round(float((f & (y == 1)).sum() / max(1, f.sum())), 4),
                                         "recall": round(float((f & (y == 1)).sum() / max(1, (y == 1).sum())), 4)}
    if "kind" in z.files:
        kinds = z["kind"].astype(str)[m]
        res["auc_by_kind"] = {k: round(auc(y[kinds == k], p[kinds == k]), 4) for k in sorted(set(kinds.tolist()))
                              if len(set(y[kinds == k].tolist())) == 2}
    write_json(args.out, res)
    log.info("eval-crops: %s", json.dumps(res))


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("verifier-data")
    p.add_argument("--data", type=Path, default=config.DATASETS_DIR / "us2")
    p.add_argument("--preds", type=Path, required=True, help="label_clean score output for the same dataset")
    p.add_argument("--splits", nargs="+", default=["train", "val_us"])
    p.add_argument("--max-chips", type=int, default=0)
    p.add_argument("--tile", type=int, default=config.CHIP_SIZE_PX)
    p.add_argument("--imgsz", type=int, default=416, help="detector input size the crops are cut from")
    p.add_argument("--input", type=int, default=112)
    p.add_argument("--context", type=float, default=1.6)
    p.add_argument("--min-side-px", type=float, default=40.0)
    p.add_argument("--min-conf", type=float, default=0.05)
    p.add_argument("--pos-jitter-copies", type=int, default=1)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", type=Path, default=config.DATA_DIR / "crops" / "verifier.npz")
    add_common_args(p)
    p = sub.add_parser("train")
    p.add_argument("--crops", type=Path, required=True)
    p.add_argument("--arch", default="resnet18")
    p.add_argument("--train-splits", nargs="+", default=["train"])
    p.add_argument("--val-splits", nargs="+", default=["val_us"])
    p.add_argument("--epochs", type=int, default=8)
    p.add_argument("--batch", type=int, default=256)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--balance", action="store_true", help="weight positives by the class ratio")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", type=Path, required=True)
    add_common_args(p)
    p = sub.add_parser("eval-verifier")
    p.add_argument("--data", type=Path, default=config.DATASETS_DIR / "us2")
    p.add_argument("--split", default="val")
    p.add_argument("--weights", type=Path, required=True)
    p.add_argument("--imgsz", type=int)
    p.add_argument("--verifier", type=Path, required=True)
    p.add_argument("--conf", type=float, default=0.05)
    p.add_argument("--out", type=Path, default=config.OUT_DIR / "verifier_eval.json")
    add_common_args(p)
    p = sub.add_parser("eval-crops")
    p.add_argument("--crops", type=Path, required=True)
    p.add_argument("--weights", type=Path, required=True)
    p.add_argument("--splits", nargs="+", default=["test"])
    p.add_argument("--thresholds", type=float, nargs="+", default=[0.3, 0.5, 0.7, 0.8, 0.9])
    p.add_argument("--out", type=Path, default=config.OUT_DIR / "context_eval.json")
    add_common_args(p)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    {"verifier-data": cmd_verifier_data, "train": cmd_train, "eval-verifier": cmd_eval_verifier,
     "eval-crops": cmd_eval_crops}[args.cmd](args)


if __name__ == "__main__":
    main()
