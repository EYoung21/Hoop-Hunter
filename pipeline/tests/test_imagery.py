"""Offline tests for the v4 imagery modules (docs/imagery.md): multi-year features and export rules, the ortho
fetcher's geometry and source choice, and the random-tile matching. No network, no models."""

from __future__ import annotations

import math
import unittest

import numpy as np

from hoop_pipeline import gold_assist, multiyear, ortho


def years_table(rows: list[tuple]) -> dict[str, np.ndarray]:
    """rows: (id, year, is_ref, ok, conf_box, verify_det, ndvi, date)."""
    cols = list(zip(*rows))
    n = len(rows)
    return {"id": np.array(cols[0], dtype=object), "year": np.array(cols[1]), "is_ref": np.array(cols[2], bool),
            "ok": np.array(cols[3], bool), "conf_box": np.array(cols[4], np.float32),
            "conf_near": np.zeros(n, np.float32), "verify_det": np.array(cols[5], np.float32),
            "ndvi_box": np.array(cols[6], np.float32), "gsd": np.full(n, 0.6), "date": np.array(cols[7], dtype=object),
            "corr_ref": np.full(n, np.nan, np.float32)}


class TestYearFeatures(unittest.TestCase):
    def test_persistent_and_new(self) -> None:
        y = years_table([
            ("a", 2023, True, True, 0.8, 0.95, -0.05, "2023-06-01"),
            ("a", 2021, False, True, 0.6, 0.9, -0.04, "2021-07-01"),
            ("a", 2019, False, True, 0.3, 0.5, -0.02, "2019-11-20"),
            ("b", 2023, True, True, 0.7, 0.9, -0.03, "2023-06-01"),
            ("b", 2021, False, True, 0.0, float("nan"), 0.45, "2021-07-01"),
            ("b", 2019, False, False, 0.0, float("nan"), 0.0, "2019-07-01"),
        ])
        f = multiyear.year_features(y)
        i = {k: j for j, k in enumerate(f["id"].tolist())}
        a, b = i["a"], i["b"]
        self.assertEqual((f["years_checked"][a], f["years_seen"][a], f["other_seen"][a]), (3, 3, 2))
        self.assertEqual(f["other_seen50"][a], 1)
        self.assertEqual(f["leafoff_checked"][a], 1)  # the November flight
        self.assertEqual(f["first_seen_year"][a], 2019)
        self.assertFalse(f["slab_appeared"][a])
        # b: only the scan year shows it; the older checked flight was vegetated -> built since then
        self.assertEqual((f["years_checked"][b], f["other_checked"][b], f["other_seen"][b]), (2, 1, 0))
        self.assertTrue(f["slab_appeared"][b])
        self.assertAlmostEqual(f["persist"][b], 0.5)
        self.assertEqual(f["verify_mean_other"][b], 0.0)  # no detection in the other year: verifier counts 0


class TestRules(unittest.TestCase):
    def cands(self) -> dict[str, np.ndarray]:
        return {"id": np.array(["p", "q", "r", "s"], dtype=object), "state": np.array(["CA"] * 4, dtype=object),
                "privacy": np.array(["public", "public", "private_home", "public"], dtype=object),
                "p_home": np.array([0.1, 0.1, 0.9, np.nan]), "conf": np.array([0.6, 0.6, 0.9, 0.9]),
                "conf_v2": np.array([0.6, 0.6, 0.9, 0.9]), "verify_scan": np.array([0.95, 0.92, 0.99, 0.99]),
                "other_checked": np.array([3, 3, 3, 3]), "other_seen": np.array([3, 0, 3, 3]),
                "persist": np.array([1.0, 0.25, 1.0, 1.0]), "conf_mean_other": np.array([0.6, 0.0, 0.6, 0.6]),
                "score_v4": np.array([0.7, 0.4, 0.9, 0.9]), "export": np.array([False, True, False, False])}

    def test_privacy_never_relaxed(self) -> None:
        d = self.cands()
        sel = multiyear.select_rule(d, multiyear.parse_rule("conf=0.5,v2=0.5,verify=0.9,score=0.0,or_v3=1"), {})
        self.assertFalse(sel[2])  # private_home
        self.assertFalse(sel[3])  # no context-CNN score

    def test_score_and_v3_rescue(self) -> None:
        d = self.cands()
        r = multiyear.parse_rule("conf=0.5,v2=0.5,verify=0.9,score=0.6,v3_score=0.35")
        self.assertEqual(multiyear.select_rule(d, r, {}).tolist(), [True, True, False, False])
        r = multiyear.parse_rule("conf=0.5,v2=0.5,verify=0.9,score=0.6,v3_score=0.5")
        self.assertEqual(multiyear.select_rule(d, r, {}).tolist(), [True, False, False, False])
        r = multiyear.parse_rule("conf=0.5,v2=0.5,verify=states,drop_unseen=1")
        self.assertEqual(multiyear.select_rule(d, r, {"CA": 0.9}).tolist(), [True, False, False, False])


class TestOrtho(unittest.TestCase):
    def test_merc_and_source(self) -> None:
        x, y = ortho.merc(0.0, 0.0)
        self.assertAlmostEqual(x, 0.0, places=3)
        self.assertAlmostEqual(y, 0.0, places=3)
        x1, _ = ortho.merc(1.0, 0.0)
        self.assertAlmostEqual(x1, ortho.R_MERC * math.radians(1.0), places=3)
        src = [{"key": "state", "states": ["NY"]}, {"key": "city", "states": ["NY"], "bbox": [-74.3, 40.4, -73.7, 41.0],
                                                     "priority": 1}]
        self.assertEqual(ortho.source_for(src, "NY", -73.99, 40.73)["key"], "city")
        self.assertEqual(ortho.source_for(src, "NY", -76.0, 43.0)["key"], "state")
        self.assertIsNone(ortho.source_for(src, "NJ", -74.2, 40.7))

    def test_blank(self) -> None:
        self.assertTrue(ortho.blank(np.full((64, 64, 3), 255, np.uint8)))
        rng = np.random.default_rng(0)
        self.assertFalse(ortho.blank(rng.integers(0, 255, (64, 64, 3), dtype=np.uint8)))


class TestGoldMatch(unittest.TestCase):
    def test_matches(self) -> None:
        lab = np.array([[100, 100, 140, 125]], float)
        pred = np.array([[102, 101, 141, 126], [300, 300, 340, 325], [115, 108, 117, 110]], float)
        m = gold_assist.matches(lab, pred)
        self.assertEqual(m[0].tolist(), [True, False, True])  # IoU match, far box, point inside the court
        self.assertEqual(gold_assist.wilson(0, 0), (None, None))
        lo, hi = gold_assist.wilson(5, 10)
        self.assertLess(lo, 0.5)
        self.assertGreater(hi, 0.5)


if __name__ == "__main__":
    unittest.main()
