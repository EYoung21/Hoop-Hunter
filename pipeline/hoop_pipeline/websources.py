"""Basketball courts from public park web pages (no imagery, no GPU).

Park departments list amenities on their web pages ("2 lighted basketball courts", an amenity icon
"Basketball", an amenity chart). This module finds park pages at scale, fetches them politely, extracts
basketball mentions with rules, locates each park, and cross-matches the parks against the national
satellite candidates and OSM courts.

Pipeline (``python -m hoop_pipeline.websources <cmd> --help``)::

    osm-seeds         Overpass: OSM parks / rec grounds / sports and community centres with website|url|contact:website
    overture-extract  Overture places (parks, sports, gyms, community centres) with websites -> data/web/ (shared with indoor)
    wikidata-seeds    Wikidata US parks with an official website (P856)
    seeds             merge the seed sources into one URL list; mark URLs shared by several parks as generic
    expand            find more park pages per host: sitemaps of hosts with park-specific seed URLs, and the
                      park directories in web_seeds.json (CivicPlus, Granicus, WordPress ... park finders)
    crawl             polite fetch of every queued URL (robots.txt, per-host delay, identifying User-Agent, cache)
    extract           rules -> one row per page (+ one per amenity-table row) with mentions, counts, indoor/outdoor
    build             locate pages (seed point, OSM / PAD-US / official park polygon, name match) ->
                      out/web/parks_with_courts.parquet
    match             cross-match with OSM courts and satellite candidates -> candidate_evidence.parquet,
                      parks_missing_detection.parquet, summary.json, by_state.csv, by_city.csv
    crawl-stats       outcomes per URL / host / seed source and robots.txt outcomes -> out/web/crawl_stats.json
    sample / score    stratified spot-check sample for reading by eye, and its precision

Rules of the road (see docs/web_sources.md): only public pages, robots.txt honoured (RFC 9309 matching with
``*`` and ``$``), Crawl-delay honoured, at least ``--delay`` seconds between requests to one host, an identifying
User-Agent, no logins, no CAPTCHA or bot-challenge bypassing (a challenge page stops that host), Google
Maps / Places never used. Only facts (a park has N basketball courts) plus a short evidence snippet and the
source URL are kept; cached page bodies stay in ``data/web/cache`` (git-ignored) and are not republished.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import json
import logging
import math
import random
import re
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests
import shapely
from pyproj import Transformer
from shapely.strtree import STRtree

from . import config
from .common import add_common_args, read_json, setup_logging, write_json
from .geoparquet import read_geoparquet, write_geoparquet

log = logging.getLogger("websources")

USER_AGENT = "HoopHunterResearch/0.1 (contact: github.com/EYoung21/Hoop-Hunter)"
ROBOTS_TOKEN = "hoophunterresearch"          # product token matched against robots.txt User-agent lines

WEB_DIR = config.DATA_DIR / "web"
CACHE_DIR = WEB_DIR / "cache"
FETCH_LOG = WEB_DIR / "fetch_log.jsonl"
ROBOTS_LOG = WEB_DIR / "robots.jsonl"
OSM_SEEDS = WEB_DIR / "osm_seeds.parquet"
OVERTURE_FILE = WEB_DIR / "overture_places_us.parquet"
WIKIDATA_SEEDS = WEB_DIR / "wikidata_seeds.parquet"
SEEDS_FILE = WEB_DIR / "seeds.parquet"
EXPANDED_FILE = WEB_DIR / "expanded.parquet"
EXPAND_HOSTS_FILE = WEB_DIR / "expand_hosts.json"   # hosts already expanded (including those that gave no pages)
PAGES_FILE = WEB_DIR / "pages.parquet"
EXTRACT_PARTS_DIR = WEB_DIR / "extract_parts"     # resumable per-chunk results of `extract`
REGISTRY_FILE = config.PIPELINE_ROOT / "web_seeds.json"

OUT_WEB = config.OUT_DIR / "web"
PARKS_FILE = OUT_WEB / "parks_with_courts.parquet"
EVIDENCE_FILE = OUT_WEB / "candidate_evidence.parquet"
MISSING_FILE = OUT_WEB / "parks_missing_detection.parquet"
SUMMARY_FILE = OUT_WEB / "summary.json"

SITES_FILE = config.DATA_DIR / "sites" / "sites_us.parquet"
OSM_COURTS_FILE = config.DATA_DIR / "osm_us_20261015" / "courts_us.parquet"
CANDIDATES_FILE = config.OUT_DIR / "national" / "v3" / "candidates_us.parquet"
TRUTH_COVERAGE = config.DATA_DIR / "truth" / "coverage.parquet"

ALBERS = "EPSG:5070"
CONUS_BBOX = (-125.0, 24.3, -66.8, 49.5)
OVERPASS_URLS = ("https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter")
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
OVERTURE_RELEASE = "2026-09-23.1"
OVERTURE_PLACES = "overturemaps-us-west-2/release/{release}/theme=places/type=place/"

_TO_M = Transformer.from_crs("EPSG:4326", ALBERS, always_xy=True)
_LOCK = threading.Lock()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def to_m(geoms: np.ndarray) -> np.ndarray:
    geoms = np.asarray(geoms, dtype=object)
    if not len(geoms):
        return geoms
    return shapely.transform(geoms, lambda xy: np.column_stack(_TO_M.transform(xy[:, 0], xy[:, 1])))


def pts_m(lons: Any, lats: Any) -> np.ndarray:
    x, y = _TO_M.transform(np.asarray(lons, float), np.asarray(lats, float))
    return shapely.points(x, y)


# =========================================================================== URLs
def normalise_url(url: str | None) -> str | None:
    """Canonical form used as the crawl key: scheme + lower-case host, no fragment, no default port,
    'www.' kept (sites differ), trailing slash dropped except for the root, tracking parameters dropped."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip().split()[0] if url.strip() else ""
    if not url:
        return None
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        if re.match(r"^(mailto|tel|javascript|data|ftp|file|sms):", url, re.I):
            return None
        url = "http://" + url.lstrip("/")
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        return None
    host = p.hostname.lower().rstrip(".")
    if "." not in host:
        return None
    port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    query = "&".join(q for q in p.query.split("&") if q and not re.match(r"(utm_[a-z]+|fbclid|gclid)=", q, re.I))
    return urlunsplit((p.scheme.lower(), host + port, path, query, ""))


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def site_of(host: str) -> str:
    """Registrable-ish domain (last two labels; three for *.xx.us and *.gov.xx style hosts)."""
    parts = host.split(".")
    if len(parts) >= 3 and (parts[-1] == "us" and len(parts[-2]) == 2 or parts[-2] in ("co", "gov", "org", "com")):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# Hosts never fetched: map / search / social sites (terms forbid scraping or the page is not the park's own),
# and URL shorteners.
BLOCKED_SITES = {
    "google.com", "goo.gl", "g.page", "maps.app.goo.gl", "facebook.com", "fb.com", "instagram.com", "twitter.com",
    "x.com", "yelp.com", "tripadvisor.com", "foursquare.com", "linkedin.com", "youtube.com", "tiktok.com",
    "pinterest.com", "nextdoor.com", "bit.ly", "tinyurl.com", "wikipedia.org", "wikimedia.org", "apple.com",
    "bing.com", "yahoo.com", "mapquest.com", "alltrails.com", "hipcamp.com", "reserveamerica.com",
    # third-party directories, real estate and event sites: not the park's own page, and their terms
    # commonly forbid automated collection
    "recreationparks.net", "cityseekr.com", "gotparks.com", "linktr.ee", "eventseeker.com", "parkopedia.com",
    "traillink.com", "parkgrades.com", "trulia.com", "walkscore.com", "yellowpages.com", "globaltennisnetwork.com",
    "stateparks.com", "redfin.com", "zillow.com", "realtor.com", "apartments.com", "mapcarta.com",
    "roadtrippers.com", "waymarking.com", "hmdb.org", "findagrave.com", "mycountyparks.com", "groupon.com",
    "eventbrite.com", "allevents.in", "patch.com", "wanderlog.com", "expedia.com", "booking.com", "hotels.com",
    "yelp.to", "superpages.com", "manta.com", "bbb.org", "chamberofcommerce.com",
    "loopnet.com", "homes.com", "niche.com", "greatschools.org", "playgroundbuzz.com", "courtsoftheworld.com",
    "pickleheads.com", "places2play.org", "hoopmaps.com", "outdoorsy.com", "campendium.com", "thedyrt.com",
    "business.site",                    # Google Business Profile sites (Google Maps listing data)
    "hikespeak.com", "tulsago.com", "janesvillecvb.com", "n-georgia.com", "historichendersonville.org",
    # parked / for-sale domains (expired park websites)
    "hugedomains.com", "sedo.com", "afternic.com", "dan.com", "domainmarket.com", "buydomains.com", "undeveloped.com",
    "eteamz.com", "teespring.com", "geonames.org", "discgolfscene.com", "dgcoursereview.com", "americasparks.com",
    "trails.com", "localwiki.org", "udisc.com", "tennisround.com", "swimmingholes.org",
    "mhvillage.com", "rvparkreviews.com", "bizapedia.com", "kidsempire.com", "infosports.com", "travelok.com",
    "visitnc.com", "mapofplay.kaboom.org", "campgroundviews.com", "goodsam.com", "koa.com", "rvparky.com",
    # sitemap expansion of these hosts found venue, business, real-estate or nature-preserve pages, not parks
    "theknot.com", "homesbymarco.com", "nature.org", "nationalparks.org", "npca.org", "atlasobscura.com",
    "carabs.com", "meetmtp.com", "sfciviccenter.org", "luckystrikeent.com", "safeharbor.com", "playxgolf.com",
    "safesplash.com", "preventionlinks.org", "maps.northwestern.edu", "facilities.upenn.edu", "unlimitedplay.org",
    "mooseintl.org", "playactivate.com", "emlerswimschool.com", "gamecocksonline.com",
    "twiddy.com", "vrbo.com", "airbnb.com",          # vacation rentals
    # federal land agencies: public domain, but essentially no basketball courts (README: RIDB / NPS)
    "nps.gov", "fs.usda.gov", "fws.gov", "blm.gov", "recreation.gov", "usace.army.mil",
}


def is_blocked_url(url: str) -> bool:
    h = host_of(url)
    return any(h == s or h.endswith("." + s) for s in BLOCKED_SITES)


# =========================================================================== robots.txt (RFC 9309)
@dataclass
class Robots:
    """Parsed robots.txt group for our token (or '*'), with RFC 9309 longest-match rules, '*' and '$'."""

    rules: list[tuple[bool, str, re.Pattern[str]]] = field(default_factory=list)   # (allow, raw, regex)
    crawl_delay: float | None = None
    sitemaps: list[str] = field(default_factory=list)
    disallow_all: bool = False

    @classmethod
    def parse(cls, text: str, token: str = ROBOTS_TOKEN) -> Robots:
        groups: list[tuple[list[str], list[tuple[str, str]]]] = []
        sitemaps: list[str] = []
        agents: list[str] = []
        lines: list[tuple[str, str]] = []
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip().lower(), v.strip()
            if k == "sitemap":
                if v:
                    sitemaps.append(v)
                continue
            if k == "user-agent":
                if not last_was_agent and (agents or lines):
                    groups.append((agents, lines))
                    agents, lines = [], []
                agents.append(v.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if k in ("allow", "disallow", "crawl-delay"):
                lines.append((k, v))
        if agents or lines:
            groups.append((agents, lines))
        mine = [g for g in groups if any(a != "*" and a and a in token for a in g[0])]
        if not mine:
            mine = [g for g in groups if "*" in g[0]]
        r = cls(sitemaps=sitemaps)
        for _, glines in mine:
            for k, v in glines:
                if k == "crawl-delay":
                    try:
                        r.crawl_delay = max(r.crawl_delay or 0.0, float(v))
                    except ValueError:
                        pass
                elif v or k == "allow":
                    if not v:
                        continue
                    pat = re.escape(v).replace(r"\*", ".*")
                    if pat.endswith(r"\$"):
                        pat = pat[:-2] + "$"
                    r.rules.append((k == "allow", v, re.compile(pat)))
        return r

    def allowed(self, url: str) -> bool:
        if self.disallow_all:
            return False
        p = urlsplit(url)
        path = (p.path or "/") + (("?" + p.query) if p.query else "")
        best_len, best_allow = -1, True
        for allow, raw, rx in self.rules:
            if rx.match(path):
                n = len(raw)
                if n > best_len or (n == best_len and allow):
                    best_len, best_allow = n, allow
        return best_allow


# =========================================================================== polite fetcher
# Bot-challenge / WAF interstitials. Not a bare "captcha": contact forms load reCAPTCHA on normal pages, and
# Cloudflare injects /cdn-cgi/challenge-platform scripts into normal pages too.
CHALLENGE_RE = re.compile(r"(<title>\s*just a moment\.\.\.|attention required! \| cloudflare|cf-browser-verification|"
                          r"cf_chl_opt|are you a robot|verify you are (a )?human|access denied.{0,40}incapsula|"
                          r"request unsuccessful\. incapsula|_incapsula_resource|perimeterx|px-captcha|"
                          r"awswaf|aws-waf-token|ddos-guard|sucuri website firewall)", re.I)


class _SharedTLSAdapter(requests.adapters.HTTPAdapter):
    """One TLS context for every connection. By default each new connection re-reads the CA bundle
    (load_verify_locations, ~1 s of CPU on Windows); with thousands of hosts that dominated the crawl."""

    def __init__(self, ctx: Any, **kw: Any) -> None:
        self._ctx = ctx
        super().__init__(**kw)

    def init_poolmanager(self, *args: Any, **kw: Any) -> None:
        kw["ssl_context"] = self._ctx
        super().init_poolmanager(*args, **kw)

    def cert_verify(self, conn: Any, url: str, verify: Any, cert: Any) -> None:
        super().cert_verify(conn, url, verify, cert)
        if verify is True:                      # the shared context already holds the CA bundle
            conn.ca_certs = None
            conn.ca_cert_dir = None


def _tls_context() -> Any:
    """urllib3's own default context (same TLS settings as a plain requests call), CA bundle loaded once."""
    import certifi
    from urllib3.util.ssl_ import create_urllib3_context

    ctx = create_urllib3_context()
    ctx.load_verify_locations(cafile=certifi.where())
    return ctx


@dataclass
class HostState:
    host: str
    robots: Robots | None = None
    robots_outcome: str = ""
    delay: float = 2.0
    next_ok: float = 0.0
    strikes: int = 0
    forbidden: int = 0
    stopped: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)


class PoliteFetcher:
    """One requests session, per-host robots.txt + delay, gzip cache, append-only JSONL fetch log."""

    def __init__(self, delay: float = 2.0, timeout: float = 25.0, max_bytes: int = 8_000_000,
                 max_crawl_delay: float = 30.0, cache_dir: Path = CACHE_DIR, log_path: Path = FETCH_LOG) -> None:
        self.delay, self.timeout, self.max_bytes, self.max_crawl_delay = delay, timeout, max_bytes, max_crawl_delay
        self.cache_dir, self.log_path = cache_dir, log_path
        import http.cookiejar

        self.sess = requests.Session()
        # no cookies (nothing to keep between visits; a jar shared by thousands of hosts made every request slow)
        # and no per-request netrc / proxy-registry lookups
        self.sess.cookies.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
        self.sess.trust_env = False
        adapter = _SharedTLSAdapter(_tls_context(), pool_connections=256, pool_maxsize=4, max_retries=0)
        self.sess.mount("https://", adapter)
        self.sess.mount("http://", adapter)
        self.sess.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                                  "Accept-Language": "en-US,en;q=0.8"})
        self.hosts: dict[str, HostState] = {}
        self._hosts_lock = threading.Lock()

    # ---- per host
    def state(self, scheme: str, host: str) -> HostState:
        with self._hosts_lock:
            hs = self.hosts.get(host)
            if hs is None:
                hs = self.hosts[host] = HostState(host, delay=self.delay)
        if hs.robots is None and not hs.robots_outcome:
            with hs.lock:
                if hs.robots is None and not hs.robots_outcome:
                    self._load_robots(scheme, hs)
        return hs

    def _load_robots(self, scheme: str, hs: HostState) -> None:
        url = f"{scheme}://{hs.host}/robots.txt"
        rec: dict[str, Any] = {"host": hs.host, "url": url, "fetched_at": now_iso()}
        try:
            r = self.sess.get(url, timeout=self.timeout, allow_redirects=True)
            rec["status"] = r.status_code
            if r.status_code == 200 and "html" not in r.headers.get("Content-Type", "").lower()[:40]:
                hs.robots = Robots.parse(r.text[:500_000])
                hs.robots_outcome = "parsed"
            elif r.status_code == 200:            # an HTML page served at /robots.txt: no rules
                hs.robots, hs.robots_outcome = Robots(), "html_no_rules"
            elif r.status_code in (401, 403):     # conservative: treat as "disallow all"
                hs.robots, hs.robots_outcome = Robots(disallow_all=True), f"http_{r.status_code}_disallow_all"
            elif r.status_code == 429:            # rate limited: treat like unreachable (as major crawlers do)
                hs.robots, hs.robots_outcome = Robots(disallow_all=True), "http_429_unreachable"
            elif 400 <= r.status_code < 500:      # RFC 9309: unavailable -> allowed
                hs.robots, hs.robots_outcome = Robots(), f"http_{r.status_code}_allow"
            else:                                 # 5xx: unreachable -> disallow (RFC 9309 2.3.1.4)
                hs.robots, hs.robots_outcome = Robots(disallow_all=True), f"http_{r.status_code}_unreachable"
        except requests.RequestException as exc:
            hs.robots, hs.robots_outcome = Robots(disallow_all=True), "error_unreachable"
            rec["error"] = type(exc).__name__
        if hs.robots is not None and hs.robots.crawl_delay:
            if hs.robots.crawl_delay > self.max_crawl_delay:
                hs.stopped = f"crawl_delay_{hs.robots.crawl_delay:g}s"
            hs.delay = max(hs.delay, hs.robots.crawl_delay)
        rec.update(outcome=hs.robots_outcome, crawl_delay=hs.robots.crawl_delay if hs.robots else None,
                   n_rules=len(hs.robots.rules) if hs.robots else 0,
                   sitemaps=hs.robots.sitemaps[:20] if hs.robots else [])
        hs.next_ok = time.monotonic() + hs.delay
        self._append(ROBOTS_LOG if self.log_path == FETCH_LOG else self.log_path.with_name("robots.jsonl"), rec)

    def _wait(self, hs: HostState) -> None:
        with hs.lock:
            wait = hs.next_ok - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            hs.next_ok = time.monotonic() + hs.delay * random.uniform(1.0, 1.25)

    def _append(self, path: Path, rec: dict[str, Any]) -> None:
        line = json.dumps(rec, separators=(",", ":"), default=str) + "\n"
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)

    def cache_path(self, url: str) -> Path:
        h = hashlib.sha1(url.encode("utf-8")).hexdigest()
        host = re.sub(r"[^a-z0-9.-]", "_", host_of(url))[:80] or "_"
        return self.cache_dir / host / f"{h[:20]}.html.gz"

    # ---- fetch
    def fetch(self, url: str, max_redirects: int = 5, accept_xml: bool = False) -> dict[str, Any]:
        """GET ``url`` politely; returns the log record (``cache`` set when a body was stored)."""
        rec: dict[str, Any] = {"url": url, "fetched_at": now_iso()}
        cur = url
        t0 = time.monotonic()
        for _hop in range(max_redirects + 1):
            p = urlsplit(cur)
            host = (p.hostname or "").lower()
            if not host or p.scheme not in ("http", "https"):
                rec["outcome"] = "bad_url"
                break
            if is_blocked_url(cur):
                rec["outcome"] = "blocked_site"
                break
            hs = self.state(p.scheme, host)
            if hs.stopped:
                rec["outcome"] = f"host_stopped:{hs.stopped}"
                break
            if hs.robots is None or not hs.robots.allowed(cur):
                rec["outcome"] = "robots_disallowed"
                break
            self._wait(hs)
            try:
                r = self.sess.get(cur, timeout=self.timeout, allow_redirects=False, stream=True)
            except requests.RequestException as exc:
                rec.update(outcome="error", error=type(exc).__name__)
                hs.strikes += 1
                if hs.strikes >= 5:
                    hs.stopped = "5_errors"
                break
            if r.is_redirect and r.headers.get("Location"):
                nxt = urljoin(cur, r.headers["Location"])
                r.close()
                if nxt == cur:
                    rec["outcome"] = "redirect_loop"
                    break
                cur = nxt
                continue
            rec.update(status=r.status_code, final_url=cur, content_type=r.headers.get("Content-Type", "")[:80])
            if r.status_code in (429, 503):
                retry = r.headers.get("Retry-After", "")
                hs.next_ok = time.monotonic() + (float(retry) if retry.isdigit() else 60.0)
                hs.strikes += 2
                if hs.strikes >= 6:
                    hs.stopped = f"http_{r.status_code}"
                rec["outcome"] = "throttled"
                r.close()
                break
            ctype = rec["content_type"].lower()
            ok_type = "html" in ctype or (accept_xml and "xml" in ctype) or (not ctype)
            body = b""
            if ok_type:
                try:
                    for chunk in r.iter_content(65536):
                        body += chunk
                        if len(body) > self.max_bytes:
                            break
                except requests.RequestException as exc:
                    rec.update(outcome="error", error=type(exc).__name__)
                    r.close()
                    break
            r.close()
            if r.status_code >= 400:
                rec["outcome"] = "http_error"
                hs.forbidden = hs.forbidden + 1 if r.status_code in (401, 403) else 0
                if hs.forbidden >= 5:          # the site refuses this client: stop asking
                    hs.stopped = f"http_{r.status_code}_refused"
                if r.status_code == 403 and CHALLENGE_RE.search(body[:20000].decode("utf-8", "replace")):
                    hs.stopped = "bot_challenge"
                    rec["outcome"] = "bot_challenge"
                break
            if not ok_type:
                rec["outcome"] = "not_html"
                break
            if len(body) > self.max_bytes:
                rec["outcome"] = "too_large"
                break
            text_head = body[:20000].decode("utf-8", "replace")
            # 202 Accepted for a page is how AWS WAF serves its JavaScript challenge
            if r.status_code == 202 or (CHALLENGE_RE.search(text_head) and len(body) < 30000):
                hs.stopped = "bot_challenge"
                rec["outcome"] = "bot_challenge"
                break
            path = self.cache_path(url)
            path.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(path, "wb", compresslevel=6) as fh:
                fh.write(body)
            rec.update(outcome="ok", bytes=len(body), cache=str(path.relative_to(self.cache_dir)).replace("\\", "/"))
            hs.strikes = 0
            hs.forbidden = 0
            break
        else:
            rec["outcome"] = "too_many_redirects"
        rec["seconds"] = round(time.monotonic() - t0, 2)
        self._append(self.log_path, rec)
        return rec


