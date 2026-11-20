"""Deterministic seed data exercising every pipeline edge case.

Scenarios
---------
D001 (Asia/Shanghai)  — normal multi-order route containing:
  * exact + spatial duplicates, a GPS jump outlier,
  * a missing-fix run and a temporal gap,
  * a fully-observed real delivery stop and an ON_BREAK stop,
  * a road-event (congestion) exposure.
D002 (Asia/Urumqi)    — cross-region route: UTC offsets and local-time labels
  differ from Shanghai; one late arrival and one early arrival.
D003 (America/New_York + America/Chicago) — cross-midnight route spanning a
  DST fall-back boundary, plus an unprovable dwell (logging pause that must be
  classified DATA_ISSUE, not a stop).

D001 also contains an *unprovable real delivery*: the O102 dwell genuinely
happened (an ON_BREAK status overlaps it), but a 12-minute logging silence
inside the dwell window drops observed coverage below stop_max_gap_ratio, so
the engine must still report it as DATA_ISSUE — demonstrating that data
quality evidence outranks contextual corroboration (no 停留误判).

Run:  python seed_data.py --db ../data/analytics.db [--reset]
Then: python -m delivery_analytics.server --db ../data/analytics.db
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from delivery_analytics.database import Database  # noqa: E402
from delivery_analytics.geo import haversine_m  # noqa: E402
from delivery_analytics.pipeline import run_pipeline  # noqa: E402
from delivery_analytics.service import ensure_initial_rules, ingest_payload  # noqa: E402
from delivery_analytics.tzutil import to_iso_z  # noqa: E402


# ---------------------------------------------------------------- track builder
class TrackBuilder:
    """Generate realistic fixes along waypoints with injectable defects."""

    def __init__(self, driver_id: str, start: datetime, step_s: int = 45):
        self.driver_id = driver_id
        self.t = start
        self.step = timedelta(seconds=step_s)
        self.points = []
        self._seq = 0

    def _pid(self):
        self._seq += 1
        return f"{self.driver_id}-P{self._seq:04d}"

    def move_to(self, lat, lng, speed_kmh=32.0, jitter=0.00003):
        """Emit fixes from current position to target at roughly speed_kmh."""
        if not self.points:
            self.points.append({"point_id": self._pid(), "driver_id": self.driver_id,
                                "ts_utc": to_iso_z(self.t), "lat": lat, "lng": lng,
                                "source": "gps"})
            return self
        clat, clng = self._last_position()
        dist = haversine_m(clat, clng, lat, lng)
        travel_s = max(self.step.total_seconds(), dist / (speed_kmh / 3.6))
        n = max(1, int(travel_s / self.step.total_seconds()))
        for i in range(1, n + 1):
            frac = i / n
            plat = clat + (lat - clat) * frac + (jitter * (1 - math.sin(frac * 7)))
            plng = clng + (lng - clng) * frac + (jitter * (1 - math.cos(frac * 5)))
            self.t += self.step
            self.points.append({"point_id": self._pid(), "driver_id": self.driver_id,
                                "ts_utc": to_iso_z(self.t),
                                "lat": round(plat, 6), "lng": round(plng, 6),
                                "source": "gps"})
        return self

    def _last_position(self):
        for p in reversed(self.points):
            if p["lat"] is not None and not p.get("_defect"):
                return p["lat"], p["lng"]
        raise RuntimeError("no positioned point yet")

    def dwell(self, minutes, lat=None, lng=None):
        """Stationary, fully-observed dwell (tiny jitter only)."""
        plat0, plng0 = self._last_position()
        lat = plat0 if lat is None else lat
        lng = plng0 if lng is None else lng
        n = int(minutes * 60 / self.step.total_seconds())
        for i in range(n):
            self.t += self.step
            self.points.append({"point_id": self._pid(), "driver_id": self.driver_id,
                                "ts_utc": to_iso_z(self.t),
                                "lat": round(lat + 0.00003 * math.sin(i), 6),
                                "lng": round(lng + 0.00003 * math.cos(i), 6),
                                "source": "gps", "accuracy_m": 18})
        return self

    def duplicate(self, exact=True):
        src_lat, src_lng = self._last_position()
        self.t += timedelta(seconds=30)
        self.points.append({"point_id": self._pid(), "driver_id": self.driver_id,
                            "ts_utc": to_iso_z(self.t),
                            "lat": src_lat if exact else round(src_lat + 0.00005, 6),
                            "lng": src_lng if exact else round(src_lng + 0.00004, 6),
                            "source": "app"})
        return self

    def gps_jump(self, far_lat, far_lng):
        """One implausible fix; geometry resumes from the pre-jump anchor."""
        self.t += self.step
        self.points.append({"point_id": self._pid(), "driver_id": self.driver_id,
                            "ts_utc": to_iso_z(self.t), "lat": far_lat, "lng": far_lng,
                            "source": "gps", "accuracy_m": 1200, "_defect": True})
        return self

    def missing_fixes(self, minutes):
        """Device logs timestamps with null coordinates."""
        n = int(minutes * 60 / self.step.total_seconds())
        for _ in range(n):
            self.t += self.step
            self.points.append({"point_id": self._pid(), "driver_id": self.driver_id,
                                "ts_utc": to_iso_z(self.t), "lat": None, "lng": None,
                                "source": "gps"})
        return self

    def silence(self, minutes):
        """Pure temporal gap: no points at all for a while."""
        self.t += timedelta(minutes=minutes)
        return self


def build() -> dict:
    orders, points, events, statuses = [], [], [], []

    # ============================================================ D001 Shanghai
    tb = TrackBuilder("D001", datetime(2026, 11, 18, 1, 0, tzinfo=timezone.utc))  # 09:00 CST
    depot = (31.2304, 121.4737)
    dest1 = (31.2200, 121.4900)   # order O100
    dest2 = (31.2050, 121.5050)   # order O101
    dest3 = (31.1900, 121.4800)   # order O102
    tb.move_to(*depot).move_to(*dest1, speed_kmh=28)
    tb.duplicate(exact=True).duplicate(exact=False)          # duplicate handling
    tb.dwell(8)                                              # real stop at O100
    tb.gps_jump(32.5000, 120.9000)                           # implausible jump
    tb.move_to(*dest2)
    tb.dwell(6)                                              # real stop at O101
    tb.missing_fixes(6)                                      # missing-fix run (DATA_ISSUE)
    tb.move_to(*dest3)
    tb.silence(12)                                           # temporal gap (< break threshold)
    tb.move_to(*dest3)                                       # re-approach after the gap
    tb.dwell(7)                                              # fully-observed real stop at O102
    points.extend(tb.points)

    orders += [
        {"order_id": "O100", "driver_id": "D001", "region": "SH-CN", "timezone": "Asia/Shanghai",
         "origin_lat": depot[0], "origin_lng": depot[1], "dest_lat": dest1[0], "dest_lng": dest1[1],
         "planned_distance_m": 2600, "promised_start_utc": "2026-11-18T00:55:00Z",
         "promised_end_utc": "2026-11-18T01:10:00Z", "planned_dwell_min": 8},
        {"order_id": "O101", "driver_id": "D001", "region": "SH-CN", "timezone": "Asia/Shanghai",
         "origin_lat": dest1[0], "origin_lng": dest1[1], "dest_lat": dest2[0], "dest_lng": dest2[1],
         "planned_distance_m": 2300, "promised_start_utc": "2026-11-18T01:15:00Z",
         "promised_end_utc": "2026-11-18T01:20:00Z", "planned_dwell_min": 6},
        # promised end 01:25 but actual arrival 01:34 -> REAL_WORLD late
        {"order_id": "O102", "driver_id": "D001", "region": "SH-CN", "timezone": "Asia/Shanghai",
         "origin_lat": dest2[0], "origin_lng": dest2[1], "dest_lat": dest3[0], "dest_lng": dest3[1],
         "planned_distance_m": 3100, "promised_start_utc": "2026-11-18T01:25:00Z",
         "promised_end_utc": "2026-11-18T01:25:00Z", "planned_dwell_min": 7},
    ]
    # congestion on the approach to O102 (track is within radius 01:30-01:33Z)
    events.append({"event_id": "EV1", "event_type": "congestion", "severity": "warning",
                   "lat": 31.1975, "lng": 121.4925, "radius_m": 900,
                   "start_utc": "2026-11-18T01:25:00Z", "end_utc": "2026-11-18T01:45:00Z",
                   "description": "Yan'an elevated road slow traffic"})
    # O102 dwell 01:34-01:41Z; break overlaps it -> break_stop corroboration
    statuses.append({"status_id": "ST1", "driver_id": "D001", "status": "ON_BREAK",
                     "start_utc": "2026-11-18T01:35:00Z", "end_utc": "2026-11-18T01:40:00Z",
                     "note": "sign-in + short break at delivery"})

    # ============================================================ D002 Urumqi
    tb2 = TrackBuilder("D002", datetime(2026, 11, 18, 4, 0, tzinfo=timezone.utc), step_s=60)
    dep2 = (43.8256, 87.6168)
    d2a = (43.8000, 87.6500)    # late order O200
    d2b = (43.8400, 87.5600)    # early order O201
    tb2.move_to(*dep2).move_to(*d2a, speed_kmh=22).dwell(7).move_to(*d2b).dwell(5)
    for p in tb2.points:
        p["driver_id"] = "D002"
        p["point_id"] = p["point_id"].replace("D002", "D002")
    points.extend(tb2.points)
    orders += [
        # Urumqi local = UTC+6. Actual arrival 04:10Z; window ends 04:05Z -> LATE
        {"order_id": "O200", "driver_id": "D002", "region": "UR-CN", "timezone": "Asia/Urumqi",
         "origin_lat": dep2[0], "origin_lng": dep2[1], "dest_lat": d2a[0], "dest_lng": d2a[1],
         "planned_distance_m": 4200, "promised_start_utc": "2026-11-18T03:55:00Z",
         "promised_end_utc": "2026-11-18T04:05:00Z", "planned_dwell_min": 7},
        # actual arrival 04:32Z, window opens 05:00Z -> EARLY (> 15 min grace)
        {"order_id": "O201", "driver_id": "D002", "region": "UR-CN", "timezone": "Asia/Urumqi",
         "origin_lat": d2a[0], "origin_lng": d2a[1], "dest_lat": d2b[0], "dest_lng": d2b[1],
         "planned_distance_m": 7600, "promised_start_utc": "2026-11-18T05:00:00Z",
         "promised_end_utc": "2026-11-18T06:00:00Z", "planned_dwell_min": 5},
    ]

    # ============================================================ D003 cross-day + DST
    # 2026-11-01 DST fall-back in the US: 06:00Z -> local EDT(-4), 07:00Z -> EST(-5)
    tb3 = TrackBuilder("D003", datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc), step_s=60)
    ny = (40.7128, -74.0060)
    mid = (40.9000, -75.1000)
    chi = (41.8781, -87.6298)
    tb3.move_to(*ny).move_to(*mid, speed_kmh=70).dwell(6)
    # logging pause: stationary-looking window but most of it unobserved -> DATA_ISSUE
    tb3.missing_fixes(14)
    tb3.dwell(3)
    tb3.silence(22)  # temporal gap < route_break_gap_min(30): stays one cross-day route
    tb3.move_to(*chi, speed_kmh=72)
    for p in tb3.points:
        p["driver_id"] = "D003"
    points.extend(tb3.points)
    orders += [
        {"order_id": "O300", "driver_id": "D003", "region": "NY-US", "timezone": "America/New_York",
         "origin_lat": ny[0], "origin_lng": ny[1], "dest_lat": mid[0], "dest_lng": mid[1],
         "planned_distance_m": 130000, "promised_start_utc": "2026-11-01T05:30:00Z",
         "promised_end_utc": "2026-11-01T09:00:00Z", "planned_dwell_min": 6},
        {"order_id": "O301", "driver_id": "D003", "region": "IL-US", "timezone": "America/Chicago",
         "origin_lat": mid[0], "origin_lng": mid[1], "dest_lat": chi[0], "dest_lng": chi[1],
         "planned_distance_m": 1100000, "promised_start_utc": "2026-11-01T12:00:00Z",
         "promised_end_utc": "2026-11-01T20:00:00Z", "planned_dwell_min": 10},
    ]
    events.append({"event_id": "EV2", "event_type": "construction", "severity": "info",
                   "lat": mid[0], "lng": mid[1], "radius_m": 1200,
                   "start_utc": "2026-11-01T06:30:00Z", "end_utc": "2026-11-01T09:00:00Z",
                   "description": "I-80 overnight roadworks"})

    return {"orders": orders, "tracking_points": points,
            "road_events": events, "driver_status": statuses}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path(__file__).resolve().parent.parent / "data" / "analytics.db"))
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    if args.reset and os.path.exists(args.db):
        for suffix in ("", "-wal", "-shm"):
            p = args.db + suffix
            if os.path.exists(p):
                os.remove(p)

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    db = Database(args.db)
    db.init_schema()
    rs = ensure_initial_rules(db)
    payload = build()
    counts = ingest_payload(db, payload)
    run = run_pipeline(db, rs)
    print("ingested:", counts)
    print("pipeline :", {k: v for k, v in run.items() if k != "run_id"})
    db.close()


if __name__ == "__main__":
    main()
