"""Stage 4 — efficiency metrics and anomaly generation.

Metrics are deterministic functions of the assembled route, its stops, the
rule parameters and the raw contextual inputs (orders, events, driver status).
Every metric row carries an evidence JSON (point IDs, measurements, rule
thresholds) so a number on the dashboard can always be retraced.

Anomalies are split at the source:

* ``DATA_ISSUE`` — the platform cannot trust the data: missing-fix runs,
  duplicates, jumps, unprovable dwells, gaps inside promised windows.
* ``REAL_WORLD`` — the data is trustworthy and describes something that
  genuinely happened on the road: late/early arrival, break stop, road-event
  delay, long real stop.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .cleaning import CleanPoint, Gap
from .geo import haversine_m
from .routes import RouteSegment
from .stops import StopEvent
from .tzutil import local_dt, local_iso, parse_utc, to_iso_z


# ---------------------------------------------------------------- route geometry
def route_distance(pts: List[CleanPoint]) -> float:
    return sum(haversine_m(a.lat, a.lng, b.lat, b.lng) for a, b in zip(pts, pts[1:]))


def empty_running_m(pts: List[CleanPoint], orders: List[Dict[str, Any]],
                    geofence_m: float) -> Tuple[float, List[Dict[str, Any]]]:
    """Distance traversed while no order destination lies within geofence of
    either segment endpoint (proxy for travel not directly serving an order)."""
    if len(pts) < 2:
        return 0.0, []
    served = [False] * (len(pts) - 1)
    trace: List[Dict[str, Any]] = []
    for o in orders:
        for i, (a, b) in enumerate(zip(pts, pts[1:])):
            da = haversine_m(a.lat, a.lng, o["dest_lat"], o["dest_lng"])
            db = haversine_m(b.lat, b.lng, o["dest_lat"], o["dest_lng"])
            if da <= geofence_m or db <= geofence_m:
                served[i] = True
    empty = 0.0
    for i, (a, b) in enumerate(zip(pts, pts[1:])):
        d = haversine_m(a.lat, a.lng, b.lat, b.lng)
        if not served[i]:
            empty += d
            trace.append({"from_point": a.point_id, "to_point": b.point_id,
                          "distance_m": round(d, 1)})
    return empty, trace


def order_arrival(pts: List[CleanPoint], order: Dict[str, Any],
                  geofence_m: float) -> Optional[CleanPoint]:
    """Arrival = first geofence *entry*: a fix inside the radius whose
    predecessor was outside. Merely having fixes inside the circle is not
    enough, since a wide geofence can overlap the approach path."""
    prev_inside = False
    for p in pts:
        inside = haversine_m(p.lat, p.lng, order["dest_lat"], order["dest_lng"]) <= geofence_m
        if inside and not prev_inside:
            return p
        prev_inside = inside
    return None


# ---------------------------------------------------------------- metrics
def compute_metrics(seg: RouteSegment, stops: List[StopEvent],
                    gaps: List[Gap], params: Dict[str, Any],
                    road_events: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    pts = seg.points
    geofence = float(params["arrival_geofence_m"])
    tz = seg.route_tz

    total_dist = route_distance(pts)
    duration_s = (pts[-1].dt - pts[0].dt).total_seconds() if len(pts) >= 2 else 0.0
    moving_s = duration_s - sum(s.duration_s for s in stops if s.stop_kind != "suspected_data_gap")
    empty_m, empty_trace = empty_running_m(pts, seg.orders, geofence)
    planned_total = sum((o.get("planned_distance_m") or 0.0) for o in seg.orders)
    planned_dwell = sum((o.get("planned_dwell_min") or 0.0) for o in seg.orders) * 60.0
    real_stops = [s for s in stops if s.stop_kind in ("real_stop", "break_stop")]
    actual_dwell = sum(s.duration_s for s in real_stops)

    # lateness per order — window comparison in absolute UTC, label in local tz
    lateness_rows: List[Dict[str, Any]] = []
    on_time = 0
    for o in seg.orders:
        arrival = order_arrival(pts, o, geofence)
        win_end = parse_utc(o["promised_end_utc"])
        win_start = parse_utc(o["promised_start_utc"])
        tol = float(params["lateness_tolerance_min"]) * 60.0
        early_grace = float(params["lateness_early_grace_min"]) * 60.0
        row: Dict[str, Any] = {
            "order_id": o["order_id"],
            "region": o["region"],
            "timezone": o["timezone"],
            "promised_window_local": f"{local_iso(win_start, o['timezone'])} ~ "
                                     f"{local_iso(win_end, o['timezone'])}",
            "arrival_point_id": arrival.point_id if arrival else None,
            "arrival_utc": arrival.ts_utc if arrival else None,
            "arrival_local": local_iso(arrival.dt, o["timezone"]) if arrival else None,
            "deviation_min": None,
            "verdict": "no_geofence_entry",
        }
        if arrival:
            dev_min = (arrival.dt - win_end).total_seconds() / 60.0
            row["deviation_min"] = round(dev_min, 1)
            if arrival.dt > win_end and (arrival.dt - win_end).total_seconds() > tol:
                row["verdict"] = "late"
            elif arrival.dt < win_start and (win_start - arrival.dt).total_seconds() > early_grace:
                row["verdict"] = "early"
            else:
                row["verdict"] = "on_time"
                on_time += 1
        lateness_rows.append(row)

    n_orders = len(seg.orders)
    on_time_ratio = (on_time / n_orders) if n_orders else None
    gap_s = sum(g.duration_s for g in gaps
                if pts[0].dt <= parse_utc(g.start_ts) and
                   (g.end_ts is None or parse_utc(g.end_ts) <= pts[-1].dt))

    metrics = [
        ("total_distance_m", round(total_dist, 1), "m",
         {"point_ids": seg.point_ids, "method": "haversine sum over accepted fixes"}),
        ("duration_min", round(duration_s / 60.0, 1), "min",
         {"from_point": pts[0].point_id, "to_point": pts[-1].point_id}),
        ("moving_time_min", round(max(moving_s, 0.0) / 60.0, 1), "min",
         {"excluded_stop_ids": [s.enter_ts for s in real_stops]}),
        ("avg_speed_kmh", round(total_dist / max(moving_s, 1.0) * 3.6, 1), "kmh",
         {"note": "distance / moving time"}),
        ("empty_running_m", round(empty_m, 1), "m",
         {"geofence_m": geofence, "segments": empty_trace[:50]}),
        ("empty_running_ratio", round(empty_m / total_dist, 3) if total_dist else 0.0, "ratio",
         {"planned_distance_m": planned_total or None}),
        ("stop_count", float(len(real_stops)), "count",
         {"stop_enters": [s.enter_ts for s in real_stops]}),
        ("dwell_min", round(actual_dwell / 60.0, 1), "min",
         {"planned_dwell_min": planned_dwell / 60.0 if planned_dwell else None}),
        ("dwell_efficiency", round(planned_dwell / actual_dwell, 3) if actual_dwell else None, "ratio",
         {"note": ">1 means faster than planned; null when no dwell/planning"}),
        ("data_gap_min", round(gap_s / 60.0, 1), "min",
         {"gap_count": len([g for g in gaps])}),
        ("on_time_ratio", on_time_ratio, "ratio",
         {"orders": lateness_rows}),
        ("order_count", float(n_orders), "count", {"order_ids": [o["order_id"] for o in seg.orders]}),
    ]
    metric_rows = [
        {"metric_key": k, "metric_value": v, "unit": u, "evidence": e}
        for (k, v, u, e) in metrics
    ]

    anomalies = _build_anomalies(seg, stops, gaps, lateness_rows, road_events, params)

    # weighted efficiency score in 0..100
    score = _score(metric_rows, anomalies, params)
    metric_rows.append({
        "metric_key": "efficiency_score", "metric_value": score, "unit": "score",
        "evidence": {"formula": "weighted sum, see rule params score_*_w",
                     "weights": {k: params[k] for k in params if k.endswith("_w")}},
    })
    return metric_rows, anomalies


def _score(metric_rows, anomalies, params) -> float:
    m = {r["metric_key"]: r["metric_value"] for r in metric_rows}
    real_world = [a for a in anomalies if a["category"] == "REAL_WORLD"]
    data_issues = [a for a in anomalies if a["category"] == "DATA_ISSUE"]

    on_time_part = (m.get("on_time_ratio") or 0.0)
    dwell_eff = m.get("dwell_efficiency")
    dwell_part = min(dwell_eff, 1.0) if dwell_eff is not None else 1.0
    empty_part = 1.0 - (m.get("empty_running_ratio") or 0.0)
    anomaly_penalty = min(1.0, 0.05 * len(real_world) + 0.1 * len(data_issues))
    anomaly_part = 1.0 - anomaly_penalty

    score = 100.0 * (
        params["score_on_time_w"] * on_time_part
        + params["score_dwell_w"] * dwell_part
        + params["score_empty_km_w"] * empty_part
        + params["score_anomaly_w"] * anomaly_part
    )
    return round(max(0.0, min(100.0, score)), 1)


def _build_anomalies(seg: RouteSegment, stops: List[StopEvent], gaps: List[Gap],
                     lateness_rows: List[Dict[str, Any]], road_events: List[Dict[str, Any]],
                     params: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    pts = seg.points

    # --- DATA_ISSUE: unobserved gaps inside the route ------------------------
    for g in gaps:
        gs = parse_utc(g.start_ts)
        ge = parse_utc(g.end_ts) if g.end_ts else None
        if ge and pts[0].dt <= gs and ge <= pts[-1].dt:
            out.append({
                "category": "DATA_ISSUE",
                "anomaly_type": g.kind,
                "severity": "warning" if g.duration_s >= 5 * 60 else "info",
                "start_utc": g.start_ts, "end_utc": g.end_ts,
                "lat": None, "lng": None,
                "message": f"{g.note} ({g.duration_s / 60:.1f} min unobserved)",
                "evidence": {"start_point_id": g.start_point_id, "end_point_id": g.end_point_id,
                             "duration_s": g.duration_s, "distance_m": g.distance_m,
                             "rule": {"missing_gap_min": params["missing_gap_min"]}},
                "related_order_id": None,
            })

    # --- DATA_ISSUE vs REAL_WORLD: stop classification passes through --------
    for s in stops:
        base = {
            "start_utc": s.enter_ts, "end_utc": s.exit_ts,
            "lat": round(s.lat, 6), "lng": round(s.lng, 6),
            "evidence": {**s.evidence, "point_ids": s.point_ids},
            "related_order_id": (s.evidence.get("order") or {}).get("order_id"),
        }
        if s.stop_kind == "suspected_data_gap":
            out.append({
                **base, "category": "DATA_ISSUE",
                "anomaly_type": "unprovable_dwell",
                "severity": "warning",
                "message": f"Stationary-looking window of {s.duration_s / 60:.1f} min has only "
                           f"{s.observed_coverage:.0%} observed coverage — treated as a data gap, "
                           "not a real stop.",
            })
        elif s.stop_kind == "break_stop":
            out.append({
                **base, "category": "REAL_WORLD",
                "anomaly_type": "driver_break",
                "severity": "info",
                "message": f"Driver break / off-duty stop for {s.duration_s / 60:.1f} min.",
            })
        else:
            sev = "warning" if s.duration_s >= 20 * 60 else "info"
            out.append({
                **base, "category": "REAL_WORLD",
                "anomaly_type": "long_stop",
                "severity": sev,
                "message": f"Real stop for {s.duration_s / 60:.1f} min "
                           f"({s.evidence.get('classification_reason', '')}).",
            })

    # --- REAL_WORLD: late / early arrivals (with local-time evidence) --------
    for row in lateness_rows:
        if row["verdict"] in ("late", "early"):
            out.append({
                "category": "REAL_WORLD",
                "anomaly_type": f"arrival_{row['verdict']}",
                "severity": "critical" if row["verdict"] == "late" else "info",
                "start_utc": row["arrival_utc"], "end_utc": row["arrival_utc"],
                "lat": None, "lng": None,
                "message": f"Order {row['order_id']} arrived {row['verdict'].replace('_', ' ')} "
                           f"by {abs(row['deviation_min']):.1f} min; local arrival "
                           f"{row['arrival_local']} vs window {row['promised_window_local']}.",
                "evidence": {"arrival_point_id": row["arrival_point_id"], **row,
                             "rule": {"lateness_tolerance_min": params["lateness_tolerance_min"],
                                      "lateness_early_grace_min": params["lateness_early_grace_min"]}},
                "related_order_id": row["order_id"],
            })
        elif row["verdict"] == "no_geofence_entry":
            out.append({
                "category": "DATA_ISSUE",
                "anomaly_type": "missing_destination_fix",
                "severity": "warning",
                "start_utc": None, "end_utc": None, "lat": None, "lng": None,
                "message": f"Order {row['order_id']} has no tracking fix inside the destination "
                           f"geofence ({params['arrival_geofence_m']} m); arrival cannot be proven.",
                "evidence": {"order_id": row["order_id"],
                             "geofence_m": params["arrival_geofence_m"]},
                "related_order_id": row["order_id"],
            })

    # --- REAL_WORLD: road-event exposure -------------------------------------
    for e in road_events:
        es, ee = parse_utc(e["start_utc"]), parse_utc(e["end_utc"]) if e.get("end_utc") else None
        hit_pts = [p for p in pts
                   if haversine_m(p.lat, p.lng, e["lat"], e["lng"]) <= float(e["radius_m"])
                   and p.dt >= es and (ee is None or p.dt <= ee)]
        if hit_pts:
            out.append({
                "category": "REAL_WORLD",
                "anomaly_type": f"road_event_{e['event_type']}",
                "severity": e["severity"],
                "start_utc": hit_pts[0].ts_utc, "end_utc": hit_pts[-1].ts_utc,
                "lat": e["lat"], "lng": e["lng"],
                "message": f"{e['event_type']} ({e.get('description', '')}) affected the route "
                           f"for {(hit_pts[-1].dt - hit_pts[0].dt).total_seconds() / 60:.1f} min.",
                "evidence": {"event_id": e["event_id"],
                             "point_ids": [p.point_id for p in hit_pts],
                             "radius_m": e["radius_m"]},
                "related_order_id": None,
            })
    return out
