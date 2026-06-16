from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import daily_autonomy_runner as dar  # noqa: E402


class DailyAutonomyStrategyReviewTest(unittest.TestCase):
    def test_build_strategy_review_summarizes_entry_shadow_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            run_dir = project_root / "reports" / "paper_runs" / "v7_live_20260525"
            research_dir = project_root / "reports" / "autonomy" / "research" / "2026-06-15"
            run_dir.mkdir(parents=True)
            research_dir.mkdir(parents=True)

            entry_shadow_path = run_dir / "classic_core_entry_shadow_models.csv"
            entry_shadow_path.write_text(
                "\n".join(
                    [
                        "entry_id,opened_at,closed_at,portfolio_group,contour,secid,model,allow,net_rub",
                        "a,2026-06-15 10:00:00,2026-06-15 10:05:00,classic_core,strict,BRQ6,tv_ema_rsi_adx_trend,true,400",
                        "b,2026-06-15 10:10:00,2026-06-15 10:20:00,classic_core,strict,BRQ6,tv_ema_rsi_adx_trend,false,-800",
                        "c,2026-06-15 10:30:00,2026-06-15 10:35:00,classic_core,strict,BRQ6,tv_ema_rsi_adx_trend,true,300",
                        "d,2026-06-15 11:00:00,2026-06-15 11:08:00,classic_core,strict,BRQ6,forum_chop_donchian_guard,false,500",
                        "e,2026-06-14 10:00:00,2026-06-14 10:05:00,classic_core,strict,BRQ6,tv_ema_rsi_adx_trend,false,-600",
                    ]
                ),
                encoding="utf-8",
            )

            review = dar.build_strategy_review(
                trade_date="2026-06-15",
                research_dir=research_dir,
                run_dir=run_dir,
                strategy_lab=[],
                research_day=[],
                research_all=[],
                research_consensus=[],
                auto_policy={},
                restriction_rows=[],
                runtime_trade_model={},
            )

            self.assertTrue(review["generated"])
            self.assertEqual(
                review["summary_path"],
                "reports/autonomy/research/2026-06-15/strategy_review_summary.md",
            )
            self.assertEqual(
                review["artifacts"],
                [
                    "reports/autonomy/research/2026-06-15/strategy_review_summary.md",
                    "reports/autonomy/research/2026-06-15/strategy_review_candidates.csv",
                ],
            )
            self.assertGreaterEqual(review["candidate_count"], 1)
            self.assertEqual(review["top_models"][0]["scope"], "tv_ema_rsi_adx_trend")

            summary_text = (research_dir / "strategy_review_summary.md").read_text(encoding="utf-8")
            self.assertIn("Entry Shadow: All Sample by Model", summary_text)
            self.assertIn("tv_ema_rsi_adx_trend", summary_text)

            with (research_dir / "strategy_review_candidates.csv").open("r", encoding="utf-8", newline="") as handle:
                candidates = list(csv.DictReader(handle))
            self.assertTrue(candidates)
            self.assertEqual(candidates[0]["model"], "tv_ema_rsi_adx_trend")
            self.assertEqual(candidates[0]["portfolio_group"], "CLASSIC_CORE")
            self.assertEqual(candidates[0]["contour"], "STRICT")
            self.assertEqual(candidates[0]["recommended_action"], "research_then_runtime")

    def test_build_strategy_review_reports_shadow_only_collection_when_no_entry_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            run_dir = project_root / "reports" / "paper_runs" / "v7_live_20260525"
            research_dir = project_root / "reports" / "autonomy" / "research" / "2026-06-16"
            run_dir.mkdir(parents=True)
            research_dir.mkdir(parents=True)

            shadow_exit_path = run_dir / "classic_core_shadow_exit_models.csv"
            shadow_exit_path.write_text(
                "\n".join(
                    [
                        "closed_at,position_kind,model,net_rub",
                        "2026-06-15 18:43:00,actual,candle_like,120.0",
                    ]
                ),
                encoding="utf-8",
            )

            review = dar.build_strategy_review(
                trade_date="2026-06-16",
                research_dir=research_dir,
                run_dir=run_dir,
                strategy_lab=[],
                research_day=[],
                research_all=[],
                research_consensus=[],
                auto_policy={},
                restriction_rows=[],
                runtime_trade_model={},
            )

            self.assertTrue(review["generated"])
            self.assertEqual(review["candidate_count"], 0)
            summary_text = (research_dir / "strategy_review_summary.md").read_text(encoding="utf-8")
            self.assertIn("entry_shadow_collection_status: shadow_only_history", summary_text)
            self.assertIn("entry_shadow_missing_files: CLASSIC_CORE", summary_text)
            self.assertIn("last_shadow_closed_at: 2026-06-15 18:43:00", summary_text)


if __name__ == "__main__":
    unittest.main()
