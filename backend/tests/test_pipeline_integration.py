"""End-to-end pipeline integration tests on a temporary database."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from delivery_analytics.database import Database
from delivery_analytics.pipeline import run_pipeline
from delivery_analytics.rules import DEFAULT_PARAMS
from delivery_analytics.service import ensure_initial_rules, ingest_payload, publish_revision
from delivery_analytics.tzutil import local_date


def move(points, driver, start, n, lat0, lng0, dlat, dlng, step=60):
    for i in range(n):
        points.append({"point_id": f"{driver}-{i:03d}", "driver_id": driver,
                       "ts_utc": start.replace(second=0).isoformat().replace("+00:00", "Z"),
                       "lat": lat0 + dlat * i, "lng": lng0 + dlng * i, "source": "gps"})
        start = start.fromtimestamp(start.timestamp() + step, tz=start.tzinfo)
    return start


class PipelineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.db.init_schema()
        self.rs = ensure_initial_rules(self.db)

    def tearDown(self):
        self.db.close()
        for suffix in ("", "-wal", "-shm"):
            p = self.tmp.name + suffix
            if os.path.exists(p):
                os.remove(p)

    def _ingest_simple(self):
        from datetime import datetime, timezone
        points = []
        t = datetime(2026, 11, 18, 1, 0, tzinfo=timezone.utc)
        t = move(points, "D1", t, 40, 31.20, 121.47, 0.0006, 0.0004)
        order = {"order_id": "O1", "driver_id": "D1", "region": "SH", "timezone": "Asia/Shanghai",
                 "origin_lat": 31.20, "origin_lng": 121.47, "dest_lat": 31.20 + 0.0006 * 39,
                 "dest_lng": 121.47 + 0.0004 * 39, "planned_distance_m": 5000,
                 "promised_start_utc": "2026-11-18T00:30:00Z",
                 "promised_end_utc": "2026-11-18T02:00:00Z", "planned_dwell_min": 5}
        ingest_payload(self.db, {"orders": [order], "tracking_points": points})
        return order

    def test_full_pipeline_writes_traceable_outputs(self):
        self._ingest_simple()
        run = run_pipeline(self.db, self.rs)
        self.assertEqual(len(run["routes_recomputed"]), 1)
        rid = run["routes_recomputed"][0]
        route = self.db.query_one("SELECT * FROM route_results WHERE route_id=? AND status='current'", (rid,))
        point_ids = json.loads(route["point_ids_json"])
        self.assertTrue(point_ids)
        # every route point has a cleaning decision under the SAME rule version
        n = self.db.query_one(
            "SELECT COUNT(*) c FROM point_cleaning WHERE rule_version_id=? AND point_id IN (%s)"
            % ",".join("?" * len(point_ids)), [self.rs.rule_version_id, *point_ids])["c"]
        self.assertEqual(n, len(point_ids))
        # metrics and anomalies are version-stamped
        self.assertTrue(self.db.query_one(
            "SELECT 1 FROM route_metrics WHERE route_id=? AND rule_version_id=?",
            (rid, self.rs.rule_version_id)))

    def test_crossday_route_kept_single_when_enabled(self):
        from datetime import datetime, timedelta, timezone
        # track from 16:00Z to 19:00Z -> 00:00..03:00 Shanghai next day
        points = []
        t = datetime(2026, 11, 18, 16, 0, tzinfo=timezone.utc)
        t = move(points, "D2", t, 120, 31.20, 121.47, 0.0001, 0.0001, step=90)
        ingest_payload(self.db, {"tracking_points": points})
        run = run_pipeline(self.db, self.rs)
        self.assertEqual(len(run["routes_recomputed"]), 1)
        rid = run["routes_recomputed"][0]
        route = self.db.query_one("SELECT * FROM route_results WHERE route_id=?", (rid,))
        self.assertEqual(route["service_date"], "2026-11-19")  # local start date
        self.assertEqual(route["route_tz"], "Asia/Shanghai")

    def test_crossday_disabled_splits_at_midnight(self):
        from datetime import datetime, timezone
        # 90s steps x 160 = 4h: 15:30Z -> 19:30Z; Shanghai local 23:30 -> 03:30
        points = []
        t = datetime(2026, 11, 18, 15, 30, tzinfo=timezone.utc)
        t = move(points, "D3", t, 160, 31.20, 121.47, 0.0001, 0.0001, step=90)
        self.assertTrue(str(local_date(t, "Asia/Shanghai")) == "2026-11-19")
        ingest_payload(self.db, {"tracking_points": points})
        params = dict(DEFAULT_PARAMS, crossday_enabled=False)
        from delivery_analytics.rules import make_rule_set
        from delivery_analytics.tzutil import now_utc, to_iso_z
        rs2 = make_rule_set("rv-x", "2.0.0", params, "no crossday",
                            to_iso_z(now_utc()), None)
        self.db.insert_rule_set(rs2.to_dict())  # FK: rule set must exist before stamping
        run = run_pipeline(self.db, rs2)
        self.assertGreaterEqual(len(run["routes_recomputed"]), 2)

    def test_rule_revision_only_recomputes_affected_routes(self):
        order = self._ingest_simple()
        run1 = run_pipeline(self.db, self.rs)
        rid = run1["routes_recomputed"][0]
        score_before = self.db.query_one(
            "SELECT metric_value FROM route_metrics WHERE route_id=? AND metric_key='efficiency_score'",
            (rid,))["metric_value"]

        # metric-scope revision: score weights change -> all routes in scope,
        # but cleaning decisions must remain identical
        result = publish_revision(self.db, {"score_on_time_w": 0.60, "score_empty_km_w": 0.0},
                                  "rebalance scoring weights")
        self.assertIn("metrics", result["changed_scopes"])
        self.assertEqual(result["affected_routes"], [rid])
        self.assertEqual(result["recomputed_routes"], [rid])
        score_after = self.db.query_one(
            "SELECT metric_value FROM route_metrics WHERE route_id=? AND rule_version_id=? "
            "AND metric_key='efficiency_score'", (rid, result["rule_version_id"]))["metric_value"]
        self.assertNotEqual(score_before, score_after)
        # old route row retained as superseded history
        self.assertIsNotNone(self.db.query_one(
            "SELECT 1 FROM route_results WHERE route_id=? AND status='superseded'", (rid,)))
        # revision row records exact scope
        rev = self.db.query_one("SELECT * FROM rule_revisions ORDER BY revision_id DESC LIMIT 1")
        self.assertEqual(json.loads(rev["affected_route_ids_json"]), [rid])

    def test_lateness_revision_skips_routes_without_orders(self):
        from datetime import datetime, timezone
        # route A with an order, route B with no orders
        self._ingest_simple()
        pts = []
        t = datetime(2026, 11, 18, 1, 0, tzinfo=timezone.utc)
        move(pts, "D9", t, 30, 39.90, 116.40, 0.0005, 0.0003)
        ingest_payload(self.db, {"tracking_points": pts})
        run_pipeline(self.db, self.rs)
        result = publish_revision(self.db, {"lateness_tolerance_min": 10.0},
                                  "widen lateness tolerance")
        self.assertEqual(result["changed_scopes"], ["lateness"])
        # only the order-serving route is affected
        rids = set(result["affected_routes"])
        no_order = [r["route_id"] for r in self.db.query(
            "SELECT route_id, order_ids_json FROM route_results WHERE status='current'")
            if not json.loads(r["order_ids_json"])]
        self.assertTrue(all(rid not in rids for rid in no_order))
        self.assertTrue(any(rid in rids for rid in result["recomputed_routes"]))

    def test_data_issue_vs_real_world_separation(self):
        from datetime import datetime, timezone
        # 3-min stationary (below stop threshold), a 12-min silence, then an
        # 8-min stationary dwell: the dwell window is mostly unobserved.
        pts = []
        for i in range(3):
            pts.append({"point_id": f"pre{i}", "driver_id": "D8",
                        "ts_utc": datetime(2026, 11, 18, 1, i, tzinfo=timezone.utc)
                        .isoformat().replace("+00:00", "Z"),
                        "lat": 31.20, "lng": 121.47, "source": "gps"})
        for i in range(8):
            pts.append({"point_id": f"x{i}", "driver_id": "D8",
                        "ts_utc": datetime(2026, 11, 18, 1, 15 + i, tzinfo=timezone.utc)
                        .isoformat().replace("+00:00", "Z"),
                        "lat": 31.20, "lng": 121.47, "source": "gps"})
        ingest_payload(self.db, {"tracking_points": pts})
        run_pipeline(self.db, self.rs)
        cats = {r["category"] for r in self.db.query("SELECT DISTINCT category FROM anomalies")}
        self.assertIn("DATA_ISSUE", cats)
        # evidence embeds point ids and rule params
        ev = json.loads(self.db.query_one(
            "SELECT evidence_json FROM anomalies WHERE anomaly_type='temporal_gap'")["evidence_json"])
        self.assertIn("rule", ev)
        self.assertIn("start_point_id", ev)


if __name__ == "__main__":
    unittest.main()
