"""Read-side repository: assemble route views with full traceability chains.

The UI never needs to know the schema; each route detail response contains its
map geometry, local-time timeline, stops, anomalies (with evidence), metrics
(with evidence), the rule version that produced them, and the point-level
cleaning trail. Historical (superseded) route rows are queryable too, which is
what powers the revision-history view.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .database import Database
from .tzutil import local_iso, parse_utc


def _loads(v, default):
    return json.loads(v) if v else default


def list_routes(db: Database, include_superseded: bool = False) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM route_results"
    if not include_superseded:
        sql += " WHERE status='current'"
    sql += " ORDER BY start_utc"
    out = []
    for r in db.query(sql):
        score = db.query_one(
            "SELECT metric_value FROM route_metrics WHERE route_id=? AND rule_version_id=? "
            "AND metric_key='efficiency_score'", (r["route_id"], r["rule_version_id"]))
        n_anom = db.query_one(
            "SELECT COUNT(*) c FROM anomalies WHERE route_id=? AND rule_version_id=?",
            (r["route_id"], r["rule_version_id"]))
        out.append({
            "route_id": r["route_id"],
            "rule_version_id": r["rule_version_id"],
            "driver_id": r["driver_id"],
            "service_date": r["service_date"],
            "route_tz": r["route_tz"],
            "start_utc": r["start_utc"],
            "end_utc": r["end_utc"],
            "start_local": local_iso(parse_utc(r["start_utc"]), r["route_tz"]),
            "end_local": local_iso(parse_utc(r["end_utc"]), r["route_tz"]),
            "order_ids": _loads(r["order_ids_json"], []),
            "status": r["status"],
            "efficiency_score": score["metric_value"] if score else None,
            "anomaly_count": n_anom["c"] if n_anom else 0,
        })
    return out


def route_detail(db: Database, route_id: str,
                 rule_version_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if rule_version_id is None:
        r = db.query_one(
            "SELECT * FROM route_results WHERE route_id=? AND status='current' ORDER BY created_at DESC LIMIT 1",
            (route_id,))
    else:
        r = db.query_one(
            "SELECT * FROM route_results WHERE route_id=? AND rule_version_id=?",
            (route_id, rule_version_id))
    if r is None:
        return None

    rv = db.get_rule_set(r["rule_version_id"])
    point_ids = _loads(r["point_ids_json"], [])
    order_ids = _loads(r["order_ids_json"], [])

    # map geometry: raw points with their cleaning decision under this version
    points = []
    if point_ids:
        placeholders = ",".join("?" * len(point_ids))
        raw = {p["point_id"]: p for p in db.query(
            f"SELECT * FROM tracking_points WHERE point_id IN ({placeholders})", point_ids)}
        decisions = {c["point_id"]: c for c in db.query(
            f"SELECT * FROM point_cleaning WHERE rule_version_id=? AND point_id IN ({placeholders})",
            [r["rule_version_id"], *point_ids])}
        for pid in point_ids:
            rp = raw.get(pid)
            dc = decisions.get(pid)
            if not rp:
                continue
            points.append({
                "point_id": pid,
                "ts_utc": rp["ts_utc"],
                "ts_local": local_iso(parse_utc(rp["ts_utc"]), r["route_tz"]),
                "lat": rp["lat"], "lng": rp["lng"],
                "source": rp["source"],
                "decision": dc["decision"] if dc else None,
                "decision_reason": dc["reason"] if dc else None,
                "evidence": _loads(dc["evidence_json"], {}) if dc else {},
            })

    order_placeholders = ",".join("?" * len(order_ids)) if order_ids else ""
    orders = [dict(o) for o in db.query(
        f"SELECT * FROM orders WHERE order_id IN ({order_placeholders})", order_ids)] if order_ids else []
    for o in orders:
        for k in ("promised_start_utc", "promised_end_utc"):
            o[k.replace("_utc", "_local")] = local_iso(parse_utc(o[k]), o["timezone"])

    stops = []
    for s in db.query(
            "SELECT * FROM stop_events WHERE route_id=? AND rule_version_id=? ORDER BY enter_utc",
            (route_id, r["rule_version_id"])):
        d = dict(s)
        d["point_ids"] = _loads(s["point_ids_json"], [])
        d["evidence"] = _loads(s["evidence_json"], {})
        d["enter_local"] = local_iso(parse_utc(s["enter_utc"]), r["route_tz"])
        d["exit_local"] = local_iso(parse_utc(s["exit_utc"]), r["route_tz"])
        stops.append(d)

    anomalies = []
    for a in db.query(
            "SELECT * FROM anomalies WHERE route_id=? AND rule_version_id=? ORDER BY start_utc",
            (route_id, r["rule_version_id"])):
        d = dict(a)
        d["evidence"] = _loads(a["evidence_json"], {})
        if a["start_utc"]:
            d["start_local"] = local_iso(parse_utc(a["start_utc"]), r["route_tz"])
        anomalies.append(d)

    metrics = []
    for m in db.query(
            "SELECT * FROM route_metrics WHERE route_id=? AND rule_version_id=? ORDER BY metric_key",
            (route_id, r["rule_version_id"])):
        d = dict(m)
        d["evidence"] = _loads(m["evidence_json"], {})
        metrics.append(d)

    # all versions this route identity has been computed under -> revision trail
    versions = []
    for v in db.query(
            "SELECT * FROM route_results WHERE route_id=? ORDER BY created_at", (route_id,)):
        versions.append({
            "rule_version_id": v["rule_version_id"],
            "status": v["status"],
            "created_at": v["created_at"],
            "parent_route_id": v["parent_route_id"],
        })

    return {
        "route": dict(r),
        "rule_set": {
            "rule_version_id": rv["rule_version_id"],
            "version": rv["version"],
            "status": rv["status"],
            "params": _loads(rv["params_json"], {}),
            "change_note": rv["change_note"],
            "created_at": rv["created_at"],
        },
        "points": points,
        "orders": orders,
        "stops": stops,
        "anomalies": anomalies,
        "metrics": metrics,
        "version_history": versions,
        "split_notes": [],
    }


def cleaning_trail(db: Database, driver_id: Optional[str] = None,
                   rule_version_id: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = "SELECT c.*, t.lat, t.lng, t.source FROM point_cleaning c " \
          "JOIN tracking_points t ON t.point_id=c.point_id WHERE 1=1"
    args: List[Any] = []
    if driver_id:
        sql += " AND c.driver_id=?"; args.append(driver_id)
    if rule_version_id:
        sql += " AND c.rule_version_id=?"; args.append(rule_version_id)
    sql += " ORDER BY c.ts_utc"
    out = []
    for r in db.query(sql, args):
        d = dict(r)
        d["evidence"] = _loads(r["evidence_json"], {})
        out.append(d)
    return out


def revision_history(db: Database) -> List[Dict[str, Any]]:
    out = []
    for r in db.query("SELECT * FROM rule_revisions ORDER BY revision_id"):
        out.append({
            "revision_id": r["revision_id"],
            "rule_version_id": r["rule_version_id"],
            "parent_version_id": r["parent_version_id"],
            "changed_params": _loads(r["changed_params_json"], {}),
            "changed_scopes": _loads(r["changed_scopes_json"], []),
            "affected_route_ids": _loads(r["affected_route_ids_json"], []),
            "recomputed_route_ids": _loads(r["recomputed_route_ids_json"], []),
            "change_note": r["change_note"],
            "created_at": r["created_at"],
        })
    return out