def read_fetch_log(path: Path = FETCH_LOG) -> dict[str, dict[str, Any]]:
    """Latest record per URL."""
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            out[rec["url"]] = rec
    return out


def load_cached(rec: dict[str, Any], cache_dir: Path = CACHE_DIR) -> str | None:
    c = rec.get("cache")
    if not c:
        return None
    p = cache_dir / c
    if not p.exists():
        return None
    with gzip.open(p, "rb") as fh:
        raw = fh.read()
    m = re.search(rb"charset=([A-Za-z0-9_-]+)", (rec.get("content_type") or "").encode()) or re.search(
        rb'charset=["\']?([A-Za-z0-9_-]+)', raw[:4000])
    enc = m.group(1).decode("ascii", "ignore").lower() if m else "utf-8"
    if enc in ("utf-8", "utf8"):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:              # mislabelled Windows-1252 pages are common on city sites
            return raw.decode("cp1252", "replace")
    try:
        return raw.decode(enc, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


# =========================================================================== seeds: OSM via Overpass
OSM_SEED_QUERY = """[out:json][timeout:600];
area["ISO3166-2"="US-{st}"][admin_level=4]->.a;
(
  nwr["leisure"~"^(park|recreation_ground|sports_centre|playground)$"]["website"](area.a);
  nwr["leisure"~"^(park|recreation_ground|sports_centre|playground)$"]["contact:website"](area.a);
  nwr["leisure"~"^(park|recreation_ground|sports_centre|playground)$"]["url"](area.a);
  nwr["amenity"="community_centre"]["website"](area.a);
  nwr["amenity"="community_centre"]["contact:website"](area.a);
);
out tags center;
"""


def overpass(query: str, sess: requests.Session, tries: int = 6) -> dict[str, Any]:
    last: Exception | None = None
    for i in range(tries):
        url = OVERPASS_URLS[i % len(OVERPASS_URLS)]
        try:
            r = sess.post(url, data={"data": query}, timeout=700)
            if r.status_code in (429, 504, 502, 503):
                raise requests.HTTPError(f"{r.status_code} from {url}")
            r.raise_for_status()
            data: dict[str, Any] = r.json()
            if data.get("remark") and "runtime error" in str(data["remark"]):
                raise RuntimeError(str(data["remark"])[:200])
            return data
        except Exception as exc:  # network, HTTP, JSON
            last = exc
            wait = 30 * (i + 1)
            log.warning("overpass try %d failed (%s); sleeping %d s", i + 1, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"overpass failed: {last}")


def _osm_kind(tags: dict[str, str]) -> str:
    if tags.get("amenity") == "community_centre":
        return "community_centre"
    return tags.get("leisure", "other")


def cmd_osm_seeds(args: argparse.Namespace) -> None:
    from .states import load_states

    states = sorted(load_states().codes.tolist()) if not args.states else [s.upper() for s in args.states]
    part_dir = WEB_DIR / "osm_seeds_parts"
    part_dir.mkdir(parents=True, exist_ok=True)
    sess = requests.Session()
    sess.headers["User-Agent"] = USER_AGENT
    for st in states:
        part = part_dir / f"{st}.json"
        if part.exists() and not args.force:
            continue
        t0 = time.monotonic()
        try:
            data = overpass(OSM_SEED_QUERY.format(st=st), sess)
        except RuntimeError as exc:          # leave this state for a re-run; cached states are skipped
            log.error("%s: %s", st, exc)
            continue
        write_json(part, {"state": st, "fetched_at": now_iso(), "osm_base": data.get("osm3s", {}).get("timestamp_osm_base"),
                          "elements": data.get("elements", [])}, indent=None)
        log.info("%s: %d elements in %.0f s", st, len(data.get("elements", [])), time.monotonic() - t0)
        time.sleep(args.sleep)
    rows: dict[str, list[Any]] = defaultdict(list)
    geoms = []
    seen: set[str] = set()
    for part in sorted(part_dir.glob("*.json")):
        d = read_json(part)
        for e in d["elements"]:
            ref = f"{e['type']}/{e['id']}"
            if ref in seen:
                continue
            seen.add(ref)
            t = e.get("tags", {})
            c = e.get("center") or ({"lon": e["lon"], "lat": e["lat"]} if "lon" in e else None)
            if not c:
                continue
            urls = [t[k] for k in ("website", "contact:website", "url") if t.get(k)]
            rows["osm_ref"].append(ref)
            rows["name"].append(t.get("name"))
            rows["kind"].append(_osm_kind(t))
            rows["website"].append(urls[0].split(";")[0].strip())
            rows["urls"].append(";".join(urls))
            rows["access"].append(t.get("access"))
            rows["operator"].append(t.get("operator"))
            rows["addr_city"].append(t.get("addr:city"))
            rows["sport"].append(t.get("sport"))
            rows["state"].append(d["state"])
            rows["lon"].append(float(c["lon"]))
            rows["lat"].append(float(c["lat"]))
            rows["fetched_at"].append(d["fetched_at"])
            geoms.append(shapely.Point(float(c["lon"]), float(c["lat"])))
    write_geoparquet(OSM_SEEDS, {k: pa.array(v) for k, v in rows.items()}, np.asarray(geoms, dtype=object))
    log.info("OSM seeds: %d features with a website -> %s (%s)", len(geoms), OSM_SEEDS, dict(Counter(rows["kind"])))


# =========================================================================== seeds: Overture places
# Taxonomy primaries kept from Overture (places theme). Parks feed the park-page crawl; the rest feed indoor.py.
OVERTURE_PARK_CATS = {"park", "playground", "state_park", "recreation_area"}
OVERTURE_SPORT_CATS = {
    "basketball_court", "basketball_stadium", "sport_court", "gym", "sport_or_fitness_facility", "sports_complex",
    "community_center", "civic_center", "youth_organization", "recreation_center", "sport_or_recreation_club",
    "health_and_wellness_club", "sports_and_recreation", "stadium_arena", "school_sport_team", "after_school_program",
    "amateur_sport_league", "sport_league", "kids_recreation_and_party", "boxing_gym", "gymnastics_center",
}
OVERTURE_NAME_RE = (r"\bymca\b|\bywca\b|\bjcc\b|jewish community cent|boys (&|and) girls club|recreation (center|centre)"
                    r"|\brec (center|centre)\b|community (center|centre)|fieldhouse|field house|sportsplex|"
                    r"basketball|\bhoops\b|athletic club|sports (center|centre)|life ?time|la fitness|24 hour fitness"
                    r"|\bgymnasium\b")


def cmd_overture_extract(args: argparse.Namespace) -> None:
    """US places in park / sport / community categories (or with a matching name) with their websites and
    addresses, read straight from Overture's public S3 bucket with pyarrow (no DuckDB needed)."""
    import pyarrow.compute as pc
    import pyarrow.fs as pafs

    s3 = pafs.S3FileSystem(anonymous=True, region="us-west-2")
    base = OVERTURE_PLACES.format(release=args.release)
    files = [f.path for f in s3.get_file_info(pafs.FileSelector(base)) if f.path.endswith(".parquet")]
    w, s, e, n = CONUS_BBOX
    cats = OVERTURE_PARK_CATS | OVERTURE_SPORT_CATS
    name_rx = re.compile(OVERTURE_NAME_RE, re.I)
    out: dict[str, list[Any]] = defaultdict(list)
    t0 = time.monotonic()
    cols = ["id", "names", "taxonomy", "basic_category", "confidence", "operating_status", "websites", "addresses",
            "brand", "sources", "bbox"]
    for path in files:
        pf = pq.ParquetFile(path, filesystem=s3)
        keep_rg = []
        for i in range(pf.metadata.num_row_groups):   # prune row groups by the bbox statistics
            rg = pf.metadata.row_group(i)
            stats = {rg.column(j).path_in_schema: rg.column(j).statistics for j in range(rg.num_columns)}
            sx, sy = stats.get("bbox.xmin"), stats.get("bbox.ymin")
            if sx is None or sy is None or not sx.has_min_max or not sy.has_min_max:
                keep_rg.append(i)
                continue
            if sx.max >= w and sx.min <= e and sy.max >= s and sy.min <= n:
                keep_rg.append(i)
        if not keep_rg:
            continue
        log.info("%s: %d / %d row groups intersect CONUS", path.rsplit("/", 1)[-1][:12], len(keep_rg),
                 pf.metadata.num_row_groups)
        for i in keep_rg:
            t = pf.read_row_group(i, columns=cols)
            bb = t.column("bbox").combine_chunks()
            x = bb.field("xmin").to_numpy(zero_copy_only=False)
            y = bb.field("ymin").to_numpy(zero_copy_only=False)
            inb = (x >= w) & (x <= e) & (y >= s) & (y <= n)
            if not inb.any():
                continue
            t = t.filter(pa.array(inb))
            tax = t.column("taxonomy").combine_chunks()
            prim = tax.field("primary").to_pylist()
            basic = t.column("basic_category").to_pylist()
            names = t.column("names").combine_chunks().field("primary").to_pylist()
            sel = [((p in cats) or (b in cats) or (nm is not None and bool(name_rx.search(nm))))
                   for p, b, nm in zip(prim, basic, names)]
            if not any(sel):
                continue
            idx = np.flatnonzero(sel)
            t = t.take(pa.array(idx))
            prim = [prim[j] for j in idx]
            basic = [basic[j] for j in idx]
            names = [names[j] for j in idx]
            hier = t.column("taxonomy").combine_chunks().field("hierarchy").to_pylist()
            webs = t.column("websites").to_pylist()
            addrs = t.column("addresses").to_pylist()
            brand = t.column("brand").combine_chunks()
            brand_names = brand.field("names").field("primary").to_pylist() if brand.type.num_fields else [None] * len(idx)
            srcs = t.column("sources").to_pylist()
            bb = t.column("bbox").combine_chunks()
            out["id"].extend(t.column("id").to_pylist())
            out["name"].extend(names)
            out["cat"].extend(prim)
            out["basic_category"].extend(basic)
            out["hier"].extend(">".join(h) if h else None for h in hier)
            out["confidence"].extend(t.column("confidence").to_pylist())
            out["operating_status"].extend(t.column("operating_status").to_pylist())
            out["website"].extend((ws[0] if ws else None) for ws in webs)
            out["websites"].extend((";".join(ws) if ws else None) for ws in webs)
            for a in addrs:
                a0 = (a or [{}])[0] or {}
                out["address"].append(a0.get("freeform"))
                out["locality"].append(a0.get("locality"))
                out["region"].append(a0.get("region"))
                out["postcode"].append(a0.get("postcode"))
                out["country"].append(a0.get("country"))
            out["brand"].extend(brand_names)
            out["datasets"].extend(";".join(sorted({x.get("dataset") or "" for x in (sr or [])})) for sr in srcs)
            out["licenses"].extend(";".join(sorted({x.get("license") or "" for x in (sr or [])})) for sr in srcs)
            out["lon"].extend(pc.cast(bb.field("xmin"), pa.float64()).to_pylist())
            out["lat"].extend(pc.cast(bb.field("ymin"), pa.float64()).to_pylist())
        log.info("  %d rows so far (%.0f s)", len(out["id"]), time.monotonic() - t0)
    keep = np.array([(c in (None, "US")) for c in out["country"]], dtype=bool)
    cols_out = {k: pa.array([v[i] for i in np.flatnonzero(keep)]) for k, v in out.items()}
    geoms = shapely.points(np.asarray(out["lon"], float)[keep], np.asarray(out["lat"], float)[keep])
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    write_geoparquet(OVERTURE_FILE, cols_out, geoms)
    meta = {"release": args.release, "generated": now_iso(), "rows": int(keep.sum()),
            "license": "CDLA-Permissive-2.0 (Overture places); Apache-2.0 for Foursquare-sourced rows; some rows CC0-1.0",
            "attribution": "Overture Maps Foundation, https://overturemaps.org; see docs/indoor.md",
            "cats": dict(Counter(np.asarray(out["cat"], dtype=object)[keep].tolist()).most_common(60))}
    write_json(OVERTURE_FILE.with_suffix(".json"), meta)
    log.info("Overture: %d US rows -> %s in %.0f s", int(keep.sum()), OVERTURE_FILE, time.monotonic() - t0)


# =========================================================================== seeds: Wikidata
WIKIDATA_QUERY = """SELECT ?item ?itemLabel ?website ?coord WHERE {
  VALUES ?type { wd:Q22698 wd:Q3363945 wd:Q2416723 wd:Q15243209 wd:Q1076486 wd:Q7138926 wd:Q60759263 }
  ?item wdt:P31 ?type ; wdt:P17 wd:Q30 ; wdt:P856 ?website ; wdt:P625 ?coord .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}"""


def cmd_wikidata_seeds(args: argparse.Namespace) -> None:
    """US parks (park, urban park, city park, recreation area, sports venue ...) with an official website.
    Wikidata is CC0."""
    r = requests.get(WIKIDATA_SPARQL, params={"query": WIKIDATA_QUERY, "format": "json"},
                     headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"}, timeout=300)
    r.raise_for_status()
    rows: dict[str, list[Any]] = defaultdict(list)
    geoms = []
    seen: set[tuple[str, str]] = set()
    for b in r.json()["results"]["bindings"]:
        m = re.match(r"Point\(([-0-9.eE]+) ([-0-9.eE]+)\)", b["coord"]["value"])
        if not m:
            continue
        lon, lat = float(m.group(1)), float(m.group(2))
        if not (CONUS_BBOX[0] <= lon <= CONUS_BBOX[2] and CONUS_BBOX[1] <= lat <= CONUS_BBOX[3]):
            continue
        key = (b["item"]["value"], b["website"]["value"])
        if key in seen:
            continue
        seen.add(key)
        rows["qid"].append(b["item"]["value"].rsplit("/", 1)[-1])
        rows["name"].append(b.get("itemLabel", {}).get("value"))
        rows["website"].append(b["website"]["value"])
        rows["lon"].append(lon)
        rows["lat"].append(lat)
        geoms.append(shapely.Point(lon, lat))
    write_geoparquet(WIKIDATA_SEEDS, {k: pa.array(v) for k, v in rows.items()}, np.asarray(geoms, dtype=object))
    log.info("Wikidata: %d US parks with a website -> %s", len(geoms), WIKIDATA_SEEDS)




# =========================================================================== HTML -> lines, tables, links
# <form> and <template> are NOT skipped: ASP.NET / DNN / OpenCities pages wrap the whole page in a <form>, and
# Vue sites keep amenity tabs in <template>. Form controls (select, option, button, label) are skipped.
SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "iframe", "select", "option", "button", "label",
             "object", "canvas", "map", "textarea"}
STRUCT_SKIP = {"nav", "header", "footer"}
SKIP_ATTR_RE = re.compile(r"(^|[\s_-])(nav|navbar|navigation|menu|menus|mainmenu|megamenu|footer|breadcrumbs?|"
                          r"masthead|skip|skiplink|cookie|cookies|share|social|translate|filters?|facets?|related)"
                          r"([\s_-]|$)", re.I)
SKIP_ROLES = {"navigation", "banner", "contentinfo", "search", "menu", "menubar", "dialog"}
BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "br", "dt", "dd", "dl", "section",
              "article", "blockquote", "pre", "figcaption", "label", "main", "aside", "table", "hr", "address",
              "fieldset", "legend", "summary", "details", "caption"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track",
             "wbr"}


