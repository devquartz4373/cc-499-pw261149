"""Pipeline orchestration: ingest -> clean -> stops -> routes -> metrics,
all stamped with a rule version, plus selective recalculation on rule revision.

Traceability contract
---------------------
* ``point_cleaning`` rows bind every raw point to a decision + evidence under
  a rule version.
* ``route_results`` bind a route to its ordered point IDs, order IDs, tz and
  rule version; old versions are kept with status='superseded'.
* ``route_metrics`` / ``anomalies`` / ``stop_events`` are all keyed by
  (route_id, rule_version_id) and embed their own evidence.

Selective recalculation
-----------------------
When a new rule version is published, ``affected_routes`` computes the exact
set of current routes whose inputs touch the changed parameter scopes. Only
those routes are recomputed; untouched routes keep their original stamp.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

from .cleaning import clean_points
from .database import Database
from .metrics import compute_metrics
from .routes import assemble_routes
from .rules import RuleSet, PARAM_SCOPES
from .stops import classify_stops
from .tzutil import now_utc, parse_utc, to_iso_z


# ---------------------------------------------------------------- route identity
def route_identity_key(driver_id: str, start_ts: str, end_ts: str) -> str:
    h = hashlib.sha1(f"{driver_id}|{start_ts}|{end_ts}".encode()).hexdigest()[:12]
    return f"RT-{driver_id}-{h}"


def new_run_id() -> str:
    return "run-" + uuid.uuid4().hex[:10]


# ---------------------------------------------------------------- ingest helpers
def _rows_to_dicts(rows) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows]


def recompute_affected_scopes(db: Database, rule_version_id: str,
                              scopes: Set[str], run_id: str) -> List[str]:
    """Return the route_ids that must be recomputed for the given scopes."""
    current = db.query(
        "SELECT DISTINCT route_id FROM route_results WHERE status='current'")
    affected: Set[str] = set()
    global_scopes = {"cleaning", "stop", "route_assembly", "metrics"}
    for r in current:
        rid = r["route_id"]
        if scopes & global_scopes:
            affected.add(rid)
        elif "lateness" in scopes:
            # only routes that actually serve promised orders can change
            row = db.query_one(
                "SELECT order_ids_json FROM route_results WHERE route_id=? AND status='current'",
                (rid,))
            if row and json.loads(row["order_ids_json"]):
                affected.add(rid)
    return sorted(affected)


# ---------------------------------------------------------------- main pipeline
def run_pipeline(db: Database, rule_set: RuleSet,
                 only_route_ids: Optional[Set[str]] = None,
                 change_note: str = "initial computation") -> Dict[str, Any]:
    """Execute the full pipeline under ``rule_set``.

    ``only_route_ids`` restricts recomputation to specific routes (used after a
    rule revision); all other current routes are left untouched.
    """
    run_id = new_run_id()
    started = to_iso_z(now_utc())
    params = rule_set.params
    rv_id = rule_set.rule_version_id

    if db.get_rule_set(rv_id) is None:
        db.insert_rule_set(rule_set.to_dict())
        db.commit()

    raw_points = _rows_to_dicts(db.query("SELECT * FROM tracking_points ORDER BY ts_utc"))
    orders = _rows_to_dicts(db.query("SELECT * FROM orders"))
    road_events = _rows_to_dicts(db.query("SELECT * FROM road_events"))
    driver_statuses = _rows_to_dicts(db.query("SELECT * FROM driver_status"))

    # ---------------- Stage 1: cleaning per driver (global stage) -------------
    # Cleaning depends only on points + cleaning-scoped params. When a revision
    # does not touch cleaning, existing decisions still apply — but a version
    # stamp per (point, rule_version) is required for traceability, so we always
    # (re)derive deterministically and upsert.
    by_driver: Dict[str, List] = {}
    all_gaps: Dict[str, List] = {}
    cleaning_rows = []
    for p in raw_points:
        by_driver.setdefault(p["driver_id"], []).append(p)

    for driver_id, pts in sorted(by_driver.items()):
        result = clean_points(pts, params)
        cleaning_rows.extend(result.decision_rows(rv_id))
        by_driver[driver_id] = result.accepted
        all_gaps[driver_id] = result.gaps

    db.executemany(
        """INSERT OR REPLACE INTO point_cleaning
           (point_id, rule_version_id, driver_id, ts_utc, decision, reason, evidence_json)
           VALUES (?,?,?,?,?,?,?)""",
        cleaning_rows)

    # ---------------- Stage 3: route assembly ---------------------------------
    segments = assemble_routes(by_driver, orders, params)

    # If restricted, map requested route_ids -> segments via identity keys
    wanted_keys: Optional[Set[str]] = None
    if only_route_ids is not None:
        wanted_keys = set(only_route_ids)

    recomputed: List[str] = []
    for seg in segments:
        rid = route_identity_key(seg.driver_id, seg.start_ts, seg.end_ts)
        if wanted_keys is not None and rid not in wanted_keys:
            continue

        # supersede previous current row for this route identity
        db.execute(
            "UPDATE route_results SET status='superseded' WHERE route_id=? AND status='current'",
            (rid,))

        db.execute(
            """INSERT INTO route_results(route_id, rule_version_id, driver_id, service_date,
               route_tz, start_utc, end_utc, order_ids_json, point_ids_json, status,
               parent_route_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,'current',?,?)""",
            (rid, rv_id, seg.driver_id, seg.service_date, seg.route_tz,
             seg.start_ts, seg.end_ts,
             json.dumps([o["order_id"] for o in seg.orders]),
             json.dumps(seg.point_ids),
             change_note, to_iso_z(now_utc())))

        # ---------------- Stage 2: stops within this route --------------------
        d_statuses = [s for s in driver_statuses if s["driver_id"] == seg.driver_id]
        seg_gaps = [g for g in all_gaps.get(seg.driver_id, [])
                    if parse_utc(g.start_ts) >= seg.points[0].dt
                    and (g.end_ts is None or parse_utc(g.end_ts) <= seg.points[-1].dt)]
        stops = classify_stops(seg.points, seg_gaps, params,
                               driver_statuses=d_statuses,
                               road_events=road_events, orders=seg.orders)
        db.execute("DELETE FROM stop_events WHERE route_id=? AND rule_version_id=?",
                   (rid, rv_id))
        for s in stops:
            db.execute(
                """INSERT INTO stop_events(route_id, rule_version_id, stop_kind, lat, lng,
                   enter_utc, exit_utc, duration_s, observed_coverage, point_ids_json, evidence_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, rv_id, s.stop_kind, s.lat, s.lng, s.enter_ts, s.exit_ts,
                 s.duration_s, s.observed_coverage, json.dumps(s.point_ids),
                 json.dumps(s.evidence, ensure_ascii=False)))

        # ---------------- Stage 4: metrics + anomalies ------------------------
        metric_rows, anomalies = compute_metrics(seg, stops, seg_gaps, params, road_events)
        db.execute("DELETE FROM route_metrics WHERE route_id=? AND rule_version_id=?", (rid, rv_id))
        db.executemany(
            """INSERT INTO route_metrics(route_id, rule_version_id, metric_key,
               metric_value, unit, evidence_json) VALUES (?,?,?,?,?,?)""",
            [(rid, rv_id, m["metric_key"],
              m["metric_value"] if m["metric_value"] is not None else None,
              m["unit"], json.dumps(m["evidence"], ensure_ascii=False)) for m in metric_rows])

        db.execute("DELETE FROM anomalies WHERE route_id=? AND rule_version_id=?", (rid, rv_id))
        db.executemany(
            """INSERT INTO anomalies(route_id, rule_version_id, category, anomaly_type,
               severity, start_utc, end_utc, lat, lng, message, evidence_json, related_order_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(rid, rv_id, a["category"], a["anomaly_type"], a["severity"],
              a.get("start_utc"), a.get("end_utc"), a.get("lat"), a.get("lng"),
              a["message"], json.dumps(a["evidence"], ensure_ascii=False),
              a.get("related_order_id")) for a in anomalies])
        recomputed.append(rid)

    db.commit()
    return {
        "run_id": run_id,
        "rule_version_id": rv_id,
        "started_at": started,
        "finished_at": to_iso_z(now_utc()),
        "routes_recomputed": recomputed,
        "routes_skipped": sorted(wanted_keys - set(recomputed)) if wanted_keys else [],
        "cleaning_decisions": len(cleaning_rows),
    }
