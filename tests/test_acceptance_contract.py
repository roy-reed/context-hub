from __future__ import annotations

import unittest

from scripts.run_acceptance import _median_within_limit, _timing_summary


class AcceptanceContractTest(unittest.TestCase):
    def test_cold_start_median_ignores_one_runner_spike(self) -> None:
        samples = [480.0, 2_500.0, 510.0]

        self.assertTrue(_median_within_limit(samples, 1_000.0))
        self.assertEqual(_timing_summary(samples)["p50_ms"], 510.0)

    def test_cold_start_median_rejects_sustained_regression(self) -> None:
        samples = [999.0, 1_001.0, 1_002.0]

        self.assertFalse(_median_within_limit(samples, 1_000.0))


if __name__ == "__main__":
    unittest.main()