class PageParser(HTMLParser):
    """Visible text as lines (block-level breaks; table rows joined with ' | '), amenity-icon hints, tables,
    title / h1 / og:title, and links. Navigation, header and footer subtrees are skipped."""

    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.lines: list[str] = []
        self.tables: list[list[list[str]]] = []
        self.links: list[tuple[str, str]] = []
        self.title = ""
        self.h1 = ""
        self.og_title = ""
        self._stack: list[tuple[str, bool]] = []      # (tag, starts a skipped subtree)
        self._skip = 0
        self._buf: list[str] = []
        self._in_title = False
        self._in_h1 = False
        self._h1_done = False
        self._tables: list[dict[str, Any]] = []        # open tables: rows, current row, current cell
        self._a_href: str | None = None
        self._a_text: list[str] = []

    # ---- helpers
    def _flush(self) -> None:
        if self._tables:           # inside a table, lines are emitted per row
            return
        t = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if t:
            self.lines.append(t)
        self._buf = []

    def _skipping(self, tag: str, attrs: dict[str, str]) -> bool:
        if tag in SKIP_TAGS or tag in STRUCT_SKIP:
            return True
        if (attrs.get("role") or "").lower() in SKIP_ROLES:
            return True
        cls = " ".join(filter(None, [attrs.get("class"), attrs.get("id")]))
        if cls and SKIP_ATTR_RE.search(cls) and tag not in ("body", "main", "html", "article", "table", "tr", "td", "th"):
            return True
        return attrs.get("aria-hidden") == "true" and tag in ("div", "ul", "section")

    # ---- events
    def handle_starttag(self, tag: str, attrs_l: list[tuple[str, str | None]]) -> None:
        attrs = {k.lower(): (v or "") for k, v in attrs_l}
        if tag == "meta" and attrs.get("property", "").lower() == "og:title":
            self.og_title = attrs.get("content", "")[:200]
        if tag == "title" and not self.title and not any(t in ("svg", "body") for t, _ in self._stack):
            self._in_title = True                    # the document title, not an <svg><title> icon label
        skip_here = self._skip == 0 and self._skipping(tag, attrs)
        if tag not in VOID_TAGS:
            self._stack.append((tag, skip_here))
            if skip_here:
                self._skip += 1
        if tag == "h1" and not self._h1_done:
            self._in_h1 = True                       # h1 is captured even inside a skipped header
        if self._skip:
            return
        if tag == "a":
            self._a_href = attrs.get("href")
            self._a_text = []
        if tag in BLOCK_TAGS:
            self._flush()
        if tag == "table":
            self._tables.append({"rows": [], "row": None, "cell": None})
        elif self._tables and tag == "tr":
            self._tables[-1]["row"] = []
        elif self._tables and tag in ("td", "th"):
            tb = self._tables[-1]
            if tb["row"] is None:
                tb["row"] = []
            tb["cell"] = []
        # amenity icons: alt / title text, and class names that spell out the amenity
        hint = ""
        if tag == "img" and (attrs.get("alt") or attrs.get("title")):
            # "Icon of a basketball." (alt) / "Basketball Courts" (title): the title is usually the amenity name
            hint = attrs.get("title") or attrs["alt"]
            hint = re.sub(r"^\s*(an? )?(icon|image|picture|graphic) (of|for) (an? )?", "", hint, flags=re.I)
        elif tag in ("i", "span", "abbr", "li", "div", "a", "svg", "use") and attrs.get("title"):
            hint = attrs["title"]
        if not hint and re.search(r"(^|\s)(amenit(y|ies)|features?|facilit(y|ies)|place_types?)[-_][a-z0-9_-]*"
                                  r"basket[-_]?ball", attrs.get("class") or "", re.I):
            # WordPress taxonomy classes on the park post ("amenities-basketball-court"): the park is tagged
            hint = "Basketball court (amenity tag)"
        if not hint and tag in ("i", "span", "img", "li", "div", "svg", "use", "a") and re.search(
                r"basket[-_ ]?ball", " ".join(v for k, v in attrs.items() if k in ("class", "id") or k.startswith("data-")),
                re.I):
            hint = "Basketball (icon)"
        if not hint and tag in ("i", "span", "img", "li", "div", "svg", "use"):
            # other amenity icons ("icon-playground"): context that tells an amenity list from a menu
            av = " ".join(v for k, v in attrs.items() if k in ("class", "id") or k.startswith("data-"))
            if re.search(r"icon|amenit|feature", av, re.I):
                am = AMENITY_WORDS.search(re.sub(r"[-_]", " ", av))
                if am:
                    hint = f"{am.group(0).title()} (icon)"
        if hint and len(hint) <= 80:
            if self._tables and self._tables[-1]["cell"] is not None:
                self._tables[-1]["cell"].append(" " + hint + " ")
            else:
                self._flush()
                self.lines.append(re.sub(r"\s+", " ", hint).strip())

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self._in_title = False
            self.title = self.title or " "
        if tag == "h1" and self._in_h1:
            self._in_h1 = False
            self._h1_done = bool(self.h1.strip())
        if tag in VOID_TAGS:
            return
        # pop to the matching open tag (tolerates unclosed children)
        for k in range(len(self._stack) - 1, -1, -1):
            if self._stack[k][0] == tag:
                for t, sk in self._stack[k:][::-1]:
                    self._close(t)
                    if sk:
                        self._skip -= 1
                del self._stack[k:]
                break

    def _close(self, tag: str) -> None:
        if self._skip:
            return
        if tag == "a" and self._a_href is not None:
            self.links.append((self._a_href, re.sub(r"\s+", " ", "".join(self._a_text)).strip()[:120]))
            self._a_href = None
        if self._tables:
            tb = self._tables[-1]
            if tag in ("td", "th") and tb["cell"] is not None and tb["row"] is not None:
                tb["row"].append(re.sub(r"\s+", " ", "".join(tb["cell"])).strip())
                tb["cell"] = None
                return
            if tag == "tr" and tb["row"] is not None:
                if any(c for c in tb["row"]):
                    tb["rows"].append(tb["row"])
                    self.lines.append(" | ".join(c for c in tb["row"] if c))
                tb["row"] = None
                return
            if tag == "table":
                done = self._tables.pop()
                if done["row"]:
                    done["rows"].append(done["row"])
                if done["rows"]:
                    self.tables.append(done["rows"])
                return
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._in_h1:
            self.h1 += data
        if self._skip:
            return
        if self._a_href is not None:
            self._a_text.append(data)
        if self._tables:
            if self._tables[-1]["cell"] is not None:
                self._tables[-1]["cell"].append(data)
        else:
            self._buf.append(data)

    def close(self) -> None:
        super().close()
        while self._tables:                            # unclosed tables
            done = self._tables.pop()
            if done["rows"]:
                self.tables.append(done["rows"])
        self._flush()
        self.title = re.sub(r"\s+", " ", self.title).strip()[:300]
        self.h1 = re.sub(r"\s+", " ", self.h1).strip()[:300]


def parse_html(html: str, base_url: str = "") -> PageParser:
    pp = PageParser(base_url)
    try:
        pp.feed(html)
        pp.close()
    except Exception as exc:  # malformed markup: keep what was parsed
        log.debug("parse error %s: %s", base_url, exc)
    return pp


