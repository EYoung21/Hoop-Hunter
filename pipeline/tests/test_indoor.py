"""Offline tests of hoop_pipeline.indoor: Overture / OSM classification, school levels, dedupe and merge,
and the app-schema GeoJSON.

    python -m unittest tests.test_indoor -v          # from pipeline/
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hoop_pipeline import indoor  # noqa: E402

LAT0, LON0 = 36.16, -86.78
DLAT = 1 / 111_320
DLON = 1 / (111_320 * 0.807)


def row(src: str, name: str | None, dx_m: float, dy_m: float, access: str = "unknown", kind: str = "rec_center",
        lik: str = "likely") -> dict[str, Any]:
    return {"src": src, "src_id": f"{src}-{name}-{dx_m}", "name": name, "lon": LON0 + dx_m * DLON,
            "lat": LAT0 + dy_m * DLAT, "kind": kind, "likelihood": lik, "access": access, "address": None,
            "evidence": src, "url": None}


class ClassifyTests(unittest.TestCase):
    def test_overture_classes(self) -> None:
        self.assertEqual(indoor.overture_class("basketball_court", "Hoops Academy", None),
                         ("basketball_venue", "listed", "unknown"))
        self.assertEqual(indoor.overture_class("gym", "LA Fitness", "LA Fitness"), ("gym", "likely", "membership"))
        self.assertIsNone(indoor.overture_class("gym", "Planet Fitness", None))
        self.assertIsNone(indoor.overture_class("gym", "Iron Temple Gym", None))       # no court signal
        # access is "public" only when the name says a government runs it
        self.assertEqual(indoor.overture_class("community_center", "Smith Recreation Center", None),
                         ("rec_center", "likely", "unknown"))
        self.assertEqual(indoor.overture_class("community_center", "City of Tulsa Smith Recreation Center", None),
                         ("rec_center", "likely", "public"))
        self.assertEqual(indoor.overture_class("community_center", "Oak Hill Community Center", None),
                         ("community_center", "possible", "unknown"))
        self.assertIsNone(indoor.overture_class("basketball_court", "Tompkins Square Park", None))   # outdoor
        self.assertIsNone(indoor.overture_class("gym", "YMCA Headstart", None))
        self.assertIsNone(indoor.overture_class("gym", "24 Hour Fitness", None))
        sport = indoor.overture_class("gym", "24 Hour Fitness Super-Sport", None)
        assert sport is not None
        self.assertEqual(sport[1], "likely")
        bgc = indoor.overture_class("youth_organization", "Boys & Girls Club of X", None)
        assert bgc is not None
        self.assertEqual(bgc[2], "membership")
        self.assertIsNone(indoor.overture_class("community_center", "First Baptist Church Fellowship", None))

    def test_osm_access(self) -> None:
        self.assertEqual(indoor.osm_access({"access": "private"}), "membership")
        self.assertEqual(indoor.osm_access({"operator:type": "government"}), "public")
        self.assertEqual(indoor.osm_access({"operator:type": "university", "access": "yes"}), "school")
        self.assertEqual(indoor.osm_access({}), "unknown")

    def test_school_levels(self) -> None:
        self.assertEqual(indoor.school_likelihood("Central High School", "school_gym"), "likely")
        self.assertEqual(indoor.school_likelihood("Oak Elementary School", "school_gym"), "possible")
        self.assertEqual(indoor.school_likelihood("State University", "college_gym"), "likely")


class DedupeTests(unittest.TestCase):
    def test_merge_rules(self) -> None:
        rows = [
            row("osm", "Smith Recreation Center", 0, 0, access="public", lik="listed"),
            row("overture", "Smith Rec Center", 60, 0),                       # same name core, 60 m -> merge
            row("web", "Smith Recreation Center", 30, 20, access="public", lik="listed"),
            row("nces_public", "Smith High School", 20, 0, access="school", kind="school_gym"),   # never with rec
            row("overture", None, 300, 0),                                    # far, unnamed -> alone
            row("overture", "Jones Gym", 320, 0),                             # 20 m from unnamed -> merge
        ]
        groups = sorted(sorted(g) for g in indoor.dedupe(rows, 150.0, 40.0))
        self.assertEqual(groups, [[0, 1, 2], [3], [4, 5]])
        pair = [row("overture", "Sanibel Recreation Center", 0, 0),
                row("nces_public", "The Sanibel School", 30, 0, access="school", kind="school_gym")]
        self.assertEqual(len(indoor.dedupe(pair, 150.0, 40.0)), 2)     # same core name, but a rec center
        m = indoor.merge_group(rows, [0, 1, 2])
        self.assertEqual(m["sources"], "osm;web;overture")
        self.assertEqual(m["access"], "public")
        self.assertEqual(m["likelihood"], "listed")
        self.assertEqual(m["name"], "Smith Recreation Center")
        self.assertEqual(indoor.status_of(m), "verified")
        s = indoor.merge_group(rows, [3])
        self.assertEqual((s["access"], s["kind"]), ("school", "school_gym"))
        self.assertEqual(indoor.status_of(s), "candidate")

    def test_geojson_schema(self) -> None:
        m = indoor.merge_group([row("overture", "Smith Rec", 0, 0)], [0])
        m.update(id="indoor-abc", region="tennessee", state="TN", status="candidate", site="rec", updated="2026-10-17")
        feat = indoor.feature(m)
        fc: dict[str, Any] = {"type": "FeatureCollection", "features": [feat]}
        self.assertEqual(indoor.validate(fc), [])
        props = feat["properties"]
        self.assertEqual(list(props), indoor.APP_KEYS + indoor.EXTRA_KEYS)
        self.assertIs(props["indoor"], True)
        props["access"] = "everyone"
        self.assertTrue(indoor.validate(fc))


if __name__ == "__main__":
    unittest.main()
