"""Unit tests for the cleaning stage."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from delivery_analytics.cleaning import clean_points
from delivery_analytics.rules import DEFAULT_PARAMS


def pt(pid, ts, lat=31.20, lng=121.47, source="gps"):
    return {"point_id": pid, "driver_id": "D1", "ts_utc": ts,
            "lat": lat, "lng": lng, "source": source}


class CleaningTests(unittest.TestCase):
    def setUp(self):
        self.params = dict(DEFAULT_PARAMS)

    def test_exact_and_spatial_duplicates(self):
        raw = [
            pt("a", "2026-11-18T01:00:00Z"),
            pt("b", "2026-11-18T01:00:30Z"),                                   # exact dup
            pt("c", "2026-11-18T01:01:00Z", lat=31.2001, lng=121.4701),       # spatial dup
        ]
        res = clean_points(raw, self.params)
        decisions = {p.point_id: p.decision for p in res.points}
        self.assertEqual(decisions, {"a": "accepted", "b": "duplicate", "c": "duplicate"})
        self.assertEqual(res.points[1].reason, "exact_duplicate")
        self.assertEqual(res.points[2].reason, "spatial_duplicate")
        self.assertEqual(len(res.accepted), 1)

    def test_gps_jump_flagged_and_does_not_poison_anchor(self):
        raw = [
            pt("a", "2026-11-18T01:00:00Z"),
            pt("jump", "2026-11-18T01:00:45Z", lat=40.0, lng=-74.0),          # ~12000 km away
            pt("b", "2026-11-18T01:03:30Z", lat=31.2002, lng=121.4702),  # outside dedup window
        ]
        res = clean_points(raw, self.params)
        d = {p.point_id: p.decision for p in res.points}
        self.assertEqual(d["jump"], "gps_jump")
        self.assertEqual(d["b"], "accepted")  # measured against 'a', not the jump
        self.assertIn("speed_kmh", res.points[1].evidence)

    def test_missing_fix_run_emits_gap(self):
        raw = [
            pt("a", "2026-11-18T01:00:00Z"),
            {**pt("m1", "2026-11-18T01:00:45Z"), "lat": None, "lng": None},
            {**pt("m2", "2026-11-18T01:01:30Z"), "lat": None, "lng": None},
            pt("b", "2026-11-18T01:02:15Z"),
        ]
        res = clean_points(raw, self.params)
        self.assertEqual([p.decision for p in res.points],
                         ["accepted", "missing_fix", "missing_fix", "accepted"])
        self.assertEqual(len(res.gaps), 1)
        self.assertEqual(res.gaps[0].kind, "missing_fix_run")
        self.assertEqual(res.gaps[0].start_point_id, "m1")
        self.assertEqual(res.gaps[0].end_point_id, "b")

    def test_temporal_gap_between_accepted_fixes(self):
        raw = [
            pt("a", "2026-11-18T01:00:00Z"),
            pt("b", "2026-11-18T01:20:00Z", lat=31.22, lng=121.50),
        ]
        res = clean_points(raw, self.params)
        self.assertEqual([p.decision for p in res.points], ["accepted", "accepted"])
        self.assertEqual(len(res.gaps), 1)
        self.assertEqual(res.gaps[0].kind, "temporal_gap")
        self.assertAlmostEqual(res.gaps[0].duration_s, 1200.0)

    def test_deterministic_under_same_rules(self):
        raw = [pt(f"p{i}", f"2026-11-18T01:{i:02d}:00Z",
                  lat=31.2 + i * 0.001, lng=121.47 + i * 0.001) for i in range(5)]
        r1 = clean_points(raw, self.params)
        r2 = clean_points(raw, self.params)
        self.assertEqual([(p.point_id, p.decision, p.reason) for p in r1.points],
                         [(p.point_id, p.decision, p.reason) for p in r2.points])

    def test_rule_change_moves_threshold(self):
        raw = [
            pt("a", "2026-11-18T01:00:00Z"),
            pt("b", "2026-11-18T01:00:45Z", lat=31.2003, lng=121.4703),  # ~44m away
        ]
        strict = dict(self.params, dedup_radius_m=30.0)
        loose = dict(self.params, dedup_radius_m=80.0)
        self.assertEqual(clean_points(raw, strict).points[1].decision, "accepted")
        self.assertEqual(clean_points(raw, loose).points[1].decision, "duplicate")


if __name__ == "__main__":
    unittest.main()