# =========================================================================== extraction rules
BB = r"basket\s?-?ball"
NUMW = {"a": 1, "an": 1, "one": 1, "single": 1, "two": 2, "pair of": 2, "three": 3, "four": 4, "five": 5, "six": 6,
        "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
NUM = r"(\d{1,2}|an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|single|pair of)"
MODS = (r"(?:(?:full|half)[- ]?(?:size[d]?|length|court)?[- ]?|outdoor[- ]|indoor[- ]|lighted[- ]|lit[- ]|"
        r"regulation[- ]|new[- ]|covered[- ]|paved[- ]|asphalt[- ]|concrete[- ]|public[- ]|small[- ]|large[- ]|"
        r"youth[- ]|\(full\)\s*|\(half\)\s*)*")
FACILITY_RE = re.compile(rf"\b{BB}s?\b\s*{MODS}(courts?|hoops?|goals?|pads?|rims?|backboards?|standards?|"
                         rf"facilit(?:y|ies)|gym(?:nasium)?s?|area|pavilions?)\b", re.I)
COURT_BB_RE = re.compile(rf"\b(courts?|hoops?|goals?)\b[^.;|]{{0,40}}\b{BB}\b", re.I)
COUNT_BEFORE = re.compile(rf"\b{NUM}\s+{MODS}{BB}\s+{MODS}(courts?|hoops?|goals?|rims?|backboards?)\b", re.I)
COUNT_AFTER = re.compile(rf"\b{BB}\s*(?:courts?|hoops?|goals?)?\s*(?:\((?:full|half|outdoor|indoor|lighted)[^)]*\)\s*)?"
                         r"(?:\(\s*(\d{1,2})\s*\)|[:\-–|x×]\s*(\d{1,2})\b"
                         r"(?!\s*(?:am|pm|a\.m|p\.m|:\d|\d|/|-\d|%|th\b|st\b|nd\b|rd\b|yr|years?|ages?|\+|ft|feet|')))",
                         re.I)
HALF_RE = re.compile(r"\bhalf[- ]?courts?\b", re.I)
AMENITY_ITEM_RE = re.compile(rf"^\W*(?:\d+\s*[x×]?\s*)?(?:(?:outdoor|indoor|lighted|lit|covered|full[- ]?court|"
                             rf"half[- ]?court)\s+)*{BB}(?:\s+(?:courts?|hoops?|goals?|area|\(icon\)))?"
                             r"\s*(?:\((?:full|half|outdoor|indoor|lighted|\d{1,2})[^)]*\))?\s*(?:[:\-–]\s*\d{1,2})?\W*$",
                             re.I)
AMENITY_SENTENCE_RE = re.compile(rf"\b(amenities|features|facilities|include[sd]?|including|offers?|offering|has|have|"
                                 rf"there (?:is|are)|equipped with|boasts?|contains?|consists? of|you(?:'ll)? find|"
                                 rf"enjoy)\b[^.]{{0,160}}\b{BB}\b", re.I)
PROGRAM_RE = re.compile(rf"\b{BB}\b\s*(leagues?|camps?|clinics?|programs?|tournaments?|teams?|lessons?|class(?:es)?|"
                        rf"registration|season|skills|academy|association|club|instruction|officials?|referees?|"
                        rf"coach(?:es|ing)?|tryouts?|schedules?|standings|games?|shootout|jamboree)\b|"
                        rf"\b(youth|adult|men'?s|women'?s|girls'?|boys'?|co-?ed|wheelchair|summer|winter|fall|spring)"
                        rf"\s+{BB}\b", re.I)
SEASON_RE = re.compile(r"^\W*(fall|winter|spring|summer|youth|adult|now registering|register)\b", re.I)
PROGRAM_SECTION_RE = re.compile(r"^\W*(programs?|classes|leagues?|camps?|lessons|registration|now registering|"
                                r"sports programs|youth sports|adult sports|fitness classes|upcoming events|events|"
                                r"activities (and|&) programs|athletics programs?)\W*$", re.I)
PRIVATE_VENUE_RE = re.compile(r"\b(rv|campgrounds?|camping|resorts?|koa|thousand trails|jellystone|yogi bear|"
                              r"mobile home|manufactured home|apartments?|condominiums?|condos?|hoa|homeowners|"
                              r"country club|golf club|housing|residence hall|student housing|hotel|motel|inn|"
                              r"vacation rentals?|rental homes?|beach house|oceanside home|oceanfront home|cottages?|"
                              r"cabins?)\b", re.I)
PROGRAM_CTX_RE = re.compile(r"\b(leagues?|camps?|clinics?|programs?|lessons?|class(?:es)?|registration|register|"
                            r"tournaments?|teams?|tryouts?|instruction|activities|sign[- ]?up|youth sports|"
                            r"variety of sports|sports including|sports offered)\b", re.I)
OPEN_GYM_RE = re.compile(rf"\b(open\s+gym|drop[- ]in|pick[- ]?up)\b[^.]{{0,50}}\b{BB}\b|\b{BB}\b[^.]{{0,50}}"
                         rf"\b(open\s+gym|drop[- ]in|pick[- ]?up)\b", re.I)
NEG_RE = re.compile(rf"\b(no|not|without|lacks?|nor)\b(?:\s+\w+){{0,3}}\s+{BB}\b|{BB}[^.;|]{{0,60}}\b(removed|demolished|"
                    rf"eliminated|no longer|not available|unavailable|replaced|converted|taken out|abandoned|defunct)\b|"
                    rf"\b(abandoned|former|defunct|old)\s+{BB}\b|"
                    rf"\b(removed|demolished|replaced|converted)\b[^.;|]{{0,40}}\b{BB}\b", re.I)
PLANNED_RE = re.compile(r"\b(future|proposed|planned|planning|coming soon|will (?:be (?:added|built|installed|constructed|"
                        r"located)|include|feature|add|receive|get|have)|under construction|to be (?:built|added|installed)|"
                        r"master ?plan|conceptual|concept plan|design phase|feasibility|survey|grant|bond (?:issue|"
                        r"program|project)|fundrais\w*|ballot|rfp|request for (?:bids|proposals)|bid (?:opening|"
                        r"documents)|wish ?list|would like)\b", re.I)
INDOOR_RE = re.compile(r"\b(indoor|indoors|(?<!outdoor )gyms?(?! equipment)|gymnasiums?|inside|fieldhouse|"
                       r"field house)\b", re.I)    # not "outdoor gym", "gym equipment"; not "multipurpose room"
OUTDOOR_RE = re.compile(r"\b(outdoor|outdoors|outside|lighted|blacktop|asphalt|open[- ]air|sport ?courts?|"
                        r"post[- ]tension)\b", re.I)
CENTER_TITLE_RE = re.compile(r"\b(recreation (center|centre|complex)|rec (center|centre)|community (center|centre)|"
                             r"gym|gymnasium|ymca|ywca|fieldhouse|field house|senior center|activity center|"
                             r"aquatic|natatorium|boys (&|and) girls club|athletic club|fitness (center|centre)|"
                             r"wellness (center|centre)|athletic (center|centre)|sports (center|centre)|health club)\b",
                             re.I)                       # not "Fitness Loop" / "Fitness Court" in a park's title
AMENITY_WORDS = re.compile(r"\b(playground|picnic|pavilion|shelter|restroom|tennis|pickleball|volleyball|soccer|"
                           r"baseball|softball|football|trail|walking|grill|bench|parking|splash|spray|pool|dog|skate|"
                           r"disc golf|horseshoe|fitness|exercise|lighted|field|garden|pond|fishing|gazebo|water "
                           r"fountain|drinking|amphitheater|sandbox|swing|community center|recreation center|"
                           r"open space|lake|boat|bbq|bocce|cricket|lacrosse|track)\b", re.I)
SCHEDULE_RE = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b|\d{1,2}(:\d{2})?\s*[ap]\.?m\b", re.I)
SECTION_RE = re.compile(r"^\W*(outdoor|outside|indoor|inside)(\s+(amenities|facilities|features|spaces|areas))?\W*$",
                        re.I)
BB_ANY = re.compile(rf"\b{BB}s?\b|\bhoops\b", re.I)


NO_VALUES = {"", "no", "n", "0", "-", "—", "–", "n/a", "na", "none", "x no", "not available"}
YES_VALUES = {"x", "yes", "y", "✓", "✔", "•", "●", "√", "available", "1/2", "half", "full"}


def _num(tok: str) -> int | None:
    tok = tok.strip().lower()
    if tok.isdigit():
        v = int(tok)
        return v if 0 < v <= 40 else None
    return NUMW.get(re.sub(r"\s+", " ", tok))


@dataclass
class Mention:
    kind: str                 # court | amenity_item | amenity_sentence | table | icon | program | open_gym | lone
    line: str
    index: int
    negative: bool = False
    planned: bool = False
    indoor: bool = False
    outdoor: bool = False
    courts: int | None = None
    half_courts: int | None = None
    hoops: int | None = None


def counts_in(line: str) -> tuple[int | None, int | None, int | None]:
    """(full or unspecified courts, half courts, hoops) stated in one line."""
    courts = half = hoops = None
    for m in COUNT_BEFORE.finditer(line):
        n = _num(m.group(1))
        if n is None:
            continue
        unit = (m.group(m.lastindex or 1) or "").lower()
        if unit.startswith(("hoop", "goal", "rim", "backboard")):
            hoops = max(hoops or 0, n)
        elif "half" in m.group(0).lower():
            half = max(half or 0, n)
        else:
            courts = max(courts or 0, n)
    for m in COUNT_AFTER.finditer(line):
        n = _num(m.group(1) or m.group(2) or "")
        if n is None:
            continue
        g = m.group(0).lower()
        if re.search(r"hoops?|goals?", g):
            hoops = max(hoops or 0, n)
        elif "half" in g:
            half = max(half or 0, n)
        else:
            courts = max(courts or 0, n)
    if half is None and re.search(rf"\b{BB}\s*(?:courts?)?\s*[|:\-–]\s*1/2\b", line, re.I):
        half = 1                                 # "Basketball Court | 1/2"
    if half is None and BB_ANY.search(line):
        for m in re.finditer(rf"\b{NUM}\s+half[- ]?courts?\b", line, re.I):
            n = _num(m.group(1))
            if n is not None:
                half = max(half or 0, n)
    return courts, half, hoops


PROGRAM_HEAD_RE = re.compile(r"\b(youth|adult|recreational|recreation) sports\b|\bsports (we )?(offer|provide)|"
                             r"\bfollowing (youth |adult )?sports\b|\bsports offered\b|\bwe (offer|provide) the "
                             r"following\b", re.I)


def in_program_section(lines: list[str], i: int) -> bool:
    """Is line ``i`` under a "Programs" / "Leagues" / "Classes" heading (within the six lines above)?"""
    for prev in reversed(lines[max(0, i - 6):i]):
        if PROGRAM_SECTION_RE.match(prev) or (len(prev) <= 100 and PROGRAM_HEAD_RE.search(prev)):
            return True
        if SECTION_RE.match(prev) or re.match(r"^\W*(amenities|features|facilities|park (features|amenities))\W*$",
                                              prev, re.I):
            return False
    return False


def classify_line(lines: list[str], i: int) -> Mention | None:
    """Rule-based class of line ``i`` if it mentions basketball (None otherwise)."""
    line = lines[i]
    if not BB_ANY.search(line) or re.search(rf"\b(than|as tall as|size of) an? {BB}\b", line, re.I):
        return None                     # "a ledge taller than a basketball hoop" is a comparison, not a court
    low = line.lower()
    has_bb = re.search(rf"\b{BB}", low) is not None
    m: Mention
    if not has_bb:                                        # "hoops" alone: only as a short item in an amenity list
        ctx = " ".join(lines[max(0, i - 3):i] + lines[i + 1:i + 4])
        if len(line) <= 40 and AMENITY_WORDS.search(ctx) and not PROGRAM_RE.search(line) and \
                not re.search(r"spray|splash|water|hula|cannon|ring toss|playground", line, re.I):
            m = Mention("amenity_item", line, i)
        else:
            return None
    elif FACILITY_RE.search(line) or COURT_BB_RE.search(line) or HALF_RE.search(line):
        m = Mention("court", line, i)
    elif len(line) <= 60 and AMENITY_ITEM_RE.match(line):
        ctx_l = lines[max(0, i - 3):i] + lines[i + 1:i + 4]
        near = sum(1 for c in ctx_l if len(c) <= 60 and AMENITY_WORDS.search(c))
        m = Mention(("icon" if "(icon)" in line else "amenity_item") if near else "lone", line, i)
        if m.kind != "lone" and (any(PROGRAM_CTX_RE.search(c) or SEASON_RE.match(c) for c in ctx_l)
                                 or in_program_section(lines, i)):
            m.kind = "program"                  # a list of sports offered ("Fall Kickball / Winter Basketball")
    elif OPEN_GYM_RE.search(line) or (" | " in line and SCHEDULE_RE.search(line)):
        m = Mention("open_gym", line, i)                    # open gym / drop-in, or a gym schedule row
    elif " | " in line and re.search(rf"^\W*{BB}", line, re.I):
        m = Mention("table", line, i)                       # "Basketball | 2" (a two-column amenity table row)
        cells = [c.strip().lower() for c in line.split(" | ")]
        if len(cells) >= 2 and cells[1] in NO_VALUES:
            m.negative = True                               # "Basketball | No"
    elif PROGRAM_RE.search(line):
        m = Mention("program", line, i)
    elif AMENITY_SENTENCE_RE.search(line):
        m = Mention("amenity_sentence", line, i)
    else:
        parts = [p.strip() for p in re.split(r"[,;|•·]| and ", line) if p.strip()]
        if len(parts) >= 3 and all(len(p.split()) <= 5 for p in parts) and any(AMENITY_WORDS.search(p) for p in parts):
            m = Mention("amenity_sentence", line, i)
        else:
            return None
    if m.kind in ("court", "amenity_sentence") and PROGRAM_RE.search(line) and not FACILITY_RE.search(line) \
            and not COURT_BB_RE.search(line):
        m.kind = "program"
    elif m.kind == "amenity_sentence" and PROGRAM_CTX_RE.search(line):
        m.kind = "program"                                   # "Sports leagues: volleyball, basketball, ..."
    m.negative = m.negative or bool(NEG_RE.search(line))
    if line.rstrip().endswith(":") and i + 1 < len(lines) and lines[i + 1].strip() and \
            lines[i + 1].strip().lower() in NO_VALUES:
        m.negative = True                                   # "Basketball:" / "No"
    m.planned = bool(PLANNED_RE.search(line))
    win = line
    if len(line) > 120:                                     # long sentence: only the words around "basketball"
        hit = BB_ANY.search(line)
        if hit is not None:
            win = line[max(0, hit.start() - 60):hit.end() + 60]
    m.indoor = bool(INDOOR_RE.search(win))      # "open gym" says gym; "pick-up" alone can be outdoor
    m.outdoor = bool(OUTDOOR_RE.search(win))
    if not (m.indoor or m.outdoor):                         # an "Outdoor" / "Indoor Amenities" section heading
        for prev in reversed(lines[max(0, i - 8):i]):
            hm = SECTION_RE.match(prev)
            if hm:
                if hm.group(1).lower() in ("indoor", "inside"):
                    m.indoor = True
                else:
                    m.outdoor = True
                break
    m.courts, m.half_courts, m.hoops = counts_in(line)
    if m.courts is None and m.half_courts is None and m.kind in ("amenity_item", "court", "icon"):
        for nxt in lines[i + 1:i + 3]:          # "Basketball" / "Full Courts: 1" (an amenity block)
            mm = re.match(r"^\W*(full|half)?[a-z ]{0,12}courts?\s*[:\-–]\s*(\d{1,2})\b", nxt, re.I)
            if mm and not re.search(r"tennis|pickle|volley|hand ?ball|racquet|bocce|shuffle|futsal", nxt, re.I):
                if (mm.group(1) or "").lower() == "half":
                    m.half_courts = _num(mm.group(2))
                else:
                    m.courts = _num(mm.group(2))
                break
            if AMENITY_WORDS.search(nxt):
                break
    return m


COURT_KINDS = ("court", "amenity_item", "amenity_sentence", "table", "icon")


def snippet_of(line: str, width: int = 200) -> str:
    m = BB_ANY.search(line)
    if len(line) <= width or m is None:
        return line[:width]
    a = max(0, m.start() - width // 2)
    b = min(len(line), a + width)
    return ("..." if a else "") + line[a:b].strip() + ("..." if b < len(line) else "")


def table_rows_with_basketball(tables: list[list[list[str]]]) -> list[tuple[str, int | None, str]]:
    """Amenity matrices: a header cell says basketball, one row per park. -> (park name, count, evidence)."""
    out: list[tuple[str, int | None, str]] = []
    for rows in tables:
        if len(rows) < 3:
            continue
        hdr_i = next((k for k, r in enumerate(rows[:3]) if any(re.search(rf"\b{BB}", c, re.I) and len(c) <= 40
                                                                  for c in r)), None)
        if hdr_i is None:
            continue
        hdr = rows[hdr_i]
        cols = [j for j, c in enumerate(hdr) if re.search(rf"\b{BB}", c, re.I) and len(c) <= 40]
        if not cols or len(hdr) < 3:
            continue
        name_col = next((j for j, c in enumerate(hdr) if re.search(r"\b(park|name|facility|location|site)\b", c, re.I)
                         and j not in cols), 0)
        if name_col in cols:
            continue
        for r in rows[hdr_i + 1:]:
            if len(r) != len(hdr):
                continue
            name = r[name_col].strip()
            if not name or len(name) > 80 or re.search(rf"\b{BB}", name, re.I) or not re.search(r"[a-z]", name, re.I):
                continue
            for j in cols:
                v = r[j].strip().lower()
                if v in NO_VALUES or not (v in YES_VALUES or v.isdigit() or re.search(rf"\b{BB}|\bhalf\b|\bfull\b", v)
                                          or (len(v) <= 3 and not v.isalpha())):
                    continue                        # an empty cell, "No", or another amenity's icon
                cnt = _num(v) if v.isdigit() else None
                ev = f"{hdr[j]}: {r[j].strip() or 'yes'} ({name})"
                out.append((name, cnt, ev[:200]))
                break
    return out


@dataclass
class PageResult:
    has_court: bool = False
    program_only: bool = False
    open_gym: bool = False
    negative_only: bool = False
    planned_only: bool = False
    indoor_outdoor: str = "unknown"       # outdoor | indoor | both | indoor_likely | unknown
    courts: int | None = None
    half_courts: int | None = None
    hoops: int | None = None
    indoor_courts: int | None = None
    mention_kind: str = ""
    snippet: str = ""
    n_mentions: int = 0
    kinds: dict[str, int] = field(default_factory=dict)


# shop pages (not prices: park pages list rental fees)
COMMERCE_RE = re.compile(r"add to (cart|bag)|shop now|buy now|checkout|free shipping|in stock|out of stock|order now|"
                         r"sale price|regular price|shopping cart", re.I)
SHOP_TITLE_RE = re.compile(r"\b(playground equipment|equipment (sales|supply|store)|online store|store|shop|dealers?|"
                           r"outlet|for sale)\b", re.I)


def judge_page(lines: list[str], title: str = "") -> PageResult:
    ms = [m for m in (classify_line(lines, i) for i in range(len(lines))) if m is not None]
    res = PageResult(n_mentions=len(ms), kinds=dict(Counter(m.kind for m in ms)))
    if not ms:
        return res
    if sum(1 for ln in lines if COMMERCE_RE.search(ln)) >= 2 or SHOP_TITLE_RE.search(title or ""):
        # a shop selling hoops, not a park
        res.mention_kind = "commerce"
        return res
    court = [m for m in ms if m.kind in COURT_KINDS and not m.negative and not m.planned]
    res.open_gym = any(m.kind == "open_gym" for m in ms)
    if not court:
        res.program_only = any(m.kind == "program" for m in ms)
        res.negative_only = any(m.negative for m in ms)
        res.planned_only = any(m.planned and m.kind in COURT_KINDS for m in ms)
        best = next((m for m in ms if m.kind == "open_gym"), ms[0])
        res.snippet, res.mention_kind = snippet_of(best.line), best.kind
        if any(m.kind == "open_gym" and m.indoor for m in ms):
            res.indoor_outdoor = "indoor"
        elif res.open_gym and CENTER_TITLE_RE.search(title or ""):
            res.indoor_outdoor = "indoor_likely"         # drop-in basketball on a community-centre page
        return res
    res.has_court = True
    center = bool(CENTER_TITLE_RE.search(title or ""))
    ind = any(m.indoor for m in court)
    out = any(m.outdoor for m in court)
    res.indoor_outdoor = "both" if (ind and out) else "indoor" if ind else "outdoor" if out else (
        "indoor_likely" if center else "unknown")
    for m in court:
        if m.courts is not None:
            if m.indoor and not m.outdoor:
                res.indoor_courts = max(res.indoor_courts or 0, m.courts)
            else:
                res.courts = max(res.courts or 0, m.courts)
        if m.half_courts is not None:
            res.half_courts = max(res.half_courts or 0, m.half_courts)
        if m.hoops is not None:
            res.hoops = max(res.hoops or 0, m.hoops)
    order = {"court": 0, "table": 1, "amenity_item": 2, "icon": 3, "amenity_sentence": 4}
    best = sorted(court, key=lambda m: (m.courts is None and m.half_courts is None, order.get(m.kind, 9), m.index))[0]
    res.mention_kind, res.snippet = best.kind, snippet_of(best.line)
    return res


def boilerplate_lines(pages: list[list[str]], min_pages: int = 6, share: float = 0.5) -> set[tuple[str, str, str]]:
    """(prev, line, next) shingles seen on at least ``share`` of a host's pages: menus, banners, footers.
    An amenity list differs between parks, so its neighbours differ and it survives."""
    if len(pages) < min_pages:
        return set()
    cnt: Counter[tuple[str, str, str]] = Counter()
    for lines in pages:
        sh = set()
        for i, ln in enumerate(lines):
            if BB_ANY.search(ln):
                sh.add((lines[i - 1] if i else "", ln, lines[i + 1] if i + 1 < len(lines) else ""))
        cnt.update(sh)
    lim = max(3, share * len(pages))
    return {k for k, v in cnt.items() if v >= lim}


def strip_boilerplate(lines: list[str], bp: set[tuple[str, str, str]]) -> list[str]:
    if not bp:
        return lines
    out = []
    for i, ln in enumerate(lines):
        if BB_ANY.search(ln) and (lines[i - 1] if i else "", ln, lines[i + 1] if i + 1 < len(lines) else "") in bp:
            continue
        out.append(ln)
    return out


# =========================================================================== names
GENERIC_NAME_WORDS = {
    "park", "parks", "playground", "playgrounds", "playfield", "playlot", "recreation", "rec", "center", "centre",
    "community", "neighborhood", "neighbourhood", "sports", "sport", "complex", "field", "fields", "area", "areas",
    "square", "commons", "the", "and", "of", "at", "municipal", "athletic", "pocket", "mini", "tot", "lot",
    "greenway", "preserve", "facility", "facilities", "site", "grounds", "ground", "public",
}
SITE_SUFFIX_RE = re.compile(r"\s+(?:\||-|–|—|::|»|:|•|·)\s+")
GENERIC_PAGE_TITLES = re.compile(r"^(home|welcome|parks?( (and|&) rec(reation)?)?( department)?|recreation|"
                                 r"facilities|amenities|park (finder|directory|locations?)|find a park|"
                                 r"page not found|404|error|search|untitled|navigation|main navigation|menu|"
                                 r"loading\.*|parks? (and|&) facilities|"
                                 r"by category|categories|locations?|facility directory|find a facility|explore|"
                                 r"directory|all parks|our parks|parks? list|facility details|details)$|"
                                 r"^(subscribe|sign up|skip to|alert|notify me|you are here|welcome to|stay connected)",
                                 re.I)


def name_tokens(s: str | None) -> list[str]:
    if not s:
        return []
    s = s.lower().replace("&", " and ").replace("'", "").replace("’", "")
    s = re.sub(r"\bst\.?\s", "saint ", s)
    s = re.sub(r"\bmt\.?\s", "mount ", s)
    s = re.sub(r"\bft\.?\s", "fort ", s)
    return re.findall(r"[a-z0-9]+", s)


def name_core(s: str | None) -> str:
    toks = name_tokens(s)
    core = [t for t in toks if t not in GENERIC_NAME_WORDS]
    return " ".join(core) if core else " ".join(toks)


def name_sim(a: str | None, b: str | None) -> float:
    ca, cb = set(name_core(a).split()), set(name_core(b).split())
    if not ca or not cb:
        return 0.0
    return len(ca & cb) / len(ca | cb)


def name_agree(a: str | None, b: str | None) -> bool:
    """Does page / polygon name ``b`` name the park ``a``? Same core tokens; ``b`` inside ``a`` ('Downs Park' in
    'Downs Park, Pasadena, Md'); a multi-word ``a`` inside ``b``; token overlap for multi-word names; or
    near-identical spelling ('Greenlake' / 'Green Lake', 'Fawn Ridge' / 'Fawnridge'). A one-word name is not
    matched by a longer one ('Gilbert Community Park' is not 'Gilbert Youth Sports')."""
    import unicodedata
    from difflib import SequenceMatcher

    def fold(s: str | None) -> str | None:
        return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode() if s else s

    ca, cb = name_core(fold(a)).split(), name_core(fold(b)).split()
    if not ca or not cb:
        return False
    sa, sb = set(ca), set(cb)
    if sa == sb:
        return True
    if sb <= sa and max(len(t) for t in sb) >= 4:
        return True
    if len(sa) >= 2 and (sa <= sb or len(sa & sb) / len(sa | sb) >= 0.34):
        return True
    ja, jb = "".join(ca), "".join(cb)
    if len(jb) >= 6 and jb in ja:
        return True
    return len(ja) >= 6 and ja in jb and len(sa) >= 2 or SequenceMatcher(None, ja, jb).ratio() >= 0.85


def page_name(title: str, h1: str, og: str = "") -> str:
    cands = [h1, og] + SITE_SUFFIX_RE.split(title or "")
    for c in cands:
        c = (c or "").strip(" -|:")
        if 3 <= len(c) <= 90 and not GENERIC_PAGE_TITLES.match(c) and not re.search(
                r"\b(city|town|county|village|township) of\b|\.gov\b|\.org\b|\.com\b", c, re.I):
            return c
    return (h1 or title or "").strip()[:90]


# =========================================================================== seeds -> URL list
PARK_KINDS = {"park", "recreation_ground", "playground", "state_park"}
CENTER_KINDS = {"community_centre", "sports_centre", "community_center", "recreation_center", "civic_center",
                "sports_complex", "basketball_court", "sport_court"}
SEED_OVERTURE_CATS = OVERTURE_PARK_CATS | {"community_center", "recreation_center", "civic_center", "sports_complex",
                                           "basketball_court", "sport_court"}


def cmd_seeds(args: argparse.Namespace) -> None:
    rows: dict[str, list[Any]] = defaultdict(list)

    def add(url: str | None, source: str, sid: str, name: Any, kind: str, state: Any, city: Any, lon: float,
            lat: float) -> None:
        u = normalise_url(url)
        if u is None or is_blocked_url(u) or re.search(r"\.(pdf|jpe?g|png|gif|docx?|xlsx?|zip|kmz?)$", u, re.I):
            return
        rows["url"].append(u)
        rows["host"].append(host_of(u))
        rows["source"].append(source)
        rows["source_id"].append(sid)
        rows["name"].append(None if name is None else str(name))
        rows["kind"].append(kind)
        rows["state"].append(None if state is None else str(state))
        rows["city"].append(None if city is None else str(city))
        rows["lon"].append(float(lon))
        rows["lat"].append(float(lat))

    if OSM_SEEDS.exists():
        o, _ = read_geoparquet(OSM_SEEDS, with_geometry=False)
        for i in range(len(o["osm_ref"])):
            for u in str(o["urls"][i] or o["website"][i]).split(";")[:2]:
                add(u, "osm", str(o["osm_ref"][i]), o["name"][i], str(o["kind"][i]), o["state"][i], o["addr_city"][i],
                    o["lon"][i], o["lat"][i])
    else:
        log.warning("%s missing (run osm-seeds)", OSM_SEEDS)
    if OVERTURE_FILE.exists():
        v, _ = read_geoparquet(OVERTURE_FILE, columns=["id", "name", "cat", "website", "region", "locality", "lon", "lat",
                                                       "operating_status"], with_geometry=False)
        for i in range(len(v["id"])):
            if v["cat"][i] in SEED_OVERTURE_CATS and v["website"][i] and v["operating_status"][i] != "permanently_closed":
                add(v["website"][i], "overture", str(v["id"][i]), v["name"][i], str(v["cat"][i]), v["region"][i],
                    v["locality"][i], v["lon"][i], v["lat"][i])
    if WIKIDATA_SEEDS.exists():
        k, _ = read_geoparquet(WIKIDATA_SEEDS, with_geometry=False)
        for i in range(len(k["qid"])):
            add(k["website"][i], "wikidata", str(k["qid"][i]), k["name"][i], "park", None, None, k["lon"][i], k["lat"][i])
    n = len(rows["url"])
    names_by_url: dict[str, set[str]] = defaultdict(set)
    for u, nm in zip(rows["url"], rows["name"]):
        names_by_url[u].add(name_core(nm) or "?")
    rows["n_names"] = [len(names_by_url[u]) for u in rows["url"]]
    root = [urlsplit(u).path in ("", "/") and not urlsplit(u).query for u in rows["url"]]
    rows["generic"] = [(nn >= 3) or (r and nn >= 2) for nn, r in zip(rows["n_names"], root)]
    rows["root"] = root
    geoms = shapely.points(np.asarray(rows["lon"], float), np.asarray(rows["lat"], float))
    write_geoparquet(SEEDS_FILE, {k: pa.array(v) for k, v in rows.items()}, geoms)
    uniq = {u for u, g in zip(rows["url"], rows["generic"]) if not g}
    log.info("seeds: %d rows, %d unique URLs (%d park-specific), %d hosts; by source %s", n, len(set(rows["url"])),
             len(uniq), len(set(rows["host"])), dict(Counter(rows["source"])))


# =========================================================================== expansion
def url_template(url: str) -> str | None:
    """Directory of a park page ('/parks/' for '/parks/smith-park'); query pages keep their key names.
    Top-level pages ('/smith-park') have no useful directory (None)."""
    p = urlsplit(url)
    path = p.path or "/"
    if p.query:
        keys = sorted(q.split("=", 1)[0].lower() for q in p.query.split("&") if q)
        return f"{path}?{'&'.join(keys)}"
    stripped = path.strip("/")
    if "/" not in stripped:
        return None
    return "/" + stripped.rsplit("/", 1)[0] + "/"


TEMPLATE_RE = re.compile(r"park|facilit|recreat|places?\b|locations?|venue|cent(er|re)s?\b|amenit|playground|"
                         r"court|\brec\b|_rec|-rec", re.I)


def same_template(url: str, template: str) -> bool:
    p = urlsplit(url)
    if "?" in template:
        path, keys = template.split("?", 1)
        got = "&".join(sorted(q.split("=", 1)[0].lower() for q in p.query.split("&") if q))
        return p.path.lower() == path.lower() and got == keys
    path = p.path or "/"
    return path.lower().startswith(template.lower()) and "/" not in path[len(template):].strip("/") and \
        len(path) > len(template)


def sitemap_urls(fetcher: PoliteFetcher, host: str, scheme: str, keywords: tuple[str, ...], limit_maps: int = 25,
                 limit_urls: int = 60000) -> list[str]:
    hs = fetcher.state(scheme, host)
    maps = list(dict.fromkeys((hs.robots.sitemaps if hs.robots else []) + [f"{scheme}://{host}/sitemap.xml"]))
    seen: set[str] = set()
    out: list[str] = []
    while maps and len(seen) < limit_maps and len(out) < limit_urls:
        sm = maps.pop(0)
        if sm in seen or host_of(sm) != host:
            continue
        seen.add(sm)
        rec = fetcher.fetch(sm, accept_xml=True)
        body = load_cached(rec, fetcher.cache_dir) if rec.get("outcome") == "ok" else None
        if not body:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body, re.I)
        if re.search(r"<sitemapindex", body[:2000], re.I):
            kids = [x.replace("&amp;", "&") for x in locs]
            # child sitemaps named for parks / places / pages first; skip media, product, author, tag, event
            # and news sitemaps, which never list park pages
            kids = [x for x in kids if not re.search(r"image|video|product|author|tag|categor|event|news|post-sitemap"
                                                       r"|blog|attachment|media", x, re.I)]
            kids.sort(key=lambda x: (not any(k in x.lower() for k in keywords), x))
            maps.extend(kids)
        else:
            out.extend(x.replace("&amp;", "&") for x in locs)
    return out


def listing_pages(fetcher: PoliteFetcher, scheme: str, host: str, template: str, max_pages: int = 20
                  ) -> list[tuple[str, str, str, str]]:
    """No sitemap: the park list is usually on the directory page or one or two levels up
    (CivicPlus: /Facilities/Facility/Details/<park> is listed on /Facilities, paginated with ?page=N)."""
    parts = [p for p in template.strip("/").split("/") if p]
    if fetcher.state(scheme, host).delay > 5.0:      # a long Crawl-delay: only the first listing pages
        max_pages = min(max_pages, 3)
    tried: list[str] = []
    for k in range(len(parts), max(0, len(parts) - 3), -1):
        tried.append(f"{scheme}://{host}/" + "/".join(parts[:k]))
    found: list[tuple[str, str, str, str]] = []
    for listing in tried:
        todo: list[str] = [listing]
        seen: set[str] = set()
        while todo and len(seen) < max_pages:
            page = todo.pop(0)
            if page in seen:
                continue
            seen.add(page)
            rec = fetcher.fetch(page)
            html = load_cached(rec, fetcher.cache_dir) if rec.get("outcome") == "ok" else None
            if not html:
                break
            base = rec.get("final_url") or page
            hrefs = [hh for hh, _ in parse_html(html, page).links] + re.findall(r"""href=["']([^"']+)["']""", html)
            for href in dict.fromkeys(hrefs):
                u = normalise_url(urljoin(base, href.replace("&amp;", "&")))
                if not u or host_of(u) != host:
                    continue
                if same_template(u, template):
                    found.append((u, host, "listing", template))
                elif urlsplit(u).path.rstrip("/").lower() == urlsplit(base).path.rstrip("/").lower() and \
                        re.fullmatch(r"(page|pg|p|start|offset)=\d+", urlsplit(u).query, re.I):
                    todo.append(u)                          # pagination of the same listing (not sort variants)
        if len(found) >= 5:
            break
    return list(dict.fromkeys(found))


def load_registry() -> list[dict[str, Any]]:
    if not REGISTRY_FILE.exists():
        return []
    reg = read_json(REGISTRY_FILE)
    return [e for e in reg.get("directories", []) if e.get("enabled", True)]


def cmd_expand(args: argparse.Namespace) -> None:
    """Directories of park pages that fetched fine (final URL after redirects; seed URLs not fetched yet count
    too), then every sibling page in the host's sitemaps or on the directory page itself."""
    s, _ = read_geoparquet(SEEDS_FILE, with_geometry=False)
    fetched = read_fetch_log()
    fetcher = PoliteFetcher(delay=args.delay)
    by_host: dict[str, Counter[str]] = defaultdict(Counter)
    host_pts: dict[str, list[tuple[float, float]]] = defaultdict(list)
    scheme_of: dict[str, str] = {}
    seed_urls = set(s["url"].tolist())
    for u, g, k, lon, lat in zip(s["url"], s["generic"], s["kind"], s["lon"], s["lat"]):
        rec = fetched.get(u)
        if rec is not None and rec.get("outcome") != "ok":
            continue                                   # dead or refused seed: its directory may be stale
        final = (rec or {}).get("final_url") or u
        h = host_of(final)
        host_pts[h].append((float(lon), float(lat)))
        scheme_of.setdefault(h, urlsplit(final).scheme)
        if g or k not in PARK_KINDS | CENTER_KINDS:
            continue
        t = url_template(final)
        if t:
            by_host[h][t] += 1
    jobs: list[tuple[str, str, list[str]]] = []
    for h, tc in by_host.items():
        # only directories that look like park / facility directories ("/parks/", "/Facilities/Facility/Details/",
        # "/park.aspx?id"), not "/departments/" or "/fishboat/fish/recreational/lakes/"
        temps = [t for t, c in tc.items() if c >= args.min_seeds and TEMPLATE_RE.search(t)]
        if temps:
            jobs.append((h, scheme_of.get(h, "https"), temps))
    jobs.sort(key=lambda j: -sum(by_host[j[0]][t] for t in j[2]))
    rows: dict[str, list[Any]] = defaultdict(list)
    done_hosts: set[str] = set()
    done_keys: set[str] = set()
    if EXPANDED_FILE.exists() and not args.force:      # incremental: keep earlier results, skip their hosts
        old = pq.read_table(EXPANDED_FILE).to_pydict()
        for k, v in old.items():
            rows[k].extend(v)
        done_hosts = {h for h, via in zip(old.get("host", []), old.get("via", [])) if via != "registry"}
        done_keys = {k for k in old.get("registry_key", []) if k}
        if EXPAND_HOSTS_FILE.exists():
            done_hosts |= set(json.loads(EXPAND_HOSTS_FILE.read_text(encoding="utf-8")))
    jobs = [j for j in jobs if j[0] not in done_hosts and j[0] not in set(args.exclude_hosts or [])]
    if args.max_hosts:
        jobs = jobs[:args.max_hosts]
    log.info("expand: %d new hosts with >= %d park-specific seed URLs sharing a directory (%d done before)",
             len(jobs), args.min_seeds, len(done_hosts))
    attempted = set(done_hosts)

    def area_of(h: str) -> tuple[float, float, float, float]:
        pts = np.asarray(host_pts.get(h) or [(0.0, 0.0)])
        pad = args.area_pad
        lo, hi = np.percentile(pts, 5, axis=0), np.percentile(pts, 95, axis=0)
        return (float(lo[0]) - pad, float(lo[1]) - pad, float(hi[0]) + pad, float(hi[1]) + pad)

    def run_host(job: tuple[str, str, list[str]]) -> list[tuple[str, str, str, str]]:
        h, scheme, temps = job
        found: list[tuple[str, str, str, str]] = []
        try:
            smaps = sitemap_urls(fetcher, h, scheme, ("park", "facilit", "location", "place", "page"))
        except Exception as exc:
            log.warning("%s sitemap: %s", h, exc)
            smaps = []
        for t in temps:
            hits = [u for u in smaps if host_of(u) == h and same_template(u, t)]
            found.extend((u, h, "sitemap", t) for u in hits[:args.max_per_template])
            if len(hits) < 5 and "?" not in t:
                found.extend(listing_pages(fetcher, scheme, h, t))
        return found

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(run_host, j): j for j in jobs}
        for f in as_completed(futs):
            h = futs[f][0]
            attempted.add(h)
            try:
                res = f.result()
            except Exception as exc:
                log.warning("%s: %s", h, exc)
                continue
            area = area_of(h)
            for u, hh, via, t in dict.fromkeys(res):
                nu = normalise_url(u)
                if not nu:
                    continue
                rows["url"].append(nu)
                rows["host"].append(hh)
                rows["via"].append(via)
                rows["template"].append(t)
                rows["registry_key"].append(None)
                rows["city"].append(None)
                rows["state"].append(None)
                for k, v in zip(("area_w", "area_s", "area_e", "area_n"), area):
                    rows[k].append(v)
            log.info("%s: %d pages via %s", h, len(res), Counter(r[2] for r in res))
    # registry park directories
    for e in load_registry():
        if (args.registry and e["key"] not in args.registry) or e["key"] in done_keys:
            continue
        found = registry_pages(fetcher, e)
        for u in found:
            rows["url"].append(u)
            rows["host"].append(host_of(u))
            rows["via"].append("registry")
            rows["template"].append(e.get("park_link") or "pages")
            rows["registry_key"].append(e["key"])
            rows["city"].append(e.get("city"))
            rows["state"].append(e.get("state"))
            w, s_, e_, n_ = e["bbox"]
            for k, v in zip(("area_w", "area_s", "area_e", "area_n"), (w, s_, e_, n_)):
                rows[k].append(float(v))
        log.info("registry %s: %d park pages", e["key"], len(found))
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    table = pa.table({k: pa.array(v) for k, v in rows.items()}) if rows else pa.table({"url": pa.array([], pa.string())})
    pq.write_table(table, EXPANDED_FILE, compression="zstd")
    EXPAND_HOSTS_FILE.write_text(json.dumps(sorted(attempted)), encoding="utf-8")
    log.info("expand: %d pages (%d new vs seeds) -> %s", len(rows["url"]),
             len(set(rows["url"]) - seed_urls), EXPANDED_FILE)


