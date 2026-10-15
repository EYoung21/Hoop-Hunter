"""Read WKB that may contain curve types (PAD-US File Geodatabase) into linear shapely geometries.

shapely/GEOS do not support ISO curve types (CircularString 8, CompoundCurve 9, CurvePolygon 10,
MultiCurve 11, MultiSurface 12). ``from_wkb_linear`` decodes everything with shapely when it can
and falls back to this small parser, which densifies circular arcs (at most ``step_deg`` per
segment) and returns ordinary (Multi)Polygons / LineStrings. Z and M values are dropped.
"""

from __future__ import annotations

import math
import struct
from typing import Any

import numpy as np
import shapely


def _arc_points(p0: tuple[float, float], p1: tuple[float, float], p2: tuple[float, float],
                step_deg: float) -> list[tuple[float, float]]:
    """Points along the circular arc p0 -> p1 -> p2 (p0 included, p2 excluded)."""
    (x0, y0), (x1, y1), (x2, y2) = p0, p1, p2
    if x0 == x2 and y0 == y2 and (x0, y0) != (x1, y1):  # full circle: p1 is diametrically opposite
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        r = math.hypot(x0 - cx, y0 - cy)
        a0 = math.atan2(y0 - cy, x0 - cx)
        n = max(8, int(math.ceil(360 / step_deg)))
        return [(cx + r * math.cos(a0 + 2 * math.pi * k / n), cy + r * math.sin(a0 + 2 * math.pi * k / n)) for k in range(n)]
    d = 2 * (x0 * (y1 - y2) + x1 * (y2 - y0) + x2 * (y0 - y1))
    if abs(d) < 1e-12:  # collinear: a straight segment
        return [p0, p1]
    s0, s1, s2 = x0 * x0 + y0 * y0, x1 * x1 + y1 * y1, x2 * x2 + y2 * y2
    cx = (s0 * (y1 - y2) + s1 * (y2 - y0) + s2 * (y0 - y1)) / d
    cy = (s0 * (x2 - x1) + s1 * (x0 - x2) + s2 * (x1 - x0)) / d
    r = math.hypot(x0 - cx, y0 - cy)
    a0, a1, a2 = (math.atan2(y - cy, x - cx) for x, y in (p0, p1, p2))
    # direction: counter-clockwise if p1 lies on the ccw path from p0 to p2
    ccw_total = (a2 - a0) % (2 * math.pi)
    ccw_mid = (a1 - a0) % (2 * math.pi)
    sweep = ccw_total if ccw_mid <= ccw_total else -((a0 - a2) % (2 * math.pi))
    n = max(2, int(math.ceil(abs(math.degrees(sweep)) / step_deg)))
    return [(cx + r * math.cos(a0 + sweep * k / n), cy + r * math.sin(a0 + sweep * k / n)) for k in range(n)]


class _Reader:
    def __init__(self, b: bytes, step_deg: float) -> None:
        self.b = b
        self.o = 0
        self.step = step_deg

    def _u32(self, le: bool) -> int:
        v: int = struct.unpack_from("<I" if le else ">I", self.b, self.o)[0]
        self.o += 4
        return v

    def _coords(self, le: bool, n: int, dim: int) -> list[tuple[float, float]]:
        fmt = ("<" if le else ">") + "d" * (n * dim)
        vals = struct.unpack_from(fmt, self.b, self.o)
        self.o += 8 * n * dim
        return [(vals[k * dim], vals[k * dim + 1]) for k in range(n)]

    def geom(self) -> tuple[str, Any]:
        le = self.b[self.o] == 1
        self.o += 1
        t = self._u32(le)
        dim = 2
        if t & 0x20000000:  # EWKB SRID
            self.o += 4
        if t & 0x80000000:
            dim += 1
        if t & 0x40000000:
            dim += 1
        t &= 0x0FFFFFFF
        if t >= 3000:
            dim, t = 4, t - 3000
        elif t >= 2000:
            dim, t = 3, t - 2000
        elif t >= 1000:
            dim, t = 3, t - 1000
        if t == 1:
            return "point", self._coords(le, 1, dim)[0]
        if t == 2:
            return "line", self._coords(le, self._u32(le), dim)
        if t == 8:  # circular string
            pts = self._coords(le, self._u32(le), dim)
            out: list[tuple[float, float]] = []
            for k in range(0, len(pts) - 2, 2):
                out += _arc_points(pts[k], pts[k + 1], pts[k + 2], self.step)
            out.append(pts[-1])
            return "line", out
        if t == 9:  # compound curve
            out = []
            for _ in range(self._u32(le)):
                _, seg = self.geom()
                out += seg if not out else seg[1:]
            return "line", out
        if t == 3:  # polygon
            return "poly", [self._coords(le, self._u32(le), dim) for _ in range(self._u32(le))]
        if t == 10:  # curve polygon
            return "poly", [self.geom()[1] for _ in range(self._u32(le))]
        if t in (4, 5, 6, 7, 11, 12, 15):
            return "multi", [self.geom() for _ in range(self._u32(le))]
        raise ValueError(f"unsupported WKB type {t}")


def _to_shapely(kind: str, val: Any) -> Any:
    if kind == "point":
        return shapely.Point(val)
    if kind == "line":
        return shapely.LineString(val)
    if kind == "poly":
        rings = [r if r[0] == r[-1] else [*r, r[0]] for r in val if len(r) >= 3]
        if not rings:
            return shapely.Polygon()
        return shapely.make_valid(shapely.Polygon(rings[0], rings[1:]))
    parts = [_to_shapely(k, v) for k, v in val]
    polys = [p for p in parts if p.geom_type in ("Polygon", "MultiPolygon") and not p.is_empty]
    if polys:
        return shapely.make_valid(shapely.union_all(polys)) if len(polys) > 1 else polys[0]
    return shapely.GeometryCollection(parts)


def linearize(b: bytes, step_deg: float = 5.0) -> Any:
    kind, val = _Reader(bytes(b), step_deg).geom()
    return _to_shapely(kind, val)


def from_wkb_linear(wkb: np.ndarray, step_deg: float = 5.0) -> np.ndarray:
    """Like shapely.from_wkb, but curve geometries are linearised instead of raising."""
    try:
        return np.asarray(shapely.from_wkb(wkb))
    except (NotImplementedError, shapely.errors.GEOSException):
        pass
    out = np.empty(len(wkb), dtype=object)
    for i, b in enumerate(wkb):
        if b is None:
            out[i] = None
            continue
        try:
            out[i] = shapely.from_wkb(b)
        except (NotImplementedError, shapely.errors.GEOSException):
            out[i] = linearize(b, step_deg)
    return out
