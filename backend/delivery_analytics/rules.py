"""Versioned rule engine.

A *rule set* is an immutable, named, parameterised version (semver-ish
``major.minor.patch``) that every derived record (route result / anomaly)
references. Rules are pure data — the same rule_version_id always produces the
same output for the same input, which makes results fully traceable and
re-runs deterministic.

Revision flow
-------------
``publish_rule_set`` clones the latest active version, applies a patch and
marks the new version active. Each parameter carries a ``scope`` naming the
pipeline stage it influences; ``affected_routes`` uses those scopes to decide
which existing route results must be recomputed:

* global scopes (``cleaning``, ``stop``, ``route_assembly``, ``metrics``)
  invalidate every route;
* the ``lateness`` scope only invalidates routes whose orders actually carry
  promised windows — a pure scoring-weight change cannot flip a cleaning flag.

Superseded rows are never deleted: they stay queryable as revision history.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Parameter catalogue: name -> (default, scope, description)
# scopes drive selective recalculation
# ---------------------------------------------------------------------------
PARAM_CATALOGUE: Dict[str, Tuple[Any, str, str]] = {
    # --- cleaning: duplicate / jump / missing-gap detection on raw points ---
    "dedup_radius_m": (
        30.0, "cleaning",
        "Points within this radius and dedup_window_s are treated as duplicates.",
    ),
    "dedup_window_s": (
        120.0, "cleaning",
        "Time window for spatial duplicate collapsing (seconds).",
    ),
    "jump_speed_kmh": (
        150.0, "cleaning",
        "Segment speed above this marks the arriving point as a GPS jump outlier.",
    ),
    "missing_gap_min": (
        10.0, "cleaning",
        "Consecutive accepted points farther apart than this are a data gap.",
    ),
    # --- stop classification: real dwell vs logging pause -------------------
    "stop_radius_m": (
        100.0, "stop",
        "Cluster radius defining a stationary dwell.",
    ),
    "stop_min_dwell_min": (
        5.0, "stop",
        "Minimum dwell inside stop_radius_m to count as a real stop.",
    ),
    "stop_max_gap_ratio": (
        0.5, "stop",
        "If unobserved gap time exceeds this fraction of the dwell window the "
        "stop is unprovable and reported as a data gap, not a stop.",
    ),
    "stop_requires_event": (
        False, "stop",
        "When true, a dwell only counts as a delivery stop with a matching "
        "road event / driver ON_BREAK / order arrival inside the window.",
    ),
    # --- cross-day route assembly -------------------------------------------
    "route_break_gap_min": (
        30.0, "route_assembly",
        "Gap longer than this (after cleaning) splits one driver track into "
        "separate route segments.",
    ),
    "crossday_enabled": (
        True, "route_assembly",
        "Allow a route segment to span local-midnight instead of splitting by day.",
    ),
    "crossday_local_tz": (
        "Asia/Shanghai", "route_assembly",
        "Timezone used to label route service dates (overridable per order region).",
    ),
    # --- metrics -------------------------------------------------------------
    "arrival_geofence_m": (
        150.0, "lateness",
        "First accepted fix within this radius of an order destination counts "
        "as the arrival instant.",
    ),
    "lateness_tolerance_min": (
        0.0, "lateness",
        "Arrival this many minutes after a promised window still counts on time.",
    ),
    "lateness_early_grace_min": (
        15.0, "lateness",
        "Arrivals earlier than the window by more than this are flagged early.",
    ),
    "score_on_time_w": (0.40, "metrics", "Score weight: on-time delivery ratio."),
    "score_dwell_w": (0.25, "metrics", "Score weight: dwell efficiency vs planned."),
    "score_empty_km_w": (0.20, "metrics", "Score weight: empty-running distance share."),
    "score_anomaly_w": (0.15, "metrics", "Score weight: anomaly penalty."),
}

DEFAULT_PARAMS: Dict[str, Any] = {k: v[0] for k, v in PARAM_CATALOGUE.items()}
PARAM_SCOPES: Dict[str, str] = {k: v[1] for k, v in PARAM_CATALOGUE.items()}
SCOPES = ("cleaning", "stop", "route_assembly", "lateness", "metrics")


@dataclass
class RuleSet:
    rule_version_id: str
    version: str
    status: str  # draft | active | retired
    params: Dict[str, Any]
    change_note: str
    created_at: str
    parent_version_id: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_version_id": self.rule_version_id,
            "version": self.version,
            "status": self.status,
            "params": dict(self.params),
            "change_note": self.change_note,
            "created_at": self.created_at,
            "parent_version_id": self.parent_version_id,
        }


def bump_version(prev: str, changed_scopes: set) -> str:
    """Major bump when cleaning/assembly semantics change, else minor. Patch for
    pure metric tuning."""
    major, minor, patch = (int(x) for x in prev.split("."))
    if changed_scopes & {"cleaning", "route_assembly", "stop"}:
        return f"{major + 1}.0.0"
    if "lateness" in changed_scopes:
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def validate_params(patch: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    for key, val in patch.items():
        if key not in PARAM_CATALOGUE:
            errors.append(f"unknown parameter: {key}")
            continue
        default, _, _ = PARAM_CATALOGUE[key]
        if isinstance(default, bool):
            if not isinstance(val, bool):
                errors.append(f"{key} must be boolean")
        elif isinstance(default, int):
            if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                errors.append(f"{key} must be a non-negative integer")
        elif isinstance(default, float):
            if not isinstance(val, (int, float)) or isinstance(val, bool) or val < 0:
                errors.append(f"{key} must be a non-negative number")
        elif isinstance(default, str):
            if not isinstance(val, str) or not val.strip():
                errors.append(f"{key} must be a non-empty string")
    if "crossday_local_tz" in patch:
        from .tzutil import is_valid_tz
        if isinstance(patch["crossday_local_tz"], str) and not is_valid_tz(patch["crossday_local_tz"]):
            errors.append(f"unknown timezone: {patch['crossday_local_tz']}")
    return errors


def changed_scopes(patch: Dict[str, Any], old_params: Dict[str, Any]) -> set:
    out = set()
    for key, val in patch.items():
        if key in PARAM_SCOPES and old_params.get(key) != val:
            out.add(PARAM_SCOPES[key])
    return out


def make_rule_set(rule_version_id: str, version: str, params: Dict[str, Any],
                  change_note: str, created_at: str,
                  parent_version_id: Optional[str], status: str = "active") -> RuleSet:
    merged = dict(DEFAULT_PARAMS)
    merged.update(params)
    return RuleSet(rule_version_id, version, status, merged, change_note,
                   created_at, parent_version_id)


def scope_of_params(param_names) -> set:
    return {PARAM_SCOPES[n] for n in param_names if n in PARAM_SCOPES}