def registry_pages(fetcher: PoliteFetcher, e: dict[str, Any]) -> list[str]:
    """Park pages of a registry entry: its fixed ``pages``, plus park links found on the start pages (directory
    pages or sitemaps; sitemap indexes and ``next_link`` listing pages are followed)."""
    found: list[str] = [u for u in (normalise_url(x) for x in e.get("pages") or []) if u]
    if not e.get("start_urls"):
        return found
    park_rx = re.compile(e["park_link"], re.I)
    next_rx = re.compile(e["next_link"], re.I) if e.get("next_link") else None
    todo = list(e["start_urls"])
    seen: set[str] = set()
    limit = max(int(e.get("max_listing_pages", 30)), 25)
    while todo and len(seen) < limit:
        u = todo.pop(0)
        if u in seen:
            continue
        seen.add(u)
        is_map = ".xml" in u.lower()
        rec = fetcher.fetch(u, accept_xml=is_map)
        html = load_cached(rec, fetcher.cache_dir) if rec.get("outcome") == "ok" else None
        if not html:
            log.warning("registry %s: %s -> %s", e["key"], u, rec.get("outcome"))
            continue
        locs = [x.replace("&amp;", "&") for x in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", html, re.I)]
        if is_map and re.search(r"<sitemapindex", html[:3000], re.I):
            todo.extend(x for x in locs if x not in seen)       # child sitemaps
            continue
        hrefs = locs + [h for h, _ in parse_html(html, u).links] + re.findall(r"""href=["']([^"']+)["']""", html)
        for href in hrefs:
            a = urljoin(rec.get("final_url") or u, href.replace("&amp;", "&"))
            if park_rx.search(a):
                nu = normalise_url(a)
                if nu and nu not in found:
                    found.append(nu)
            elif next_rx is not None and next_rx.search(a) and a not in seen:
                todo.append(a)
    return found[:int(e.get("max_pages", 3000))]


# =========================================================================== crawl
def crawl_queue() -> list[str]:
    urls: list[str] = []
    if SEEDS_FILE.exists():
        s, _ = read_geoparquet(SEEDS_FILE, columns=["url", "generic"], with_geometry=False)
        urls.extend(u for u, g in zip(s["url"], s["generic"]) if not g)
    if EXPANDED_FILE.exists():
        e = pq.read_table(EXPANDED_FILE, columns=["url"]).column("url").to_pylist()
        per_host: Counter[str] = Counter()
        for u in e:                          # state wildlife / natural-resource / tourism sites: few courts
            h = host_of(u)
            per_host[h] += 1
            if LOW_YIELD_HOST_RE.search(h) and per_host[h] > LOW_YIELD_CAP:
                continue
            urls.append(u)
    return list(dict.fromkeys(urls))


LOW_YIELD_HOST_RE = re.compile(r"(^|\.)(dec\.ny\.gov|wdfw\.wa\.gov|tpwd\.texas\.gov|fws\.gov|nps\.gov|mass\.gov|"
                               r"mdc\.mo\.gov|thetrustees\.org|swfwmd\.state\.fl\.us)$|dnr|"
                               r"stateparks|state-parks|wildlife|fishgame|tourism|travel|visit|arkansas\.com", re.I)
LOW_YIELD_CAP = 150


def cmd_crawl(args: argparse.Namespace) -> None:
    queue = crawl_queue()
    done = read_fetch_log()
    final = {"ok", "http_error", "robots_disallowed", "not_html", "blocked_site", "bad_url", "bot_challenge",
             "host_stopped:bot_challenge", "host_stopped:http_401_refused", "host_stopped:http_403_refused",
             "too_many_redirects", "redirect_loop"}
    # hosts whose robots.txt could not be fetched last time (timeout, 5xx) get one more try
    robots_retry: set[str] = set()
    if ROBOTS_LOG.exists():
        latest: dict[str, str] = {}
        with open(ROBOTS_LOG, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rr = json.loads(line)
                except ValueError:
                    continue
                latest[rr["host"]] = str(rr.get("outcome"))
        robots_retry = {h for h, o in latest.items() if "unreachable" in o}
    def is_final(o: str | None) -> bool:
        return o in final or str(o).startswith("host_stopped:crawl_delay")

    # an older, looser challenge test flagged normal pages that load reCAPTCHA (HTTP 200): retry those hosts
    false_challenge = {host_of(u) for u, r in done.items() if r.get("outcome") == "bot_challenge"
                       and r.get("status") == 200}
    todo = [u for u in queue if args.refetch or not is_final(done.get(u, {}).get("outcome"))
            or (done[u].get("outcome") == "robots_disallowed" and host_of(u) in robots_retry)
            or (str(done[u].get("outcome")).endswith("bot_challenge") and host_of(u) in false_challenge)]
    by_host: dict[str, list[str]] = defaultdict(list)
    for u in todo:
        by_host[host_of(u)].append(u)
    for h in by_host:
        by_host[h] = by_host[h][:args.max_per_host]
    hosts = sorted((h for h in by_host if h not in set(args.exclude_hosts or [])), key=lambda h: -len(by_host[h]))
    total = sum(len(v) for v in by_host.values())
    log.info("crawl: %d queued, %d to fetch on %d hosts (delay %.1f s/host, %d workers)", len(queue), total, len(hosts),
             args.delay, args.workers)
    fetcher = PoliteFetcher(delay=args.delay, timeout=args.timeout)
    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes else math.inf
    counter: Counter[str] = Counter()
    t0 = time.monotonic()

    def run(h: str) -> None:
        for u in by_host[h]:
            if time.monotonic() > deadline:
                return
            rec = fetcher.fetch(u)
            with _LOCK:
                counter[rec.get("outcome", "?")] += 1
                n = sum(counter.values())
            if n % 500 == 0:
                log.info("%d / %d fetched (%.1f/s) %s", n, total, n / max(1.0, time.monotonic() - t0),
                         dict(counter.most_common(6)))
            if str(rec.get("outcome", "")).startswith("host_stopped"):
                return

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(run, hosts))
    log.info("crawl done in %.0f min: %s", (time.monotonic() - t0) / 60, dict(counter))


# =========================================================================== extract
RELATED_HEAD_RE = re.compile(r"^\W*(you may also like|you might also like|nearby (parks|places|locations|facilities)|"
                             r"other (parks|locations|facilities)( nearby)?|similar (parks|places)|more (parks|places)"
                             r"( nearby| to explore)?|related (parks|places|locations|facilities|content|links|pages)|"
                             r"explore (more|nearby)( parks)?)\W*$", re.I)


def cut_related(lines: list[str]) -> list[str]:
    """Drop a "You May Also Like" / "Nearby Parks" block (and what follows it): its amenity icons belong to other
    parks. Only from the fourth line on, so a page that is itself a list of parks keeps its body."""
    for k, ln in enumerate(lines):
        if k >= 3 and len(ln) <= 60 and RELATED_HEAD_RE.match(ln):
            return lines[:k]
    return lines


def _extract_host(items: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    parsed = []
    for url, rec in items:
        html = load_cached(rec)
        if html is None:
            continue
        pp = parse_html(html, url)
        parsed.append((url, rec, pp))
    bp = boilerplate_lines([pp.lines for _, _, pp in parsed])
    out: list[dict[str, Any]] = []
    for url, rec, pp in parsed:
        lines = cut_related(strip_boilerplate(pp.lines, bp))
        matrix = table_rows_with_basketball(pp.tables)
        matrix_lines: set[str] = set()
        if len(matrix) >= 2:                    # an amenity chart: its rows are parks, not this page's park
            for rows in pp.tables:
                if any(re.search(rf"\b{BB}", c, re.I) for r in rows[:3] for c in r):
                    matrix_lines.update(" | ".join(c for c in r if c) for r in rows)
        res = judge_page([ln for ln in lines if ln not in matrix_lines], pp.title)
        pname = page_name(pp.title, pp.h1, pp.og_title)
        n_links = len(pp.links)
        base = {"url": url, "final_url": rec.get("final_url") or url, "host": host_of(url),
                "fetched_at": rec.get("fetched_at"), "title": pp.title[:200], "h1": pp.h1[:200], "page_name": pname,
                "n_lines": len(lines), "n_links": n_links, "boilerplate_removed": len(pp.lines) - len(lines)}
        out.append({**base, "from_table": False, "table_name": None,
                    **{k: v for k, v in res.__dict__.items() if k != "kinds"}, "kinds": json.dumps(res.kinds)})
        rows_out = [(n, c, e, "matrix") for n, c, e in matrix] if len(matrix) >= 2 else []
        rows_out += [(n, c, e, "list_page") for n, c, e in basketball_list_page(pname, lines)]
        if not rows_out:
            rows_out += [(n, c, e, "park_list") for n, c, e in multi_park_rows(lines, pp.tables)]
        for name, cnt, ev, kind in rows_out:
            out.append({**base, "from_table": True, "table_name": name, "has_court": True, "program_only": False,
                        "open_gym": False, "negative_only": False, "planned_only": False, "indoor_outdoor": "unknown",
                        "courts": cnt, "half_courts": None, "hoops": None, "indoor_courts": None,
                        "mention_kind": kind, "snippet": ev, "n_mentions": 1, "kinds": "{}"})
    return out


LIST_PAGE_TITLE_RE = re.compile(rf"\b{BB}\s+(courts?|court locations?|locations?)\b|\b(parks?|locations?|places) "
                                rf"(with|for) {BB}|where to play {BB}|\b{BB}\b[^|]*\b(in|at) (our |city |county )?parks\b",
                                re.I)
PARK_LINE_RE = re.compile(r"\b(park|playground|playfield|recreation|rec center|community center|centre|center|"
                          r"square|commons|field|green|plaza|school)\b", re.I)


def _park_name_cell(cell: str) -> str | None:
    """'City Park 9701 E. 16th Ave.' -> 'City Park'; None when the cell does not look like a park name."""
    name = re.split(r"\s+\d{2,6}\s+[NSEW]?\.?\s*\w|\s+[-–—|]\s+|\s*\(|\s*:\s|\s+\d{3}[-.)]\d{3}", cell)[0]
    name = name.strip(" -|:,�")
    if not (4 <= len(name) <= 60) or BB_ANY.search(name) or len(name.split()) > 7 or "," in name or \
            name.endswith(".") or re.match(r"(?i)about\b", name):
        return None
    return name if PARK_LINE_RE.search(name) else None


def multi_park_rows(lines: list[str], tables: list[list[list[str]]]) -> list[tuple[str, int | None, str]]:
    """Pages that describe many parks at once: a table with one park per row and its amenities in another
    cell ('City Park | basketball courts (2), playground'), or a park-name line followed by its amenity line.
    At least three parks with a basketball mention are needed."""
    out: list[tuple[str, int | None, str]] = []
    for rows in tables:
        hits = []
        for r in rows:
            if len(r) < 2:
                continue
            name = _park_name_cell(r[0])
            rest = " | ".join(r[1:])
            if name and (FACILITY_RE.search(rest) or re.search(rf"\b{BB}\b", rest, re.I)) and not NEG_RE.search(rest):
                c, _h, _ = counts_in(rest)
                hits.append((name, c, f"{name} | {snippet_of(rest, 150)}"))
        if len(hits) >= 3:
            out.extend(hits)
    hits = []
    for i, ln in enumerate(lines):
        name = _park_name_cell(ln)
        if not name:
            continue
        for nxt in lines[i + 1:i + 3]:
            if _park_name_cell(nxt):
                break
            first = nxt.split(" | ")[0].strip()
            if " | " in nxt and len(first.split()) >= 2 and not re.match(rf"^\W*{BB}", first, re.I) and \
                    re.fullmatch(r"[A-Z0-9][\w.'&-]*(\s+[\w.'&-]+){1,7}", first):
                break               # "Healey Basketball Courts | 5 Meacham Street": its own facility row
            m = classify_line([nxt], 0)
            if m is not None and m.kind in COURT_KINDS and not m.negative and not m.planned:
                hits.append((name, m.courts, f"{name}: {snippet_of(nxt, 150)}"))
                break
    if len({h[0] for h in hits}) >= 3:
        out.extend(hits)
    return list({h[0]: h for h in out}.values())


def basketball_list_page(title: str, lines: list[str]) -> list[tuple[str, int | None, str]]:
    """A page about basketball courts ("Basketball Court Locations") whose short lines are park names: one
    record per line that names a park. Lines are kept only if ``build`` can match them to a park polygon."""
    if not LIST_PAGE_TITLE_RE.search(title or ""):
        return []
    out: list[tuple[str, int | None, str]] = []
    for ln in lines:
        name = re.split(r"\s+[-–|:(]\s*|\s*,\s*\d", ln)[0].strip()
        if not (4 <= len(name) <= 60) or BB_ANY.search(name) or not PARK_LINE_RE.search(name) or \
                re.search(r"\b(hours|rules|reserv|permit|contact|programs?|leagues?|closed|map)\b", ln, re.I):
            continue
        courts, half, _ = counts_in(ln)
        # "(4)", "(2 Courts)", "- 2 full courts"; not "- 33 Sheep Hill Drive"
        m = re.search(r"\((\d{1,2})(?:\s+(?:full\s+)?courts?)?\)|[-–:]\s*(\d{1,2})\s*(full|courts?|half)\b", ln, re.I)
        if courts is None and m and not re.search(r"\bhalf\b", ln, re.I):    # half courts: no full-court count
            courts = _num(m.group(1) or m.group(2))
        out.append((name, courts, f"{title[:80]}: {ln[:110]}"))
    return out if len(out) >= 3 else []


RULES_SECTIONS = ("HTML -> lines, tables, links", "extraction rules", "names", "extract")


def _rules_fingerprint() -> str:
    """Hash of the code that turns a cached page into rows: the module header (constants) and the parsing, rule,
    name and extract sections. Editing crawl, build, match or export code keeps finished extract parts valid."""
    text = Path(__file__).read_text(encoding="utf-8")
    parts = re.split(r"^# =+ (.+)$", text, flags=re.M)
    keep = [parts[0]] + [parts[i + 1] for i in range(1, len(parts) - 1, 2) if parts[i].strip() in RULES_SECTIONS]
    return hashlib.sha1("".join(keep).encode()).hexdigest()[:12]


def cmd_extract(args: argparse.Namespace) -> None:
    from concurrent.futures import ProcessPoolExecutor

    logd = read_fetch_log()
    by_host: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for u, rec in logd.items():
        if rec.get("outcome") == "ok" and rec.get("status") != 202 \
                and not str(rec.get("content_type", "")).lower().count("xml") \
                and not is_blocked_url(u) and not is_blocked_url(rec.get("final_url") or u):
            by_host[host_of(u)].append((u, rec))
    log.info("extract: %d pages on %d hosts", sum(len(v) for v in by_host.values()), len(by_host))
    t0 = time.monotonic()
    # Resumable: whole hosts in chunks of about --chunk-pages pages; each chunk is written to its own part file,
    # keyed by the rules code and the chunk's pages, so a crash or a re-run only redoes missing chunks.
    rules_fp = _rules_fingerprint()
    chunks: list[list[str]] = []
    cur: list[str] = []
    n = 0
    for h in sorted(by_host):
        cur.append(h)
        n += len(by_host[h])
        if n >= args.chunk_pages:
            chunks.append(cur)
            cur, n = [], 0
    if cur:
        chunks.append(cur)
    EXTRACT_PARTS_DIR.mkdir(parents=True, exist_ok=True)

    def part_path(chunk: list[str]) -> Path:
        key = "\n".join(f"{u}\t{rec.get('cache')}" for h in chunk for u, rec in sorted(by_host[h], key=lambda x: x[0]))
        return EXTRACT_PARTS_DIR / f"part-{hashlib.sha1((rules_fp + key).encode()).hexdigest()[:16]}.parquet"

    paths = [part_path(c) for c in chunks]
    todo = [(c, p) for c, p in zip(chunks, paths) if args.force or not p.exists()]
    log.info("extract: %d chunks, %d to do (%d workers)", len(chunks), len(todo), args.workers)
    for i, (chunk, path) in enumerate(todo, 1):
        # a fresh pool per chunk, so worker memory (and Windows handles) is released between chunks
        # (max_tasks_per_child can deadlock on Python 3.12)
        part: list[dict[str, Any]] = []
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for res in pool.map(_extract_host, [by_host[h] for h in chunk], chunksize=2):
                part.extend(res)
        cols = sorted({k for r in part for k in r})
        tbl = pa.table({c: pa.array([r.get(c) for r in part]) for c in cols}) if part else pa.table({})
        tmp = path.with_suffix(".tmp")
        pq.write_table(tbl, tmp, compression="zstd")
        tmp.replace(path)
        log.info("extract: chunk %d / %d (%d hosts, %d rows) in %.0f s", i, len(todo), len(chunk), len(part),
                 time.monotonic() - t0)
    tables = [t for t in (pq.read_table(p) for p in paths) if t.num_columns]
    table = pa.concat_tables(tables, promote_options="default") if tables else pa.table({})
    table = table.select(sorted(table.column_names))
    pq.write_table(table, PAGES_FILE, compression="zstd")
    keep = {p.name for p in paths}
    for old in EXTRACT_PARTS_DIR.glob("part-*.parquet"):     # parts of earlier rules or page sets
        if old.name not in keep:
            old.unlink()
    rows = table.select([c for c in ("from_table", "has_court", "program_only", "open_gym")
                         if c in table.column_names]).to_pylist()
    pages = [r for r in rows if not r["from_table"]]
    log.info("extract: %d pages (%d with a court mention, %d program-only, %d open-gym), %d amenity-table rows "
             "-> %s in %.0f s", len(pages), sum(r["has_court"] for r in pages), sum(r["program_only"] for r in pages),
             sum(r["open_gym"] for r in pages), len(rows) - len(pages), PAGES_FILE, time.monotonic() - t0)


# =========================================================================== places (city names)
PLACES_URL = "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_place_500k.zip"
PLACES_FILE = config.CENSUS_DIR / "cb_2023_us_place_500k.zip"


class Places:
    """Census incorporated places and CDPs (cartographic boundary file 2023, public domain): point -> 'City, ST'."""

    def __init__(self) -> None:
        import zipfile

        import shapefile  # pyshp
        from shapely.geometry import shape

        if not PLACES_FILE.exists():
            PLACES_FILE.parent.mkdir(parents=True, exist_ok=True)
            r = requests.get(PLACES_URL, timeout=300, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            PLACES_FILE.write_bytes(r.content)
        names, geoms = [], []
        with zipfile.ZipFile(PLACES_FILE) as zf:
            stem = next(n[:-4] for n in zf.namelist() if n.endswith(".shp"))
            with zf.open(stem + ".shp") as shp, zf.open(stem + ".dbf") as dbf, zf.open(stem + ".shx") as shx:
                rd = shapefile.Reader(shp=shp, dbf=dbf, shx=shx)
                fields = [f[0] for f in rd.fields[1:]]
                for sr in rd.iterShapeRecords():
                    rec = dict(zip(fields, sr.record))
                    names.append(f"{rec.get('NAME')}, {rec.get('STUSPS')}")
                    geoms.append(shape(sr.shape.__geo_interface__))
        self.names = np.asarray(names, dtype=object)
        self.geoms = np.asarray(geoms, dtype=object)
        self.tree = STRtree(self.geoms)

    def assign(self, lons: Any, lats: Any) -> np.ndarray:
        pts = shapely.points(np.asarray(lons, float), np.asarray(lats, float))
        out = np.full(len(pts), None, dtype=object)
        if not len(pts):
            return out
        pi, gi = self.tree.query(pts, predicate="within")
        out[pi] = self.names[gi]
        return out


# =========================================================================== park polygons
class ParkIndex:
    """Park-like polygons (OSM parks / playgrounds / rec grounds / sports and community centres, PAD-US public
    land, official park polygons from the truth set) with name and spatial lookups."""

    MAX_AREA_M2 = 20e6

    def __init__(self) -> None:
        names: list[Any] = []
        srcs: list[str] = []
        refs: list[str] = []
        geoms: list[Any] = []
        if SITES_FILE.exists():
            a, g = read_geoparquet(SITES_FILE, columns=["site", "source", "name", "ref", "geom_kind"],
                                   filters=[("site", "in", ["park", "rec", "community"]), ("geom_kind", "=", "polygon")])
            names.extend(a["name"].tolist())
            srcs.extend(a["source"].tolist())
            refs.extend(a["ref"].tolist())
            geoms.extend(g.tolist())
        if TRUTH_COVERAGE.exists():
            c, cg = read_geoparquet(TRUTH_COVERAGE)
            k = c["kind"] == "park"
            names.extend(c["name"][k].tolist())
            srcs.extend(f"official:{s}" for s in c["source"][k])
            refs.extend(c["coverage_id"][k].tolist())
            geoms.extend(cg[k].tolist())
        self.names = np.asarray(names, dtype=object)
        self.sources = np.asarray(srcs, dtype=object)
        self.refs = np.asarray(refs, dtype=object)
        self.geoms = np.asarray(geoms, dtype=object)
        self.geoms_m = to_m(self.geoms)
        self.area = shapely.area(self.geoms_m)
        ok = (self.area > 0) & (self.area <= self.MAX_AREA_M2)
        self.names, self.sources, self.refs = self.names[ok], self.sources[ok], self.refs[ok]
        self.geoms, self.geoms_m, self.area = self.geoms[ok], self.geoms_m[ok], self.area[ok]
        self.tree = STRtree(self.geoms_m)
        cent = shapely.centroid(self.geoms)
        self.cx, self.cy = shapely.get_x(cent), shapely.get_y(cent)
        self.by_ref = {r: i for i, (r, s) in enumerate(zip(self.refs, self.sources)) if s == "osm"}
        self.by_core: dict[str, list[int]] = defaultdict(list)
        for i, nm in enumerate(self.names):
            if nm:
                self.by_core[name_core(nm)].append(i)
        self.rank = np.array([0 if s == "osm" else 1 if str(s).startswith("official") else 2 for s in self.sources])
        log.info("park index: %d polygons %s", len(self.geoms), dict(Counter(
            "official" if str(s).startswith("official") else s for s in self.sources)))

    def containing(self, lon: float, lat: float, name: str | None = None, near_m: float = 30.0) -> int | None:
        p = pts_m([lon], [lat])[0]
        idx = self.tree.query(p, predicate="dwithin", distance=near_m)
        if not len(idx):
            return None
        inside = [i for i in idx if self.geoms_m[i].contains(p)]
        pool = inside or [i for i in idx if name and name_agree(name, self.names[i])]
        if not pool:
            return None
        return min(pool, key=lambda i: (self.rank[i], self.area[i]))

    def by_name(self, name: str | None, area: tuple[float, float, float, float]) -> tuple[int | None, str]:
        core = name_core(name)
        if not core or len(core) < 3:
            return None, "no_name"
        w, s, e, n = area
        cands = [i for i in self.by_core.get(core, []) if w <= self.cx[i] <= e and s <= self.cy[i] <= n]
        if len(core.split()) == 1:
            # a one-word core ("Youth Playground" -> "youth") must not jump to another kind of place ("Youth
            # Center"): the names may differ only by "park" / "playground" / "the"
            # ("Krogh" = "Krogh Park"), but "Chimborazo Playground" is not "Chimborazo Park"
            q = set(name_tokens(name))
            cands = [i for i in cands if (set(name_tokens(self.names[i])) ^ q) <= {"park", "the"}
                     or (set(name_tokens(self.names[i])) ^ q) <= {"playground", "the"}]
        if not cands:
            return None, "no_match"
        if len(cands) > 1:
            pts = shapely.points(self.cx[cands], self.cy[cands])
            spread = float(shapely.distance(to_m(pts[:1])[0], to_m(pts)).max()) if len(cands) else 0.0
            if spread > 1500:
                exact = [i for i in cands if name_tokens(self.names[i]) == name_tokens(name)]
                if len(exact) == 1:
                    return exact[0], "name_exact"
                return None, "ambiguous"
        return min(cands, key=lambda i: (self.rank[i], -self.area[i])), "name"


# =========================================================================== build
def _host_areas(seeds: dict[str, np.ndarray], expanded: dict[str, list[Any]], pad: float
                ) -> dict[str, tuple[float, float, float, float]]:
    pts: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for u, lon, lat in zip(seeds["url"], seeds["lon"], seeds["lat"]):
        pts[host_of(u)].append((float(lon), float(lat)))
    areas: dict[str, tuple[float, float, float, float]] = {}
    for h, p in pts.items():
        a = np.asarray(p)
        # one stray seed (a Wisconsin park linking a Connecticut town's page) must not stretch the box across
        # states; a statewide site keeps its spread (fences at 3 x the interquartile range, plus 0.5 degrees)
        q1, q3 = np.percentile(a, 25, axis=0), np.percentile(a, 75, axis=0)
        iqr = q3 - q1
        near = ((a >= q1 - 3 * iqr - 0.5) & (a <= q3 + 3 * iqr + 0.5)).all(axis=1)
        a = a[near] if near.any() else a
        lo_x, lo_y = np.percentile(a[:, 0], 5), np.percentile(a[:, 1], 5)
        hi_x, hi_y = np.percentile(a[:, 0], 95), np.percentile(a[:, 1], 95)
        areas[h] = (float(lo_x) - pad, float(lo_y) - pad, float(hi_x) + pad, float(hi_y) + pad)
    for i, u in enumerate(expanded.get("url", [])):
        h = host_of(u)
        if expanded["via"][i] == "registry" or h not in areas:
            areas[h] = (expanded["area_w"][i], expanded["area_s"][i], expanded["area_e"][i], expanded["area_n"][i])
    return areas


DEPARTMENT_RE = re.compile(r"\bparks? (and|&) rec(reation)?\b|\brecreation (department|dept|commission|district)\b|"
                           r"\bparks? (department|dept|district|commission|board|division|authority)\b", re.I)
KIND_WORDS = {"pond", "lake", "pool", "trail", "trails", "garden", "gardens", "school", "library", "cemetery", "beach",
              "marina", "golf", "skate", "skatepark", "dog", "boat", "launch", "landing", "nature", "wildlife", "forest",
              "river", "creek", "reservoir", "dam", "museum", "zoo"}
STATE_AREA_RE = re.compile(r"\bstate (park|recreation area|forest|beach|natural area)\b", re.I)


def kind_agrees(seed: str | None, page: str | None) -> bool:
    """A seed for a pond, a trail or a skate park is not the community centre of the same name ("Meadowbrook
    Pond" / "Meadowbrook Community Center"), and a city park is not the state recreation area of the same name
    ("Farewell Bend Park" in Bend / "Farewell Bend State Recreation Area")."""
    s, p = set(name_core(seed).split()), set(name_core(page).split())
    if (s & KIND_WORDS) - p:
        return False
    return not (STATE_AREA_RE.search(page or "") and not re.search(r"\bstate\b", seed or "", re.I))


def _in_area(lon: float, lat: float, area: tuple[float, float, float, float] | None) -> bool:
    return area is None or (area[0] <= lon <= area[2] and area[1] <= lat <= area[3])


def cmd_build(args: argparse.Namespace) -> None:
    from .states import load_states

    queue = set(crawl_queue())                 # pages fetched for other purposes (indoor sample) are not used
    pg = [r for r in pq.read_table(PAGES_FILE).to_pylist() if r["url"] in queue]
    seeds, _ = read_geoparquet(SEEDS_FILE, with_geometry=False)
    expanded = pq.read_table(EXPANDED_FILE).to_pydict() if EXPANDED_FILE.exists() else {}
    areas = _host_areas(seeds, expanded, args.area_pad)
    seeds_by_url: dict[str, list[int]] = defaultdict(list)
    for i, (u, g) in enumerate(zip(seeds["url"], seeds["generic"])):
        if not g:
            seeds_by_url[u].append(i)
    reg_city = {u: (c, s) for u, c, s in zip(expanded.get("url", []), expanded.get("city", []), expanded.get("state", []))
                if c}
    idx = ParkIndex()
    located: list[dict[str, Any]] = []
    why: Counter[str] = Counter()
    for r in pg:
        url = r["url"]
        cands: list[dict[str, Any]] = []
        if r["from_table"]:
            pi_t, basis = idx.by_name(r["table_name"], areas.get(r["host"], CONUS_BBOX))
            if pi_t is not None:
                cands.append({"poly": pi_t, "basis": f"table_{basis}", "name": r["table_name"], "seed": None})
            else:
                why[f"table_{basis}"] += 1
        elif seeds_by_url.get(url):
            title_core = " ".join(name_tokens(f"{r['title']} {r['h1']}"))
            is_root = re.fullmatch(r"/?(index\.\w+|home|default\.aspx)?/?", urlsplit(r["final_url"] or url).path,
                                   re.I) is not None
            for si in seeds_by_url[url]:
                nm = seeds["name"][si]
                if DEPARTMENT_RE.search(nm or ""):
                    why["seed_is_a_department"] += 1    # "East Ridge Parks and Rec": its point is an office or
                    continue                            # some park, and its page is the department's home page
                sim = name_sim(nm, r["page_name"])
                core = name_core(nm)
                # the page must name the seed's park (a bare home page of a league, a resort or an expired
                # domain names something else)
                # a site's home page must name the park itself (its h1 is often a carousel headline)
                agrees = (name_agree(nm, r["page_name"]) or (len(core.split()) >= 2 and f" {core} " in f" {title_core} "
                                                             and not is_root)) and kind_agrees(nm, r["page_name"])
                if not agrees:
                    why["seed_name_disagrees"] += 1
                    continue
                lon, lat = float(seeds["lon"][si]), float(seeds["lat"][si])
                if not _in_area(lon, lat, areas.get(r["host"])):
                    why["seed_far_from_host_area"] += 1     # an Iowa park pointing at a Texas city's page
                    continue
                pi = idx.by_ref.get(str(seeds["source_id"][si])) if seeds["source"][si] == "osm" else None
                if pi is None:
                    pi = idx.containing(lon, lat, nm)
                cands.append({"poly": pi, "basis": f"seed_{seeds['source'][si]}", "name": nm, "seed": si,
                              "lon": lon, "lat": lat, "sim": sim})
        else:
            pi_p, basis = idx.by_name(r["page_name"], areas.get(r["host"], CONUS_BBOX))
            if pi_p is not None:
                cands.append({"poly": pi_p, "basis": f"page_{basis}", "name": r["page_name"], "seed": None})
            else:
                why[f"page_{basis}"] += 1
        # one park per distinct polygon / point
        seen_keys: set[str] = set()
        for c in cands:
            if c["poly"] is not None:
                pi = c["poly"]
                key = f"{idx.sources[pi]}:{idx.refs[pi]}"
                pt = shapely.point_on_surface(idx.geoms[pi])
                lon, lat = float(pt.x), float(pt.y)
            else:
                lon, lat = c["lon"], c["lat"]
                key = f"pt:{lon:.4f},{lat:.4f}:{name_core(c['name'])}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            si = c["seed"]
            located.append({**r, "park_key": key, "park_name": (idx.names[c["poly"]] if c["poly"] is not None and
                                                                idx.sources[c["poly"]] == "osm" and idx.names[c["poly"]]
                                                                else c["name"]) or r["page_name"],
                            "lon": lon, "lat": lat, "location_basis": c["basis"],
                            "polygon_index": c["poly"], "name_sim": c.get("sim"),
                            "seed_source": seeds["source"][si] if si is not None else None,
                            "seed_id": seeds["source_id"][si] if si is not None else None,
                            "seed_kind": seeds["kind"][si] if si is not None else None,
                            "seed_city": (seeds["city"][si] if si is not None else None) or reg_city.get(url, (None,))[0]})
    log.info("build: %d of %d page rows located (%d parks); not located: %s", len(located), len(pg),
             len({x["park_key"] for x in located}), dict(why.most_common()))
    states = load_states()
    places = Places()
    lons = np.array([x["lon"] for x in located])
    lats = np.array([x["lat"] for x in located])
    st = states.assign(lons, lats) if len(located) else np.array([])
    city = places.assign(lons, lats) if len(located) else np.array([])
    for x, s_, c_ in zip(located, st, city):
        x["state"] = s_ or None
        x["city"] = c_
    # located pages (every basketball signal, for coverage and indoor.py)
    lp_cols = ["park_key", "park_name", "url", "final_url", "host", "fetched_at", "title", "page_name", "from_table",
               "table_name", "has_court", "program_only", "open_gym", "negative_only", "planned_only", "indoor_outdoor",
               "courts", "half_courts", "hoops", "indoor_courts", "mention_kind", "snippet", "location_basis",
               "name_sim", "seed_source", "seed_id", "seed_city", "state", "city", "lon", "lat"]
    write_geoparquet(WEB_DIR / "located_pages.parquet", {c: pa.array([x.get(c) for x in located]) for c in lp_cols},
                     shapely.points(lons, lats) if len(located) else np.array([], dtype=object))
    # parks with courts: group located court rows by park
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for x in located:
        if x["has_court"] and x["state"] and x["seed_kind"] in PARK_KINDS | {None} and not PRIVATE_VENUE_RE.search(
                f"{x['park_name']} {x['title']}"):
            groups[x["park_key"]].append(x)
    # a seed point outside every polygon (Overture) next to a same-named polygon park (OSM) is the same park
    poly_by_core: dict[str, list[tuple[str, float, float]]] = defaultdict(list)
    for key, xs in groups.items():
        if not key.startswith("pt:"):
            poly_by_core[name_core(xs[0]["park_name"])].append((key, xs[0]["lon"], xs[0]["lat"]))
    for key in [k for k in groups if k.startswith("pt:")]:
        x0 = groups[key][0]
        for pk, lon, lat in poly_by_core.get(name_core(x0["park_name"]), []):
            dx = (x0["lon"] - lon) * 111_320 * math.cos(math.radians(lat))
            if math.hypot(dx, (x0["lat"] - lat) * 110_540) < 1000:
                groups[pk].extend(groups.pop(key))
                break
    rows: dict[str, list[Any]] = defaultdict(list)
    geoms, polys = [], []
    io_rank = {"both": 0, "outdoor": 1, "unknown": 2, "indoor_likely": 3, "indoor": 4}
    for key, xs in groups.items():
        best = sorted(xs, key=lambda x: (x["courts"] is None and x["half_courts"] is None, x["from_table"],
                                         x["mention_kind"] != "court"))[0]
        ios = {x["indoor_outdoor"] for x in xs}
        io = "both" if ("both" in ios or ({"indoor", "indoor_likely"} & ios and "outdoor" in ios)) else min(
            ios, key=lambda v: io_rank.get(v, 9))
        pi = best["polygon_index"]
        rows["park_id"].append("web-" + hashlib.sha1(key.encode()).hexdigest()[:12])
        rows["park_key"].append(key)
        rows["park_name"].append(best["park_name"])
        rows["state"].append(best["state"])
        rows["city"].append(best["city"] or best["seed_city"])
        rows["lon"].append(best["lon"])
        rows["lat"].append(best["lat"])
        rows["polygon_source"].append(None if pi is None else (
            "official" if str(idx.sources[pi]).startswith("official") else str(idx.sources[pi])))
        rows["polygon_ref"].append(None if pi is None else str(idx.refs[pi]))
        rows["polygon_name"].append(None if pi is None else idx.names[pi])
        rows["polygon_area_m2"].append(None if pi is None else round(float(idx.area[pi]), 1))
        polys.append(None if pi is None else shapely.to_wkb(idx.geoms[pi]))
        rows["location_basis"].append(best["location_basis"])
        cs = [x["courts"] for x in xs if x["courts"] is not None]
        hs = [x["half_courts"] for x in xs if x["half_courts"] is not None]
        ics = [x["indoor_courts"] for x in xs if x["indoor_courts"] is not None]
        rows["court_count"].append(max(cs) if cs else None)
        rows["half_court_count"].append(max(hs) if hs else None)
        rows["indoor_court_count"].append(max(ics) if ics else None)
        rows["indoor_outdoor"].append(io)
        rows["mention_kind"].append(best["mention_kind"])
        rows["evidence"].append(best["snippet"])
        rows["source_url"].append(best["url"])
        rows["source_urls"].append(";".join(sorted({x["url"] for x in xs}))[:2000])
        rows["n_pages"].append(len({x["url"] for x in xs}))
        rows["fetch_date"].append(str(best["fetched_at"])[:10])
        rows["page_title"].append(best["title"])
        rows["name_sim"].append(best["name_sim"])
        rows["seed_source"].append(best["seed_source"])
        rows["seed_id"].append(best["seed_id"])
        rows["host"].append(best["host"])
        geoms.append(shapely.Point(best["lon"], best["lat"]))
    cols = {k: pa.array(v) for k, v in rows.items()}
    cols["polygon_wkb"] = pa.array(polys, type=pa.binary())
    OUT_WEB.mkdir(parents=True, exist_ok=True)
    write_geoparquet(PARKS_FILE, cols, np.asarray(geoms, dtype=object))
    log.info("parks_with_courts: %d parks (%d with a polygon, %d with a stated count), indoor/outdoor %s -> %s",
             len(geoms), sum(p is not None for p in polys), sum(c is not None for c in rows["court_count"]),
             dict(Counter(rows["indoor_outdoor"])), PARKS_FILE)


# =========================================================================== match
def _iv(v: Any) -> int | None:
    """int, or None for null / NaN (nullable parquet ints come back as float NaN)."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return int(v)


SMALL_POLY_M2 = 20_000.0      # a park polygon under 2 ha is often just the play area: search 60 m around it


def _zones(parks: dict[str, np.ndarray], point_r: float, poly_buf: float, max_poly_m2: float) -> np.ndarray:
    """Search zone per park: its polygon + ``poly_buf`` (+60 m for polygons under 2 ha), else a circle."""
    pts = pts_m(parks["lon"], parks["lat"])
    zones = shapely.buffer(pts, point_r)
    for i, b in enumerate(parks["polygon_wkb"]):
        area = _fv(parks["polygon_area_m2"][i])
        if b is not None and area is not None and area <= max_poly_m2:
            buf = max(poly_buf, 60.0) if area < SMALL_POLY_M2 else poly_buf
            zones[i] = shapely.buffer(to_m(np.asarray([shapely.from_wkb(b)], dtype=object))[0], buf)
    return zones


def _fv(v: Any) -> float | None:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return float(v)


def cmd_match(args: argparse.Namespace) -> None:
    parks, pgeo = read_geoparquet(PARKS_FILE)
    n = len(pgeo)
    zones = _zones(parks, args.point_radius, args.poly_buffer, args.max_poly_km2 * 1e6)
    # OSM basketball features
    oc, og = read_geoparquet(OSM_COURTS_FILE, columns=["osm_ref", "outdoor", "center_lon", "center_lat"])
    ogm = to_m(og)
    otree = STRtree(ogm)
    zi, oi = otree.query(zones, predicate="intersects")
    n_osm = np.bincount(zi[oc["outdoor"][oi].astype(bool)], minlength=n)
    n_osm_any = np.bincount(zi, minlength=n)
    # satellite candidates (never near an OSM court)
    cc = pq.read_table(CANDIDATES_FILE, columns=["id", "state", "lon", "lat", "conf", "conf_v2", "verify_scan",
                                                 "privacy", "site", "export"]).to_pydict()
    cm = pts_m(cc["lon"], cc["lat"])
    ctree = STRtree(cm)
    zj, cj = ctree.query(zones, predicate="contains")
    # detection score: the better of v2 (the detector that produced every candidate) and the v3 re-score;
    # v3 scores 0 on many v2 detections it would not have made itself
    conf = np.fmax(np.asarray(cc["conf"], float), np.asarray(cc["conf_v2"], float))
    export = np.asarray([bool(x) for x in cc["export"]])
    per_park: dict[int, list[int]] = defaultdict(list)
    for a, b in zip(zj, cj):
        per_park[int(a)].append(int(b))
    status, ncand, ncand3, ncand5, nexp, best_conf = [], [], [], [], [], []
    io = parks["indoor_outdoor"]
    for i in range(n):
        cs = per_park.get(i, [])
        c = conf[cs] if cs else np.array([])
        ncand.append(len(cs))
        ncand3.append(int((c >= 0.3).sum()))
        ncand5.append(int((c >= 0.5).sum()))
        nexp.append(int(export[cs].sum()) if cs else 0)
        best_conf.append(float(c.max()) if len(c) else None)
        if io[i] in ("indoor", "indoor_likely"):
            status.append("indoor_only")
        elif n_osm[i] > 0:
            status.append("confirmed_osm")
        elif ncand3[-1] > 0:
            status.append("confirmed_detected")
        elif ncand[-1] > 0:
            status.append("weak_detection")
        else:
            status.append("missing")
    listed = np.array([(_iv(a) or 0) + (_iv(b) or 0) for a, b in zip(parks["court_count"], parks["half_court_count"])])
    found = n_osm + np.asarray(ncand3)
    extra = {"status": status, "n_osm_courts": n_osm.astype(int).tolist(), "n_osm_basketball_any": n_osm_any.astype(int).tolist(),
             "n_candidates": ncand, "n_candidates_conf03": ncand3, "n_candidates_conf05": ncand5,
             "n_candidates_exported": nexp, "best_candidate_conf": best_conf,
             "listed_courts": [int(v) if v else None for v in listed],
             "short_of_listed": [bool(lv and lv > f) for lv, f in zip(listed, found)]}
    cols = {k: pa.array(v.tolist() if isinstance(v, np.ndarray) else v) for k, v in parks.items()}
    cols.update({k: pa.array(v) for k, v in extra.items()})
    write_geoparquet(PARKS_FILE, cols, pgeo)
    # candidate evidence: candidates inside parks whose page lists an outdoor (or unspecified) court
    ev: dict[str, list[Any]] = defaultdict(list)
    for i in range(n):
        if status[i] == "indoor_only":
            continue
        for j in per_park.get(i, []):
            ev["candidate_id"].append(cc["id"][j])
            ev["state"].append(cc["state"][j])
            ev["lon"].append(cc["lon"][j])
            ev["lat"].append(cc["lat"][j])
            ev["conf"].append(cc["conf"][j])
            ev["det_score"].append(float(conf[j]))
            ev["conf_v2"].append(cc["conf_v2"][j])
            ev["verify_scan"].append(cc["verify_scan"][j])
            ev["privacy"].append(cc["privacy"][j])
            ev["site"].append(cc["site"][j])
            ev["exported"].append(bool(cc["export"][j]))
            ev["park_id"].append(parks["park_id"][i])
            ev["park_name"].append(parks["park_name"][i])
            ev["park_status"].append(status[i])
            ev["in_polygon"].append(parks["polygon_wkb"][i] is not None)
            ev["dist_to_park_point_m"].append(round(float(shapely.distance(cm[j], pts_m([parks["lon"][i]],
                                                                                    [parks["lat"][i]])[0])), 1))
            ev["listed_courts"].append(extra["listed_courts"][i])
            ev["indoor_outdoor"].append(io[i])
            ev["n_candidates_in_park"].append(ncand[i])
            ev["n_osm_courts_in_park"].append(int(n_osm[i]))
            ev["source_url"].append(parks["source_url"][i])
            ev["evidence"].append(parks["evidence"][i])
            ev["fetch_date"].append(parks["fetch_date"][i])
            ev["evidence_kind"].append("park_web_page_lists_basketball")
    pq.write_table(pa.table({k: pa.array(v) for k, v in ev.items()}), EVIDENCE_FILE, compression="zstd")
    # missing: listed outdoor / unspecified court, nothing mapped or detected
    miss = [i for i in range(n) if status[i] == "missing"]
    mz = np.asarray([zones[i] for i in miss], dtype=object)
    to_ll = Transformer.from_crs(ALBERS, "EPSG:4326", always_xy=True)
    mz_ll = shapely.transform(mz, lambda xy: np.column_stack(to_ll.transform(xy[:, 0], xy[:, 1]))) if len(mz) else mz
    mcols = {k: pa.array([parks[k][i] for i in miss]) for k in (
        "park_id", "park_name", "state", "city", "lon", "lat", "court_count", "half_court_count", "indoor_outdoor",
        "mention_kind", "evidence", "source_url", "fetch_date", "polygon_source", "polygon_ref", "polygon_area_m2",
        "location_basis")}
    def zone_label(i: int) -> str:
        area = _fv(parks["polygon_area_m2"][i])
        if parks["polygon_wkb"][i] is None or area is None or area > args.max_poly_km2 * 1e6:
            return f"point+{args.point_radius:g}m"
        return f"polygon+{max(args.poly_buffer, 60.0) if area < SMALL_POLY_M2 else args.poly_buffer:g}m"

    mcols["zone"] = pa.array([zone_label(i) for i in miss])
    mcols["outdoor_stated"] = pa.array([io[i] in ("outdoor", "both") for i in miss])
    write_geoparquet(MISSING_FILE, mcols, mz_ll)
    summarise(parks, extra, args)


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(c - h, 3), round(c + h, 3))


def summarise(parks: dict[str, np.ndarray], extra: dict[str, list[Any]], args: argparse.Namespace) -> None:
    status = np.asarray(extra["status"])
    lp, _ = read_geoparquet(WEB_DIR / "located_pages.parquet", columns=["park_key", "state", "city", "has_court",
                                                                         "from_table"], with_geometry=False)
    def tab(key: str) -> list[dict[str, Any]]:
        vals = parks[key]
        keys = sorted({v for v in vals if v}, key=lambda v: -int((vals == v).sum()))
        out = []
        loc_parks: dict[Any, set[str]] = defaultdict(set)
        for pk, v in zip(lp["park_key"], lp[key]):
            loc_parks[v].add(pk)
        for v in keys:
            m = vals == v
            out.append({key: v, "park_pages_located": len(loc_parks.get(v, ())), "parks_with_courts": int(m.sum()),
                        "with_polygon": int(sum(parks["polygon_wkb"][i] is not None for i in np.flatnonzero(m))),
                        "with_count": int(sum(_iv(parks["court_count"][i]) is not None or _iv(parks["half_court_count"][i])
                                              is not None for i in np.flatnonzero(m))),
                        **{s: int((m & (status == s)).sum()) for s in ("confirmed_osm", "confirmed_detected",
                                                                        "weak_detection", "missing", "indoor_only")}})
        return out

    by_state, by_city = tab("state"), tab("city")
    for name, rows in (("by_state.csv", by_state), ("by_city.csv", by_city)):
        if rows:
            with open(OUT_WEB / name, "w", encoding="utf-8", newline="") as fh:
                wr = csv.DictWriter(fh, fieldnames=list(rows[0]))
                wr.writeheader()
                wr.writerows(rows)
    total = {"parks_with_courts": int(len(status)), **{s: int((status == s).sum()) for s in (
        "confirmed_osm", "confirmed_detected", "weak_detection", "missing", "indoor_only")},
        "park_pages_located": len(set(lp["park_key"].tolist())),
        "with_polygon": int(sum(b is not None for b in parks["polygon_wkb"])),
        "candidate_evidence_rows": pq.read_metadata(EVIDENCE_FILE).num_rows,
        "short_of_listed": int(sum(extra["short_of_listed"]))}
    write_json(SUMMARY_FILE, {"generated": now_iso(), "zones": {"point_radius_m": args.point_radius,
                                                                "polygon_buffer_m": args.poly_buffer,
                                                                "max_polygon_km2": args.max_poly_km2},
                              "total": total, "by_state": by_state, "by_city": by_city[:200]})
    log.info("match: %s", total)


# =========================================================================== app layer
WEB_LAYER_FILE = OUT_WEB / "web_courts.geojson"
WEB_PUBLIC_FILE = OUT_WEB / "web_courts_public.geojson"    # placed on a detection, or in a small park
APP_KEYS = ["id", "name", "source", "status", "confidence", "hoops", "surface", "lit", "address", "region", "osm_id",
            "updated"]
WEB_EXTRA_KEYS = ["site", "indoor", "state", "listed_courts", "half_courts", "indoor_outdoor", "park_status", "position",
                  "position_uncertainty_m", "candidate_id", "evidence", "source_url", "mention_kind", "polygon_source",
                  "seed_source"]


def web_layer_rows(parks: dict[str, np.ndarray], ev: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Parks whose page lists an outdoor or unspecified court that the app does not show yet.

    * ``confirmed_osm`` parks are skipped (the OSM layer has the court), and so is any park with an exported
      satellite candidate inside (the national layer shows it; the page is extra evidence in candidate_evidence).
    * ``confirmed_detected`` with no exported candidate: the court is placed on the best public candidate
      (``position=detected_candidate``).
    * ``missing`` / ``weak_detection``: the court is somewhere in the park; the point is the polygon's
      representative point, or the park point (``position=park``, with the radius it may be off by).
    """
    best: dict[str, tuple[float, str, float, float]] = {}
    for pid, cid, sc, priv, lon, lat in zip(ev.get("park_id", []), ev.get("candidate_id", []), ev.get("det_score", []),
                                            ev.get("privacy", []), ev.get("lon", []), ev.get("lat", [])):
        if priv == "public" and sc >= 0.3 and (pid not in best or sc > best[pid][0]):
            best[pid] = (float(sc), str(cid), float(lon), float(lat))
    # the same park placed twice (two same-named OSM polygons, or a seed point next to the polygon): one row,
    # and none if the twin already shows the court (OSM or an exported candidate)
    by_core: dict[str, list[int]] = defaultdict(list)
    for i, nm in enumerate(parks["park_name"]):
        by_core[name_core(nm)].append(i)

    def twins(i: int) -> list[int]:
        out_t = []
        for j in by_core[name_core(parks["park_name"][i])]:
            if j != i:
                dx = (parks["lon"][i] - parks["lon"][j]) * 111_320 * math.cos(math.radians(parks["lat"][i]))
                if math.hypot(dx, (parks["lat"][i] - parks["lat"][j]) * 110_540) < 300:
                    out_t.append(j)
        return out_t

    def shown(j: int) -> bool:
        return parks["status"][j] == "confirmed_osm" or (_iv(parks["n_candidates_exported"][j]) or 0) > 0

    done: set[int] = set()
    out: list[dict[str, Any]] = []
    for i, pid in enumerate(parks["park_id"]):
        st = parks["status"][i]
        if st in ("confirmed_osm", "indoor_only") or (_iv(parks["n_candidates_exported"][i]) or 0) > 0:
            continue
        if parks["indoor_outdoor"][i] in ("indoor", "indoor_likely"):
            continue
        tw = twins(i)
        if any(shown(j) or j in done for j in tw):
            continue
        done.add(i)
        cand_id = None
        if st == "confirmed_detected":
            if pid not in best:
                continue
            _, cand_id, lon, lat = best[pid]
            position, unc = "detected_candidate", 20.0
        else:
            area = _fv(parks["polygon_area_m2"][i])
            if parks["polygon_wkb"][i] is not None and area:
                pt = shapely.point_on_surface(shapely.from_wkb(parks["polygon_wkb"][i]))
                lon, lat = float(pt.x), float(pt.y)
                unc = round(min(math.sqrt(area / math.pi) * 1.5, 2000.0), 0)   # about the farthest edge
            else:
                lon, lat = float(parks["lon"][i]), float(parks["lat"][i])
                unc = 150.0
            position = "park"
        out.append({
            "id": pid, "name": (parks["park_name"][i] or "")[:200] or None, "source": "web", "status": "candidate",
            "confidence": None, "hoops": None, "surface": None, "lit": None, "address": parks["city"][i] or None,
            "region": STATE_REGION.get(parks["state"][i] or "", "unknown"),
            "osm_id": parks["polygon_ref"][i] if parks["polygon_source"][i] == "osm" else None,
            "updated": parks["fetch_date"][i], "site": "park", "indoor": False, "state": parks["state"][i] or None,
            "listed_courts": _iv(parks["court_count"][i]), "half_courts": _iv(parks["half_court_count"][i]),
            "indoor_outdoor": parks["indoor_outdoor"][i], "park_status": st, "position": position,
            "position_uncertainty_m": unc, "candidate_id": cand_id, "evidence": parks["evidence"][i],
            "source_url": parks["source_url"][i], "mention_kind": parks["mention_kind"][i],
            "polygon_source": parks["polygon_source"][i], "seed_source": parks["seed_source"][i],
            "_lon": lon, "_lat": lat})
    return out


def _state_regions() -> dict[str, str]:
    from .states import load_states

    s = load_states()
    return {str(c): str(n).lower().replace(" ", "_") for c, n in zip(s.codes, s.names)}


STATE_REGION: dict[str, str] = {}


def web_feature(r: dict[str, Any]) -> dict[str, Any]:
    props = {k: r[k] for k in APP_KEYS + WEB_EXTRA_KEYS}
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(r["_lon"], 6), round(r["_lat"], 6)]},
            "properties": props}


