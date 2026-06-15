from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import daily_autonomy_runner as dar  # noqa: E402


class DailyAutonomyRunnerSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.run_dir = ROOT / "reports" / "paper_runs" / "v7_live_20260525"
        cls.profiles_path = ROOT / "reports" / "futures_scalp_profiles_v7_paper_20260525.csv"
        if not cls.run_dir.exists() or not cls.profiles_path.exists():
            raise unittest.SkipTest("paper runtime artifacts are unavailable")

        cls.profiles = dar.load_profiles(cls.profiles_path)
        cls.all_rows = dar.load_primary_trades(cls.run_dir)
        if not cls.all_rows:
            raise unittest.SkipTest("no paper trades available")
        cls.trade_date = dar.latest_trade_date(cls.all_rows)
        if not cls.trade_date:
            raise unittest.SkipTest("no latest trade date available")

        cls.all_wide_spread_rows = dar.load_wide_spread_reviews(cls.run_dir)
        for row in cls.all_rows:
            row["family"] = dar.family_for_row(row, cls.profiles)
            row["group_key"] = f"{row.get('portfolio_group', '')}/{row.get('contour', '')}"

        cls.day_rows = dar.filter_trade_date(cls.all_rows, cls.trade_date)
        cls.day_history = dar.build_day_history(cls.all_rows, cls.profiles)
        cls.recurring_tickers = dar.build_recurring_killers(cls.day_history, "worst_ticker")
        cls.recurring_families = dar.build_recurring_killers(cls.day_history, "worst_family")
        cls.all_group_family_metrics = dar.metrics_map(
            cls.all_rows,
            lambda row: f"{str(row.get('portfolio_group') or '').upper()}/{str(row.get('contour') or '').upper()}::{str(row.get('family') or '').upper()}",
        )
        cls.microstructure_summary = dar.build_microstructure_summary(cls.all_wide_spread_rows, cls.all_group_family_metrics)
        cls.research_day = dar.build_research_scenarios(cls.all_rows, cls.day_rows, cls.profiles)
        cls.research_all = dar.build_research_scenarios(cls.all_rows, cls.all_rows, cls.profiles)
        cls.scenario_history = dar.build_scenario_history(cls.all_rows, cls.profiles)
        cls.research_consensus = dar.summarize_scenario_history(cls.scenario_history)
        cls.policy = dar.build_auto_policy(
            all_rows=cls.all_rows,
            profiles=cls.profiles,
            trade_date=cls.trade_date,
            day_history=cls.day_history,
            recurring_tickers=cls.recurring_tickers,
            recurring_families=cls.recurring_families,
            microstructure_summary=cls.microstructure_summary,
            research_day=cls.research_day,
            research_all=cls.research_all,
            research_consensus=cls.research_consensus,
        )

    def test_autopromotes_ticker_pause(self) -> None:
        active = self.policy["active"]
        self.assertEqual(active.get("pause_ticker_after_losses"), 1)
        self.assertEqual(active.get("pause_after_loss_minutes"), 120)

    def test_keeps_broad_blackout(self) -> None:
        windows = self.policy["active"].get("entry_blackout_windows") or []
        self.assertIn("12:00-15:59", windows)


if __name__ == "__main__":
    unittest.main()

