"""Stage 3 — route segment assembly with cross-day support.

A driver's cleaned track is split into route segments whenever two consecutive
accepted fixes are separated by more than ``route_break_gap_min`` (after
cleaning). Segments are *not* split at local midnight when
``crossday_enabled`` is set: instead the route keeps one identity across the
boundary and is labelled with the service date on which it *started*, in the
route's IANA timezone. All internal arithmetic stays in absolute UTC; the
timezone only participates in wall-clock labelling and promised-window
comparisons, which keeps cross-region routes unambiguous.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .cleaning import CleanPoint
from .geo import haversine_m
from .tzutil import local_date, parse_utc


@dataclass
class RouteSegment:
    driver_id: str
    points: List[CleanPoint]
    orders: List[Dict[str, Any]] = field(default_factory=list)
    route_tz: str = "UTC"
    service_date: str = ""
    start_ts: str = ""
    end_ts: str = ""
    split_reasons: List[str] = field(default_factory=list)

    @property
    def point_ids(self) -> List[str]:
        return [p.point_id for p in self.points]


def split_segments(accepted: List[CleanPoint], params: Dict[str, Any]) -> List[List[CleanPoint]]:
    if not accepted:
        return []
    break_s = float(params["route_break_gap_min"]) * 60.0
    segments: List[List[CleanPoint]] = [[accepted[0]]]
    for prev, cur in zip(accepted, accepted[1:]):
        gap = (cur.dt - prev.dt).total_seconds()
        if gap > break_s:
            segments.append([cur])
        else:
            segments[-1].append(cur)
    return segments


def _segment_timezone(seg_points: List[CleanPoint], orders: List[Dict[str, Any]],
                      params: Dict[str, Any]) -> str:
    """Prefer the timezone of an order whose promised window overlaps the
    segment; fall back to the rule default."""
    if not seg_points:
        return params["crossday_local_tz"]
    seg_start, seg_end = seg_points[0].dt, seg_points[-1].dt
    for o in orders:
        ws, we = parse_utc(o["promised_start_utc"]), parse_utc(o["promised_end_utc"])
        if ws <= seg_end and we >= seg_start:
            return o["timezone"]
    return params["crossday_local_tz"]


def assign_orders(seg_points: List[CleanPoint], orders: List[Dict[str, Any]],
                  geofence_m: float) -> List[Dict[str, Any]]:
    """An order belongs to a segment when (a) it belongs to the same driver and
    (b) at least one accepted fix enters its destination geofence."""
    matched: List[Dict[str, Any]] = []
    seen = set()
    for o in orders:
        if o["driver_id"] != seg_points[0].driver_id or o["order_id"] in seen:
            continue
        for p in seg_points:
            if haversine_m(p.lat, p.lng, o["dest_lat"], o["dest_lng"]) <= geofence_m:
                matched.append(o)
                seen.add(o["order_id"])
                break
    return matched


def assemble_routes(cleaned_by_driver: Dict[str, List[CleanPoint]],
                    all_orders: List[Dict[str, Any]],
                    params: Dict[str, Any]) -> List[RouteSegment]:
    geofence = float(params["arrival_geofence_m"])
    routes: List[RouteSegment] = []
    for driver_id, pts in sorted(cleaned_by_driver.items()):
        driver_orders = [o for o in all_orders if o["driver_id"] == driver_id]
        for seg_points in split_segments(pts, params):
            tz_name = _segment_timezone(seg_points, driver_orders, params)
            start_dt, end_dt = seg_points[0].dt, seg_points[-1].dt
            start_local = local_date(start_dt, tz_name)
            end_local = local_date(end_dt, tz_name)
            crosses = start_local != end_local
            if crosses and not bool(params["crossday_enabled"]):
                # split at local midnight: find first point whose local date differs
                cut = next((i for i, p in enumerate(seg_points)
                            if local_date(p.dt, tz_name) != start_local), len(seg_points))
                pieces = [seg_points[:cut], seg_points[cut:]]
            else:
                pieces = [seg_points]
            for piece in pieces:
                if not piece:
                    continue
                seg = RouteSegment(
                    driver_id=driver_id,
                    points=piece,
                    route_tz=tz_name,
                    service_date=str(local_date(piece[0].dt, tz_name)),
                    start_ts=piece[0].ts_utc,
                    end_ts=piece[-1].ts_utc,
                )
                if crosses and bool(params["crossday_enabled"]):
                    seg.split_reasons.append(
                        f"cross-day route: spans local dates {start_local} -> {end_local} in {tz_name}, "
                        "kept as one route (crossday_enabled=true)")
                seg.orders = assign_orders(piece, driver_orders, geofence)
                routes.append(seg)
    return routes
