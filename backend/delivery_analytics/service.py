"""Rule revision service and data ingestion.

``publish_revision`` is the only supported way to change analytics behaviour:
clone active params -> validate patch -> bump version -> retire parent ->
compute the exact affected route set -> recompute only those -> record a
rule_revisions row (including routes that were skipped, so the revision history
explains both what changed and why other routes kept their numbers).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Set

from .database import Database
from .pipeline import recompute_affected_scopes, run_pipeline
from .geo import valid_latlat
from .rules import (RuleSet, bump_version, changed_scopes, make_rule_set,
                    validate_params)
from .tzutil import is_valid_tz, now_utc, parse_utc, to_iso_z


def ensure_initial_rules(db: Database) -> RuleSet:
    """Create the 1.0.0 baseline on first boot."""
    existing = db.active_rule_set()
    if existing:
        return RuleSet(existing["rule_version_id"], existing["version"],
                       existing["status"], json.loads(existing["params_json"]),
                       existing["change_note"], existing["created_at"],
                       existing["parent_version_id"])
    from .rules import DEFAULT_PARAMS
    rs = make_rule_set(
        rule_version_id="rv-0001", version="1.0.0", params=DEFAULT_PARAMS,
        change_note="baseline rule set", created_at=to_iso_z(now_utc()),
        parent_version_id=None)
    db.insert_rule_set(rs.to_dict())
    db.commit()
    return rs


def publish_revision(db: Database, patch: Dict[str, Any],
                     change_note: str) -> Dict[str, Any]:
    errors = validate_params(patch)
    if errors:
        raise ValueError("; ".join(errors))

    parent_row = db.active_rule_set()
    if parent_row is None:
        ensure_initial_rules(db)
        parent_row = db.active_rule_set()

    old_params = json.loads(parent_row["params_json"])
    scopes = changed_scopes(patch, old_params)
    if not scopes:
        raise ValueError("patch changes no effective parameter values")

    new_version = bump_version(parent_row["version"], scopes)
    new_id = f"rv-{int(parent_row['rule_version_id'].split('-')[1]) + 1:04d}"
    new_params = dict(old_params)
    new_params.update(patch)
    rs = make_rule_set(
        rule_version_id=new_id, version=new_version, params=new_params,
        change_note=change_note, created_at=to_iso_z(now_utc()),
        parent_version_id=parent_row["rule_version_id"])

    affected = recompute_affected_scopes(db, parent_row["rule_version_id"], scopes, new_id)

    db.retire_rule_set(parent_row["rule_version_id"])
    db.insert_rule_set(rs.to_dict())
    result = run_pipeline(db, rs, only_route_ids=set(affected),
                          change_note=f"recomputed under {new_version}: {change_note}")

    db.execute(
        """INSERT INTO rule_revisions(rule_version_id, parent_version_id,
           changed_params_json, changed_scopes_json, affected_route_ids_json,
           recomputed_route_ids_json, change_note, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (new_id, parent_row["rule_version_id"],
         json.dumps(patch, ensure_ascii=False),
         json.dumps(sorted(scopes)),
         json.dumps(affected),
         json.dumps(result["routes_recomputed"]),
         change_note, to_iso_z(now_utc())))
    db.commit()
    return {
        "rule_version_id": new_id,
        "version": new_version,
        "changed_scopes": sorted(scopes),
        "affected_routes": affected,
        "recomputed_routes": result["routes_recomputed"],
        "skipped_routes": result["routes_skipped"],
        "run": result,
    }


# ---------------------------------------------------------------- ingestion
class IngestionError(ValueError):
    pass


def _require(d: Dict[str, Any], keys: List[str], ctx: str) -> None:
    missing = [k for k in keys if k not in d or d[k] in (None, "")]
    if missing:
        raise IngestionError(f"{ctx}: missing fields {missing}")


