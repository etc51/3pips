from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import daily_autonomy_runner as dar  # noqa: E402


class DailyAutonomyEntryShadowGatePolicyTest(unittest.TestCase):
    def test_strategy_review_candidates_flow_into_proposed_policy(self) -> None:
        auto_policy = {
            "active": {"notes": []},
            "active_base": {"notes": []},
            "proposed": {"notes": []},
        }
        strategy_review = {
            "candidates": [
                {
                    "candidate": "entry_shadow_gate::CLASSIC_CORE/STRICT/tv_ema_rsi_adx_trend",
                    "portfolio_group": "CLASSIC_CORE",
                    "contour": "STRICT",
                    "model": "tv_ema_rsi_adx_trend",
                    "trades": 5,
                    "delta_vs_base_rub": 1600.0,
                    "model_net_rub": 2100.0,
                    "skipped_losses": 3,
                    "skipped_wins": 1,
                    "note": "best gate for classic_core/strict",
                }
            ]
        }

        updated = dar.apply_strategy_review_candidates(auto_policy, strategy_review)
        candidate_rows = updated["proposed"]["candidate_entry_shadow_gate_rows"]

        self.assertEqual(len(candidate_rows), 1)
        self.assertEqual(candidate_rows[0]["portfolio_group"], "CLASSIC_CORE")
        self.assertEqual(candidate_rows[0]["contour"], "STRICT")
        self.assertEqual(candidate_rows[0]["model"], "tv_ema_rsi_adx_trend")
        self.assertEqual(candidate_rows[0]["promote_after_days"], 2)
        self.assertTrue(any("Entry-shadow runtime gate candidates:" in note for note in updated["proposed"]["notes"]))

        restriction_rows = dar.build_restriction_rows(updated)
        self.assertTrue(
            any(
                row["restriction_type"] == "candidate_entry_shadow_gate_group_model"
                and row["value"] == "CLASSIC_CORE/STRICT::tv_ema_rsi_adx_trend"
                for row in restriction_rows
            )
        )


if __name__ == "__main__":
    unittest.main()
