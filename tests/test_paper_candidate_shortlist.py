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

import paper_candidate_shortlist as pcs  # noqa: E402


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class PaperCandidateShortlistTest(unittest.TestCase):
    def test_build_shortlist_marks_autopolicy_ready_and_candidate_runtime_review(self) -> None:
        trade_date = "2026-06-16"
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            latest_dir = project_root / "reports" / "autonomy" / "latest"
            research_dir = project_root / "reports" / "autonomy" / "research" / trade_date
            write_csv(
                project_root / "data" / "processed" / "research_strategy_registry.csv",
                [
                    {
                        "registry_id": "strategy_lab:strict_primary",
                        "as_of_date": trade_date,
                        "status": "paper_candidate",
                        "paper_route": "paper_autopolicy",
                        "candidate_label": "strict primary baseline",
                        "validation_state": "consensus_backed",
                        "autopromote_ready": "True",
                        "priority": 98,
                        "rank": 1,
                        "scenario_anchor": "contour_only_strict",
                        "beat_base_pct": 100.0,
                        "stability_days": 2,
                        "positive_days": 2,
                        "delta_total_rub": 4072.82,
                        "latest_day_delta_rub": 3191.01,
                        "median_daily_net_rub": 7059.02,
                        "worst_day_rub": 3982.97,
                        "sample_trades": 33,
                        "sample_win_rate_pct": 81.82,
                        "sample_expectancy_rub": 427.82,
                        "sample_profit_factor": 2.7757,
                        "latest_day_expectancy_rub": 165.96,
                        "latest_day_profit_factor": 1.6629,
                        "required_features": "trade csv",
                        "recommended_use": "candidate_runtime_tune",
                        "execution_params_json": "{}",
                        "evidence_json": "{}",
                    },
                    {
                        "registry_id": "strategy_lab:family_router",
                        "as_of_date": trade_date,
                        "status": "validated",
                        "paper_route": "candidate_runtime",
                        "candidate_label": "family-specific routing by regime",
                        "validation_state": "consensus_backed",
                        "autopromote_ready": "False",
                        "priority": 70,
                        "rank": 2,
                        "scenario_anchor": "day_history",
                        "required_features": "day history",
                        "recommended_use": "candidate_runtime",
                        "execution_params_json": "{}",
                        "evidence_json": "{}",
                    },
                ],
            )

            rows, summary = pcs.build_and_persist_paper_candidate_shortlist(
                project_root=project_root,
                trade_date=trade_date,
                research_dir=research_dir,
                latest_dir=latest_dir,
            )

            self.assertEqual(summary["rows"], 2)
            self.assertEqual(summary["runtime_ready"], 1)
            self.assertEqual(rows[0]["candidate_label"], "strict primary baseline")
            self.assertEqual(rows[0]["runtime_ready"], "True")
            self.assertEqual(rows[0]["shortlist_status"], "ready_now")
            self.assertEqual(rows[0]["sample_expectancy_rub"], 427.82)
            self.assertEqual(rows[0]["sample_profit_factor"], 2.7757)
            self.assertEqual(rows[1]["runtime_ready"], "False")
            self.assertEqual(rows[1]["shortlist_status"], "review_only")
            self.assertEqual(rows[1]["blocking_reason"], "candidate_runtime_needs_manual_release")
            self.assertTrue((latest_dir / "paper_candidate_shortlist.csv").exists())
            self.assertTrue((latest_dir / "paper_candidate_shortlist_summary.json").exists())
            self.assertTrue((latest_dir / "paper_candidate_shortlist.md").exists())

    def test_build_shortlist_uses_fresh_contract_selection_for_leadlag_candidates(self) -> None:
        trade_date = "2026-06-16"
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            latest_dir = project_root / "reports" / "autonomy" / "latest"
            write_csv(
                project_root / "data" / "processed" / "research_strategy_registry.csv",
                [
                    {
                        "registry_id": "third_pass:fixed_plus1_only:global_no_overlap",
                        "as_of_date": trade_date,
                        "status": "paper_candidate",
                        "paper_route": "leadlag_orderbook_monitor",
                        "candidate_label": "Leadlag global no-overlap",
                        "validation_state": "unit_corrected_positive",
                        "autopromote_ready": "False",
                        "priority": 88,
                        "rank": 1,
                        "scenario_anchor": "",
                        "required_features": "orderbook snapshots",
                        "recommended_use": "leadlag_paper_candidate",
                        "execution_params_json": "{\"selected_feature_set\":\"plus1_only\"}",
                        "evidence_json": "{}",
                    }
                ],
            )
            write_csv(
                project_root / "reports" / "paper_contract_selection.csv",
                [
                    {
                        "run_date": trade_date,
                        "target_contract": "NGQ6",
                        "plus1_contract": "NGU6",
                        "selection_method": "auto_front_plus1",
                        "selected_ok": "True",
                        "warning": "",
                        "orderbook_source_effective": "tbank-stream",
                        "run_id": "leadlag_20260616",
                    }
                ],
            )

            rows, summary = pcs.build_and_persist_paper_candidate_shortlist(
                project_root=project_root,
                trade_date=trade_date,
                latest_dir=latest_dir,
            )

            self.assertEqual(summary["leadlag_ready"], 1)
            self.assertEqual(rows[0]["runtime_ready"], "True")
            self.assertEqual(rows[0]["target_contract"], "NGQ6")
            self.assertEqual(rows[0]["plus1_contract"], "NGU6")
            self.assertEqual(rows[0]["selection_fresh"], "True")
            self.assertEqual(rows[0]["shortlist_status"], "ready_now")

    def test_build_shortlist_marks_stale_contract_selection_as_blocking(self) -> None:
        trade_date = "2026-06-16"
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            latest_dir = project_root / "reports" / "autonomy" / "latest"
            write_csv(
                project_root / "data" / "processed" / "research_strategy_registry.csv",
                [
                    {
                        "registry_id": "third_pass:fixed_plus1_only:global_no_overlap",
                        "as_of_date": trade_date,
                        "status": "paper_candidate",
                        "paper_route": "leadlag_orderbook_monitor",
                        "candidate_label": "Leadlag global no-overlap",
                        "validation_state": "unit_corrected_positive",
                        "autopromote_ready": "False",
                        "priority": 88,
                        "rank": 1,
                        "required_features": "orderbook snapshots",
                        "recommended_use": "leadlag_paper_candidate",
                        "execution_params_json": "{}",
                        "evidence_json": "{}",
                    }
                ],
            )
            write_csv(
                project_root / "reports" / "paper_contract_selection.csv",
                [
                    {
                        "run_date": "2026-05-24",
                        "target_contract": "NGM6",
                        "plus1_contract": "NGN6",
                        "selection_method": "manual_override",
                        "selected_ok": "True",
                        "warning": "",
                        "orderbook_source_effective": "tbank-stream",
                        "run_id": "old_pair",
                    }
                ],
            )

            rows, summary = pcs.build_and_persist_paper_candidate_shortlist(
                project_root=project_root,
                trade_date=trade_date,
                latest_dir=latest_dir,
            )

            self.assertEqual(summary["waiting_contract_selection"], 1)
            self.assertEqual(rows[0]["runtime_ready"], "False")
            self.assertEqual(rows[0]["shortlist_status"], "waiting_contract_selection")
            self.assertEqual(rows[0]["blocking_reason"], "stale_contract_selection")
            self.assertEqual(rows[0]["selection_fresh"], "False")

    def test_compute_stability_score_rewards_expectancy_and_profit_factor(self) -> None:
        base = {
            "beat_base_pct": 100.0,
            "stability_days": 2,
            "positive_days": 2,
            "delta_total_rub": 4072.82,
            "median_daily_net_rub": 7059.02,
            "latest_day_delta_rub": 3191.01,
            "worst_day_rub": 3982.97,
            "autopromote_ready": "True",
            "status": "paper_candidate",
            "paper_route": "paper_autopolicy",
            "priority": 98,
        }
        weak = dict(
            base,
            sample_trades=6,
            sample_expectancy_rub=-25.0,
            sample_profit_factor=0.85,
            latest_day_expectancy_rub=-10.0,
            latest_day_profit_factor=0.9,
        )
        strong = dict(
            base,
            sample_trades=33,
            sample_expectancy_rub=427.82,
            sample_profit_factor=2.7757,
            latest_day_expectancy_rub=165.96,
            latest_day_profit_factor=1.6629,
        )

        self.assertGreater(pcs.compute_stability_score(strong), pcs.compute_stability_score(weak))
