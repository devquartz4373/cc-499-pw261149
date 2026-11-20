"""Timezone / cross-region / DST verification tests."""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from delivery_analytics.tzutil import (format_offset, local_date, local_dt,
                                       parse_utc, to_iso_z, utc_offset_minutes)


class TzTests(unittest.TestCase):
    def test_utc_absolute_storage(self):
        dt = parse_utc("2026-11-18T01:00:00Z")
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual(to_iso_z(dt), "2026-11-18T01:00:00Z")
        # offset-bearing input normalises to the same UTC instant
        self.assertEqual(parse_utc("2026-11-18T09:00:00+08:00"), dt)

    def test_shanghai_vs_urumqi_labels(self):
        dt = parse_utc("2026-11-18T04:00:00Z")
        self.assertEqual(local_dt(dt, "Asia/Shanghai").strftime("%H:%M"), "12:00")  # +8
        self.assertEqual(local_dt(dt, "Asia/Urumqi").strftime("%H:%M"), "10:00")   # +6
        self.assertEqual(format_offset(utc_offset_minutes(dt, "Asia/Shanghai")), "UTC+08:00")

    def test_dst_fall_back_offset_flip(self):
        # US 2026 fall-back is exactly 06:00Z: 05:30Z is EDT(-4), 06:30Z is EST(-5)
        before = parse_utc("2026-11-01T05:30:00Z")
        after = parse_utc("2026-11-01T06:30:00Z")
        self.assertEqual(utc_offset_minutes(before, "America/New_York"), -4 * 60)
        self.assertEqual(utc_offset_minutes(after, "America/New_York"), -5 * 60)
        # the 01:00 local hour is repeated
        self.assertEqual(local_dt(before, "America/New_York").strftime("%H"), "01")
        self.assertEqual(local_dt(after, "America/New_York").strftime("%H"), "01")

    def test_local_midnight_service_date(self):
        # 2026-11-18T17:00Z = 2026-11-19 01:00 Shanghai -> service date rolls over
        dt = parse_utc("2026-11-18T17:00:00Z")
        self.assertEqual(str(local_date(dt, "Asia/Shanghai")), "2026-11-19")
        self.assertEqual(str(local_date(dt, "UTC")), "2026-11-18")

    def test_chicago_vs_new_york_same_instant(self):
        dt = parse_utc("2026-11-01T22:09:00Z")  # post fall-back
        self.assertEqual(local_dt(dt, "America/New_York").strftime("%Z%z"), "EST-0500")
        self.assertEqual(local_dt(dt, "America/Chicago").strftime("%Z%z"), "CST-0600")

    def test_rejects_naive_timestamp(self):
        with self.assertRaises(ValueError):
            parse_utc("2026-11-18T01:00:00")
