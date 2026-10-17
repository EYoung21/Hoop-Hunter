"""Offline tests of hoop_pipeline.websources: robots.txt matching, URL handling, HTML parsing, extraction
rules, amenity tables, boilerplate removal, names, and the polite fetcher against a fake session.

    python -m unittest tests.test_websources -v          # from pipeline/
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hoop_pipeline import websources as w  # noqa: E402


def mention(line: str, before: list[str] | None = None, after: list[str] | None = None) -> w.Mention | None:
    lines = (before or []) + [line] + (after or [])
    return w.classify_line(lines, len(before or []))


class RobotsTests(unittest.TestCase):
    def test_wildcards_longest_match_and_groups(self) -> None:
        r = w.Robots.parse("User-agent: *\nDisallow: /*?\nDisallow: /admin\nAllow: /admin/public$\n"
                           "Crawl-delay: 5\nSitemap: https://x.gov/s.xml\n")
        self.assertFalse(r.allowed("https://x.gov/parks?id=1"))
        self.assertTrue(r.allowed("https://x.gov/parks/smith"))
        self.assertFalse(r.allowed("https://x.gov/admin/x"))
        self.assertTrue(r.allowed("https://x.gov/admin/public"))
        self.assertFalse(r.allowed("https://x.gov/admin/public/x"))
        self.assertEqual(r.crawl_delay, 5.0)
        self.assertEqual(r.sitemaps, ["https://x.gov/s.xml"])

    def test_specific_group_wins_over_star(self) -> None:
        r = w.Robots.parse("User-agent: HoopHunterResearch\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
        self.assertFalse(r.allowed("https://x.gov/parks"))
        r = w.Robots.parse("User-agent: Googlebot\nDisallow: /\n\nUser-agent: *\nDisallow: /private\n")
        self.assertTrue(r.allowed("https://x.gov/parks"))
        self.assertFalse(r.allowed("https://x.gov/private/a"))

    def test_challenge_markers(self) -> None:
        self.assertTrue(w.CHALLENGE_RE.search("<html><head><title>Just a moment...</title>"))
        self.assertTrue(w.CHALLENGE_RE.search("Attention Required! | Cloudflare"))
        self.assertFalse(w.CHALLENGE_RE.search('<script src="https://www.google.com/recaptcha/api.js"></script>'))
        self.assertFalse(w.CHALLENGE_RE.search('<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js">'))

    def test_empty_disallow_allows_everything(self) -> None:
        self.assertTrue(w.Robots.parse("User-agent: *\nDisallow:\n").allowed("https://x.gov/a"))
        self.assertFalse(w.Robots(disallow_all=True).allowed("https://x.gov/a"))


class UrlTests(unittest.TestCase):
    def test_normalise(self) -> None:
        self.assertEqual(w.normalise_url("www.Foo.gov/Parks/Smith/?utm_source=x#top"), "http://www.foo.gov/Parks/Smith")
        self.assertEqual(w.normalise_url("https://foo.gov"), "https://foo.gov/")
        self.assertIsNone(w.normalise_url("mailto:a@b.c"))
        self.assertIsNone(w.normalise_url("localhost/x"))

    def test_blocked_and_templates(self) -> None:
        self.assertTrue(w.is_blocked_url("https://www.facebook.com/smithpark"))
        self.assertTrue(w.is_blocked_url("https://maps.google.com/?q=x"))
        self.assertFalse(w.is_blocked_url("https://www.dallasparks.org/Facilities/Facility/Details/x-1"))
        self.assertEqual(w.url_template("https://a.gov/parks/smith-park"), "/parks/")
        self.assertIsNone(w.url_template("https://a.gov/smith"))
        self.assertEqual(w.url_template("https://a.gov/park.aspx?id=3&x=1"), "/park.aspx?id&x")
        self.assertTrue(w.same_template("https://a.gov/parks/jones", "/parks/"))
        self.assertFalse(w.same_template("https://a.gov/parks/a/b", "/parks/"))
        self.assertTrue(w.same_template("https://a.gov/park.aspx?x=2&id=9", "/park.aspx?id&x"))


class ParserTests(unittest.TestCase):
    HTML = """<html><head><title>Smith Park | City of Foo</title></head><body>
      <nav><a href='/x'>Basketball League</a></nav>
      <div class='main-menu'><a href='/y'>Basketball courts</a></div>
      <div class='content'><h1>Smith Park</h1>
        <ul class='amenities'><li><img alt='Basketball'></li><li>Playground</li><li>Picnic Shelter</li></ul>
        <table><tr><th>Park</th><th>Basketball</th><th>Tennis</th></tr>
          <tr><td>Smith Park</td><td>2</td><td>X</td></tr><tr><td>Jones Park</td><td></td><td>X</td></tr>
          <tr><td>Oak Park</td><td>X</td><td></td></tr></table>
        <p>Smith park offers two lighted basketball courts.</p>
        <a href='/parks/jones'>Jones Park</a></div>
      <footer>Basketball Courts info</footer></body></html>"""

    def test_parse_skips_navigation_and_keeps_icons_tables_links(self) -> None:
        pp = w.parse_html(self.HTML, "https://foo.gov/parks/smith")
        self.assertEqual(pp.title, "Smith Park | City of Foo")
        icon = w.parse_html("<html><head><title>Oak Park</title></head><body><svg><title>Arrow Left</title></svg>"
                            "<h1>Oak Park</h1></body></html>")
        self.assertEqual(icon.title, "Oak Park")
        tagged = w.parse_html('<div class="post-7 places amenities-basketball-court amenities-playground"><p>Hi</p></div>')
        self.assertEqual(w.judge_page(tagged.lines, "Arno Park").mention_kind, "court")   # WordPress taxonomy class
        self.assertEqual(w.page_name("Facilities • Oak Park", "", ""), "Oak Park")
        self.assertEqual(pp.h1, "Smith Park")
        joined = "\n".join(pp.lines)
        self.assertNotIn("League", joined)
        self.assertNotIn("info", joined)
        self.assertIn("Basketball", pp.lines)
        self.assertIn("Smith Park | 2 | X", pp.lines)
        self.assertEqual(len(pp.tables), 1)
        self.assertIn(("/parks/jones", "Jones Park"), pp.links)

    def test_amenity_matrix(self) -> None:
        pp = w.parse_html(self.HTML)
        rows = w.table_rows_with_basketball(pp.tables)
        self.assertEqual([(n, c) for n, c, _ in rows], [("Smith Park", 2), ("Oak Park", None)])

    def test_multi_park_rows(self) -> None:
        tables = [[["Park", "Amenities"], ["City Park 9701 E. 16th Ave.", "basketball courts (2), playground"],
                   ["Del Mar Park", "basketball court, tennis"], ["Utah Park", "picnic, playground"],
                   ["Expo Park", "basketball, soccer"]]]
        self.assertEqual([(n, c) for n, c, _ in w.multi_park_rows([], tables)],
                         [("City Park", 2), ("Del Mar Park", None), ("Expo Park", None)])
        lines = ["Adams Park", "Playground, Basketball court", "Bates Park", "Picnic shelter", "Cedar Park",
                 "Basketball court, Tennis courts", "Divide Park", "Half-court basketball"]
        self.assertEqual([n for n, _, _ in w.multi_park_rows(lines, [])], ["Adams Park", "Cedar Park", "Divide Park"])
        self.assertEqual(w.multi_park_rows(["Smith Park", "Two basketball courts", "Picnic Shelter"], []), [])

    def test_basketball_list_page(self) -> None:
        lines = ["Alamosa Park - 2 half courts", "Country Hills Park (4)", "Park hours are 6am to 10pm",
                 "Los Altos Park", "Contact us"]
        rows = w.basketball_list_page("Basketball Court Locations", lines)
        self.assertEqual([(n, c) for n, c, _ in rows], [("Alamosa Park", None), ("Country Hills Park", 4),
                                                        ("Los Altos Park", None)])
        self.assertEqual(w.basketball_list_page("Smith Park", lines), [])

    def test_page_judgement(self) -> None:
        pp = w.parse_html(self.HTML)
        res = w.judge_page(pp.lines, pp.title)
        self.assertTrue(res.has_court)
        self.assertEqual(res.courts, 2)
        self.assertEqual(res.indoor_outdoor, "outdoor")
        self.assertEqual(w.page_name(pp.title, pp.h1), "Smith Park")


class RuleTests(unittest.TestCase):
    AMEN = ["Playground", "Picnic Shelter"]

    def check(self, line: str, kind: str | None, courts: int | None = None, half: int | None = None,
              hoops: int | None = None, negative: bool = False, planned: bool = False) -> w.Mention | None:
        m = mention(line, self.AMEN, ["Restrooms"])
        if kind is None:
            self.assertIsNone(m, line)
            return None
        assert m is not None, line
        self.assertEqual(m.kind, kind, line)
        self.assertEqual((m.courts, m.half_courts, m.hoops), (courts, half, hoops), line)
        self.assertEqual((m.negative, m.planned), (negative, planned), line)
        return m

    def test_court_lines_and_counts(self) -> None:
        self.check("Two lighted basketball courts", "court", courts=2)
        self.check("Basketball Courts (2)", "court", courts=2)
        self.check("Basketball Court: 2", "court", courts=2)
        self.check("4 half-court basketball courts", "court", half=4)
        self.check("1 full basketball court and 2 half courts", "court", courts=1, half=2)
        self.check("Basketball | 2", "table", courts=2)
        self.check("6 basketball hoops", "court", hoops=6)
        self.check("Basketball Court | 1/2", "court", half=1)
        m = w.classify_line(["Athletic Fields & Courts", "Basketball", "Full Courts: 1", "Multipurpose Field"], 1)
        assert m is not None
        self.assertEqual((m.kind, m.courts), ("amenity_item", 1))
        m = w.classify_line(["Picnic", "Basketball", "Tennis Courts: 2"], 1)
        assert m is not None
        self.assertIsNone(m.courts)

    def test_amenity_lists(self) -> None:
        self.check("Basketball", "amenity_item")
        self.check("Hoops", "amenity_item")
        self.check("Playground, Basketball, Tennis, Restrooms", "amenity_sentence")
        self.check("Amenities include a playground, picnic shelter and basketball.", "amenity_sentence")
        lone = w.classify_line(["Programs", "Basketball", "Volunteer"], 1)   # no amenity list around it
        assert lone is not None
        self.assertEqual(lone.kind, "lone")
        self.assertNotIn(lone.kind, w.COURT_KINDS)

    def test_program_lists_are_not_amenities(self) -> None:
        m = w.classify_line(["NOW REGISTERING", "Fall Kickball", "Basketball", "Winter Soccer"], 2)
        assert m is not None
        self.assertEqual(m.kind, "program")
        m = w.classify_line(["Programs", "Fitness Classes", "Basketball", "Fencing"], 2)
        assert m is not None
        self.assertEqual(m.kind, "program")
        m = w.classify_line(["Features", "Barbecues", "Baseball / Softball", "Basketball", "Grassy areas"], 3)
        assert m is not None
        self.assertEqual(m.kind, "amenity_item")
        self.assertTrue(w.PRIVATE_VENUE_RE.search("White Oak Shores Camping & RV Resort"))
        self.assertIsNone(w.PRIVATE_VENUE_RE.search("Smith Park"))

    def test_programs_negatives_and_plans(self) -> None:
        self.check("Youth Basketball League registration is open", "program")
        m = self.check("Open gym basketball on Tuesdays", "open_gym")
        assert m is not None
        self.assertTrue(m.indoor)
        self.check("The basketball court was removed in 2019.", "court", negative=True)
        self.check("There are no basketball courts at this park.", "court", negative=True)
        self.check("A new basketball court will be added in the future.", "court", courts=1, planned=True)
        self.check("Basketball: 9am - 9pm", None)
        self.check("Tennis courts and a food court", None)

    def test_indoor_outdoor(self) -> None:
        m = mention("Gymnasium with two basketball courts")
        assert m is not None
        self.assertTrue(m.indoor)
        self.assertEqual(m.courts, 2)
        res = w.judge_page(["Indoor basketball court in the gym", "Outdoor basketball court"], "Smith Rec Center")
        self.assertEqual(res.indoor_outdoor, "both")
        res = w.judge_page(["Basketball court"], "Smith Recreation Center")
        self.assertEqual(res.indoor_outdoor, "indoor_likely")
        m = w.classify_line(["Gym", "Outdoor", "Basketball Court", "Drinking Fountain"], 2)   # section heading
        assert m is not None
        self.assertEqual((m.indoor, m.outdoor), (False, True))
        m = w.classify_line(["Basketball | Monday | 6-8:30 p.m. | 16+"], 0)                   # gym schedule
        assert m is not None
        self.assertEqual((m.kind, m.indoor), ("open_gym", False))
        res = w.judge_page(["Basketball | Monday | 6-8:30 p.m. | 16+"], "Jefferson Community Center")
        self.assertEqual((res.has_court, res.open_gym, res.indoor_outdoor), (False, True, "indoor_likely"))
        res = w.judge_page(["Basketball court", "Pick-up basketball games every Saturday"], "Colonel Summers Park")
        self.assertEqual(res.indoor_outdoor, "unknown")
        res = w.judge_page(["Basketball leagues for all ages"], "Smith Park")
        self.assertFalse(res.has_court)
        self.assertTrue(res.program_only)

    def test_spot_check_fixes(self) -> None:
        # found in the final spot check: related-park blocks, "Basketball:" / "No", sports-offered lists,
        # playground hoops, outdoor gyms, rows about other facilities, shops
        page = ["Ravina Park", "South Bend, IN", "Get directions", "You May Also Like", "LaSalle Park",
                "Basketball (icon)", "Playground (icon)"]
        self.assertEqual(w.cut_related(page), page[:3])
        self.assertEqual(w.cut_related(["Nearby Parks", "Smith Park"]), ["Nearby Parks", "Smith Park"])
        m = w.classify_line(["Soccer:", "No", "Basketball:", "No", "Tennis:", "No"], 2)
        assert m is not None
        self.assertTrue(m.negative)
        m = w.classify_line(["We provide the following youth sports:", "Basketball", "Baseball", "Soccer"], 1)
        assert m is not None
        self.assertEqual(m.kind, "program")
        self.assertIsNone(w.classify_line(["BTG Map", "BTG Playground Hoops", "BTG Playground Kids"], 1))
        m = w.classify_line(["Basketball court", "Gazebo", "Outdoor Gym Equipment"], 0)
        assert m is not None
        self.assertFalse(m.indoor)
        lines = ["Grimmons Park", "Healey Basketball Courts | 5 Meacham Street | Somerville, MA", "Adams Park",
                 "Playground, Basketball court", "Bates Park", "Basketball court, Tennis", "Cedar Park",
                 "Basketball | 2"]
        self.assertEqual([n for n, _, _ in w.multi_park_rows(lines, [])], ["Adams Park", "Bates Park", "Cedar Park"])
        res = w.judge_page(["Playsets", "Basketball Hoops", "Trampolines"], "Playground Equipment in Michigan | Kids Play")
        self.assertEqual((res.has_court, res.mention_kind), (False, "commerce"))
        res = w.judge_page(["Full Basketball Court"], "East Miller Park - Basketball Court, Fitness Loop")
        self.assertEqual(res.indoor_outdoor, "unknown")
        self.assertFalse(w.kind_agrees("Meadowbrook Pond", "Meadowbrook Community Center"))
        self.assertFalse(w.kind_agrees("Farewell Bend Park", "Farewell Bend State Recreation Area"))
        self.assertTrue(w.kind_agrees("Green Lake Park", "Green Lake"))
        rows = w.basketball_list_page("Basketball Courts", ["Eisenhower Park (2 Courts) - 33 Sheep Hill Drive.",
                                                            "Fernridge Park (1 court) - 567 Fern Street.",
                                                            "Wolcott Park - 1134 New Britain Ave."])
        self.assertEqual([c for _, c, _ in rows], [2, 1, None])
        self.assertIsNone(mention("a vertical surface that is taller than a basketball hoop."))
        m = mention("Youth Sports (flag football, volleyball, basketball, indoor soccer) for ages 5-14.")
        assert m is not None
        self.assertEqual(m.kind, "program")

    def test_boilerplate(self) -> None:
        menu = ["Home", "Basketball Leagues", "Contact"]
        pages = [menu + [f"Park {i}", "Playground"] for i in range(8)]
        pages[0] += ["Basketball", "Tennis"]
        bp = w.boilerplate_lines(pages)
        self.assertIn(("Home", "Basketball Leagues", "Contact"), bp)
        kept = w.strip_boilerplate(pages[0], bp)
        self.assertNotIn("Basketball Leagues", kept)
        self.assertIn("Basketball", kept)


class NameTests(unittest.TestCase):
    def test_names(self) -> None:
        self.assertEqual(w.name_core("St. John's Memorial Park"), "saint johns memorial")
        self.assertEqual(w.name_core("Central Park"), "central")
        self.assertEqual(w.name_core("Park"), "park")
        self.assertEqual(w.name_sim("Smith Park", "Smith Park & Rec Center"), 1.0)
        self.assertEqual(w.name_sim("Smith Park", "Jones Park"), 0.0)
        self.assertEqual(w.page_name("Parks & Recreation | City of X", "Parks & Recreation", ""), "Parks & Recreation")
        self.assertEqual(w.page_name("Oak Grove Park - City of X", "", ""), "Oak Grove Park")

    def test_name_agree(self) -> None:
        for x, y in [("Greenlake Park Seattle", "Green Lake Park"), ("Fairgrounds Park", "Fairground Park"),
                     ("Stoney Creek Metro Park", "Stony Creek Metropark"), ("Downs Park, Pasadena, Md", "Downs Park")]:
            self.assertTrue(w.name_agree(x, y), (x, y))
        for x, y in [("Mountain Shadows Park", "Neighborhood Parks"), ("Smith Park", "Jones Park"),
                     ("Washington Park", "Parks"), ("Gilbert Community Park", "Gilbert Youth Sports"),
                     ("Optimist Park", "Optimist Miracle Movers")]:
            self.assertFalse(w.name_agree(x, y), (x, y))


class FakeResp:
    def __init__(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.text = body.decode("utf-8", "replace")
        self.is_redirect = status in (301, 302, 303, 307, 308)

    def iter_content(self, n: int) -> Any:
        yield self._body

    def close(self) -> None:
        pass


class FakeSession:
    def __init__(self, routes: dict[str, FakeResp]) -> None:
        self.routes = routes
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []

    def get(self, url: str, **kw: Any) -> FakeResp:
        self.calls.append(url)
        return self.routes.get(url, FakeResp(404))


class FetcherTests(unittest.TestCase):
    def test_robots_redirects_cache_and_challenge(self) -> None:
        html = b"<html><title>Smith Park</title><body><p>Two basketball courts</p></body></html>"
        routes = {
            "https://a.gov/robots.txt": FakeResp(200, b"User-agent: *\nDisallow: /private\n", {"Content-Type": "text/plain"}),
            "https://a.gov/parks/smith": FakeResp(301, headers={"Location": "/parks/smith-park"}),
            "https://a.gov/parks/smith-park": FakeResp(200, html, {"Content-Type": "text/html; charset=utf-8"}),
            "https://b.gov/robots.txt": FakeResp(404),
            "https://b.gov/p": FakeResp(200, b"<html><title>Just a moment...</title></html>", {"Content-Type": "text/html"}),
            "https://b.gov/q": FakeResp(200, html, {"Content-Type": "text/html"}),
        }
        with tempfile.TemporaryDirectory() as td:
            f = w.PoliteFetcher(delay=0.0, cache_dir=Path(td) / "cache", log_path=Path(td) / "log.jsonl")
            f.sess = FakeSession(routes)  # type: ignore[assignment]
            self.assertEqual(f.fetch("https://a.gov/private/x")["outcome"], "robots_disallowed")
            rec = f.fetch("https://a.gov/parks/smith")
            self.assertEqual(rec["outcome"], "ok")
            self.assertEqual(rec["final_url"], "https://a.gov/parks/smith-park")
            text = w.load_cached(rec, Path(td) / "cache")
            assert text is not None
            self.assertIn("basketball", text)
            self.assertEqual(f.fetch("https://b.gov/p")["outcome"], "bot_challenge")
            self.assertTrue(f.fetch("https://b.gov/q")["outcome"].startswith("host_stopped"))
            self.assertNotIn("https://b.gov/q", f.sess.calls)  # type: ignore[attr-defined]
            self.assertEqual(len(w.read_fetch_log(Path(td) / "log.jsonl")), 4)


class WebLayerTests(unittest.TestCase):
    def test_host_area_ignores_a_stray_seed(self) -> None:
        import numpy as np

        lons = [-72.76, -72.75, -72.74, -72.73, -72.73, -72.72, -88.38]   # one Wisconsin seed on a CT town site
        lats = [41.78, 41.73, 41.76, 41.74, 41.74, 41.75, 43.31]
        seeds = {"url": np.asarray(["https://www.westhartfordct.gov/p"] * 7), "lon": np.asarray(lons),
                 "lat": np.asarray(lats)}
        w_, s_, e_, n_ = w._host_areas(seeds, {}, 0.15)["www.westhartfordct.gov"]
        self.assertGreater(w_, -73.0)
        self.assertLess(n_, 42.0)                       # Holyoke, MA (42.2) is outside

    def test_rows_and_schema(self) -> None:
        import numpy as np
        import shapely

        poly = shapely.to_wkb(shapely.box(-85.31, 35.04, -85.309, 35.041))

        def col(*v: Any) -> np.ndarray:
            return np.asarray(v, dtype=object)

        parks = {
            "park_id": col("web-a", "web-b", "web-c", "web-d", "web-e"),
            "park_name": col("Alpha Park", "Beta Park", "Gamma Park", "Delta Park", "Eps Park"),
            "status": col("missing", "confirmed_osm", "confirmed_detected", "weak_detection", "missing"),
            "n_candidates_exported": col(0, 0, 0, 1, 0),
            "indoor_outdoor": col("outdoor", "unknown", "unknown", "unknown", "indoor_likely"),
            "polygon_wkb": col(poly, None, None, None, None), "polygon_area_m2": col(10000.0, None, None, None, None),
            "lon": col(-85.3, -85.3, -85.3, -85.3, -85.3), "lat": col(35.0, 35.0, 35.0, 35.0, 35.0),
            "city": col("Chattanooga, TN", None, None, None, None), "state": col("TN", "TN", "TN", "TN", "TN"),
            "polygon_ref": col("way/1", None, None, None, None), "polygon_source": col("osm", None, None, None, None),
            "fetch_date": col(*["2026-10-17"] * 5), "court_count": col(2, None, None, None, None),
            "half_court_count": col(None, None, None, None, None), "evidence": col(*["2 basketball courts"] * 5),
            "source_url": col(*["https://x.gov/p"] * 5), "mention_kind": col(*["court"] * 5),
            "seed_source": col(*["osm"] * 5),
        }
        ev: dict[str, list[Any]] = {"park_id": ["web-c", "web-c"], "candidate_id": ["det-1", "det-2"], "det_score": [0.4, 0.8],
              "privacy": ["public", "public"], "lon": [-85.2, -85.21], "lat": [35.1, 35.11]}
        w.STATE_REGION.setdefault("TN", "tennessee")
        rows = w.web_layer_rows(parks, ev)
        # b: OSM has it; d: an exported candidate shows it; e: indoor
        self.assertEqual([r["id"] for r in rows], ["web-a", "web-c"])
        a, c = rows
        self.assertEqual((a["position"], a["listed_courts"], a["osm_id"]), ("park", 2, "way/1"))
        self.assertTrue(-85.31 <= a["_lon"] <= -85.309)
        self.assertEqual((c["position"], c["candidate_id"], c["_lon"]), ("detected_candidate", "det-2", -85.21))
        fc: dict[str, Any] = {"type": "FeatureCollection", "features": [w.web_feature(r) for r in rows]}
        self.assertEqual(w.validate_web(fc), [])
        fc["features"][0]["properties"]["status"] = "verified"
        self.assertTrue(w.validate_web(fc))


if __name__ == "__main__":
    unittest.main()
