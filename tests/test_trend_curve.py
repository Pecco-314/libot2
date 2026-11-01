import unittest

import numpy as np

from src.common.trend_curve import smooth_trend


class TrendCurveTests(unittest.TestCase):
    def test_shape_preserved_across_jumps_and_irregular_samples(self):
        cases = [
            ([0, 1, 2, 3, 4, 5], [100, 100, 10000, 100, 100, 100]),
            ([0, 1, 1.001, 10, 20], [1000, 1000, 0, 0, 500]),
            ([0, 1, 2, 3, 4], [0, 10, 10000, 10001, 10001]),
            ([0, 1, 2, 3], [10, 10, 10, 10]),
            ([0, 1, 2], [0, 1000, 0]),
        ]
        for x, y in cases:
            with self.subTest(y=y):
                sx, sy = smooth_trend(x, y)
                np.testing.assert_allclose(sy[np.searchsorted(sx, x)], y)
                for i in range(len(x) - 1):
                    section = sy[(sx >= x[i]) & (sx <= x[i + 1])]
                    self.assertGreaterEqual(section.min(), min(y[i:i + 2]) - 1e-8)
                    self.assertLessEqual(section.max(), max(y[i:i + 2]) + 1e-8)
                    direction = np.sign(y[i + 1] - y[i])
                    self.assertTrue(np.all(np.diff(section) * direction >= -1e-8))

    def test_dense_data_preserves_every_peak(self):
        x = np.arange(1000)
        y = np.zeros(1000)
        y[503] = 10000
        sx, sy = smooth_trend(x, y)
        self.assertEqual(sy[np.searchsorted(sx, 503)], 10000)
        self.assertEqual(sy.max(), 10000)

    def test_two_points_remain_linear(self):
        x, y = smooth_trend([1, 2], [100, 200])
        np.testing.assert_array_equal(x, [1, 2])
        np.testing.assert_array_equal(y, [100, 200])
