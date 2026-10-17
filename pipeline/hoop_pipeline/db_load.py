"""Load app-schema court GeoJSON into the Hoop Hunter Supabase database, and apply its migrations.

    python -m hoop_pipeline.db_load migrate [--dry-run]
    python -m hoop_pipeline.db_load load PATH_OR_GLOB ... [--layer NAME] [--only-source SRC] [--skip-near M]
                                          [--prune] [--force] [--skip-invalid] [--dry-run] [--no-db]
    python -m hoop_pipeline.db_load report

Input: GeoJSON FeatureCollections of Point features in the app schema (app/README.md "Data file"):
id, name, source, status, confidence, hoops, surface, lit, address, region, osm_id, updated, plus the
optional site, indoor and state. Any other property is kept in courts.extra, so a new layer (web, Overture,
indoor) can carry its own fields without a schema change.

How a load works (one transaction):
  1. Every feature is validated: types, enums, coordinates inside the US, ids unique across the inputs.
     Any problem aborts the load (or, with --skip-invalid, drops that feature and reports it).
  2. Rows are COPYed into a temp table, then upserted into public.courts by id:
     * a new id is inserted;
     * an existing id is updated only if something changed and the feature's `updated` date is not older
       than the row's (--force overrides the date check), so re-running the same files changes nothing;
     * status 'hidden' (set by a moderator) is never undone by a load.
  3. --skip-near M drops new ids within M metres of an existing published court with the same indoor flag
     (dedupes a new layer against what is already loaded). --prune retires rows of the same layer, states and
     sources that are missing from the input (a newer export dropped them, for example after a privacy re-check).
  4. --dry-run runs all of that and rolls back, so the report shows exactly what would change.

The DB URL comes from $SUPABASE_DB_URL, else from ~/.config/hoop-hunter/supabase.env (override with --env).
It is never printed. Needs psycopg 3: pip install -r requirements-db.txt
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import logging
import math
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("db_load")

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PIPELINE_ROOT.parent
MIGRATIONS_DIR = REPO_ROOT / "supabase" / "migrations"
DEFAULT_ENV_FILE = Path.home() / ".config" / "hoop-hunter" / "supabase.env"

# Must match the courts_source_check constraint (supabase/migrations/*_courts.sql).
SOURCES = ("osm", "nyc_parks", "detected", "user", "overture", "web", "indoor", "nces")
# A file can only publish; hidden / retired are database-side states.
FILE_STATUSES = ("verified", "candidate")
PUBLISHED = ("verified", "candidate")

APP_KEYS = ("id", "name", "source", "status", "confidence", "hoops", "surface", "lit", "address", "region", "osm_id", "updated")
OPTIONAL_KEYS = ("site", "indoor", "state")
TEXT_LIMITS = {"id": 128, "name": 200, "surface": 100, "address": 300, "region": 64, "site": 32, "osm_id": 64}

STATE_NAMES = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district_of_columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new_hampshire": "NH",
    "new_jersey": "NJ",
    "new_mexico": "NM",
    "new_york": "NY",
    "north_carolina": "NC",
    "north_dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode_island": "RI",
    "south_carolina": "SC",
    "south_dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west_virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "puerto_rico": "PR",
    # Metro regions used by the app's bundled file.
    "nyc": "NY",
    "chattanooga": "TN",
}
STATE_CODES = set(STATE_NAMES.values())

# (west, south, east, north): lower 48, Alaska (both sides of 180), Hawaii, Puerto Rico / USVI, Guam / CNMI.
US_BOXES = (
    (-125.0, 24.3, -66.8, 49.5),
    (-180.0, 51.0, -129.9, 71.6),
    (172.0, 51.0, 180.0, 53.1),
    (-160.6, 18.8, -154.7, 22.3),
    (-67.4, 17.6, -64.5, 18.6),
    (144.5, 13.2, 146.1, 20.6),
)


# --------------------------------------------------------------------------- env / connection
def load_db_url(env_file: Path) -> str:
    url = os.environ.get("SUPABASE_DB_URL")
    if url:
        return url
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "SUPABASE_DB_URL":
                return value.strip().strip('"').strip("'")
    raise SystemExit(f"SUPABASE_DB_URL is not set and not found in {env_file}")


def connect(env_file: Path):
    try:
        import psycopg
    except ImportError as e:  # pragma: no cover - depends on the venv
        raise SystemExit("psycopg 3 is not installed: .venv/Scripts/pip install -r requirements-db.txt") from e
    url = load_db_url(env_file)
    try:
        return psycopg.connect(url, connect_timeout=30, application_name="hoop_pipeline.db_load")
    except psycopg.Error as e:
        # psycopg messages name the host and user, never the password; scrub the URL anyway.
        raise SystemExit(f"Could not connect to the database: {str(e).replace(url, '<SUPABASE_DB_URL>')}") from None


# --------------------------------------------------------------------------- parsing + validation
@dataclass
class Row:
    id: str
    name: str | None
    source: str
    status: str
    confidence: float | None
    hoops: int | None
    surface: str | None
    lit: bool | None
    indoor: bool
    address: str | None
    region: str | None
    state: str | None
    site: str | None
    osm_id: str | None
    updated: dt.date | None
    lon: float
    lat: float
    extra: dict[str, Any]

    def copy_tuple(self, jsonb) -> tuple:
        return (
            self.id,
            self.name,
            self.source,
            self.status,
            self.confidence,
            self.hoops,
            self.surface,
            self.lit,
            self.indoor,
            self.address,
            self.region,
            self.state,
            self.site,
            self.osm_id,
            self.updated,
            self.lon,
            self.lat,
            jsonb(self.extra),
        )


@dataclass
class Batch:
    rows: list[Row] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    filtered_out: Counter = field(default_factory=Counter)
    per_file: dict[str, int] = field(default_factory=dict)


def in_us(lon: float, lat: float) -> bool:
    return any(w <= lon <= e and s <= lat <= n for w, s, e, n in US_BOXES)


def _text(props: dict, key: str, where: str, problems: list[str]) -> str | None:
    value = props.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        problems.append(f"{where}: {key} must be a string or null, got {type(value).__name__}")
        return None
    value = value.strip()
    if len(value) > TEXT_LIMITS.get(key, 10_000):
        problems.append(f"{where}: {key} is longer than {TEXT_LIMITS[key]} characters")
        return None
    return value or None


def _date(value: Any, where: str, problems: list[str]) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            pass
    problems.append(f"{where}: updated must be an ISO date, got {value!r}")
    return None


def state_for(props: dict, path: Path, region: str | None) -> str | None:
    state = props.get("state")
    if isinstance(state, str) and state.upper() in STATE_CODES:
        return state.upper()
    if re.fullmatch(r"[A-Z]{2}", path.stem) and path.stem in STATE_CODES:
        return path.stem
    if region:
        return STATE_NAMES.get(region.lower())
    return None


def parse_feature(feature: Any, path: Path, index: int, layer: str, rel: str, problems: list[str]) -> Row | None:
    where = f"{rel}#{index}"
    if not isinstance(feature, dict) or feature.get("type") != "Feature":
        problems.append(f"{where}: not a GeoJSON Feature")
        return None
    props = feature.get("properties")
    geom = feature.get("geometry")
    if not isinstance(props, dict):
        problems.append(f"{where}: missing properties")
        return None
    fid = props.get("id")
    if isinstance(fid, str) and fid.strip():
        where = f"{rel}#{index} ({fid})"
    before = len(problems)

    missing = [k for k in APP_KEYS if k not in props]
    if missing:
        problems.append(f"{where}: missing properties {missing}")

    if not isinstance(geom, dict) or geom.get("type") != "Point" or not isinstance(geom.get("coordinates"), list):
        problems.append(f"{where}: geometry must be a Point")
        lon = lat = math.nan
    else:
        coords = geom["coordinates"]
        ok = len(coords) >= 2 and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in coords[:2])
        lon, lat = (float(coords[0]), float(coords[1])) if ok else (math.nan, math.nan)
        if not (math.isfinite(lon) and math.isfinite(lat)):
            problems.append(f"{where}: bad coordinates {coords!r}")
        elif not in_us(lon, lat):
            problems.append(f"{where}: point {lon:.5f},{lat:.5f} is outside the US (flipped sign?)")

    cid = _text(props, "id", where, problems)
    if not cid:
        problems.append(f"{where}: id is required")
    source = props.get("source")
    if source not in SOURCES:
        problems.append(f"{where}: source {source!r} is not one of {SOURCES}")
    status = props.get("status")
    if status not in FILE_STATUSES:
        problems.append(f"{where}: status {status!r} is not one of {FILE_STATUSES}")

    confidence = props.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            problems.append(f"{where}: confidence must be 0-1 or null, got {confidence!r}")
            confidence = None
        else:
            confidence = float(confidence)

    hoops = props.get("hoops")
    if hoops is not None:
        if isinstance(hoops, bool) or not isinstance(hoops, (int, float)) or hoops != int(hoops) or not 0 <= hoops <= 100:
            problems.append(f"{where}: hoops must be a whole number 0-100 or null, got {hoops!r}")
            hoops = None
        else:
            hoops = int(hoops)

    lit = props.get("lit")
    if lit is not None and not isinstance(lit, bool):
        problems.append(f"{where}: lit must be true, false or null")
        lit = None
    indoor = props.get("indoor", False)
    if indoor is None:
        indoor = False
    if not isinstance(indoor, bool):
        problems.append(f"{where}: indoor must be true, false or null")
        indoor = False

    region = _text(props, "region", where, problems)
    row = Row(
        id=cid or "",
        name=_text(props, "name", where, problems),
        source=source if isinstance(source, str) else "",
        status=status if isinstance(status, str) else "",
        confidence=confidence,
        hoops=hoops,
        surface=_text(props, "surface", where, problems),
        lit=lit,
        indoor=indoor or source == "indoor",
        address=_text(props, "address", where, problems),
        region=region,
        state=state_for(props, path, region),
        site=_text(props, "site", where, problems),
        osm_id=_text(props, "osm_id", where, problems),
        updated=_date(props.get("updated"), where, problems),
        lon=round(lon, 7) if math.isfinite(lon) else lon,
        lat=round(lat, 7) if math.isfinite(lat) else lat,
        extra={"layer": layer, "file": rel} | {k: v for k, v in props.items() if k not in APP_KEYS and k not in OPTIONAL_KEYS},
    )
    return row if len(problems) == before else None


def expand_inputs(patterns: list[str]) -> list[Path]:
    paths: list[Path] = []
    for pattern in patterns:
        matches = sorted(glob.glob(pattern, recursive=True)) if glob.has_magic(pattern) else [pattern]
        if not matches:
            raise SystemExit(f"No files match {pattern}")
        for m in matches:
            p = Path(m)
            if p.is_dir():
                paths.extend(sorted(p.glob("*.geojson")))
            elif p.suffix.lower() in (".geojson", ".json") and p.is_file():
                paths.append(p)
            elif not p.exists():
                raise SystemExit(f"No such file: {p}")
    unique: dict[Path, None] = {}
    for p in paths:
        if p.name != "index.json":
            unique.setdefault(p.resolve(), None)
    return list(unique)


def default_layer(path: Path) -> str:
    """'national' for pipeline app_ready exports, otherwise the file's stem (e.g. 'courts')."""
    return "national" if "app_ready" in path.parts else path.stem


