"""Offline tests of the v3 helpers: the privacy update for a new p_home, and the dataset builder's extra layers.

    python -m unittest tests.test_v3 -v          # from pipeline/
"""

from __future__ import annotations

import itertools
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import shapely  # noqa: E402
from shapely import STRtree  # noqa: E402

from hoop_pipeline import build_dataset_us as bd  # noqa: E402
from hoop_pipeline import v3  # noqa: E402
from hoop_pipeline.geoparquet import write_geoparquet  # noqa: E402
from hoop_pipeline.privacy import classify_point  # noqa: E402


def features(**kw: object) -> dict:
    """A point_features() dict with nothing nearby, updated with kw."""
    f = {"poly_sites": [], "pt_sites": [], "restricted": False, "private_site": False, "padus_inset_m": -1.0,
         "in_residential": False, "in_apartments": False, "apt_bldg_40m": False, "house_35m": 0, "house_60m": 0,
         "mid_100m": 0, "large_150m": 0, "nearest_bldg_m": math.inf, "nearest_bldg_area": math.nan,
         "nearest_bldg_class": "", "apt_class_40m": False, "civic_150m": False, "house_xy_35m": []}
    f.update(kw)
    return f


class ReclassifyTests(unittest.TestCase):
    """reclassify(rules-only class, p_home) must equal classify_point(features, p_home) in class and site."""

    CASES = {
        "restricted": features(restricted=True),
        "osm_park_polygon": features(poly_sites=[("park", "osm", 5000.0, "X Park", 1e9)]),
        "padus_polygon": features(poly_sites=[("park", "padus", 9e5, "Forest", 45.0)]),
        "padus_edge_then_unknown": features(poly_sites=[("park", "padus", 9e5, "Forest", 5.0)]),
        "private_site": features(private_site=True),
        "apartments": features(in_apartments=True),
        "point_site_clear": features(pt_sites=[("school", "nces_public", 90.0, "S", 0.0, 0.0)]),
        "point_site_house_lot": features(pt_sites=[("church", "overture", 60.0, "C", 0.0, 0.0)], house_35m=1,
                                         house_xy_35m=[(40.0, 40.0)]),
        "point_site_house_lot_mid": features(pt_sites=[("church", "overture", 60.0, "C", 0.0, 0.0)], house_35m=1,
                                             house_xy_35m=[(40.0, 40.0)], mid_100m=1),
        "point_site_own_building": features(pt_sites=[("church", "osm", 30.0, "C", 0.0, 0.0)], house_35m=1,
                                            house_xy_35m=[(5.0, 5.0)]),
        "house": features(house_35m=2),
        "residential": features(in_residential=True),
        "unknown": features(),
    }

    def test_matches_classify_point(self) -> None:
        for (name, f), ph in itertools.product(self.CASES.items(), [0.0, 0.1, 0.24, 0.25, 0.3, 0.49, 0.5, 0.69, 0.7,
                                                                   0.84, 0.85, 0.99]):
            rules = classify_point(f, float("nan"))
            want = classify_point(f, ph)
            got = v3.reclassify(*rules, ph)
            with self.subTest(case=name, p_home=ph, rules=rules):
                self.assertEqual(got[0], want[0])
                self.assertEqual(got[1], want[1])

    def test_nan_keeps_class(self) -> None:
        for f in self.CASES.values():
            rules = classify_point(f, float("nan"))
            self.assertEqual(v3.reclassify(*rules, float("nan")), rules)


class BuilderExtraTests(unittest.TestCase):
    """merge_extra_courts drops extras that OSM covers; compute_labels labels extras, drops maybe chips, flags hard."""

    LAT, LON = 40.0, -100.0

    def box(self, cx: float, cy: float, w: float = 28.0, h: float = 15.0) -> shapely.Polygon:
        k = 111_320.0
        dx, dy = 1 / (k * math.cos(math.radians(self.LAT))), 1 / k
        x, y = self.LON + cx * dx, self.LAT + cy * dy
        return shapely.box(x - w / 2 * dx, y - h / 2 * dy, x + w / 2 * dx, y + h / 2 * dy)

    def layer(self, geoms: list, roles: list[str]) -> bd.CourtLayer:
        g = np.asarray(geoms, dtype=object)
        c = shapely.centroid(g)
        n = len(g)
        return bd.CourtLayer(np.array([f"way/{i}" for i in range(n)], dtype=object), np.full(n, "KS", dtype=object),
                             shapely.get_x(c), shapely.get_y(c), g, np.asarray(roles, dtype=object), np.full(n, 15.0),
                             STRtree(g), np.full(n, "osm", dtype=object))

    def test_merge_and_labels(self) -> None:
        osm = self.layer([self.box(0, 0)], ["label"])
        extra = [("pseudo:a", "pseudo", "label", self.box(1, 0)),       # on the OSM court -> dropped
                 ("official:b", "official", "label", self.box(60, 0)),  # new label
                 ("pseudo:c", "pseudo", "label", self.box(61, 1)),      # under the official one -> dropped
                 ("maybe:d", "maybe", "ambiguous", self.box(0, 200)),   # likely court -> chip dropped
                 ("hard:e", "hard", "hard", self.box(-200, -40)),       # look-alike -> hard negative
                 ("maybe:f", "maybe", "ambiguous", self.box(0, 1))]     # on the OSM court -> dropped
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "extra.parquet"
            g = [e[3] for e in extra]
            c = shapely.centroid(np.asarray(g, dtype=object))
            write_geoparquet(path, {"ref": [e[0] for e in extra], "kind": [e[1] for e in extra],
                                    "role": [e[2] for e in extra], "state": ["KS"] * len(extra),
                                    "center_lon": shapely.get_x(c), "center_lat": shapely.get_y(c)}, g)
            merged = bd.merge_extra_courts(osm, path)
        self.assertEqual(sorted(merged.ref[1:].tolist()), ["hard:e", "maybe:d", "official:b"])

        class Cat:  # just what plan_windows needs: one UTM-ish metric grid (here: EPSG:5070 at 0.6 m)
            epsg = np.array([5070])
            transform = np.array([[0.6, 0.0, -1e6, 0.0, -0.6, 2e6]])

        from pyproj import Transformer

        tr = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
        x0, y0 = tr.transform(self.LON, self.LAT)
        Cat.transform = np.array([[0.6, 0.0, x0 - 5000, 0.0, -0.6, y0 + 5000]])
        from hoop_pipeline.naip_index import plan_windows

        # chip A: centred between the OSM court and the official one; chip B: on the maybe box; chip C: on the hard box
        lons = np.array([self.LON + 30 / (111_320 * math.cos(math.radians(self.LAT))), self.LON,
                         self.LON - 200 / (111_320 * math.cos(math.radians(self.LAT)))])
        lats = np.array([self.LAT, self.LAT + 200 / 111_320, self.LAT - 40 / 111_320])
        wp = plan_windows(Cat(), np.zeros(3, np.int64), lons, lats, 320, 0.6)  # type: ignore[arg-type]
        lab = bd.compute_labels(wp, lons, lats, merged, 0.6, 0.35, 6.0)
        self.assertEqual(sorted(lab.refs[0]), ["official:b", "way/0"])
        self.assertTrue(lab.ambiguous[1])
        self.assertIsNotNone(lab.ambiguous_extra)
        assert lab.ambiguous_extra is not None and lab.hard is not None
        self.assertTrue(lab.ambiguous_extra[1])
        self.assertFalse(lab.ambiguous[0])
        self.assertTrue(lab.hard[2])
        self.assertFalse(lab.hard[0])


if __name__ == "__main__":
    unittest.main()
