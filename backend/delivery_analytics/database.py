"""SQLite storage layer.

Schema is organised in three layers:

1. Raw inputs (orders, tracking_points, road_events, driver_status) — written
   once and never mutated by the pipeline.
2. Rule versioning (rule_sets, rule_revisions).
3. Derived, version-stamped outputs (route_results, route_metrics, anomalies,
   point_cleaning, stop_events) — every row references the point IDs it was
   computed from and the rule_version_id in force, so any number can be
   retraced. Derived rows are superseded, never silently overwritten.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- raw inputs
CREATE TABLE IF NOT EXISTS orders (
    order_id            TEXT PRIMARY KEY,
    driver_id           TEXT NOT NULL,
    region              TEXT NOT NULL,              -- business region code
    timezone            TEXT NOT NULL,              -- IANA tz of the order region
    origin_lat          REAL NOT NULL,
    origin_lng          REAL NOT NULL,
    dest_lat            REAL NOT NULL,
    dest_lng            REAL NOT NULL,
    planned_distance_m  REAL,
    promised_start_utc  TEXT NOT NULL,              -- ISO-8601 Z, absolute
    promised_end_utc    TEXT NOT NULL,
    planned_dwell_min   REAL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_driver ON orders(driver_id);

CREATE TABLE IF NOT EXISTS tracking_points (
    point_id        TEXT PRIMARY KEY,
    driver_id       TEXT NOT NULL,
    ts_utc          TEXT NOT NULL,                  -- absolute UTC instant
    lat             REAL,                           -- nullable: missing fix
    lng             REAL,
    accuracy_m      REAL,
    source          TEXT NOT NULL DEFAULT 'gps',    -- gps|app|manual
    raw_quality     TEXT NOT NULL DEFAULT 'raw',    -- raw|missing
    ingested_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_points_driver_ts
    ON tracking_points(driver_id, ts_utc);

CREATE TABLE IF NOT EXISTS road_events (
    event_id        TEXT PRIMARY KEY,
    event_type      TEXT NOT NULL,                  -- accident|closure|congestion|construction|checkpoint
    severity        TEXT NOT NULL DEFAULT 'info',   -- info|warning|critical
    lat             REAL NOT NULL,
    lng             REAL NOT NULL,
    radius_m        REAL NOT NULL DEFAULT 500,
    start_utc       TEXT NOT NULL,
    end_utc         TEXT,                           -- open-ended if null
    description     TEXT
);

CREATE TABLE IF NOT EXISTS driver_status (
    status_id       TEXT PRIMARY KEY,
    driver_id       TEXT NOT NULL,
    status          TEXT NOT NULL,                  -- DRIVING|ON_BREAK|OFF_DUTY|LOADING|SERVICE
    start_utc       TEXT NOT NULL,
    end_utc         TEXT,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_status_driver ON driver_status(driver_id, start_utc);

-- ------------------------------------------------------------- rule versioning
CREATE TABLE IF NOT EXISTS rule_sets (
    rule_version_id TEXT PRIMARY KEY,
    version         TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL,                  -- draft|active|retired
    params_json     TEXT NOT NULL,
    change_note     TEXT NOT NULL,
    parent_version_id TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_revisions (
    revision_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_version_id TEXT NOT NULL REFERENCES rule_sets(rule_version_id),
    parent_version_id TEXT,
    changed_params_json TEXT NOT NULL,
    changed_scopes_json TEXT NOT NULL,
    affected_route_ids_json TEXT NOT NULL,          -- exact recalc scope
    recomputed_route_ids_json TEXT NOT NULL,
    change_note     TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- ------------------------------------------------------------- derived outputs
CREATE TABLE IF NOT EXISTS point_cleaning (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id            TEXT NOT NULL REFERENCES tracking_points(point_id),
    rule_version_id     TEXT NOT NULL REFERENCES rule_sets(rule_version_id),
    driver_id           TEXT NOT NULL,
    ts_utc              TEXT NOT NULL,
    decision            TEXT NOT NULL,  -- accepted|duplicate|gps_jump|missing_fix
    reason              TEXT NOT NULL,  -- machine-readable rule reason
    evidence_json       TEXT NOT NULL,  -- neighbour point ids, distance/speed...
    UNIQUE(point_id, rule_version_id)
);
CREATE INDEX IF NOT EXISTS idx_clean_version ON point_cleaning(rule_version_id);
CREATE INDEX IF NOT EXISTS idx_clean_driver ON point_cleaning(driver_id, rule_version_id);

CREATE TABLE IF NOT EXISTS route_results (
    route_id            TEXT NOT NULL,
    rule_version_id     TEXT NOT NULL REFERENCES rule_sets(rule_version_id),
    driver_id           TEXT NOT NULL,
    service_date        TEXT NOT NULL,              -- local date in route tz
    route_tz            TEXT NOT NULL,
    start_utc           TEXT NOT NULL,
    end_utc             TEXT NOT NULL,
    order_ids_json      TEXT NOT NULL,
    point_ids_json      TEXT NOT NULL,              -- full traceability chain
    status              TEXT NOT NULL,              -- current|superseded
    parent_route_id     TEXT,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (route_id, rule_version_id)
);
CREATE INDEX IF NOT EXISTS idx_routes_current ON route_results(driver_id, status);

CREATE TABLE IF NOT EXISTS stop_events (
    stop_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id            TEXT NOT NULL,
    rule_version_id     TEXT NOT NULL,
    stop_kind           TEXT NOT NULL,  -- real_stop|suspected_data_gap|break_stop
    lat                 REAL NOT NULL,
    lng                 REAL NOT NULL,
    enter_utc           TEXT NOT NULL,
    exit_utc            TEXT NOT NULL,
    duration_s          INTEGER NOT NULL,
    observed_coverage   REAL NOT NULL,  -- fraction of window with accepted fixes
    point_ids_json      TEXT NOT NULL,
    evidence_json       TEXT NOT NULL,  -- driver status / events / order linkage
    UNIQUE(route_id, rule_version_id, enter_utc)
);

CREATE TABLE IF NOT EXISTS anomalies (
    anomaly_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id            TEXT NOT NULL,
    rule_version_id     TEXT NOT NULL,
    category            TEXT NOT NULL,  -- DATA_ISSUE | REAL_WORLD
    anomaly_type        TEXT NOT NULL,
    severity            TEXT NOT NULL,  -- info|warning|critical
    start_utc           TEXT,
    end_utc             TEXT,
    lat                 REAL,
    lng                 REAL,
    message             TEXT NOT NULL,
    evidence_json       TEXT NOT NULL,  -- point ids + measurements + rule params
    related_order_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_anomaly_route ON anomalies(route_id, rule_version_id);

CREATE TABLE IF NOT EXISTS route_metrics (
    route_id            TEXT NOT NULL,
    rule_version_id     TEXT NOT NULL,
    metric_key          TEXT NOT NULL,
    metric_value        REAL,                        -- nullable = not applicable
    unit                TEXT NOT NULL,
    evidence_json       TEXT NOT NULL,
    PRIMARY KEY (route_id, rule_version_id, metric_key)
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        # Shared across ThreadingHTTPServer workers; all access is serialised
        # by the server's RLock, so cross-thread use with WAL is safe.
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------- generic helpers
    def query(self, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> None:
        self.conn.executemany(sql, [tuple(p) for p in seq])

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------- rule sets
    def active_rule_set(self) -> Optional[sqlite3.Row]:
        return self.query_one(
            "SELECT * FROM rule_sets WHERE status='active' ORDER BY created_at DESC LIMIT 1"
        )

    def get_rule_set(self, rule_version_id: str) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM rule_sets WHERE rule_version_id=?",
                              (rule_version_id,))

    def list_rule_sets(self) -> List[sqlite3.Row]:
        return self.query("SELECT * FROM rule_sets ORDER BY created_at")

    def insert_rule_set(self, rs: Dict[str, Any]) -> None:
        self.execute(
            """INSERT INTO rule_sets(rule_version_id, version, status, params_json,
               change_note, parent_version_id, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (rs["rule_version_id"], rs["version"], rs["status"],
             json.dumps(rs["params"], ensure_ascii=False), rs["change_note"],
             rs["parent_version_id"], rs["created_at"]),
        )

    def retire_rule_set(self, rule_version_id: str) -> None:
        self.execute("UPDATE rule_sets SET status='retired' WHERE rule_version_id=?",
                     (rule_version_id,))

    @staticmethod
    def row_rule_params(row: sqlite3.Row) -> Dict[str, Any]:
        return json.loads(row["params_json"])