def validate_web(fc: dict[str, Any]) -> list[str]:
    """App schema keys in order, then the web keys; the checks db_load applies to the shared keys."""
    errs: list[str] = []
    ids: set[str] = set()
    for i, f in enumerate(fc.get("features", [])):
        p = f.get("properties") or {}
        if list(p) != APP_KEYS + WEB_EXTRA_KEYS:
            errs.append(f"[{i}] keys {list(p)}")
            continue
        lon, lat = (f.get("geometry") or {}).get("coordinates") or [None, None]
        if not (isinstance(lon, float) and isinstance(lat, float) and -180 <= lon <= -60 and 15 <= lat <= 72):
            errs.append(f"[{i}] coordinates {lon},{lat}")
        if not p["id"] or p["id"] in ids or len(p["id"]) > 128:
            errs.append(f"[{i}] id {p['id']!r}")
        ids.add(p["id"])
        if p["source"] != "web" or p["status"] != "candidate" or p["indoor"] is not False or p["site"] != "park":
            errs.append(f"[{i}] source/status/indoor/site")
        if not isinstance(p["region"], str) or not p["region"] or len(p["region"]) > 64:
            errs.append(f"[{i}] region")
        if p["state"] is not None and not re.fullmatch(r"[A-Z]{2}", p["state"]):
            errs.append(f"[{i}] state")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(p["updated"])):
            errs.append(f"[{i}] updated")
        if (p["name"] is not None and len(p["name"]) > 200) or (p["osm_id"] is not None and len(p["osm_id"]) > 64) \
                or (p["address"] is not None and len(p["address"]) > 300):
            errs.append(f"[{i}] text length")
    return errs


