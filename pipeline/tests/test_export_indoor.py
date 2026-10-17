"""Offline tests for the OSM indoor flag in the app export: indoor venues get ``indoor: true``, roofed outdoor
courts stay outdoor, and the validator accepts the optional key only on OSM courts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hoop_pipeline import export_app


def osm_court(osm_id: int, tags: dict[str, str]) -> dict:
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-85.3, 35.0]},
            "properties": {"osm_type": "way", "osm_id": osm_id, "osm_ref": f"way/{osm_id}",
                           "center_lon": -85.3, "center_lat": 35.0, "tags": {"sport": "basketball", **tags}}}


class OsmIndoor(unittest.TestCase):
    def test_indoor_venues(self) -> None:
        for tags in ({"leisure": "sports_centre"}, {"leisure": "sports_hall"}, {"leisure": "fitness_centre"},
                     {"building": "sports_hall"}, {"amenity": "community_centre"},
                     {"leisure": "pitch", "indoor": "yes"}, {"leisure": "pitch", "building": "yes"},
                     {"leisure": "pitch", "covered": "yes", "indoor": "yes"}):
            self.assertTrue(export_app.osm_indoor(tags), tags)

    def test_outdoor_and_roofed_courts(self) -> None:
        for tags in ({"leisure": "pitch"}, {"leisure": "pitch", "covered": "yes"}, {"leisure": "pitch", "lit": "yes"}):
            self.assertFalse(export_app.osm_indoor(tags), tags)

    def test_export_marks_only_indoor_and_validates(self) -> None:
        fc = {"type": "FeatureCollection", "features": [
            osm_court(1, {"leisure": "pitch"}),
            osm_court(2, {"leisure": "sports_centre"}),
            osm_court(3, {"leisure": "pitch", "covered": "yes"}),
        ]}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "courts.geojson"
            path.write_text(json.dumps(fc), encoding="utf-8")
            feats = export_app.osm_features(path, "tennessee", "2026-10-17")
        by_id = {f["properties"]["id"]: f["properties"] for f in feats}
        self.assertNotIn("indoor", by_id["osm-way-1"])
        self.assertIs(by_id["osm-way-2"]["indoor"], True)
        self.assertNotIn("indoor", by_id["osm-way-3"])
        self.assertEqual(export_app.validate({"type": "FeatureCollection", "features": feats}), [])

    def test_validator_rejects_indoor_elsewhere(self) -> None:
        base = {"id": "det-1", "name": None, "source": "detected", "status": "candidate", "confidence": 0.9,
                "hoops": None, "surface": None, "lit": None, "address": None, "region": "tennessee",
                "osm_id": None, "updated": "2026-10-17"}
        feat = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-85.3, 35.0]},
                "properties": {**base, "indoor": True}}
        self.assertTrue(export_app.validate({"type": "FeatureCollection", "features": [feat]}))
        osm = {**base, "id": "osm-way-9", "source": "osm", "status": "verified", "confidence": None,
               "osm_id": "way/9", "indoor": False}
        feat = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-85.3, 35.0]}, "properties": osm}
        self.assertTrue(export_app.validate({"type": "FeatureCollection", "features": [feat]}))


if __name__ == "__main__":
    unittest.main()
