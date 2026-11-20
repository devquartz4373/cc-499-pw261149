"""Stage 2 — stop classification: real dwell vs logging pause.

The classic 停留误判 (false-stop) problem: a cluster of stationary fixes can
mean the driver truly stopped, *or* that the device simply stopped reporting
while the vehicle kept moving. We separate the two with three signals:

1. **Observed coverage** — fraction of the dwell window actually covered by
   accepted fixes (gap intervals subtract from coverage). Below
   ``stop_max_gap_ratio`` the stop is *unprovable* and emitted as a data gap.
2. **Corroboration** — a driver status (ON_BREAK / LOADING / SERVICE), a road
   event at the cluster location, or an order destination inside the cluster.
3. **Dwell duration** — windows under ``stop_min_dwell_min`` are transient
   slowdowns, not stops.

Every classification carries its point IDs and the measurements used, so the
UI can show evidence rather than a bare label.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .cleaning import CleanPoint, Gap
from .geo import haversine_m
from .tzutil import parse_utc, to_iso_z


@dataclass
class StopEvent:
    stop_kind: str                 # real_stop | break_stop | suspected_data_gap
    lat: float
    lng: float
    enter_ts: str
    exit_ts: str
    duration_s: int
    observed_coverage: float
    point_ids: List[str] = field(default_factory=list)
    gap_seconds: float = 0.0
    evidence: Dict[str, Any] = field(default_factory=dict)


def _intersects_gaps(enter, exit_, gaps: List[Gap]) -> float:
    """Unobserved seconds inside the [enter, exit_] dwell window. Gaps are
    clipped to the window, so a gap extending past the dwell cannot inflate
    the unobserved fraction beyond the window itself."""
    total = 0.0
    for g in gaps:
        gs = parse_utc(g.start_ts)
        ge = parse_utc(g.end_ts) if g.end_ts else None
        if ge is None:
            ge = exit_  # open-ended missing run: charge only up to dwell exit
        overlap_start = max(gs, enter)
        overlap_end = min(ge, exit_)
        if overlap_end > overlap_start:
            total += (overlap_end - overlap_start).total_seconds()
    return max(0.0, total)


def classify_stops(accepted: List[CleanPoint], gaps: List[Gap],
                   params: Dict[str, Any],
                   driver_statuses: Optional[List[Dict[str, Any]]] = None,
                   road_events: Optional[List[Dict[str, Any]]] = None,
                   orders: Optional[List[Dict[str, Any]]] = None) -> List[StopEvent]:
    """Cluster accepted fixes into dwell windows and classify each."""
    radius = float(params["stop_radius_m"])
    min_dwell_s = float(params["stop_min_dwell_min"]) * 60.0
    max_gap_ratio = float(params["stop_max_gap_ratio"])
    require_event = bool(params["stop_requires_event"])

    driver_statuses = driver_statuses or []
    road_events = road_events or []
    orders = orders or []

    events: List[StopEvent] = []
    cluster: List[CleanPoint] = []

    def flush():
        if len(cluster) < 2:
            return
        enter, exit_ = cluster[0].dt, cluster[-1].dt
        duration_s = (exit_ - enter).total_seconds()
        if duration_s < min_dwell_s:
            return  # transient slowdown, not a stop

        # cluster anchor = centroid of accepted fixes
        clat = sum(p.lat for p in cluster) / len(cluster)
        clng = sum(p.lng for p in cluster) / len(cluster)
        gap_s = _intersects_gaps(enter, exit_, gaps)
        coverage = max(0.0, 1.0 - gap_s / duration_s) if duration_s else 1.0

        corr_status = _matching_status(driver_statuses, enter, exit_)
        corr_event = _matching_road_event(road_events, clat, clng, enter, exit_)
        corr_order = _matching_order(orders, clat, clng, radius)
        corroborated = bool(corr_status or corr_event or corr_order)

        ev = {
            "fix_count": len(cluster),
            "cluster_radius_m": round(max(
                haversine_m(clat, clng, p.lat, p.lng) for p in cluster), 1),
            "unobserved_s": round(gap_s, 1),
            "observed_coverage": round(coverage, 3),
            "driver_status": corr_status,
            "road_event": corr_event,
            "order": corr_order,
            "rule": {"stop_radius_m": radius, "stop_min_dwell_min": params["stop_min_dwell_min"],
                     "stop_max_gap_ratio": max_gap_ratio,
                     "stop_requires_event": require_event},
        }

        if coverage < (1.0 - max_gap_ratio):
            kind = "suspected_data_gap"
            ev["classification_reason"] = (
                f"unobserved fraction {1 - coverage:.0%} exceeds stop_max_gap_ratio "
                f"{max_gap_ratio:.0%}; dwell cannot be proven")
        elif require_event and not corroborated:
            kind = "suspected_data_gap"
            ev["classification_reason"] = (
                "stop_requires_event enabled but no status/event/order corroborates the dwell")
        elif corr_status and corr_status["status"] in ("ON_BREAK", "OFF_DUTY"):
            kind = "break_stop"
            ev["classification_reason"] = f"corroborated by driver status {corr_status['status']}"
        else:
            kind = "real_stop"
            ev["classification_reason"] = (
                "stationary fixes cover the dwell window"
                + (", corroborated" if corroborated else ", uncorroborated but fully observed"))

        events.append(StopEvent(
            stop_kind=kind, lat=clat, lng=clng,
            enter_ts=to_iso_z(enter), exit_ts=to_iso_z(exit_),
            duration_s=int(duration_s), observed_coverage=round(coverage, 3),
            point_ids=[p.point_id for p in cluster], gap_seconds=gap_s, evidence=ev))

    for p in accepted:
        if not cluster:
            cluster = [p]
            continue
        anchor = cluster[0]
        # A point outside the cluster's anchor radius ends the dwell *now*:
        # flushing against the previous point instead would swallow an
        # intervening gap into the dwell and fabricate an unprovable stop.
        if haversine_m(anchor.lat, anchor.lng, p.lat, p.lng) <= radius:
            cluster.append(p)
        else:
            flush()
            cluster = [p]
    flush()
    return events


def _matching_status(statuses, enter, exit_):
    for s in statuses:
        ss = parse_utc(s["start_utc"])
        se = parse_utc(s["end_utc"]) if s.get("end_utc") else None
        if ss <= exit_ and (se is None or se >= enter):
            return {"status_id": s["status_id"], "status": s["status"], "note": s.get("note")}
    return None


def _matching_road_event(events, lat, lng, enter, exit_):
    for e in events:
        es = parse_utc(e["start_utc"])
        ee = parse_utc(e["end_utc"]) if e.get("end_utc") else None
        if es > exit_ or (ee is not None and ee < enter):
            continue
        if haversine_m(lat, lng, e["lat"], e["lng"]) <= float(e["radius_m"]):
            return {"event_id": e["event_id"], "event_type": e["event_type"],
                    "severity": e["severity"], "description": e.get("description")}
    return None


def _matching_order(orders, lat, lng, radius):
    for o in orders:
        if haversine_m(lat, lng, o["dest_lat"], o["dest_lng"]) <= radius:
            return {"order_id": o["order_id"], "region": o["region"]}
    return None
