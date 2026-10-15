"""Blind head-to-head of two models on the random CONUS gold tiles, with model-assistant labels (NOT gold).

Boxes of every model (>= the annotate threshold, 0.15) are clustered per tile (IoU >= 0.3), so each object is
judged once and the reviewer does not see which model produced it or its score.

    python cloud/gold_review.py prepare --preds v2=data/gold/preds.json v1=data/gold/preds_v1.json \
        --carry out/review_gold/assistant_labels_v1_gold_tiles.json --out out/review_gold/h2h
    # look at out/review_gold/h2h/sheet_*.jpg and write out/review_gold/h2h/labels.json:
    #   {"labeler": "...", "note": "...", "labels": {"<n>": "C|N|U[:P|H|R|?]"}}   (n = number printed on the crop)
    python cloud/gold_review.py score --dir out/review_gold/h2h --labels out/review_gold/h2h/labels.json

C = basketball court, N = not a court, U = unsure; after the colon the access judged from the imagery
(P public site, H home lot, R residents only, ? can't tell). ``--carry`` (an earlier review of one model's boxes)
is only used to report agreement with the new blind labels.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

THRESHOLDS = (0.15, 0.25, 0.35, 0.5, 0.6, 0.7)


def iou(a: list[float], b: list[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    return (round((c - r) / d, 3), round((c + r) / d, 3))


def box_of(b: dict[str, Any]) -> list[float]:
    return [float(b["x0"]), float(b["y0"]), float(b["x1"]), float(b["y1"])]


def cmd_prepare(args: argparse.Namespace) -> None:
    meta = json.loads((args.gold / "tiles.json").read_text(encoding="utf-8"))
    tiles = {t["id"]: t for t in meta["tiles"]}
    models: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for spec in args.preds:
        name, _, path = spec.partition("=")
        models[name] = json.loads(Path(path).read_text(encoding="utf-8"))["preds"]
    clusters: list[dict[str, Any]] = []
    for tid in sorted(tiles):
        boxes = [(name, box_of(b), float(b["conf"])) for name, preds in models.items() for b in preds.get(tid, [])
                 if float(b["conf"]) >= args.conf]
        boxes.sort(key=lambda x: -x[2])
        tile_clusters: list[dict[str, Any]] = []
        for name, bx, cf in boxes:
            for c in tile_clusters:
                if iou(c["box"], bx) >= args.iou:
                    c["conf"][name] = max(c["conf"].get(name, 0.0), cf)
                    break
            else:
                tile_clusters.append({"tile": tid, "box": [round(v, 1) for v in bx], "conf": {name: cf}})
        clusters.extend(tile_clusters)
    rng = np.random.default_rng(args.seed)  # shuffle so neither score order nor model order is visible
    order = rng.permutation(len(clusters))
    clusters = [clusters[i] for i in order]
    carry: dict[int, str] = {}
    if args.carry and args.carry.exists():
        cj = json.loads(args.carry.read_text(encoding="utf-8"))
        for it in cj["items"]:
            code = cj["labels"].get(str(it["rank"]))
            if code is None:
                continue
            for n, c in enumerate(clusters, start=1):
                if c["tile"] == it["tile"] and iou(c["box"], box_of(it["box"])) >= args.iou:
                    carry[n] = code
                    break
    out = args.out
    (out / "crops").mkdir(parents=True, exist_ok=True)
    half = args.crop_px // 2
    thumbs = []
    for n, c in enumerate(clusters, start=1):
        t = tiles[c["tile"]]
        c.update({"n": n, "region": t["region"], "stratum": t["stratum"], "state": t["state"],
                  "train_overlap": bool(t.get("train_overlap")), "carried_label": carry.get(n)})
        im = Image.open(args.gold / "images" / f"{c['tile']}.jpg").convert("RGB")
        cx, cy = (c["box"][0] + c["box"][2]) / 2, (c["box"][1] + c["box"][3]) / 2
        x0 = int(min(max(0, cx - half), im.width - args.crop_px))
        y0 = int(min(max(0, cy - half), im.height - args.crop_px))
        crop = im.crop((x0, y0, x0 + args.crop_px, y0 + args.crop_px))
        s = args.thumb_px / args.crop_px
        crop = crop.resize((args.thumb_px, args.thumb_px), Image.Resampling.BICUBIC)
        d = ImageDraw.Draw(crop)
        bx = [(c["box"][0] - x0) * s - 3, (c["box"][1] - y0) * s - 3, (c["box"][2] - x0) * s + 3, (c["box"][3] - y0) * s + 3]
        d.rectangle(bx, outline=(255, 255, 0), width=2)
        d.rectangle([0, 0, 44, 16], fill=(0, 0, 0))
        d.text((3, 2), f"{n:03d}", fill=(255, 255, 0))
        crop.save(out / "crops" / f"{n:03d}.jpg", quality=90)
        thumbs.append(crop)
    cols, rows = 5, 4
    for k in range(0, len(thumbs), cols * rows):
        sheet = Image.new("RGB", (cols * args.thumb_px, rows * args.thumb_px), "white")
        for j, th in enumerate(thumbs[k:k + cols * rows]):
            sheet.paste(th, ((j % cols) * args.thumb_px, (j // cols) * args.thumb_px))
        sheet.save(out / f"sheet_{k // (cols * rows):02d}.jpg", quality=88)
    info = {"note": "clusters of model boxes on the random gold tiles; review blind (no model / score shown)",
            "models": list(models), "conf": args.conf, "iou": args.iou, "crop_m": round(args.crop_px * meta["gsd"], 1),
            "strata_weights": meta.get("strata_weights", {}), "tile_km2": (meta["tile_px"] * meta["gsd"] / 1000) ** 2,
            "tiles": {tid: {"region": t["region"], "stratum": t["stratum"], "train_overlap": bool(t.get("train_overlap"))}
                      for tid, t in tiles.items()},
            "clusters": clusters}
    (out / "clusters.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
    per_model = ", ".join(f"{m}: {sum(m in c['conf'] for c in clusters)}" for m in models)
    print(f"{len(clusters)} clusters ({per_model}), {len(carry)} with a carried label -> {out}")


def cmd_score(args: argparse.Namespace) -> None:
    info = json.loads((args.dir / "clusters.json").read_text(encoding="utf-8"))
    lab = json.loads(args.labels.read_text(encoding="utf-8"))["labels"]
    clusters = info["clusters"]
    weights: dict[str, float] = info["strata_weights"]
    tiles = info["tiles"]
    n_tiles = Counter(f"{t['region']}|{t['stratum']}" for t in tiles.values())
    tile_km2 = float(info["tile_km2"])
    res: dict[str, Any] = {"note": "model-assistant labels (Claude, from 640 px NAIP gold tiles), NOT gold",
                           "labeled": sum(str(c["n"]) in lab for c in clusters), "clusters": len(clusters), "models": {}}
    agree = [(c["carried_label"].split(":")[0], lab[str(c["n"])].split(":")[0]) for c in clusters
             if c.get("carried_label") and str(c["n"]) in lab]
    res["carry_agreement"] = {"n": len(agree), "same": sum(a == b for a, b in agree),
                              "pairs": dict(Counter(f"{a}->{b}" for a, b in agree))}
    for m in info["models"]:
        per_t: dict[str, Any] = {}
        for t in THRESHOLDS:
            sel = [c for c in clusters if c["conf"].get(m, 0.0) >= t and str(c["n"]) in lab]
            codes = [lab[str(c["n"])].split(":")[0] for c in sel]
            k_c, k_n = codes.count("C"), codes.count("N")
            # CONUS-weighted false positives per 1,000 km2: per-cell FP rate per tile, weighted by area share
            fp_cell = Counter(f"{c['region']}|{c['stratum']}" for c, code in zip(sel, codes) if code == "N")
            tp_cell = Counter(f"{c['region']}|{c['stratum']}" for c, code in zip(sel, codes) if code == "C")
            fp_w = sum(weights.get(cell, 0.0) * fp_cell[cell] / max(1, n_tiles[cell]) for cell in n_tiles) / tile_km2
            tp_w = sum(weights.get(cell, 0.0) * tp_cell[cell] / max(1, n_tiles[cell]) for cell in n_tiles) / tile_km2
            by_stratum = {}
            for s in ("urban", "suburban", "rural"):
                cs = [code for c, code in zip(sel, codes) if c["stratum"] == s]
                by_stratum[s] = {"C": cs.count("C"), "N": cs.count("N"), "U": cs.count("U")}
            per_t[f"{t:.2f}"] = {"boxes": len(sel), "C": k_c, "N": k_n, "U": codes.count("U"),
                                 "precision": round(k_c / max(1, k_c + k_n), 3), "ci95": wilson(k_c, k_c + k_n),
                                 "precision_U_as_FP": round(k_c / max(1, len(sel)), 3),
                                 "conus_fp_per_1000km2": round(1000 * fp_w, 2),
                                 "conus_courts_found_per_1000km2": round(1000 * tp_w, 2), "by_stratum": by_stratum}
        res["models"][m] = per_t
    for t in THRESHOLDS:
        found = {m: {c["n"] for c in clusters if c["conf"].get(m, 0.0) >= t and lab.get(str(c["n"]), "").startswith("C")}
                 for m in info["models"]}
        union = set().union(*found.values())
        res.setdefault("courts_found", {})[f"{t:.2f}"] = {"union": len(union), **{m: len(v) for m, v in found.items()}}
    out = args.out or args.dir / "scores.json"
    out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    ms = info["models"]
    print("| threshold | " + " | ".join(f"{m}: boxes, C/N/U, precision (95% CI), FP per 1,000 km2" for m in ms)
          + " | courts found (" + " / ".join(ms) + " / union) |")
    print("|---|" + "---|" * len(ms) + "---|")
    for t in THRESHOLDS:
        cells = []
        for m in ms:
            r = res["models"][m][f"{t:.2f}"]
            ci = f"{r['ci95'][0]:.2f}-{r['ci95'][1]:.2f}" if r["ci95"] else "-"
            cells.append(f"{r['boxes']}, {r['C']}/{r['N']}/{r['U']}, {r['precision']:.2f} ({ci}), "
                         f"{r['conus_fp_per_1000km2']:.1f}")
        f = res["courts_found"][f"{t:.2f}"]
        print(f"| {t:.2f} | " + " | ".join(cells) + " | " + " / ".join(str(f[m]) for m in ms) + f" / {f['union']} |")
    print(f"carried-label agreement: {res['carry_agreement']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--gold", type=Path, default=Path("data/gold"))
    p.add_argument("--preds", nargs="+", required=True, help="name=preds.json (from gold annotate)")
    p.add_argument("--carry", type=Path, help="earlier assistant labels of one model's boxes (agreement check only)")
    p.add_argument("--conf", type=float, default=0.15)
    p.add_argument("--iou", type=float, default=0.3)
    p.add_argument("--crop-px", type=int, default=200, help="crop side in tile pixels (0.6 m)")
    p.add_argument("--thumb-px", type=int, default=300)
    p.add_argument("--seed", type=int, default=3)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("score")
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--labels", type=Path, required=True)
    p.add_argument("--out", type=Path)
    args = ap.parse_args()
    {"prepare": cmd_prepare, "score": cmd_score}[args.cmd](args)


if __name__ == "__main__":
    main()
