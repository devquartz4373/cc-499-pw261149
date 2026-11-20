"""HTTP API integration tests: real server on an ephemeral port + urllib."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, json.loads(r.read().decode())


def _post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


class ApiTests(unittest.TestCase):
    proc = None
    port = None
    base = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.tmp.close()
        cls.port = _free_port()
        env = dict(os.environ)
        cls.proc = subprocess.Popen(
            [sys.executable, "-I", str(BACKEND / "run_server.py"),
             "--db", cls.tmp.name, "--port", str(cls.port), "--host", "127.0.0.1"],
            cwd=BACKEND, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        cls.base = f"http://127.0.0.1:{cls.port}"
        for _ in range(50):
            try:
                _get(cls.base + "/api/health")
                break
            except Exception:
                time.sleep(0.2)
        else:
            cls.proc.terminate()
            raise RuntimeError("server did not start")
        # load seed data through the same generator the app ships with
        seed = subprocess.run(
            [sys.executable, "-I", str(BACKEND / "seed_data.py"), "--db", cls.tmp.name],
            cwd=BACKEND, capture_output=True, text=True, timeout=60)
        if seed.returncode != 0:
            raise RuntimeError(seed.stdout + seed.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        for suffix in ("", "-wal", "-shm"):
            p = cls.tmp.name + suffix
            if os.path.exists(p):
                os.remove(p)

    def test_health_dashboard_routes(self):
        _, health = _get(self.base + "/api/health")
        self.assertEqual(health["status"], "ok")
        _, dash = _get(self.base + "/api/dashboard")
        self.assertGreaterEqual(dash["route_count"], 3)
        _, routes = _get(self.base + "/api/routes")
        self.assertEqual(len(routes), 3)
        self.assertTrue(all("efficiency_score" in r for r in routes))

    def test_route_detail_traceability_chain(self):
        _, routes = _get(self.base + "/api/routes")
        rid = routes[0]["route_id"]
        _, d = _get(self.base + f"/api/routes/{rid}")
        for key in ("points", "anomalies", "metrics", "stops", "orders",
                    "rule_set", "version_history"):
            self.assertIn(key, d)
        self.assertTrue(d["points"])
        # every point carries a cleaning decision + evidence
        self.assertTrue(all(p["decision"] for p in d["points"]))
        # anomalies split cleanly into the two evidence categories
        cats = {a["category"] for a in d["anomalies"]}
        self.assertTrue(cats.issubset({"DATA_ISSUE", "REAL_WORLD"}))
        # metrics carry evidence for retracing
        self.assertTrue(all("evidence" in m for m in d["metrics"]))

    def test_historical_version_query(self):
        _, routes = _get(self.base + "/api/routes")
        rid = routes[0]["route_id"]
        _, d = _get(self.base + f"/api/routes/{rid}?rule_version_id=rv-0001")
        self.assertEqual(d["rule_set"]["rule_version_id"], "rv-0001")
        # rv-0001 is the baseline: current only before any revision, superseded after
        self.assertIn(d["route"]["status"], {"current", "superseded"})
        self.assertEqual(d["rule_set"]["version"], "1.0.0")

    def test_revision_selective_recalc_and_history(self):
        status, body = _post(self.base + "/api/rules/revision", {
            "params": {"score_on_time_w": 0.5}, "change_note": "api test weight tweak"})
        self.assertEqual(status, 200)
        self.assertIn("metrics", body["changed_scopes"])
        self.assertTrue(body["affected_routes"])
        _, revs = _get(self.base + "/api/revisions")
        self.assertTrue(any(r["rule_version_id"] == body["rule_version_id"] for r in revs))
        latest = revs[-1]
        self.assertEqual(latest["affected_route_ids"], latest["recomputed_route_ids"])

    def test_revision_validation(self):
        status, body = _post(self.base + "/api/rules/revision", {
            "params": {"stop_radius_m": -9}, "change_note": "bad"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        status, body = _post(self.base + "/api/rules/revision",
                             {"params": {"stop_radius_m": 90}})
        self.assertEqual(status, 400)  # missing change_note

    def test_static_and_path_traversal(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as r:
            self.assertEqual(r.status, 200)
            self.assertIn("text/html", r.headers["Content-Type"])
        with urllib.request.urlopen(self.base + "/app.js", timeout=10) as r:
            self.assertEqual(r.status, 200)
        try:
            urllib.request.urlopen(self.base + "/../delivery_analytics/database.py", timeout=10)
            self.fail("traversal should be blocked")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_ingest_validation(self):
        status, body = _post(self.base + "/api/ingest", {"orders": [{"order_id": "X"}]})
        self.assertEqual(status, 400)
        self.assertIn("missing fields", body["error"])

    def test_cleaning_trail_endpoint(self):
        _, rows = _get(self.base + "/api/cleaning")
        self.assertTrue(rows)
        decisions = {r["decision"] for r in rows}
        self.assertIn("accepted", decisions)
        self.assertTrue(all("evidence" in r for r in rows))


if __name__ == "__main__":
    unittest.main()
