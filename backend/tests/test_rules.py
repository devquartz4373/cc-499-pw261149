"""Tests for rule versioning, version bumps and selective recalculation scope."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from delivery_analytics.rules import (DEFAULT_PARAMS, bump_version, changed_scopes,
                                      validate_params)


class RuleEngineTests(unittest.TestCase):
    def test_version_bump_semantics(self):
        self.assertEqual(bump_version("1.0.0", {"cleaning"}), "2.0.0")
        self.assertEqual(bump_version("1.2.0", {"stop"}), "2.0.0")
        self.assertEqual(bump_version("1.2.0", {"route_assembly"}), "2.0.0")
        self.assertEqual(bump_version("1.2.0", {"lateness"}), "1.3.0")
        self.assertEqual(bump_version("1.2.3", {"metrics"}), "1.2.4")

    def test_changed_scopes_detects_only_real_changes(self):
        patch = {"jump_speed_kmh": 999, "score_on_time_w": 0.5}
        scopes = changed_scopes(patch, DEFAULT_PARAMS)
        self.assertEqual(scopes, {"cleaning", "metrics"})
        self.assertEqual(changed_scopes({"jump_speed_kmh": DEFAULT_PARAMS["jump_speed_kmh"]},
                                        DEFAULT_PARAMS), set())

    def test_validate_params(self):
        self.assertEqual(validate_params({"stop_radius_m": -1}),
                         ["stop_radius_m must be a non-negative number"])
        self.assertEqual(validate_params({"stop_requires_event": "yes"}),
                         ["stop_requires_event must be boolean"])
        self.assertEqual(validate_params({"nope": 1}), ["unknown parameter: nope"])
        self.assertEqual(validate_params({"crossday_local_tz": "Mars/Olympus"}),
                         ["unknown timezone: Mars/Olympus"])
        self.assertEqual(validate_params({"stop_radius_m": 80.0}), [])
