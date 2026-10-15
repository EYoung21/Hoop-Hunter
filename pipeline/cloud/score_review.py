"""Score a visual review of scan candidates (model-assistant labels, NOT gold).

    python cloud/score_review.py out/review_v2/sample.geojson out/review_v2/assistant_labels.json \
        --out out/review_v2/assistant_scores.json

``assistant_labels.json``: {"labeler": "...", "note": "...", "labels": {"<rank>": "C|N|U[:P|H|R|?]"}} where rank is the
1-based position of the feature in the sample (the crop file prefix). C = basketball court, N = not a court,
U = unsure; after the colon the access judged from the imagery: P = public site, H = home lot,
R = residents only (apartments / HOA), ? = can't tell.

With ``<sample>.strata.json`` (written by sample_review.py) the per-threshold precision is weighted by the
population of each (confidence band, public / other) stratum, with a stratified bootstrap 95% interval, so it
estimates the precision of *all* candidates above the threshold, not of the equal-size sample.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    return (round((c - r) / d, 3), round((c + r) / d, 3))


def weighted(rows_by_stratum: dict[str, list[dict[str, Any]]], pops: dict[str, int], strata: list[str],
             hit: str = "C") -> tuple[float, float, float]:
    """Population-weighted share of ``hit`` among decided (C/N) rows; returns (share, expected hits, population)."""
    num = den = 0.0
    for s in strata:
        r = [x for x in rows_by_stratum.get(s, []) if x["court"] in ("C", "N")]
        if not r or not pops.get(s):
            continue
        p = sum(x["court"] == "C" and (hit == "C" or x["access"] == hit) for x in r) / len(r)
        num += pops[s] * p
        den += pops[s]
    return (num / den if den else float("nan"), num, den)


def bootstrap(rows_by_stratum: dict[str, list[dict[str, Any]]], pops: dict[str, int], strata: list[str],
              hit: str = "C", n: int = 2000, seed: int = 5) -> tuple[float, float] | None:
    rng = random.Random(seed)
    vals = []
    for _ in range(n):
        res = {s: [rng.choice(v) for _ in v] for s, v in rows_by_stratum.items() if v}
        w = weighted(res, pops, strata, hit)[0]
        if w == w:
            vals.append(w)
    if not vals:
        return None
    vals.sort()
    return (round(vals[int(0.025 * len(vals))], 3), round(vals[int(0.975 * len(vals)) - 1], 3))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sample", type=Path)
    ap.add_argument("labels", type=Path)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    feats = json.loads(args.sample.read_text(encoding="utf-8"))["features"]
    lab = json.loads(args.labels.read_text(encoding="utf-8"))
    strata_file = args.sample.with_suffix(".strata.json")
    meta = json.loads(strata_file.read_text(encoding="utf-8")) if strata_file.exists() else None
    rows = []
    for k, f in enumerate(feats, start=1):
        v = lab["labels"].get(str(k))
        if v is None:
            continue
        court, _, access = v.partition(":")
        p = f["properties"]
        rows.append({"rank": k, "conf": p["confidence"], "band": p.get("band"), "privacy": p.get("privacy"),
                     "stratum": p.get("stratum") or f"{p.get('band')}|{'public' if p.get('privacy') == 'public' else 'other'}",
                     "site": p.get("site"), "court": court, "access": access or "?"})
    res: dict[str, Any] = {"labeler": lab.get("labeler"), "note": "model-assistant labels from NAIP crops, not gold",
                           "reviewed": len(rows), "codes": dict(Counter(r["court"] for r in rows)),
                           "by_stratum": {}, "by_threshold": {}, "privacy": {}}
    by_stratum: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_stratum.setdefault(r["stratum"], []).append(r)
    for s in sorted(by_stratum):
        b = by_stratum[s]
        c, n = sum(r["court"] == "C" for r in b), sum(r["court"] == "N" for r in b)
        res["by_stratum"][s] = {"reviewed": len(b), "court": c, "not": n, "unsure": len(b) - c - n,
                                "precision": round(c / max(1, c + n), 3), "ci95": wilson(c, c + n),
                                "home_courts": sum(r["court"] == "C" and r["access"] == "H" for r in b),
                                "population": (meta or {}).get("strata", {}).get(s, {}).get("population")}
    if meta:
        pops = {s: v["population"] for s, v in meta["strata"].items()}
        bands = sorted({s.split("|")[0] for s in pops}, key=lambda b: float(b.split("-")[0]))
        for b in bands:
            lo = float(b.split("-")[0])
            above = [x for x in bands if float(x.split("-")[0]) >= lo - 1e-9]
            pub = [f"{x}|public" for x in above]
            allc = pub + [f"{x}|other" for x in above]
            pp, exp_c, pop = weighted(by_stratum, pops, pub)
            pa_, _, pop_all = weighted(by_stratum, pops, allc)
            home_share, exp_home, _ = weighted(by_stratum, pops, pub, hit="H")
            res["by_threshold"][f"{lo:.2f}"] = {
                "public_candidates": int(pop), "public_precision": round(pp, 3),
                "public_ci95_bootstrap": bootstrap(by_stratum, pops, pub),
                "expected_public_courts": round(exp_c, 1),
                "public_home_court_share": round(home_share, 3), "expected_home_courts_in_public": round(exp_home, 1),
                "all_candidates": int(pop_all), "all_precision": round(pa_, 3),
                "all_ci95_bootstrap": bootstrap(by_stratum, pops, allc),
                "note": "population-weighted over (band, public/other) strata; unsure (U) left out"}
    else:  # no strata file: unweighted over the sample
        for t in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
            b = [r for r in rows if r["conf"] >= t]
            c, n = sum(r["court"] == "C" for r in b), sum(r["court"] == "N" for r in b)
            bp = [r for r in b if r["privacy"] == "public"]
            cp, npub = sum(r["court"] == "C" for r in bp), sum(r["court"] == "N" for r in bp)
            res["by_threshold"][f"{t:.2f}"] = {"sample_precision": round(c / max(1, c + n), 3), "ci95": wilson(c, c + n),
                                               "public_sample_precision": round(cp / max(1, cp + npub), 3),
                                               "public_ci95": wilson(cp, cp + npub), "n": c + n, "n_public": cp + npub,
                                               "note": "unweighted over the sample"}
    courts = [r for r in rows if r["court"] == "C" and r["access"] in ("P", "H", "R")]
    conf_mx = Counter((r["access"], r["privacy"]) for r in courts)
    home = [r for r in courts if r["access"] == "H"]
    flagged = [r for r in courts if r["privacy"] == "private_home"]
    res["privacy"] = {
        "note": "sample counts (not population-weighted); access judged from the crop: P public, H home lot, R residents",
        "courts_with_access_judged": len(courts),
        "confusion_access_vs_class": {f"{a}->{c}": v for (a, c), v in sorted(conf_mx.items())},
        "home_courts": len(home),
        "home_flag_recall": round(sum(r["privacy"] == "private_home" for r in home) / max(1, len(home)), 3),
        "home_flag_precision": round(sum(r["access"] == "H" for r in flagged) / max(1, len(flagged)), 3),
        "home_courts_classified_public": sum(r["privacy"] == "public" for r in home),
        "public_class_courts_that_are_home": f"{sum(r['access'] == 'H' for r in courts if r['privacy'] == 'public')}"
                                             f"/{sum(r['privacy'] == 'public' for r in courts)}",
    }
    text = json.dumps(res, indent=1)
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