def relpath(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def read_batch(paths: list[Path], layer: str | None, only_sources: set[str] | None) -> Batch:
    batch = Batch()
    seen: dict[str, str] = {}
    for path in paths:
        rel = relpath(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            batch.problems.append(f"{rel}: cannot read GeoJSON ({e})")
            continue
        if not isinstance(data, dict) or data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
            batch.problems.append(f"{rel}: not a FeatureCollection")
            continue
        file_layer = layer or default_layer(path)
        kept = 0
        for i, feature in enumerate(data["features"]):
            source = (feature.get("properties") or {}).get("source") if isinstance(feature, dict) else None
            if only_sources and source not in only_sources:
                batch.filtered_out[source] += 1
                continue
            row = parse_feature(feature, path, i, file_layer, rel, batch.problems)
            if row is None:
                continue
            if row.id in seen:
                batch.problems.append(f"{rel}#{i} ({row.id}): duplicate id, also in {seen[row.id]}")
                continue
            seen[row.id] = rel
            batch.rows.append(row)
            kept += 1
        batch.per_file[rel] = kept
    return batch


# --------------------------------------------------------------------------- reporting
def print_counts(title: str, counts: dict[tuple[str, str], int], sources: list[str] | None = None) -> None:
    """counts keyed by (state, source)."""
    states = sorted({s for s, _ in counts}, key=lambda s: (s == "?", s))
    srcs = sources or sorted({src for _, src in counts}, key=lambda s: SOURCES.index(s) if s in SOURCES else 99)
    width = max([9] + [len(s) + 1 for s in srcs])
    print(f"\n{title}")
    print("state " + "".join(f"{s:>{width}}" for s in srcs) + f"{'total':>{width}}")
    totals: Counter = Counter()
    for st in states:
        line = [counts.get((st, s), 0) for s in srcs]
        totals.update(dict(zip(srcs, line)))
        print(f"{st:<6}" + "".join(f"{n:>{width},}" for n in line) + f"{sum(line):>{width},}")
    print("all   " + "".join(f"{totals[s]:>{width},}" for s in srcs) + f"{sum(totals.values()):>{width},}")


def batch_counts(rows: list[Row]) -> dict[tuple[str, str], int]:
    return dict(Counter((r.state or "?", r.source) for r in rows))


# --------------------------------------------------------------------------- load
STAGE_COLUMNS = (
    "id, name, source, status, confidence, hoops, surface, lit, indoor, address, region, state, site, "
    "osm_id, updated, lon, lat, extra"
)

UPSERT_SQL = """
insert into public.courts as c
  (id, name, source, status, confidence, hoops, surface, lit, indoor, address, region, state, site, osm_id,
   updated, geom, extra)
select id, name, source, status, confidence, hoops, surface, lit, indoor, address, region, state, site, osm_id,
       updated, extensions.st_setsrid(extensions.st_makepoint(lon, lat), 4326)::extensions.geography, extra
from stage
on conflict (id) do update set
  name = excluded.name,
  source = excluded.source,
  status = case when c.status = 'hidden' then 'hidden' else excluded.status end,
  confidence = excluded.confidence,
  hoops = excluded.hoops,
  surface = excluded.surface,
  lit = excluded.lit,
  indoor = excluded.indoor,
  address = excluded.address,
  region = excluded.region,
  state = excluded.state,
  site = excluded.site,
  osm_id = excluded.osm_id,
  updated = excluded.updated,
  geom = excluded.geom,
  extra = c.extra || excluded.extra,
  loaded_at = now()
where (%(force)s or c.updated is null or excluded.updated is null or excluded.updated >= c.updated)
  and (
    (c.name, c.source, c.status, c.confidence, c.hoops, c.surface, c.lit, c.indoor, c.address, c.region, c.state,
     c.site, c.osm_id, c.updated, extensions.st_asbinary(c.geom), c.extra)
    is distinct from
    (excluded.name, excluded.source, case when c.status = 'hidden' then 'hidden' else excluded.status end,
     excluded.confidence, excluded.hoops, excluded.surface, excluded.lit, excluded.indoor, excluded.address,
     excluded.region, excluded.state, excluded.site, excluded.osm_id, excluded.updated,
     extensions.st_asbinary(excluded.geom), c.extra || excluded.extra)
  )
returning (xmax = 0) as inserted, c.state, c.source
"""


def load(args: argparse.Namespace) -> int:
    only = set(args.only_source) if args.only_source else None
    if only and not only <= set(SOURCES):
        raise SystemExit(f"--only-source must be among {SOURCES}")
    paths = expand_inputs(args.paths)
    if not paths:
        raise SystemExit("No input files")
    batch = read_batch(paths, args.layer, only)

    print(f"Read {len(paths)} file(s), {len(batch.rows):,} valid features")
    for rel, n in batch.per_file.items():
        log.debug("  %s: %d", rel, n)
    if batch.filtered_out:
        print("Left out by --only-source: " + ", ".join(f"{k} {v:,}" for k, v in sorted(batch.filtered_out.items(), key=str)))
    if batch.problems:
        print(f"\n{len(batch.problems)} invalid feature(s):")
        for p in batch.problems[:50]:
            print("  " + p)
        if len(batch.problems) > 50:
            print(f"  ... and {len(batch.problems) - 50} more")
        if not args.skip_invalid:
            print("\nNothing loaded. Fix the input or pass --skip-invalid to load the valid features only.")
            return 2
        print("--skip-invalid: loading the valid features only.")
    if not batch.rows:
        print("Nothing to load.")
        return 0
    print_counts("Input by state and source", batch_counts(batch.rows))
    layers = sorted({r.extra["layer"] for r in batch.rows})
    print(f"\nLayer(s): {', '.join(layers)}")

    if args.no_db:
        print("\n--no-db: validated only, database not touched.")
        return 0

    from psycopg.types.json import Jsonb

    with connect(Path(args.env)) as conn:
        cur = conn.cursor()
        cur.execute(
            "create temp table stage (id text primary key, name text, source text, status text, confidence real, "
            "hoops smallint, surface text, lit boolean, indoor boolean, address text, region text, state text, "
            "site text, osm_id text, updated date, lon double precision, lat double precision, extra jsonb) "
            "on commit drop"
        )
        with cur.copy(f"copy stage ({STAGE_COLUMNS}) from stdin") as copy:
            for row in batch.rows:
                copy.write_row(row.copy_tuple(Jsonb))
        cur.execute("analyze stage")

        cur.execute("select count(*) from stage s join public.courts c using (id)")
        existing = cur.fetchone()[0]
        older = 0
        if not args.force:
            cur.execute("select count(*) from stage s join public.courts c using (id) where s.updated < c.updated")
            older = cur.fetchone()[0]

        retired: Counter = Counter()
        if args.prune:
            states = sorted({r.state for r in batch.rows if r.state})
            sources = sorted(only or {r.source for r in batch.rows})
            cur.execute(
                """
                update public.courts c set status = 'retired', loaded_at = now()
                where c.extra ->> 'layer' = any(%(layers)s) and c.state = any(%(states)s) and c.source = any(%(sources)s)
                  and c.status in ('verified', 'candidate')
                  and not exists (select 1 from stage s where s.id = c.id)
                returning c.state, c.source
                """,
                {"layers": layers, "states": states, "sources": sources},
            )
            retired = Counter((st or "?", src) for st, src in cur.fetchall())

        skipped_near: Counter = Counter()
        if args.skip_near:
            cur.execute(
                """
                delete from stage s
                where not exists (select 1 from public.courts c where c.id = s.id)
                  and exists (
                    -- Same indoor flag only: a gym next to an outdoor court is not a duplicate of it.
                    select 1 from public.courts c
                    where c.id <> s.id and c.status in ('verified', 'candidate') and c.indoor = s.indoor
                      and extensions.st_dwithin(
                        c.geom,
                        extensions.st_setsrid(extensions.st_makepoint(s.lon, s.lat), 4326)::extensions.geography,
                        %(m)s)
                  )
                returning s.state, s.source
                """,
                {"m": float(args.skip_near)},
            )
            skipped_near = Counter((st or "?", src) for st, src in cur.fetchall())

        cur.execute(UPSERT_SQL, {"force": bool(args.force)})
        changed = cur.fetchall()
        inserted = Counter((st or "?", src) for ins, st, src in changed if ins)
        updated = Counter((st or "?", src) for ins, st, src in changed if not ins)
        n_ins, n_upd = sum(inserted.values()), sum(updated.values())
        n_staged = len(batch.rows) - sum(skipped_near.values())
        unchanged = n_staged - n_ins - n_upd - older

        if n_ins + n_upd + sum(retired.values()) > 500:
            cur.execute("analyze public.courts")

        print("\nChanges" + (" (dry run, rolled back)" if args.dry_run else ""))
        print(f"  inserted            {n_ins:>8,}")
        print(f"  updated             {n_upd:>8,}")
        print(f"  unchanged           {unchanged:>8,}")
        print(f"  kept (row is newer) {older:>8,}" + ("" if args.force else "   (--force to overwrite)"))
        if args.skip_near:
            print(f"  skipped, within {args.skip_near:g} m of another court {sum(skipped_near.values()):>6,}")
        if args.prune:
            print(f"  retired (--prune)   {sum(retired.values()):>8,}")
        print(f"  ids already in the database: {existing:,}")
        if inserted:
            print_counts("Inserted by state and source", inserted)
        if skipped_near:
            print_counts(f"Skipped (within {args.skip_near:g} m) by state and source", skipped_near)
        if retired:
            print_counts("Retired by state and source", retired)

        print_db_summary(cur)
        if args.dry_run:
            conn.rollback()
            print("\nDry run: rolled back, nothing changed.")
        else:
            conn.commit()
            print("\nCommitted.")
    return 0


def print_db_summary(cur) -> None:
    cur.execute(
        "select coalesce(state, '?'), source, count(*) from public.courts where status in ('verified', 'candidate') group by 1, 2"
    )
    print_counts("Published courts in the database by state and source", {(s, src): n for s, src, n in cur.fetchall()})
    cur.execute("select status, count(*) from public.courts group by 1 order by 1")
    print("By status: " + ", ".join(f"{s} {n:,}" for s, n in cur.fetchall()))
    cur.execute(
        "select coalesce(extra ->> 'layer', '?'), source, count(*) from public.courts "
        "where status in ('verified', 'candidate') group by 1, 2 order by 1, 2"
    )
    print("By layer:  " + ", ".join(f"{layer}/{src} {n:,}" for layer, src, n in cur.fetchall()))


def report(args: argparse.Namespace) -> int:
    with connect(Path(args.env)) as conn:
        print_db_summary(conn.cursor())
    return 0


# --------------------------------------------------------------------------- migrations
MIGRATIONS_TABLE_SQL = """
create schema if not exists supabase_migrations;
create table if not exists supabase_migrations.schema_migrations (version text not null primary key);
alter table supabase_migrations.schema_migrations add column if not exists statements text[];
alter table supabase_migrations.schema_migrations add column if not exists name text;
"""


def migrate(args: argparse.Namespace) -> int:
    """Apply supabase/migrations/*.sql in order, recording them where the Supabase CLI does."""
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise SystemExit(f"No migrations in {MIGRATIONS_DIR}")
    with connect(Path(args.env)) as conn:
        cur = conn.cursor()
        cur.execute(MIGRATIONS_TABLE_SQL)
        cur.execute("select version from supabase_migrations.schema_migrations")
        applied = {v for (v,) in cur.fetchall()}
        conn.commit()
        todo = [f for f in files if args.reapply or f.name.split("_", 1)[0] not in applied]
        for f in files:
            state = "applied" if f.name.split("_", 1)[0] in applied else "pending"
            print(f"  {state:<8} {f.name}" + ("  (re-applying)" if args.reapply and state == "applied" else ""))
        if args.dry_run or not todo:
            print("Dry run: nothing applied." if args.dry_run else "Up to date.")
            return 0
        for f in todo:
            version, _, name = f.stem.partition("_")
            sql = f.read_text(encoding="utf-8")
            with conn.transaction():
                cur.execute(sql)
                cur.execute(
                    "insert into supabase_migrations.schema_migrations (version, name, statements) values (%s, %s, %s) "
                    "on conflict (version) do update set name = excluded.name, statements = excluded.statements",
                    (version, name, [sql]),
                )
            print(f"Applied {f.name}")
        conn.commit()
    return 0


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m hoop_pipeline.db_load", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--env", default=str(DEFAULT_ENV_FILE), help="env file with SUPABASE_DB_URL (default: %(default)s)")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("load", help="validate and upsert court GeoJSON files")
    p.add_argument(
        "paths", nargs="+", help="GeoJSON files, directories or globs (quote globs), e.g. 'out/national/v3/app_ready/*.geojson'"
    )
    p.add_argument(
        "--layer", help="provenance layer stored in extra.layer (default: 'national' for app_ready files, else the file stem)"
    )
    p.add_argument(
        "--only-source", action="append", metavar="SRC", help=f"load only these sources (repeatable): {', '.join(SOURCES)}"
    )
    p.add_argument("--skip-near", type=float, metavar="M", help="skip new ids within M metres of an existing published court")
    p.add_argument(
        "--prune", action="store_true", help="retire rows of the same layer, states and sources that the input no longer has"
    )
    p.add_argument("--force", action="store_true", help="overwrite rows even when their `updated` date is newer than the file's")
    p.add_argument("--skip-invalid", action="store_true", help="load the valid features even if some are invalid")
    p.add_argument("--dry-run", action="store_true", help="do everything in a transaction, report, then roll back")
    p.add_argument("--no-db", action="store_true", help="validate and count only; do not connect")
    p.set_defaults(func=load)

    p = sub.add_parser("migrate", help="apply supabase/migrations/*.sql that are not applied yet")
    p.add_argument("--dry-run", action="store_true", help="list pending migrations only")
    p.add_argument("--reapply", action="store_true", help="run every migration again (they are idempotent)")
    p.set_defaults(func=migrate)

    p = sub.add_parser("report", help="print published court counts by state, source, status and layer")
    p.set_defaults(func=report)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
