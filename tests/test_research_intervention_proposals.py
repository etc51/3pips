from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import research_intervention_proposals as rip  # noqa: E402


class ResearchInterventionProposalsTest(unittest.TestCase):
    def test_build_and_persist_produces_proposal_only_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            trade_date = "2026-06-15"
            research_dir = project_root / "reports" / "autonomy" / "research" / trade_date
            latest_dir = project_root / "reports" / "autonomy" / "latest"
            research_dir.mkdir(parents=True, exist_ok=True)
            latest_dir.mkdir(parents=True, exist_ok=True)

            strategy_lab_rows = [
                {
                    "hypothesis_id": "shadow_tail_risk_normalized_exit",
                    "priority": 91,
                    "category": "exit_model",
                    "candidate": "tail-risk normalized exits",
                    "scope": "all futures layers",
                    "action_type": "shadow_backtest",
                    "safe_mode": "research_only",
                    "scenario_anchor": "contour_only_strict",
                    "required_features": "trade ledger, candles, exit path",
                    "recommended_next_step": "Backtest alternative exits.",
                    "evidence": "avg_loss=746.78 vs avg_win=358.47 on accumulated sample",
                },
                {
                    "hypothesis_id": "shadow_opening_range_continuation",
                    "priority": 88,
                    "category": "shadow_strategy",
                    "candidate": "opening-range continuation only",
                    "scope": "10:15-13:00 Moscow session",
                    "action_type": "shadow_backtest",
                    "safe_mode": "research_only",
                    "scenario_anchor": "contour_only_strict",
                    "required_features": "1m candles, family, session clock",
                    "recommended_next_step": "Isolate the strongest morning window.",
                    "evidence": "morning_net=1320.0 while late_net=-910.0 on latest day",
                },
                {
                    "hypothesis_id": "runtime_family_regime_routing",
                    "priority": 86,
                    "category": "regime_routing",
                    "candidate": "family-specific routing by regime",
                    "scope": "destructive families only",
                    "action_type": "research_then_runtime",
                    "safe_mode": "paper_only",
                    "scenario_anchor": "day_history",
                    "required_features": "day history, family pnl, rollover state",
                    "recommended_next_step": "Separate stable, mixed, destructive families.",
                    "evidence": "killer_days=2/5 (40.0%) in accumulated history",
                },
                {
                    "hypothesis_id": "shadow_vwap_reversion_family_probe",
                    "priority": 80,
                    "category": "new_strategy",
                    "candidate": "VWAP reversion probe on weak families",
                    "scope": "MM",
                    "action_type": "shadow_backtest",
                    "safe_mode": "research_only",
                    "scenario_anchor": "missing_anchor",
                    "required_features": "vwap, z-score, spread filter",
                    "recommended_next_step": "Explore a new family-specific alpha branch.",
                    "evidence": "exploratory idea without sufficient numeric backing",
                },
            ]
            strategy_review = {
                "candidates": [
                    {
                        "candidate": "entry_shadow_gate::CLASSIC_CORE/STRICT/tv_ema_rsi_adx_trend",
                        "portfolio_group": "CLASSIC_CORE",
                        "contour": "STRICT",
                        "model": "tv_ema_rsi_adx_trend",
                        "delta_vs_base_rub": 1750.0,
                        "skipped_losses": 3,
                        "skipped_wins": 1,
                        "recommended_action": "research_then_runtime",
                        "note": "Model stays positive and skips more losing trades than winners.",
                    }
                ]
            }
            research_day_rows = [
                {
                    "scenario": "contour_only_strict",
                    "expectancy_rub": 125.0,
                    "avg_win_rub": 489.26,
                    "avg_loss_rub": -825.5,
                    "top3_loss_rub": 2476.51,
                    "delta_vs_base_rub": 980.0,
                }
            ]
            research_all_rows = [
                {
                    "scenario": "contour_only_strict",
                    "expectancy_rub": 77.25,
                    "avg_win_rub": 358.47,
                    "avg_loss_rub": -746.78,
                    "top3_loss_rub": 3167.67,
                    "delta_vs_base_rub": 1305.0,
                }
            ]

            rows, summary = rip.build_and_persist_research_intervention_proposals(
                project_root=project_root,
                trade_date=trade_date,
                strategy_lab_rows=strategy_lab_rows,
                strategy_review=strategy_review,
                research_day_rows=research_day_rows,
                research_all_rows=research_all_rows,
                research_dir=research_dir,
                latest_dir=latest_dir,
            )

            self.assertEqual(summary["trade_date"], trade_date)
            self.assertEqual(summary["rows"], len(rows))
            self.assertEqual(summary["runtime_mutation_allowed"], 0)
            self.assertEqual(summary["live_mode_allowed"], 0)
            self.assertEqual(summary["explicit_user_approval_required"], len(rows))
            self.assertEqual(summary["filtered_low_evidence_rows"], 1)
            self.assertEqual(summary["top_candidate"], rows[0]["candidate_label"])
            self.assertGreaterEqual(summary["evidence_backed_rows"], 3)
            self.assertTrue((research_dir / "research_intervention_proposals.csv").exists())
            self.assertTrue((research_dir / "research_intervention_proposals.md").exists())
            self.assertTrue((latest_dir / "research_intervention_proposals.csv").exists())
            self.assertTrue((latest_dir / "research_intervention_proposals_summary.json").exists())

            by_family = {str(row.get("intervention_family") or ""): row for row in rows}
            self.assertIn("entry_shadow_gate", by_family)
            self.assertIn("exit_tail_risk", by_family)
            self.assertIn("session_window", by_family)
            self.assertIn("family_regime", by_family)
            self.assertNotIn("new_alpha_probe", by_family)

            self.assertEqual(rows[0]["intervention_family"], "entry_shadow_gate")
            self.assertEqual(by_family["entry_shadow_gate"]["actionability_tier"], "runtime_candidate_review")
            self.assertEqual(by_family["entry_shadow_gate"]["runtime_mutation_allowed"], "False")
            self.assertEqual(by_family["entry_shadow_gate"]["live_mode_allowed"], "False")
            self.assertEqual(by_family["entry_shadow_gate"]["requires_explicit_user_approval"], "True")
            self.assertGreater(float(by_family["entry_shadow_gate"]["delta_vs_base_rub"]), 1000.0)
            self.assertGreater(float(by_family["exit_tail_risk"]["trigger_value"]), 2.0)
            self.assertEqual(by_family["family_regime"]["target_stage"], "research_then_manual_paper_release")
            self.assertEqual(float(by_family["session_window"]["trigger_value"]), -910.0)

            entry_shadow_instructions = json.loads(by_family["entry_shadow_gate"]["instructions_json"])
            self.assertIn("entry_shadow_gate", entry_shadow_instructions)
            self.assertEqual(entry_shadow_instructions["entry_shadow_gate"]["model"], "tv_ema_rsi_adx_trend")

            session_instructions = json.loads(by_family["session_window"]["instructions_json"])
            self.assertEqual(session_instructions["shadow_backtest"]["entry_window_moscow"], ["10:15", "13:00"])

            with (latest_dir / "research_intervention_proposals.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            self.assertEqual(len(csv_rows), len(rows))
            self.assertTrue(all(row["runtime_mutation_allowed"] == "False" for row in csv_rows))
            self.assertTrue(all(row["live_mode_allowed"] == "False" for row in csv_rows))
            self.assertTrue(any(row["actionability_tier"] == "runtime_candidate_review" for row in csv_rows))
            self.assertTrue(all(int(row["evidence_score"]) >= 3 for row in csv_rows if row["source_type"] == "strategy_lab"))

    def test_build_and_persist_ignores_runtime_policy_strategy_lab_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            trade_date = "2026-06-15"
            research_dir = project_root / "reports" / "autonomy" / "research" / trade_date
            latest_dir = project_root / "reports" / "autonomy" / "latest"
            research_dir.mkdir(parents=True, exist_ok=True)
            latest_dir.mkdir(parents=True, exist_ok=True)

            strategy_lab_rows = [
                {
                    "hypothesis_id": "runtime_strict_primary",
                    "priority": 98,
                    "category": "runtime_policy",
                    "candidate": "strict primary baseline",
                    "scope": "all classic futures",
                    "action_type": "runtime_policy",
                    "safe_mode": "paper_autopolicy",
                    "scenario_anchor": "contour_only_strict",
                    "required_features": "trade csv, day history, research consensus",
                    "recommended_next_step": "Keep strict baseline.",
                    "evidence": "strict consensus delta_total=4072.82",
                },
                {
                    "hypothesis_id": "shadow_tail_risk_normalized_exit",
                    "priority": 91,
                    "category": "exit_model",
                    "candidate": "tail-risk normalized exits",
                    "scope": "all futures layers",
                    "action_type": "shadow_backtest",
                    "safe_mode": "research_only",
                    "scenario_anchor": "contour_only_strict",
                    "required_features": "trade ledger, candles, exit path",
                    "recommended_next_step": "Backtest alternative exits.",
                    "evidence": "avg_loss=746.78 vs avg_win=358.47 on accumulated sample",
                },
            ]
            research_day_rows = [{"scenario": "contour_only_strict", "expectancy_rub": 125.0}]
            research_all_rows = [
                {
                    "scenario": "contour_only_strict",
                    "expectancy_rub": 77.25,
                    "avg_win_rub": 358.47,
                    "avg_loss_rub": -746.78,
                    "top3_loss_rub": 3167.67,
                }
            ]

            rows, summary = rip.build_and_persist_research_intervention_proposals(
                project_root=project_root,
                trade_date=trade_date,
                strategy_lab_rows=strategy_lab_rows,
                strategy_review={"candidates": []},
                research_day_rows=research_day_rows,
                research_all_rows=research_all_rows,
                research_dir=research_dir,
                latest_dir=latest_dir,
            )

            self.assertEqual(summary["rows"], 1)
            self.assertEqual(rows[0]["source_id"], "shadow_tail_risk_normalized_exit")
            self.assertEqual(rows[0]["intervention_family"], "exit_tail_risk")
            self.assertTrue(all(row["source_id"] != "runtime_strict_primary" for row in rows))


if __name__ == "__main__":
    unittest.main()
