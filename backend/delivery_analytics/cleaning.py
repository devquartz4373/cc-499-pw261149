"""Stage 1 — raw point cleaning.

Processes one driver's raw tracking points and classifies every single point:

* ``missing_fix``  — null coordinates (device logged time without a position);
* ``duplicate``    — same place, same time window as the last accepted fix;
* ``gps_jump``     — implausible speed versus the last accepted fix;
* ``accepted``     — usable fix.

Alongside per-point decisions it emits *unobserved intervals* (gaps). A gap is
not a point decision — it is the absence of trustworthy observation between two
instants, and it is the key input that lets stop classification distinguish a
real dwell from a logging pause.

Everything is pure functions over inputs + a rule parameter dict, so a given
rule version always yields the same decisions (deterministic, re-runnable).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .geo import haversine_m, segment_speed_kmh
from .tzutil import parse_utc, to_iso_z


@dataclass
class CleanPoint:
    point_id: str
    driver_id: str
    ts_utc: str          # normalized ISO-Z
    lat: Optional[float]
    lng: Optional[float]
    accuracy_m: Optional[float]
    source: str
    decision: str
    reason: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    _dt: Any = None      # parsed datetime, internal use

    @property
    def dt(self):
        if self._dt is None:
            self._dt = parse_utc(self.ts_utc)
        return self._dt


@dataclass
class Gap:
    kind: str                       # temporal_gap | missing_fix_run
    start_point_id: str
    end_point_id: Optional[str]     # None when track ends inside a missing run
    start_ts: str
    end_ts: Optional[str]
    duration_s: float
    distance_m: Optional[float]
    note: str


@dataclass
class CleaningResult:
    points: List[CleanPoint]            # all points, ordered by ts
    accepted: List[CleanPoint]          # accepted subset, ordered
    gaps: List[Gap]

    def decision_rows(self, rule_version_id: str):
        for p in self.points:
            yield (p.point_id, rule_version_id, p.driver_id, p.ts_utc,
                   p.decision, p.reason, _json(p.evidence))


def _json(obj: Dict[str, Any]) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def clean_points(raw_points: List[Dict[str, Any]], params: Dict[str, Any]) -> CleaningResult:
    """Clean one driver's points.

    ``raw_points`` are dict rows from tracking_points (or the ingest payload).
    Sorting is by timestamp; identical timestamps keep ingest order.
    """
    dedup_r = float(params["dedup_radius_m"])
    dedup_w = float(params["dedup_window_s"])
    jump_speed = float(params["jump_speed_kmh"])
    gap_min = float(params["missing_gap_min"])

    ordered = sorted(raw_points, key=lambda r: (parse_utc(r["ts_utc"]), r["point_id"]))

    cleaned: List[CleanPoint] = []
    accepted: List[CleanPoint] = []
    gaps: List[Gap] = []

    # Track runs of consecutive missing-fix points to emit one gap per run.
    missing_run_start: Optional[CleanPoint] = None

    def close_missing_run(end_point: Optional[CleanPoint]):
        nonlocal missing_run_start
        if missing_run_start is None:
            return
        end_ts = end_point.ts_utc if end_point else None
        duration = (end_point.dt - missing_run_start.dt).total_seconds() if end_point else None
        gaps.append(Gap(
            kind="missing_fix_run",
            start_point_id=missing_run_start.point_id,
            end_point_id=end_point.point_id if end_point else None,
            start_ts=missing_run_start.ts_utc,
            end_ts=end_ts,
            duration_s=duration if duration is not None else 0.0,
            distance_m=None,
            note="consecutive positioning failures; no trustworthy observation",
        ))
        missing_run_start = None

    last_accepted: Optional[CleanPoint] = None

    for raw in ordered:
        cp = CleanPoint(
            point_id=raw["point_id"],
            driver_id=raw["driver_id"],
            ts_utc=to_iso_z(parse_utc(raw["ts_utc"])),
            lat=raw.get("lat"),
            lng=raw.get("lng"),
            accuracy_m=raw.get("accuracy_m"),
            source=raw.get("source", "gps"),
            decision="",
            reason="",
        )

        # 1) missing fix ------------------------------------------------------
        if cp.lat is None or cp.lng is None:
            cp.decision = "missing_fix"
            cp.reason = "missing_gps_fix"
            cp.evidence = {"ts_utc": cp.ts_utc, "source": cp.source}
            if missing_run_start is None:
                missing_run_start = cp
            cleaned.append(cp)
            continue

        close_missing_run(cp)  # a positioned point closes any missing run

        if last_accepted is None:
            cp.decision, cp.reason = "accepted", "first_fix"
            cp.evidence = {}
        else:
            dist = haversine_m(last_accepted.lat, last_accepted.lng, cp.lat, cp.lng)
            dt_s = (cp.dt - last_accepted.dt).total_seconds()

            # 2) exact / spatial duplicate ------------------------------------
            if dt_s <= dedup_w and dist <= dedup_r:
                cp.decision = "duplicate"
                cp.reason = "spatial_duplicate" if dist > 0 else "exact_duplicate"
                cp.evidence = {
                    "anchor_point_id": last_accepted.point_id,
                    "distance_m": round(dist, 2),
                    "delta_s": round(dt_s, 1),
                    "rule": {"dedup_radius_m": dedup_r, "dedup_window_s": dedup_w},
                }
                cleaned.append(cp)
                continue

            # 3) implausible speed jump ---------------------------------------
            speed = segment_speed_kmh(dist, dt_s)
            if dt_s <= 0 or speed > jump_speed:
                cp.decision = "gps_jump"
                cp.reason = "same_timestamp_conflict" if dt_s <= 0 else "implausible_speed"
                cp.evidence = {
                    "anchor_point_id": last_accepted.point_id,
                    "distance_m": round(dist, 2),
                    "delta_s": round(dt_s, 1),
                    "speed_kmh": None if dt_s <= 0 else round(speed, 1),
                    "rule": {"jump_speed_kmh": jump_speed},
                }
                cleaned.append(cp)
                continue

            # 4) legitimate fix — register any temporal gap before accepting
            if dt_s > gap_min * 60:
                gaps.append(Gap(
                    kind="temporal_gap",
                    start_point_id=last_accepted.point_id,
                    end_point_id=cp.point_id,
                    start_ts=last_accepted.ts_utc,
                    end_ts=cp.ts_utc,
                    duration_s=dt_s,
                    distance_m=round(dist, 2),
                    note="interval between accepted fixes exceeds missing_gap_min",
                ))
            cp.decision = "accepted"
            cp.reason = "sequential_fix"
            cp.evidence = {
                "anchor_point_id": last_accepted.point_id,
                "distance_m": round(dist, 2),
                "delta_s": round(dt_s, 1),
                "speed_kmh": round(speed, 1),
            }

        cleaned.append(cp)
        accepted.append(cp)
        last_accepted = cp

    close_missing_run(None)
    return CleaningResult(points=cleaned, accepted=accepted, gaps=gaps)
