"""Where do two detectors disagree on a labelled split? Crops for a visual head-to-head (OSM labels are noisy).

Run ON the instance from ~/hh/pipeline:
    PYTHONPATH=. ~/hhvenv/bin/python cloud/disagree.py --a weights/best_v1.pt --a-imgsz 320 --a-thr 0.5 \
        --b weights/best_v2.pt --b-imgsz 416 --b-thr 0.5 --data datasets/us2 --split val --out out/disagree_tn
    python cloud/disagree.py --out out/disagree_tn --score out/disagree_tn/labels.json   # after a blind review

For each chip, a box of one model at or above its threshold that no box of the other model (at a lower
threshold, ``--slack``) overlaps with IoU >= 0.3 is a disagreement. Writes crops in random order named only by a
number (magenta = the box, green = OSM label), so the review is blind to the model; ``disagreements.json`` maps each
crop to its model, score and OSM verdict (matches a label or not).
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from hoop_pipeline.common import setup_logging, write_json
from hoop_pipeline.evaluate import iou_matrix
from hoop_pipeline.fastdet import FastDetector
from hoop_pipeline.label_clean import iter_images, load_label_boxes


def run(det: FastDetector, paths: list[Path], conf: float) -> dict[str, np.ndarray]:
    import torch

    out: dict[str, np.ndarray] = {}
    for chunk, arr in iter_images(paths, 256, 16):
        d = det.detect(torch.from_numpy(arr), conf=conf).cpu().numpy()
        for i, p in enumerate(chunk):
            out[p.stem] = d[d[:, 0] == i, 1:6]
    return out


def score(out: Path, labels: Path) -> None:
    """Blind labels {"labels": {"<crop number>": "C|N|U"}} -> how often each model's lone boxes are courts."""
    rows = json.loads((out / "disagreements.json").read_text(encoding="utf-8"))["rows"]
    lab = json.loads(labels.read_text(encoding="utf-8"))["labels"]
    res: dict[str, dict[str, int]] = {}
    for r in rows:
        code = lab.get(r.get("crop_file", "")[:3].lstrip("0"))
        if code is None:
            continue
        c = res.setdefault(r["model"], {"C": 0, "N": 0, "U": 0, "osm_match": 0, "C_and_osm_match": 0})
        c[code.split(":")[0]] += 1
        c["osm_match"] += int(r["osm_match"])
        c["C_and_osm_match"] += int(r["osm_match"] and code.startswith("C"))
    write_json(out / "disagreements_scored.json", {"note": "model-assistant labels, not gold", "labels": str(labels),
                                                   "by_model": res})
    print(json.dumps(res))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", type=Path, help="score blind labels of the crops in --out instead of running the models")
    ap.add_argument("--a", type=Path)
    ap.add_argument("--a-imgsz", type=int, default=320)
    ap.add_argument("--a-thr", type=float, default=0.5)
    ap.add_argument("--b", type=Path)
    ap.add_argument("--b-imgsz", type=int, default=416)
    ap.add_argument("--b-thr", type=float, default=0.5)
    ap.add_argument("--slack", type=float, default=0.2, help="the other model's boxes down to thr - slack count as agreement")
    ap.add_argument("--data", type=Path, default=Path("datasets/us2"))
    ap.add_argument("--split", default="val")
    ap.add_argument("--max-crops", type=int, default=60)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    setup_logging(0)
    if args.score:
        score(args.out, args.score)
        return
    if args.a is None or args.b is None:
        ap.error("--a and --b are required unless --score is given")
    paths = sorted((args.data / "images" / args.split).glob("*.jpg"))
    low = min(args.a_thr, args.b_thr) - args.slack
    pa = run(FastDetector(args.a, imgsz=args.a_imgsz), paths, max(0.01, low))
    pb = run(FastDetector(args.b, imgsz=args.b_imgsz), paths, max(0.01, low))
    rows = []
    for p in paths:
        gt = load_label_boxes(args.data / "labels" / args.split / f"{p.stem}.txt", 320)
        for who, mine, thr, other, othr in (("a", pa[p.stem], args.a_thr, pb[p.stem], args.b_thr - args.slack),
                                             ("b", pb[p.stem], args.b_thr, pa[p.stem], args.a_thr - args.slack)):
            m = mine[mine[:, 4] >= thr]
            o = other[other[:, 4] >= othr]
            if not len(m):
                continue
            iou_o = iou_matrix(m[:, :4], o[:, :4]) if len(o) else np.zeros((len(m), 0))
            iou_g = iou_matrix(m[:, :4], gt) if len(gt) else np.zeros((len(m), 0))
            for k in range(len(m)):
                if iou_o.shape[1] and iou_o[k].max() >= 0.3:
                    continue
                rows.append({"chip": p.stem, "model": who, "box": [round(float(v), 1) for v in m[k, :4]],
                             "conf": round(float(m[k, 4]), 3),
                             "osm_match": bool(iou_g.shape[1] and iou_g[k].max() >= 0.3)})
    rng = random.Random(0)
    for who in ("a", "b"):
        sub = [r for r in rows if r["model"] == who]
        rng.shuffle(sub)
        for r in sub[: args.max_crops // 2]:
            r["crop"] = True
    args.out.mkdir(parents=True, exist_ok=True)
    picked = [r for r in rows if r.get("crop")]
    rng.shuffle(picked)  # blind review: file names carry only a number, the model is in disagreements.json
    for k, r in enumerate(picked, start=1):
        split_dir = args.data / "images" / args.split
        im = Image.open(split_dir / f"{r['chip']}.jpg").convert("RGB")
        d = ImageDraw.Draw(im)
        for g in load_label_boxes(args.data / "labels" / args.split / f"{r['chip']}.txt", 320):
            d.rectangle(list(g), outline=(0, 255, 0), width=1)
        x0, y0, x1, y1 = r["box"]
        d.rectangle([x0 - 2, y0 - 2, x1 + 2, y1 + 2], outline=(255, 0, 255), width=2)
        r["crop_file"] = f"{k:03d}.jpg"
        im.save(args.out / r["crop_file"], quality=92)
    summ = {"a": str(args.a), "b": str(args.b), "a_thr": args.a_thr, "b_thr": args.b_thr, "slack": args.slack,
            "only_a": sum(r["model"] == "a" for r in rows), "only_b": sum(r["model"] == "b" for r in rows),
            "only_a_osm_match": sum(r["model"] == "a" and r["osm_match"] for r in rows),
            "only_b_osm_match": sum(r["model"] == "b" and r["osm_match"] for r in rows), "crops": len(picked)}
    write_json(args.out / "disagreements.json", {"summary": summ, "rows": rows})
    print(json.dumps(summ))


if __name__ == "__main__":
    main()
