"""Geospatial primitives: haversine distance, speed, bearing, web mercator.

All angles are decimal degrees (WGS84), distances metres, speeds km/h.
Pure stdlib, no external geo dependency.
"""
from __future__ import annotations

import math
from typing import List, Sequence, Tuple

EARTH_RADIUS_M = 6_371_008.8


def valid_latlat(lat: float, lng: float) -> bool:
    return isinstance(lat, (int, float)) and isinstance(lng, (int, float)) \
        and -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two coordinates, metres."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def segment_speed_kmh(distance_m: float, dt_s: float) -> float:
    if dt_s <= 0:
        return float("inf") if distance_m > 0 else 0.0
    return (distance_m / dt_s) * 3.6


def bearing_deg(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlng = math.radians(lng2 - lng1)
    y = math.sin(dlng) * math.cos(rlat2)
    x = math.cos(rlat1) * math.sin(rlat2) - math.sin(rlat1) * math.cos(rlat2) * math.cos(dlng)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def polyline_length_m(points: Sequence[Tuple[float, float]]) -> float:
    total = 0.0
    for (la1, lo1), (la2, lo2) in zip(points, points[1:]):
        total += haversine_m(la1, lo1, la2, lo2)
    return total


def web_mercator(lat: float, lng: float) -> Tuple[float, float]:
    """Project to metres-ish unit coordinates (x in [-pi*R, pi*R], y clipped at 85.06deg)."""
    x = EARTH_RADIUS_M * math.radians(lng)
    lat = max(min(lat, 85.05112878), -85.05112878)
    y = EARTH_RADIUS_M * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    return x, y


def fit_bounds(coords: List[Tuple[float, float]], width: float, height: float,
               padding: float = 40.0) -> Tuple[float, float, float]:
    """Return (scale, offset_x, offset_y) mapping mercator coords into an SVG viewport."""
    if not coords:
        return 1.0, 0.0, 0.0
    proj = [web_mercator(la, lo) for la, lo in coords]
    minx = min(p[0] for p in proj)
    maxx = max(p[0] for p in proj)
    miny = min(p[1] for p in proj)
    maxy = max(p[1] for p in proj)
    span_x = max(maxx - minx, 1.0)
    span_y = max(maxy - miny, 1.0)
    scale = min((width - 2 * padding) / span_x, (height - 2 * padding) / span_y)
    off_x = padding - minx * scale + (width - 2 * padding - span_x * scale) / 2
    # mercator y grows northward; SVG y grows downward -> flip
    off_y = padding + maxy * scale + (height - 2 * padding - span_y * scale) / 2
    return scale, off_x, off_y


def project_svg(lat: float, lng: float, scale: float, ox: float, oy: float) -> Tuple[float, float]:
    x, y = web_mercator(lat, lng)
    return x * scale + ox, oy - y * scale