def cmd_export(args: argparse.Namespace) -> None:
    """out/web/web_courts.geojson: the web layer in the app schema (source 'web', status 'candidate')."""
    parks, _ = read_geoparquet(PARKS_FILE, with_geometry=False)
    if "status" not in parks:
        raise SystemExit("run `websources match` first")
    ev = pq.read_table(EVIDENCE_FILE).to_pydict() if EVIDENCE_FILE.exists() else {}
    STATE_REGION.update(_state_regions())
    rows = web_layer_rows(parks, ev)
    if args.kinds:
        rows = [r for r in rows if r["mention_kind"] in set(args.kinds)]
    feats = [web_feature(r) for r in rows]
    fc: dict[str, Any] = {"type": "FeatureCollection", "features": feats}
    errs = validate_web(fc)
    if errs:
        raise SystemExit(f"web layer invalid: {errs[:10]}")
    write_json(WEB_LAYER_FILE, fc)
    log.info("export: %d web courts (%s; %s) -> %s", len(rows), dict(Counter(r["park_status"] for r in rows)),
             dict(Counter(r["position"] for r in rows)), WEB_LAYER_FILE)
    # the subset whose pin is close to the court: on a detected candidate, or the point of a park small enough
    # that the court is within --public-max-uncertainty metres of it
    pub = [f for f in feats if f["properties"]["position"] == "detected_candidate"
           or f["properties"]["position_uncertainty_m"] <= args.public_max_uncertainty]
    write_json(WEB_PUBLIC_FILE, {"type": "FeatureCollection", "features": pub})
    log.info("export: %d of them placed within %g m -> %s", len(pub), args.public_max_uncertainty, WEB_PUBLIC_FILE)


