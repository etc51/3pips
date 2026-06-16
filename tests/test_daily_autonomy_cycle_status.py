from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import daily_autonomy_outputs as dao  # noqa: E402
import daily_autonomy_runner as dar  # noqa: E402


class DailyAutonomyCycleStatusTest(unittest.TestCase):
    def test_stage_map_includes_strategy_review_email_and_archive_starts_pending(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            status = dar.build_nightly_cycle_status(
                trade_date="2026-06-15",
                overall={"trades": 7, "net_rub": 1234.5},
                research_day=[{"scenario": "base"}],
                research_all=[{"scenario": "base"}],
                research_consensus=[{"scenario": "base"}],
                optimizer_candidates=[{"scenario": "no_new_after_1745"}],
                strategy_lab=[{"candidate": "strict_plus_aggressive"}],
                strategy_review={
                    "generated": True,
                    "candidate_count": 0,
                    "entry_shadow_rows_day": 0,
                    "entry_shadow_rows_all": 0,
                    "shadow_rows_all": 75,
                    "collection_status": "waiting_for_runtime_rows",
                    "missing_entry_files": ["CLASSIC_CORE", "GL_WATCH"],
                    "summary_path": r"reports\autonomy\research\2026-06-15\strategy_review_summary.md",
                    "artifacts": [
                        r"reports\autonomy\research\2026-06-15\strategy_review_summary.md",
                        r"reports\autonomy\research\2026-06-15\strategy_review_candidates.csv",
                    ],
                },
                intervention_proposals={
                    "generated": True,
                    "rows": 2,
                    "evidence_backed_rows": 1,
                    "top_candidate": "tail-risk normalized exits",
                    "summary_path": r"reports\autonomy\research\2026-06-15\research_intervention_proposals.md",
                    "artifacts": [
                        r"reports\autonomy\research\2026-06-15\research_intervention_proposals.md",
                        r"reports\autonomy\research\2026-06-15\research_intervention_proposals.csv",
                    ],
                },
                microstructure_gate_research={
                    "rows": 12,
                    "source_review_rows_day": 240,
                    "source_review_rows_all": 480,
                    "latest_day_rows": 6,
                    "all_sample_rows": 6,
                    "backtest_candidates": 2,
                    "monitor_only": 3,
                    "collection_status": "proxy_backtest_candidate_ready",
                    "evaluation_state": "review_event_proxy",
                    "next_action": "Backtest proxy microstructure gate for TAIL_RESEARCH/AGGRESSIVE::MM at spread_to_stop_ratio > 0.75.",
                    "top_candidate_group": "TAIL_RESEARCH/AGGRESSIVE::MM",
                    "top_candidate_status": "backtest_candidate",
                },
                microstructure_counterfactual={
                    "rows": 0,
                    "source_entry_shadow_rows_day": 0,
                    "source_entry_shadow_rows_all": 0,
                    "unique_entries_day": 0,
                    "unique_entries": 0,
                    "candidate_count": 0,
                    "monitor_only": 0,
                    "collection_status": "awaiting_entry_shadow_rows",
                    "evaluation_state": "trade_level_counterfactual",
                    "next_action": "Collect first entry-shadow rows before promoting trade-level microstructure decisions.",
                    "top_candidate_group": "",
                    "top_candidate_status": "",
                },
                restriction_rows=[{"restriction_type": "entry_no_new_after"}],
                auto_policy={"active": {"entry_no_new_after": "17:45"}},
                email_to="etc00051@yandex.ru",
            )

        stages = status["stages"]
        self.assertEqual(
            set(stages),
            {
                "analyze",
                "research",
                "optimizer",
                "strategy_lab",
                "strategy_review",
                "intervention_proposals",
                "microstructure_gate_research",
                "microstructure_counterfactual",
                "restrictions",
                "candidate_gate",
                "summary",
                "email",
            },
        )
        self.assertEqual(stages["candidate_gate"]["status"], "ok")
        self.assertEqual(stages["candidate_gate"]["pending"], 0)
        self.assertEqual(stages["strategy_review"]["status"], "ok")
        self.assertTrue(stages["strategy_review"]["generated"])
        self.assertEqual(stages["strategy_review"]["candidate_count"], 0)
        self.assertEqual(stages["strategy_review"]["entry_shadow_rows_day"], 0)
        self.assertEqual(stages["strategy_review"]["entry_shadow_rows_all"], 0)
        self.assertEqual(stages["strategy_review"]["shadow_rows_all"], 75)
        self.assertEqual(stages["strategy_review"]["collection_status"], "waiting_for_runtime_rows")
        self.assertEqual(stages["strategy_review"]["missing_entry_files"], ["CLASSIC_CORE", "GL_WATCH"])
        self.assertEqual(
            stages["strategy_review"]["summary_path"],
            r"reports\autonomy\research\2026-06-15\strategy_review_summary.md",
        )
        self.assertEqual(
            stages["strategy_review"]["artifacts"],
            [
                r"reports\autonomy\research\2026-06-15\strategy_review_summary.md",
                r"reports\autonomy\research\2026-06-15\strategy_review_candidates.csv",
            ],
        )
        self.assertEqual(stages["intervention_proposals"]["status"], "ok")
        self.assertTrue(stages["intervention_proposals"]["generated"])
        self.assertEqual(stages["intervention_proposals"]["rows"], 2)
        self.assertEqual(stages["intervention_proposals"]["evidence_backed_rows"], 1)
        self.assertEqual(stages["intervention_proposals"]["top_candidate"], "tail-risk normalized exits")
        self.assertEqual(
            stages["intervention_proposals"]["summary_path"],
            r"reports\autonomy\research\2026-06-15\research_intervention_proposals.md",
        )
        self.assertEqual(stages["microstructure_gate_research"]["status"], "ok")
        self.assertTrue(stages["microstructure_gate_research"]["generated"])
        self.assertEqual(stages["microstructure_gate_research"]["collection_status"], "proxy_backtest_candidate_ready")
        self.assertEqual(stages["microstructure_gate_research"]["source_review_rows_day"], 240)
        self.assertEqual(stages["microstructure_gate_research"]["source_review_rows_all"], 480)
        self.assertEqual(stages["microstructure_gate_research"]["backtest_candidates"], 2)
        self.assertIn("Backtest proxy microstructure gate", stages["microstructure_gate_research"]["next_action"])
        self.assertEqual(stages["microstructure_gate_research"]["top_candidate_status"], "backtest_candidate")
        self.assertEqual(stages["microstructure_counterfactual"]["status"], "ok")
        self.assertTrue(stages["microstructure_counterfactual"]["generated"])
        self.assertEqual(stages["microstructure_counterfactual"]["collection_status"], "awaiting_entry_shadow_rows")
        self.assertEqual(stages["microstructure_counterfactual"]["source_entry_shadow_rows_day"], 0)
        self.assertEqual(stages["microstructure_counterfactual"]["source_entry_shadow_rows_all"], 0)
        self.assertEqual(stages["microstructure_counterfactual"]["unique_entries_day"], 0)
        self.assertEqual(stages["microstructure_counterfactual"]["unique_entries"], 0)
        self.assertEqual(stages["microstructure_counterfactual"]["candidate_count"], 0)
        self.assertIn("Collect first entry-shadow rows", stages["microstructure_counterfactual"]["next_action"])
        self.assertFalse(stages["summary"]["archive_ready"])
        self.assertEqual(stages["summary"]["archive_path"], "")
        self.assertEqual(stages["email"]["status"], "disabled_missing_smtp")
        self.assertFalse(stages["email"]["configured"])
        self.assertFalse(stages["email"]["sent"])
        self.assertEqual(stages["email"]["recipient"], "etc00051@yandex.ru")

    def test_strategy_lab_counts(self) -> None:
        counts = dao.build_strategy_lab_counts(
            [
                {"action_type": "runtime_policy", "autopromote_ready": True},
                {"action_type": "shadow_backtest", "autopromote_ready": False},
                {"action_type": "research_then_runtime", "autopromote_ready": True},
            ]
        )
        self.assertEqual(
            counts,
            {
                "total": 3,
                "runtime_policy": 1,
                "shadow_backtest": 1,
                "research_then_runtime": 1,
                "autopromote_ready": 2,
            },
        )


if __name__ == "__main__":
    unittest.main()
