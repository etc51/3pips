from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import daily_autonomy_runner as dar  # noqa: E402


class DailyAutonomyEntryWindowPolicyTest(unittest.TestCase):
    def test_activates_positive_entry_window_overlay(self) -> None:
        all_rows = [
            {"secid": "AAA", "family": "AA", "portfolio_group": "classic_core", "contour": "strict", "net_rub": 300.0},
            {"secid": "BBB", "family": "BB", "portfolio_group": "tail_research", "contour": "aggressive", "net_rub": 120.0},
            {"secid": "CCC", "family": "CC", "portfolio_group": "gl_watch", "contour": "strict", "net_rub": 80.0},
        ]
        research_day = [
            {"scenario": "entry_window_1015_1159", "trades": 12, "net_rub": 2967.31},
            {"scenario": "base", "trades": 19, "net_rub": -1720.65},
        ]
        research_all = [
            {"scenario": "entry_window_1015_1159", "trades": 14, "net_rub": 1709.90},
            {"scenario": "base", "trades": 22, "net_rub": -2978.06},
        ]
        research_consensus = [
            {
                "scenario": "base",
                "days": 2,
                "beat_base_days": 0,
                "delta_total_rub": 0.0,
                "latest_day_delta_rub": 0.0,
            }
        ]

        policy = dar.build_auto_policy(
            all_rows=all_rows,
            profiles={},
            trade_date="2026-06-15",
            day_history=[],
            recurring_tickers=[],
            recurring_families=[],
            microstructure_summary=[],
            research_day=research_day,
            research_all=research_all,
            research_consensus=research_consensus,
        )

        active = policy["active"]
        self.assertEqual(active.get("entry_no_trade_before"), "10:15")
        self.assertEqual(active.get("entry_no_new_after"), "11:59")
        self.assertTrue(
            any("entry_window_1015_1159" in note for note in active.get("notes") or []),
            active.get("notes"),
        )


if __name__ == "__main__":
    unittest.main()
