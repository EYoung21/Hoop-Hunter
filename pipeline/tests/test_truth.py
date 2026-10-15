"""Offline tests of hoop_pipeline.truth: filter language, Esri geometry conversion, build + evaluate.

    python -m unittest tests.test_truth -v          # from pipeline/
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402
import shapely  # noqa: E402
from shapely.geometry import shape  # noqa: E402

from hoop_pipeline import truth  # noqa: E402

LAT0, LON0 = 40.70, -73.95
DLAT = 1 / 111_320          # degrees per metre (north)
DLON = 1 / (111_320 * 0.7581)  # degrees per metre (east) at 40.7 N


def rect(cx_m: float, cy_m: float, w: float = 28, h: float = 15) -> dict:
    x0, x1 = LON0 + (cx_m - w / 2) * DLON, LON0 + (cx_m + w / 2) * DLON
    y0, y1 = LAT0 + (cy_m - h / 2) * DLAT, LAT0 + (cy_m + h / 2) * DLAT
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


class FilterTests(unittest.TestCase):
    def test_predicates(self):
        p = {"TYPE": "Basketball ", "N": "2", "STATUS": "Removed", "FLAG": "Y"}
        self.assertTrue(truth.predicate({"field": "type", "eq": "basketball"}, p))
        self.assertTrue(truth.predicate({"field": "TYPE", "regex": "basket"}, p))
        self.assertTrue(truth.predicate({"all": [{"field": "N", "gt": 1}, {"field": "FLAG", "truthy": True}]}, p))
        self.assertFalse(truth.predicate({"field": "STATUS", "notin": ["removed"]}, p))
        self.assertTrue(truth.predicate({"sum_gt": ["N", "MISSING"], "value": 1}, p))
        self.assertTrue(truth.predicate({"not": {"field": "MISSING", "isnull": False}}, p))

    def test_counts_and_maps(self):
        p = {"FULL": "2", "HALF": 1, "S": "Inactive"}
        self.assertEqual(truth._court_count(p, ["FULL", "HALF"]), 3)
        self.assertEqual(truth._court_count(p, {"weights": {"FULL": 1, "HALF": 0.5}}), 3)
        self.assertIsNone(truth._court_count({}, "FULL"))
        self.assertEqual(truth._court_count({"D": "6 Hoops"}, {"weights": {"D": 0.5}}), 3)
        self.assertIsNone(truth._court_count({"D": "Half Court"}, {"weights": {"D": 0.5}}))
        self.assertEqual(truth._mapped(p, {"field": "S", "map": {"inactive": "inactive"}}, "x"), "inactive")
        self.assertEqual(truth._mapped(p, {"field": "S", "regex_map": [["^in", "i"]]}, "x"), "i")
        self.assertEqual(truth._mapped(p, "outdoor", "unknown"), "outdoor")

    def test_esri_rings(self):
        shell = [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]       # clockwise = outer ring in Esri JSON
        hole = [[2, 2], [4, 2], [4, 4], [2, 4], [2, 2]]            # counter-clockwise = hole
        g = shape(truth.esri_geometry_to_geojson({"rings": [shell, hole]}))
        self.assertAlmostEqual(g.area, 96.0)
        self.assertEqual(truth.esri_geometry_to_geojson({"x": 1.5, "y": 2.5})["coordinates"], [1.5, 2.5])
        self.assertIsNone(truth.esri_geometry_to_geojson({"x": None, "y": None}))


class BuildEvaluateTests(unittest.TestCase):
    def test_build_and_evaluate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            raw = tmp / "raw" / "demo"
            raw.mkdir(parents=True)
            courts = [
                {"type": "Feature", "properties": {"ID": "a", "KIND": "Basketball", "ST": "Active"}, "geometry": rect(0, 0)},
                {"type": "Feature", "properties": {"ID": "b", "KIND": "Basketball", "ST": "Active"}, "geometry": rect(60, 0)},
                {"type": "Feature", "properties": {"ID": "c", "KIND": "Basketball", "ST": "Removed"}, "geometry": rect(0, 60)},
                {"type": "Feature", "properties": {"ID": "d", "KIND": "Tennis", "ST": "Active"}, "geometry": rect(0, -60)},
            ]
            (raw / "courts.geojsonl").write_text("\n".join(json.dumps(f) for f in courts) + "\n", encoding="utf-8")
            park = {"type": "Feature", "properties": {"PID": "P1", "NAME": "Demo Park"},
                    "geometry": rect(30, 0, w=200, h=200)}
            (raw / "coverage.geojsonl").write_text(json.dumps(park) + "\n", encoding="utf-8")
            spec = {"key": "demo", "city": "Demo", "state": "NY", "precision": "court_polygon",
                    "api": {"kind": "file", "url": "x"},
                    "select": {"field": "KIND", "eq": "Basketball"},
                    "fields": {"source_id": "ID", "indoor": "outdoor",
                               "status": {"field": "ST", "map": {"Active": "active", "Removed": "removed"}}},
                    "coverage": {"kind": "park", "complete": "yes",
                                 "layer": {"kind": "file", "url": "x", "id_field": "PID", "name_field": "NAME"}}}
            paths = dict(RAW_DIR=tmp / "raw", TRUTH_DIR=tmp, COURTS_FILE=tmp / "c.parquet",
                         COVERAGE_FILE=tmp / "cov.parquet", MANIFEST_FILE=tmp / "s.json")
            with mock.patch.multiple(truth, **paths):
                man = truth.build([spec])
                self.assertEqual(man["sources"]["demo"]["rows"], 2)
                self.assertEqual(man["sources"]["demo"]["rows_in_coverage"], 2)
                cols, _ = truth.load_truth(tmp / "c.parquet")
                self.assertEqual(sorted(cols["park_name"].tolist()), ["Demo Park", "Demo Park"])
                self.assertTrue(all(a > 400 for a in cols["area_m2"]))
                # candidates: one on court a (0.9), one 8 m off court b (0.3), one stray in the park (0.8)
                cand = tmp / "cand.parquet"
                lons = [LON0, LON0 + 60 * DLON, LON0 + 30 * DLON]
                lats = [LAT0, LAT0 + 15 * DLAT, LAT0 + 70 * DLAT]
                pq.write_table(pa.table({"lon": lons, "lat": lats, "conf": [0.9, 0.3, 0.8],
                                         "state": ["NY"] * 3}), cand)
                res = truth.evaluate(cand, None, [0.25, 0.5], 40.0, 15.0, 250.0, None, None, False, None)
            e = res["sources"]["demo"]
            self.assertEqual(e["recall"], {"0.25": 1.0, "0.50": 0.5})
            self.assertEqual(e["in_covered_parks"]["0.25"], {"detections": 3, "match_official": 2, "share": 0.667})
            self.assertEqual(e["in_covered_parks"]["0.50"]["match_official"], 1)
            self.assertTrue(shapely.is_valid(shapely.from_wkb(cols["footprint_wkb"][0])))


if __name__ == "__main__":
    unittest.main()