def ingest_payload(db: Database, payload: Dict[str, Any]) -> Dict[str, int]:
    """Validate and upsert raw inputs. Raw tables are append/upsert only; the
    pipeline never mutates them."""
    counts = {"orders": 0, "tracking_points": 0, "road_events": 0, "driver_status": 0}
    ts = to_iso_z(now_utc())

    for o in payload.get("orders", []):
        _require(o, ["order_id", "driver_id", "region", "timezone",
                     "origin_lat", "origin_lng", "dest_lat", "dest_lng",
                     "promised_start_utc", "promised_end_utc"], f"order {o.get('order_id')}")
        if not is_valid_tz(o["timezone"]):
            raise IngestionError(f"order {o['order_id']}: unknown timezone {o['timezone']}")
        for k in ("origin_lat", "dest_lat"):
            if not valid_latlat(o[k], o[k.replace("lat", "lng")]):
                raise IngestionError(f"order {o['order_id']}: invalid coordinates")
        parse_utc(o["promised_start_utc"]); parse_utc(o["promised_end_utc"])
        db.execute(
            """INSERT OR REPLACE INTO orders(order_id, driver_id, region, timezone,
               origin_lat, origin_lng, dest_lat, dest_lng, planned_distance_m,
               promised_start_utc, promised_end_utc, planned_dwell_min, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (o["order_id"], o["driver_id"], o["region"], o["timezone"],
             o["origin_lat"], o["origin_lng"], o["dest_lat"], o["dest_lng"],
             o.get("planned_distance_m"),
             to_iso_z(parse_utc(o["promised_start_utc"])),
             to_iso_z(parse_utc(o["promised_end_utc"])),
             o.get("planned_dwell_min"), ts))
        counts["orders"] += 1

    for p in payload.get("tracking_points", []):
        _require(p, ["point_id", "driver_id", "ts_utc"], f"point {p.get('point_id')}")
        lat, lng = p.get("lat"), p.get("lng")
        if lat is not None and not valid_latlat(lat, lng):
            raise IngestionError(f"point {p['point_id']}: invalid coordinates")
        db.execute(
            """INSERT OR REPLACE INTO tracking_points(point_id, driver_id, ts_utc, lat, lng,
               accuracy_m, source, raw_quality, ingested_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (p["point_id"], p["driver_id"], to_iso_z(parse_utc(p["ts_utc"])),
             lat, lng, p.get("accuracy_m"), p.get("source", "gps"),
             "missing" if lat is None else "raw", ts))
        counts["tracking_points"] += 1

    for e in payload.get("road_events", []):
        _require(e, ["event_id", "event_type", "lat", "lng", "start_utc"],
                 f"event {e.get('event_id')}")
        if not valid_latlat(e["lat"], e["lng"]):
            raise IngestionError(f"event {e['event_id']}: invalid coordinates")
        db.execute(
            """INSERT OR REPLACE INTO road_events(event_id, event_type, severity, lat, lng,
               radius_m, start_utc, end_utc, description)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (e["event_id"], e["event_type"], e.get("severity", "info"),
             e["lat"], e["lng"], e.get("radius_m", 500),
             to_iso_z(parse_utc(e["start_utc"])),
             to_iso_z(parse_utc(e["end_utc"])) if e.get("end_utc") else None,
             e.get("description")))
        counts["road_events"] += 1

    for s in payload.get("driver_status", []):
        _require(s, ["status_id", "driver_id", "status", "start_utc"],
                 f"status {s.get('status_id')}")
        db.execute(
            """INSERT OR REPLACE INTO driver_status(status_id, driver_id, status,
               start_utc, end_utc, note) VALUES (?,?,?,?,?,?)""",
            (s["status_id"], s["driver_id"], s["status"],
             to_iso_z(parse_utc(s["start_utc"])),
             to_iso_z(parse_utc(s["end_utc"])) if s.get("end_utc") else None,
             s.get("note")))
        counts["driver_status"] += 1

    db.commit()
    return counts
