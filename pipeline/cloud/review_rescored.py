"""Blind visual review of re-scored scan candidates, to pick an export threshold (model-assistant labels, NOT gold).

    python cloud/review_rescored.py sample --candidates out/scan2_tn_v1/detections_privacy.parquet \
        --chips data/rescore/tn_v1 --classes public --min-conf 0.4 --per-band 30 --out out/review_tn_rescore
    # confirm every member of a pool instead of sampling it (one band, no cap):
    python cloud/review_rescored.py sample ... --min-conf2 0.3 --min-verify 0.9 --bands 0 1.01 --per-band 100000 \
        --out out/review_tn_confirm
    # look at out/review_tn_rescore/sheet_*.jpg, write labels.json {"labels": {"<n>": "C|N|U[:P|H|R|?]"}}
    python cloud/review_rescored.py score --dir out/review_tn_rescore --labels out/review_tn_rescore/labels.json
    python cloud/review_rescored.py confirmed --dir out/review_tn_confirm --labels out/review_tn_confirm/labels.json \
        --geojson out/scan2_tn_v1/detections_privacy.geojson --out out/scan2_tn_v1/candidates_confirmed.geojson

The sample is stratified by the second detector's score (``conf2`` from hoop_pipeline.rescore) among candidates
of the given privacy classes with v1 confidence >= --min-conf. Crops are the central 120 m of the cached 192 m
chips with the candidate's box drawn, shown in random order with only a number, so the reviewer sees neither
score. ``score`` weights every band by its population and reports, for a grid of (v1, conf2) thresholds, the
estimated precision (court / (court + not), unsure left out, and unsure counted as not) with a stratified
bootstrap 95% interval, plus the expected number of real courts kept.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
from PIL import Image, ImageDraw

BANDS = [0.0, 0.3, 0.5, 0.6, 0.7, 1.01]
GSD = 0.6


def band_of(x: float, bands: list[float]) -> str:
    for lo, hi in zip(bands[:-1], bands[1:]):
        if lo <= x < hi:
            return f"{lo:.2f}-{hi:.2f}"
    return "nan"


def cmd_sample(args: argparse.Namespace) -> None:
    t = pq.read_table(args.candidates).to_pydict()
    n = len(t["id"])
    rows = [{k: t[k][i] for k in ("id", "conf", "conf2", "verify2", "p_home", "privacy", "site", "box_w_m", "box_h_m",
                                  "lon", "lat", "near_known_osm")} for i in range(n)]
    pool = [r for r in rows if not r["near_known_osm"] and r["privacy"] in args.classes and r["conf"] >= args.min_conf
            and r["conf2"] is not None and r["conf2"] == r["conf2"] and r["conf2"] >= args.min_conf2
            and (r["verify2"] or 0.0) >= args.min_verify and (args.chips / f"{r['id']}.jpg").exists()]
    rng = random.Random(args.seed)
    strata: dict[str, dict[str, int]] = {}
    picked: list[dict[str, Any]] = []
    for lo, hi in zip(args.bands[:-1], args.bands[1:]):
        b = f"{lo:.2f}-{hi:.2f}"
        members = [r for r in pool if lo <= r["conf2"] < hi]
        take = rng.sample(members, min(args.per_band, len(members)))
        strata[b] = {"population": len(members), "sampled": len(take)}
        for r in take:
            r["band"] = b
        picked += take
        print(f"conf2 {b}: {len(members)} candidates, {len(take)} sampled")
    rng.shuffle(picked)
    out = args.out
    (out / "crops").mkdir(parents=True, exist_ok=True)
    thumbs = []
    for k, r in enumerate(picked, start=1):
        r["n"] = k
        im = Image.open(args.chips / f"{r['id']}.jpg").convert("RGB")
        c = im.width / 2
        h = args.crop_px / 2
        im = im.crop((int(c - h), int(c - h), int(c + h), int(c + h))).resize((args.thumb_px, args.thumb_px),
                                                                            Image.Resampling.BICUBIC)
        s = args.thumb_px / args.crop_px
        w, hh = r["box_w_m"] / GSD * s / 2, r["box_h_m"] / GSD * s / 2
        m = args.thumb_px / 2
        d = ImageDraw.Draw(im)
        d.rectangle([m - w - 4, m - hh - 4, m + w + 4, m + hh + 4], outline=(255, 255, 0), width=2)
        d.rectangle([0, 0, 34, 14], fill=(0, 0, 0))
        d.text((2, 1), f"{k:03d}", fill=(255, 255, 0))
        im.save(out / "crops" / f"{k:03d}.jpg", quality=90)
        thumbs.append(im)
    cols, rws = 5, 4
    for k in range(0, len(thumbs), cols * rws):
        sheet = Image.new("RGB", (cols * args.thumb_px, rws * args.thumb_px), "white")
        for j, th in enumerate(thumbs[k:k + cols * rws]):
            sheet.paste(th, ((j % cols) * args.thumb_px, (j // cols) * args.thumb_px))
        sheet.save(out / f"sheet_{k // (cols * rws):02d}.jpg", quality=88)
    meta = {"candidates": str(args.candidates), "classes": args.classes, "min_conf": args.min_conf,
            "min_conf2": args.min_conf2, "min_verify": args.min_verify, "bands": args.bands,
            "strata": strata, "crop_m": args.crop_px * GSD, "items": picked,
            "note": "blind review sample; NOT gold. Strata = conf2 bands among the selected classes"}
    (out / "sample.json").write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
    print(f"{len(picked)} crops -> {out}")


def estimate(items: list[dict[str, Any]], strata: dict[str, dict[str, int]], t1: float, t2: float, unsure_as_not: bool,
             rng: random.Random | None = None) -> tuple[float, float, float]:
    """(precision, expected courts, population) over candidates with conf >= t1 and conf2 >= t2, weighting each
    conf2 band by population x (sampled share passing the thresholds)."""
    num = den = pop = 0.0
    by_band: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        by_band.setdefault(it["band"], []).append(it)
    for b, its in by_band.items():
        if rng is not None:
            its = [rng.choice(its) for _ in its]
        n_b = strata[b]["population"]
        passing = [it for it in its if it["conf"] >= t1 and it["conf2"] >= t2]
        if not passing:
            continue
        share = len(passing) / len(its)  # share of the band's population passing both thresholds
        decided = [it for it in passing if it["code"] in ("C", "N") or (unsure_as_not and it["code"] == "U")]
        if not decided:
            continue
        p = sum(it["code"] == "C" for it in decided) / len(decided)
        num += n_b * share * p
        den += n_b * share
        pop += n_b * share
    return (num / den if den else float("nan"), num, pop)


def cmd_score(args: argparse.Namespace) -> None:
    meta = json.loads((args.dir / "sample.json").read_text(encoding="utf-8"))
    lab = json.loads(args.labels.read_text(encoding="utf-8"))["labels"]
    items = []
    for it in meta["items"]:
        code = lab.get(str(it["n"]))
        if code is None:
            continue
        it["code"], _, it["access"] = code.partition(":")
        items.append(it)
    strata = meta["strata"]
    res: dict[str, Any] = {"note": "model-assistant labels (Claude, blind), not gold", "labeled": len(items),
                           "codes": {c: sum(it["code"] == c for it in items) for c in ("C", "N", "U")},
                           "by_band": {}, "grid": []}
    for b in strata:
        its = [it for it in items if it["band"] == b]
        res["by_band"][b] = {"population": strata[b]["population"], "C": sum(i["code"] == "C" for i in its),
                             "N": sum(i["code"] == "N" for i in its), "U": sum(i["code"] == "U" for i in its),
                             "home": sum(i["code"] == "C" and i["access"] == "H" for i in its)}
    rng = random.Random(7)
    for t1 in args.t1:
        for t2 in args.t2:
            p, exp_c, pop = estimate(items, strata, t1, t2, False)
            p_strict, _, _ = estimate(items, strata, t1, t2, True)
            boots = sorted(v for v in (estimate(items, strata, t1, t2, False, rng)[0] for _ in range(1000)) if v == v)
            ci = (round(boots[25], 3), round(boots[974], 3)) if len(boots) >= 1000 else None
            n_s = sum(1 for it in items if it["conf"] >= t1 and it["conf2"] >= t2)
            res["grid"].append({"v1_min": t1, "conf2_min": t2, "candidates": round(pop), "precision": round(p, 3),
                                "precision_unsure_as_not": round(p_strict, 3), "ci95": ci,
                                "expected_courts": round(exp_c), "reviewed": n_s})
    (args.out or args.dir / "scores.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in res.items() if k != "grid"}, indent=1))
    print("| v1 >= | conf2 >= | candidates | precision (95% CI) | unsure as not | expected courts | reviewed |")
    print("|---|---|---|---|---|---|---|")
    for g in res["grid"]:
        ci_s = f"{g['ci95'][0]:.2f}-{g['ci95'][1]:.2f}" if g["ci95"] else "-"
        print(f"| {g['v1_min']:.2f} | {g['conf2_min']:.2f} | {g['candidates']} | {g['precision']:.2f} ({ci_s}) | "
              f"{g['precision_unsure_as_not']:.2f} | {g['expected_courts']} | {g['reviewed']} |")


def cmd_confirmed(args: argparse.Namespace) -> None:
    """GeoJSON of the candidates labelled as courts on a public site (C or C:P / C:?) in a review, for export_app."""
    meta = json.loads((args.dir / "sample.json").read_text(encoding="utf-8"))
    lab = json.loads(args.labels.read_text(encoding="utf-8"))["labels"]
    keep = {it["id"] for it in meta["items"] if lab.get(str(it["n"]), "").split(":")[0] == "C"
            and lab[str(it["n"])].partition(":")[2] in ("", "P", "?")}
    fc = json.loads(args.geojson.read_text(encoding="utf-8"))
    feats = [f for f in fc["features"] if f["properties"].get("id") in keep]
    for f in feats:
        f["properties"]["review"] = "assistant-confirmed"
    fc["features"] = feats
    args.out.write_text(json.dumps(fc), encoding="utf-8")
    print(f"{len(feats)} of {len(keep)} confirmed candidates written to {args.out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sample")
    p.add_argument("--candidates", type=Path, required=True, help="privacy classify --rescore output parquet")
    p.add_argument("--chips", type=Path, required=True, help="hoop_pipeline.rescore --chips-dir")
    p.add_argument("--classes", nargs="+", default=["public"])
    p.add_argument("--min-conf", type=float, default=0.4)
    p.add_argument("--min-conf2", type=float, default=0.0)
    p.add_argument("--min-verify", type=float, default=0.0)
    p.add_argument("--bands", type=float, nargs="+", default=BANDS)
    p.add_argument("--per-band", type=int, default=30)
    p.add_argument("--crop-px", type=int, default=200)
    p.add_argument("--thumb-px", type=int, default=300)
    p.add_argument("--seed", type=int, default=13)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("score")
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--labels", type=Path, required=True)
    p.add_argument("--t1", type=float, nargs="+", default=[0.4, 0.5, 0.6, 0.7])
    p.add_argument("--t2", type=float, nargs="+", default=[0.0, 0.3, 0.5, 0.6, 0.7])
    p.add_argument("--out", type=Path)
    p = sub.add_parser("confirmed", help="GeoJSON of the review's confirmed public courts (input for export_app)")
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--labels", type=Path, required=True)
    p.add_argument("--geojson", type=Path, required=True, help="privacy classify GeoJSON (all candidates)")
    p.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    np.seterr(all="ignore")
    {"sample": cmd_sample, "score": cmd_score, "confirmed": cmd_confirmed}[args.cmd](args)


if __name__ == "__main__":
    main()
