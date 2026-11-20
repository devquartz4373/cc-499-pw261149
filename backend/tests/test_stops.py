"""Tests for stop classification: real dwell vs logging pause (停留误判)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from delivery_analytics.cleaning import clean_points, Gap
from delivery_analytics.rules import DEFAULT_PARAMS
from delivery_analytics.stops import classify_stops


def raw_track():
    pts = [{"point_id": "p0", "driver_id": "D1", "ts_utc": "2026-11-18T01:00:00Z",
            "lat": 31.20, "lng": 121.47, "source": "gps"}]
    # 8 minutes of stationary fixes (45s step)
    for i in range(1, 11):
        pts.append({"point_id": f"s{i}", "driver_id": "D1",
                    "ts_utc": f"2026-11-18T01:{i:02d}:00Z",
                    "lat": 31.20 + 0.00002 * (i % 2), "lng": 121.47, "source": "gps"})
    return pts


class StopTests(unittest.TestCase):
    def setUp(self):
        self.params = dict(DEFAULT_PARAMS)

    def _accepted(self, raw):
        return clean_points(raw, self.params).accepted

    def test_fully_observed_dwell_is_real_stop(self):
        acc = self._accepted(raw_track())
        stops = classify_stops(acc, [], self.params)
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0].stop_kind, "real_stop")
        self.assertEqual(stops[0].observed_coverage, 1.0)
        self.assertGreaterEqual(stops[0].duration_s, 5 * 60)

    def test_dwell_with_large_gap_is_data_issue_not_stop(self):
        raw = raw_track()
        # punch a 6-minute unobserved hole into the middle of the dwell
        acc = self._accepted(raw)
        hole = Gap(kind="temporal_gap", start_point_id="s4", end_point_id="s9",
                   start_ts="2026-11-18T01:04:00Z", end_ts="2026-11-18T01:10:00Z",
                   duration_s=360, distance_m=0.0, note="test hole")
        stops = classify_stops(acc, [hole], self.params)
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0].stop_kind, "suspected_data_gap")
        self.assertLess(stops[0].observed_coverage, 1.0 - self.params["stop_max_gap_ratio"])

    def test_short_dwell_ignored(self):
        raw = [{"point_id": "a", "driver_id": "D1", "ts_utc": "2026-11-18T01:00:00Z",
                "lat": 31.20, "lng": 121.47, "source": "gps"},
               {"point_id": "b", "driver_id": "D1", "ts_utc": "2026-11-18T01:02:00Z",
                "lat": 31.20001, "lng": 121.47, "source": "gps"}]
        stops = classify_stops(self._accepted(raw), [], self.params)
        self.assertEqual(stops, [])

    def test_break_status_corroborates_break_stop(self):
        acc = self._accepted(raw_track())
        statuses = [{"status_id": "st1", "driver_id": "D1", "status": "ON_BREAK",
                     "start_utc": "2026-11-18T01:02:00Z", "end_utc": "2026-11-18T01:08:00Z",
                     "note": "break"}]
        stops = classify_stops(acc, [], self.params, driver_statuses=statuses)
        self.assertEqual(stops[0].stop_kind, "break_stop")
        self.assertEqual(stops[0].evidence["driver_status"]["status_id"], "st1")

    def test_data_gap_outranks_break_corroboration(self):
        """Even with an ON_BREAK, an unobserved dwell window stays a DATA_ISSUE."""
        raw = raw_track()
        acc = self._accepted(raw)
        hole = Gap(kind="temporal_gap", start_point_id="s4", end_point_id="s9",
                   start_ts="2026-11-18T01:04:00Z", end_ts="2026-11-18T01:10:00Z",
                   duration_s=360, distance_m=0.0, note="hole")
        statuses = [{"status_id": "st1", "driver_id": "D1", "status": "ON_BREAK",
                     "start_utc": "2026-11-18T01:00:00Z", "end_utc": "2026-11-18T01:11:00Z",
                     "note": "break"}]
        stops = classify_stops(acc, [hole], self.params, driver_statuses=statuses)
        self.assertEqual(stops[0].stop_kind, "suspected_data_gap")

    def test_requires_event_rejects_uncorroborated_dwell(self):
        params = dict(self.params, stop_requires_event=True)
        acc = self._accepted(raw_track())
        stops = classify_stops(acc, [], params)
        self.assertEqual(stops[0].stop_kind, "suspected_data_gap")


if __name__ == "__main__":
    unittest.main()
