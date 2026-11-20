"""HTTP API + static file server, stdlib only.

Run:  python -m delivery_analytics.server --db ./data/analytics.db --port 8080
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from . import repository
from .database import Database
from .pipeline import run_pipeline
from .rules import DEFAULT_PARAMS, PARAM_CATALOGUE
from .service import IngestionError, ensure_initial_rules, ingest_payload, publish_revision
from .tzutil import local_iso, parse_utc

_FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

# A single database connection guarded by a lock: SQLite + ThreadingHTTPServer.
_STATE: Dict[str, Any] = {"db": None, "lock": threading.RLock()}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _json_response(handler, payload, status: int = 200):
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "DeliveryAnalytics/1.0"

    def log_message(self, fmt, *args):  # quieter logs
        pass

    # ------------------------------------------------------------------ routing
    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            db = _STATE["db"]

            if path == "/api/health":
                return _json_response(self, {"status": "ok"})
            if path == "/api/routes":
                with _STATE["lock"]:
                    return _json_response(self, repository.list_routes(
                        db, include_superseded=qs.get("all") == "1"))
            if path.startswith("/api/routes/"):
                rid = path.split("/")[3]
                with _STATE["lock"]:
                    detail = repository.route_detail(db, rid, qs.get("rule_version_id"))
                if detail is None:
                    raise ApiError(404, f"route {rid} not found")
                return _json_response(self, detail)
            if path == "/api/rules":
                with _STATE["lock"]:
                    rows = db.list_rule_sets()
                    return _json_response(self, [
                        {**dict(r), "params": json.loads(r["params_json"])} for r in rows])
            if path == "/api/rules/catalogue":
                return _json_response(self, {
                    "defaults": DEFAULT_PARAMS,
                    "catalogue": {k: {"default": d, "scope": s, "description": desc}
                                  for k, (d, s, desc) in PARAM_CATALOGUE.items()}})
            if path == "/api/revisions":
                with _STATE["lock"]:
                    return _json_response(self, repository.revision_history(db))
            if path == "/api/cleaning":
                with _STATE["lock"]:
                    return _json_response(self, repository.cleaning_trail(
                        db, qs.get("driver_id"), qs.get("rule_version_id")))
            if path == "/api/dashboard":
                with _STATE["lock"]:
                    return _json_response(self, self._dashboard(db))
            if path == "/api/raw":
                with _STATE["lock"]:
                    return _json_response(self, {
                        "orders": [dict(r) for r in db.query("SELECT * FROM orders")],
                        "tracking_points": [dict(r) for r in db.query("SELECT * FROM tracking_points ORDER BY ts_utc")],
                        "road_events": [dict(r) for r in db.query("SELECT * FROM road_events")],
                        "driver_status": [dict(r) for r in db.query("SELECT * FROM driver_status")],
                    })
            return self._static(path)
        except ApiError as e:
            _json_response(self, {"error": e.message}, e.status)
        except Exception as e:  # noqa: BLE001
            _json_response(self, {"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                raise ApiError(400, "invalid JSON body")
            db = _STATE["db"]
            path = urlparse(self.path).path

            if path == "/api/ingest":
                with _STATE["lock"]:
                    try:
                        counts = ingest_payload(db, payload)
                    except IngestionError as e:
                        raise ApiError(400, str(e))
                    rs = ensure_initial_rules(db)
                    run = run_pipeline(db, rs)
                return _json_response(self, {"ingested": counts, "run": run})

            if path == "/api/recompute":
                with _STATE["lock"]:
                    rs = ensure_initial_rules(db)
                    run = run_pipeline(db, rs, change_note="manual full recomputation")
                return _json_response(self, run)

            if path == "/api/rules/revision":
                patch = payload.get("params") or {}
                note = payload.get("change_note", "")
                if not note:
                    raise ApiError(400, "change_note is required")
                with _STATE["lock"]:
                    try:
                        result = publish_revision(db, patch, note)
                    except ValueError as e:
                        raise ApiError(400, str(e))
                return _json_response(self, result)

            raise ApiError(404, f"unknown endpoint {path}")
        except ApiError as e:
            _json_response(self, {"error": e.message}, e.status)
        except Exception as e:  # noqa: BLE001
            _json_response(self, {"error": f"{type(e).__name__}: {e}"}, 500)

    # ------------------------------------------------------------------ helpers
    def _dashboard(self, db):
        routes = repository.list_routes(db)
        total_anom = {"DATA_ISSUE": 0, "REAL_WORLD": 0}
        scores = []
        for r in routes:
            rows = db.query(
                "SELECT category, COUNT(*) c FROM anomalies WHERE route_id=? AND rule_version_id=? GROUP BY category",
                (r["route_id"], r["rule_version_id"]))
            for row in rows:
                total_anom[row["category"]] = total_anom.get(row["category"], 0) + row["c"]
            if r["efficiency_score"] is not None:
                scores.append(r["efficiency_score"])
        active = db.active_rule_set()
        return {
            "route_count": len(routes),
            "driver_count": len({r["driver_id"] for r in routes}),
            "avg_efficiency_score": round(sum(scores) / len(scores), 1) if scores else None,
            "anomalies_by_category": total_anom,
            "active_rule_version": active["version"] if active else None,
            "rule_revision_count": db.query_one("SELECT COUNT(*) c FROM rule_revisions")["c"],
        }

    def _static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        target = (_FRONTEND_DIR / path.lstrip("/")).resolve()
        if not str(target).startswith(str(_FRONTEND_DIR.resolve())) or not target.is_file():
            raise ApiError(404, f"not found: {path}")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="./data/analytics.db")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    db = Database(args.db)
    db.init_schema()
    _STATE["db"] = db
    ensure_initial_rules(db)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Delivery analytics API on http://{args.host}:{args.port} (db={args.db})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
