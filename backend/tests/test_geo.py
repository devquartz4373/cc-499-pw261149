"""Geo primitive tests."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from delivery_analytics.geo import (bearing_deg, fit_bounds, haversine_m,
                                    polyline_length_m, project_svg,
                                    segment_speed_kmh, valid_latlat)


class GeoTests(unittest.TestCase):
    def test_haversine_known_distance(self):
        # ~111.32 km per degree of latitude at the equator
        d = haversine_m(0.0, 0.0, 1.0, 0.0)
        self.assertAlmostEqual(d, 111_195.0, delta=200)

    def test_zero_distance(self):
        self.assertEqual(haversine_m(31.2, 121.4, 31.2, 121.4), 0.0)

    def test_speed_and_bearing(self):
        self.assertEqual(segment_speed_kmh(1000.0, 0), float("inf"))
        self.assertAlmostEqual(segment_speed_kmh(1000.0, 36.0), 100.0)
        self.assertAlmostEqual(bearing_deg(0, 0, 1, 0), 0.0, places=1)
        self.assertAlmostEqual(bearing_deg(0, 0, 0, 1), 90.0, places=1)

    def test_polyline_and_projection(self):
        pts = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0)]
        self.assertGreater(polyline_length_m(pts), 200_000)
        scale, ox, oy = fit_bounds(pts, 800, 600)
        x, y = project_svg(0.0, 0.0, scale, ox, oy)
        self.assertTrue(0 <= x <= 800 and 0 <= y <= 600)

    def test_valid_latlat(self):
        self.assertTrue(valid_latlat(31.2, 121.4))
        self.assertFalse(valid_latlat(91.0, 0.0))
        self.assertFalse(valid_latlat(0.0, 181.0))


if __name__ == "__main__":
    unittest.main()
