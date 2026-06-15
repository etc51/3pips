from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import daily_autonomy_runner as dar  # noqa: E402


class DailyAutonomyTickerQuarantinePolicyTest(unittest.TestCase):
    def test_quarantines_destructive_ticker_with_fresh_loss(self) -> None:
        all_rows = [
            {
                "closed_at": "2026-06-14 15:00:00",
                "secid": "BTM6",
                "family": "BT",
                "portfolio_group": "classic_core",
                "contour": "strict",
                "net_rub": -1800.0,
            },
            {
                "closed_at": "2026-06-15 10:32:00",
                "secid": "BTM6",
                "family": "BT",
                "portfolio_group": "classic_core",
                "contour": "strict",
                "net_rub": -985.43,
            },
            {
                "closed_at": "2026-06-15 14:19:00",
                "secid": "BTM6",
                "family": "BT",
                "portfolio_group": "classic_core",
                "contour": "aggressive",
                "net_rub": -985.43,
            },
            {
                "closed_at": "2026-06-15 11:00:00",
                "secid": "PDM6",
                "family": "PD",
                "portfolio_group": "tail_research",
                "contour": "aggressive",
                "net_rub": 400.0,
            },
            {
                "closed_at": "2026-06-15 11:20:00",
                "secid": "PDM6",
                "family": "PD",
                "portfolio_group": "tail_research",
                "contour": "aggressive",
                "net_rub": -500.0,
            },
            {
                "closed_at": "2026-06-15 11:45:00",
                "secid": "PDM6",
                "family": "PD",
                "portfolio_group": "tail_research",
                "contour": "strict",
                "net_rub": 150.0,
            },
        ]

        policy = dar.build_auto_policy(
            all_rows=all_rows,
            profiles={},
            trade_date="2026-06-15",
            day_history=[],
            recurring_tickers=[],
            recurring_families=[],
            microstructure_summary=[],
            research_day=[{"scenario": "base", "trades": 6, "net_rub": -3720.86}],
            research_all=[{"scenario": "base", "trades": 6, "net_rub": -3720.86}],
            research_consensus=[],
        )

        active = policy["active"]
        self.assertIn("BTM6", active.get("observe_only_tickers") or [])
        self.assertNotIn("PDM6", active.get("observe_only_tickers") or [])
        self.assertTrue(
            any("BTM6" in note and "токсичным" in note for note in active.get("notes") or []),
            active.get("notes"),
        )


if __name__ == "__main__":
    unittest.main()