# =========================================================================== spot-check sample / score
def cmd_crawl_stats(args: argparse.Namespace) -> None:
    """What was crawled: outcomes per URL (latest fetch), per host, per seed source, robots.txt outcomes."""
    queue = crawl_queue()
    logd = read_fetch_log()
    recs = {u: logd[u] for u in queue if u in logd}
    hosts: dict[str, Counter[str]] = defaultdict(Counter)
    for u, r in recs.items():
        hosts[host_of(u)][str(r.get("outcome"))] += 1
    robots: dict[str, str] = {}
    if ROBOTS_LOG.exists():
        with open(ROBOTS_LOG, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rr = json.loads(line)
                except ValueError:
                    continue
                robots[rr["host"]] = str(rr.get("outcome"))
    by_seed: dict[str, Counter[str]] = defaultdict(Counter)
    if SEEDS_FILE.exists():
        s, _ = read_geoparquet(SEEDS_FILE, columns=["url", "source", "generic"], with_geometry=False)
        for u, src, g in zip(s["url"], s["source"], s["generic"]):
            if not g:
                by_seed[str(src)][str((logd.get(u) or {}).get("outcome", "not_fetched"))] += 1
    by_via: dict[str, Counter[str]] = defaultdict(Counter)
    if EXPANDED_FILE.exists():
        e = pq.read_table(EXPANDED_FILE, columns=["url", "via"]).to_pydict()
        qs = set(queue)
        for u, via in zip(e["url"], e["via"]):
            if u in qs:
                by_via[str(via)][str((logd.get(u) or {}).get("outcome", "not_fetched"))] += 1
    out = {
        "generated": now_iso(),
        "queued_urls": len(queue), "attempted": len(recs),
        "outcomes": dict(Counter(str(r.get("outcome")) for r in recs.values()).most_common()),
        "hosts_attempted": len(hosts),
        "hosts_with_an_ok_page": sum(1 for c in hosts.values() if c["ok"]),
        "hosts_robots_disallowed_or_unreachable": sum(1 for c in hosts.values() if c["robots_disallowed"] and not c["ok"]),
        "hosts_with_a_bot_challenge": sum(1 for c in hosts.values() if c["bot_challenge"]),
        "robots_outcomes": dict(Counter(robots.values()).most_common()),
        "ok_pages_mb": round(sum((r.get("bytes") or 0) for r in recs.values() if r.get("outcome") == "ok") / 1e6, 1),
        "by_seed_source": {k: dict(v.most_common()) for k, v in by_seed.items()},
        "expanded_by_via": {k: dict(v.most_common()) for k, v in by_via.items()},
    }
    write_json(OUT_WEB / "crawl_stats.json", out)
    print(json.dumps(out, indent=1))


def cmd_sample(args: argparse.Namespace) -> None:
    parks, _ = read_geoparquet(PARKS_FILE, with_geometry=False)
    logd = read_fetch_log()
    rng = random.Random(args.seed)
    strata: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i in range(len(parks["park_id"])):
        mk = str(parks["mention_kind"][i])
        mk = "text" if mk in ("court", "amenity_sentence") else "list" if mk in ("amenity_item", "icon", "table") else mk
        strata[(str(parks["status"][i]) if "status" in parks else "-", mk)].append(i)
    picks: list[tuple[tuple[str, str], int]] = []
    for k, idxs in sorted(strata.items()):
        rng.shuffle(idxs)
        picks.extend((k, i) for i in idxs[:args.per_stratum])
    out = []
    for (st, mk), i in picks:
        url = parks["source_url"][i]
        rec = logd.get(url, {})
        html = load_cached(rec) or ""
        pp = parse_html(html, url)
        ev = parks["evidence"][i] or ""
        j = next((k for k, ln in enumerate(pp.lines) if ev[:60].strip(".") in ln), None)
        ctx = pp.lines[max(0, j - 3):j + 4] if j is not None else []
        out.append({"park_id": parks["park_id"][i], "stratum": f"{st}/{mk}", "park_name": parks["park_name"][i],
                    "state": parks["state"][i], "city": parks["city"][i], "url": url, "page_title": parks["page_title"][i],
                    "location_basis": parks["location_basis"][i], "court_count": parks["court_count"][i],
                    "indoor_outdoor": parks["indoor_outdoor"][i], "evidence": ev, "context": ctx})
    d = OUT_WEB / "review"
    d.mkdir(parents=True, exist_ok=True)
    write_json(d / "sample.json", out)
    with open(d / "sample.txt", "w", encoding="utf-8") as fh:
        for n_o, o in enumerate(out):
            fh.write(f"[{n_o}] {o['park_id']} {o['stratum']} | {o['park_name']} ({o['city']}) | {o['location_basis']}\n"
                     f"    title: {o['page_title']}\n    url: {o['url']}\n    count={o['court_count']} "
                     f"io={o['indoor_outdoor']}\n    evidence: {o['evidence']}\n")
            for c in o["context"]:
                fh.write(f"      > {c[:220]}\n")
    log.info("sample: %d rows from %d strata -> %s", len(out), len(strata), d / "sample.txt")


def cmd_score(args: argparse.Namespace) -> None:
    d = OUT_WEB / "review"
    sample = {o["park_id"]: o for o in read_json(d / "sample.json")}
    labels = read_json(Path(args.labels))
    res: dict[str, Any] = {}
    by: dict[str, Counter[str]] = defaultdict(Counter)
    for pid, lab in labels.items():
        if pid not in sample:
            continue
        lab_c = lab["court"] if isinstance(lab, dict) else lab
        by["all"][lab_c] += 1
        by[sample[pid]["stratum"]][lab_c] += 1
        by["kind:" + sample[pid]["stratum"].split("/")[1]][lab_c] += 1
    for k, c in sorted(by.items()):
        n = c["C"] + c["N"]
        res[k] = {"C": c["C"], "N": c["N"], "U": c["U"], "precision": round(c["C"] / n, 3) if n else None,
                  "wilson95": _wilson(c["C"], n)}
    # population estimates: stratum precisions weighted by stratum size (normal-approximation 95% interval)
    parks, _ = read_geoparquet(PARKS_FILE, columns=["status", "mention_kind"], with_geometry=False)
    size: Counter[str] = Counter()
    for st, mk in zip(parks["status"], parks["mention_kind"]):
        mk = "text" if mk in ("court", "amenity_sentence") else "list" if mk in ("amenity_item", "icon", "table") else mk
        size[f"{st}/{mk}"] += 1
    groups = {"weighted:all_parks": lambda s: True,
              "weighted:outdoor_or_unspecified": lambda s: not s.startswith("indoor_only"),
              "weighted:web_layer (missing, weak_detection, confirmed_detected)":
                  lambda s: s.split("/")[0] in ("missing", "weak_detection", "confirmed_detected")}
    for name, keep in groups.items():
        strata = [s for s in size if keep(s) and s in by and by[s]["C"] + by[s]["N"] > 0]
        tot = sum(size[s] for s in strata)
        if not tot:
            continue
        p = sum(size[s] / tot * by[s]["C"] / (by[s]["C"] + by[s]["N"]) for s in strata)
        # 95% interval: Jeffreys Beta(C + 1/2, N + 1/2) per stratum, weighted, by simulation (a stratum with
        # 8 of 8 correct is not taken as certain, unlike the normal approximation)
        rng_s = np.random.default_rng(0)
        draws = sum(size[s] / tot * rng_s.beta(by[s]["C"] + 0.5, by[s]["N"] + 0.5, 20_000) for s in strata)
        uncovered = sum(size[s] for s in size if keep(s) and s not in strata)
        c_n = [sum(by[s][x] for s in strata) for x in ("C", "N")]
        res[name] = {"parks": tot, "parks_in_unsampled_strata": uncovered, "precision": round(p, 3),
                     "posterior_median": round(float(np.median(draws)), 3),
                     "ci95": (round(float(np.percentile(draws, 2.5)), 3), round(float(np.percentile(draws, 97.5)), 3)),
                     "sample_C": c_n[0], "sample_N": c_n[1], "sample_precision": round(c_n[0] / sum(c_n), 3),
                     "sample_wilson95": _wilson(c_n[0], sum(c_n))}
    write_json(d / "scores.json", res)
    for k, v in res.items():
        if k.startswith("weighted:"):
            print(f"{k:40} parks={v['parks']}  weighted={v['precision']} (Bayes median {v['posterior_median']}, "
                  f"95% {v['ci95']})  sample C={v['sample_C']} N={v['sample_N']} -> {v['sample_precision']} "
                  f"Wilson {v['sample_wilson95']}  (unsampled strata: {v['parks_in_unsampled_strata']} parks)")
        else:
            print(f"{k:40} C={v['C']:3d} N={v['N']:3d} U={v['U']:3d}  precision={v['precision']}  CI={v['wilson95']}")


# =========================================================================== CLI
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m hoop_pipeline.websources", description=__doc__.split("\n\n")[0])
    add_common_args(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("osm-seeds", help="Overpass: OSM parks etc. with a website tag, per state")
    p.add_argument("--states", nargs="*", default=None)
    p.add_argument("--sleep", type=float, default=5.0, help="pause between Overpass queries (s)")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_osm_seeds)
    p = sub.add_parser("overture-extract", help="Overture places (parks, sport, community) with websites")
    p.add_argument("--release", default=OVERTURE_RELEASE)
    p.set_defaults(func=cmd_overture_extract)
    p = sub.add_parser("wikidata-seeds", help="Wikidata US parks with an official website")
    p.set_defaults(func=cmd_wikidata_seeds)
    p = sub.add_parser("seeds", help="merge seed sources into the URL list")
    p.set_defaults(func=cmd_seeds)
    p = sub.add_parser("expand", help="more park pages per host (sitemaps, directory pages, web_seeds.json)")
    p.add_argument("--min-seeds", type=int, default=2, help="park-specific seed URLs sharing a directory")
    p.add_argument("--max-per-template", type=int, default=1500)
    p.add_argument("--max-hosts", type=int, default=0)
    p.add_argument("--force", action="store_true", help="redo hosts and registry entries expanded before")
    p.add_argument("--exclude-hosts", nargs="*", default=None, help="skip these hosts this time (still being crawled)")
    p.add_argument("--registry", nargs="*", default=None, help="only these web_seeds.json keys")
    p.add_argument("--area-pad", type=float, default=0.15, help="degrees around a host's seed parks for name matching")
    p.add_argument("--delay", type=float, default=2.0)
    p.add_argument("--workers", type=int, default=16)
    p.set_defaults(func=cmd_expand)
    p = sub.add_parser("crawl", help="polite fetch of the queued URLs")
    p.add_argument("--delay", type=float, default=2.0, help="minimum seconds between requests to one host")
    p.add_argument("--timeout", type=float, default=25.0)
    p.add_argument("--workers", type=int, default=32, help="hosts fetched in parallel")
    p.add_argument("--max-per-host", type=int, default=2500)
    p.add_argument("--max-minutes", type=float, default=0)
    p.add_argument("--refetch", action="store_true")
    p.add_argument("--exclude-hosts", nargs="*", default=None, help="leave these hosts for a later run")
    p.set_defaults(func=cmd_crawl)
    p = sub.add_parser("extract", help="rules over the cached pages -> data/web/pages.parquet")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--chunk-pages", type=int, default=3000, help="pages per resumable part (whole hosts)")
    p.add_argument("--force", action="store_true", help="redo every part")
    p.set_defaults(func=cmd_extract)
    p = sub.add_parser("build", help="locate pages and write out/web/parks_with_courts.parquet")
    p.add_argument("--area-pad", type=float, default=0.15)
    p.set_defaults(func=cmd_build)
    p = sub.add_parser("match", help="cross-match parks with OSM courts and satellite candidates")
    p.add_argument("--point-radius", type=float, default=150.0, help="zone around a park point without a polygon (m)")
    p.add_argument("--poly-buffer", type=float, default=15.0, help="buffer around a park polygon (m)")
    p.add_argument("--max-poly-km2", type=float, default=20.0, help="larger polygons fall back to the point zone")
    p.set_defaults(func=cmd_match)
    p = sub.add_parser("export", help="app-schema GeoJSON of courts the app lacks -> out/web/web_courts.geojson")
    p.add_argument("--kinds", nargs="*", default=None, help="keep only these mention kinds (e.g. court amenity_item)")
    p.add_argument("--public-max-uncertainty", type=float, default=150.0,
                   help="park-level points in web_courts_public.geojson: parks this small (m)")
    p.set_defaults(func=cmd_export)
    p = sub.add_parser("crawl-stats", help="what was crawled -> out/web/crawl_stats.json")
    p.set_defaults(func=cmd_crawl_stats)
    p = sub.add_parser("sample", help="stratified spot-check sample -> out/web/review/sample.txt")
    p.add_argument("--per-stratum", type=int, default=10)
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(func=cmd_sample)
    p = sub.add_parser("score", help="precision of the labelled sample")
    p.add_argument("--labels", default=str(OUT_WEB / "review" / "labels.json"))
    p.set_defaults(func=cmd_score)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    args.func(args)


if __name__ == "__main__":
    main()
