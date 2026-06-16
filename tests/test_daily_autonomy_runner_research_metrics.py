from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import daily_autonomy_runner as dar  # noqa: E402


class DailyAutonomyRunnerResearchMetricsTest(unittest.TestCase):
    def test_evaluate_scenario_includes_payout_and_top3_loss_metrics(self) -> None:
        rows = [
            {"net_rub": 100.0, "qty": 1},
            {"net_rub": -50.0, "qty": 1},
            {"net_rub": 200.0, "qty": 1},
            {"net_rub": -150.0, "qty": 1},
            {"net_rub": -300.0, "qty": 1},
        ]

        result = dar.evaluate_scenario("probe", rows, {}, note="test")

        self.assertEqual(result["trades"], 5)
        self.assertEqual(result["expectancy_rub"], -40.0)
        self.assertEqual(result["avg_win_rub"], 150.0)
        self.assertEqual(result["avg_loss_rub"], -166.67)
        self.assertEqual(result["top3_loss_rub"], 500.0)
        self.assertEqual(result["profit_factor"], 0.6)

    def test_build_optimizer_candidates_preserves_payout_and_tail_metrics(self) -> None:
        research_day = [
            {
                "scenario": "contour_only_strict",
                "note": "single signal layer only",
                "trades": 20,
                "wins": 18,
                "losses": 2,
                "win_rate_pct": 90.0,
                "net_rub": 4989.81,
                "expectancy_rub": 249.49,
                "avg_win_rub": 301.17,
                "avg_loss_rub": -216.59,
                "top3_loss_rub": 433.18,
                "profit_factor": 3.7361,
            }
        ]

        rows = dar.build_optimizer_candidates(research_day, [], [])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["scenario"], "contour_only_strict")
        self.assertEqual(rows[0]["avg_win_rub"], 301.17)
        self.assertEqual(rows[0]["avg_loss_rub"], -216.59)
        self.assertEqual(rows[0]["top3_loss_rub"], 433.18)


if __name__ == "__main__":
    unittest.main()
