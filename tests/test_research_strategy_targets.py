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

import research_strategy_targets as rst  # noqa: E402


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


class ResearchStrategyTargetsTest(unittest.TestCase):
    def test_build_targets_marks_autopolicy_overlay_launch_ready(self) -> None:
        trade_date = "2026-06-16"
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            latest_dir = project_root / "reports" / "autonomy" / "latest"
            research_dir = project_root / "reports" / "autonomy" / "research" / trade_date
            write_csv(
                latest_dir / "paper_candidate_shortlist.csv",
                [
                    {
                        "trade_date": trade_date,
                        "shortlist_rank": 1,
                        "registry_id": "strategy_lab:strict_primary",
                        "candidate_label": "strict primary baseline",
                        "paper_route": "paper_autopolicy",
                        "registry_status": "paper_candidate",
                        "validation_state": "consensus_backed",
                        "runtime_ready": "True",
                        "shortlist_status": "ready_now",
                        "blocking_reason": "",
                        "stability_score": 62.4,
                        "autopromote_ready": "True",
                        "priority": 98,
                        "registry_rank": 1,
                        "scenario_anchor": "contour_only_strict",
                        "recommended_use": "candidate_runtime_tune",
                        "required_features": "trade csv",
                        "execution_params_json": "{\"safe_mode\":\"paper_autopolicy\"}",
                        "evidence_json": "{}",
                    }
                ],
            )

            rows, summary = rst.build_and_persist_research_strategy_targets(
                project_root=project_root,
                trade_date=trade_date,
                research_dir=research_dir,
                latest_dir=latest_dir,
            )

            self.assertEqual(summary["rows"], 1)
            self.assertEqual(summary["launch_ready"], 1)
            self.assertEqual(rows[0]["target_kind"], "policy_overlay")
            self.assertEqual(rows[0]["launch_ready"], "True")
            self.assertEqual(rows[0]["target_status"], "launch_ready")
            self.assertEqual(rows[0]["shortlist_id"], f"{trade_date}:1:strategy_lab:strict_primary")
            self.assertEqual(rows[0]["target_contract"], "")
            self.assertEqual(rows[0]["plus1_contract"], "")
            self.assertTrue((latest_dir / "research_strategy_targets.csv").exists())
            self.assertTrue((latest_dir / "research_strategy_targets_summary.json").exists())
            self.assertTrue((latest_dir / "research_strategy_targets.md").exists())

    def test_build_targets_resolves_leadlag_pair_from_shortlist_contract_fields(self) -> None:
        trade_date = "2026-06-16"
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            latest_dir = project_root / "reports" / "autonomy" / "latest"
            write_csv(
                latest_dir / "paper_candidate_shortlist.csv",
                [
                    {
                        "trade_date": trade_date,
                        "shortlist_rank": 1,
                        "registry_id": "third_pass:leadlag",
                        "candidate_label": "Leadlag global no-overlap",
                        "paper_route": "leadlag_orderbook_monitor",
                        "registry_status": "paper_candidate",
                        "validation_state": "unit_corrected_positive",
                        "runtime_ready": "True",
                        "shortlist_status": "ready_now",
                        "blocking_reason": "",
                        "stability_score": 77.5,
                        "priority": 88,
                        "registry_rank": 1,
                        "selection_run_date": trade_date,
                        "selection_age_days": 0,
                        "selection_fresh": "True",
                        "target_contract": "NGQ6",
                        "plus1_contract": "NGU6",
                        "selection_method": "auto_front_plus1",
                        "selected_ok": "True",
                        "orderbook_source_effective": "tbank-stream",
                        "run_id": "leadlag_20260616",
                        "execution_params_json": "{\"strategy_mode\":\"fixed_plus1_only\",\"portfolio_mode\":\"global_no_overlap\",\"selected_feature_set\":\"plus1_only\",\"selected_threshold\":0.0}",
                        "evidence_json": "{}",
                    }
                ],
            )

            rows, summary = rst.build_and_persist_research_strategy_targets(
                project_root=project_root,
                trade_date=trade_date,
                latest_dir=latest_dir,
            )

            self.assertEqual(summary["contract_pair_ready"], 1)
            self.assertEqual(rows[0]["target_kind"], "contract_pair")
            self.assertEqual(rows[0]["launch_ready"], "True")
            self.assertEqual(rows[0]["target_contract"], "NGQ6")
            self.assertEqual(rows[0]["plus1_contract"], "NGU6")
            self.assertEqual(rows[0]["strategy_mode"], "fixed_plus1_only")
            self.assertEqual(rows[0]["portfolio_mode"], "global_no_overlap")
            self.assertEqual(rows[0]["selected_feature_set"], "plus1_only")

    def test_build_targets_can_fallback_to_contract_selection_when_shortlist_binding_missing(self) -> None:
        trade_date = "2026-06-16"
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            latest_dir = project_root / "reports" / "autonomy" / "latest"
            write_csv(
                latest_dir / "paper_candidate_shortlist.csv",
                [
                    {
                        "trade_date": trade_date,
                        "shortlist_rank": 1,
                        "registry_id": "third_pass:leadlag",
                        "candidate_label": "Leadlag global no-overlap",
                        "paper_route": "leadlag_orderbook_monitor",
                        "registry_status": "paper_candidate",
                        "validation_state": "unit_corrected_positive",
                        "runtime_ready": "True",
                        "shortlist_status": "ready_now",
                        "blocking_reason": "",
                        "stability_score": 77.5,
                        "priority": 88,
                        "registry_rank": 1,
                        "selection_run_date": "",
                        "selection_age_days": "",
                        "selection_fresh": "False",
                        "target_contract": "",
                        "plus1_contract": "",
                        "selection_method": "",
                        "selected_ok": "False",
                        "orderbook_source_effective": "",
                        "run_id": "",
                        "execution_params_json": "{\"strategy_mode\":\"fixed_plus1_only\"}",
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
                        "run_id": "selection_20260616",
                    }
                ],
            )

            rows, summary = rst.build_and_persist_research_strategy_targets(
                project_root=project_root,
                trade_date=trade_date,
                latest_dir=latest_dir,
            )

            self.assertEqual(summary["launch_ready"], 1)
            self.assertEqual(rows[0]["selection_run_date"], trade_date)
            self.assertEqual(rows[0]["target_contract"], "NGQ6")
            self.assertEqual(rows[0]["plus1_contract"], "NGU6")
            self.assertEqual(rows[0]["launch_ready"], "True")

    def test_build_targets_keeps_candidate_runtime_blocked(self) -> None:
        trade_date = "2026-06-16"
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            latest_dir = project_root / "reports" / "autonomy" / "latest"
            write_csv(
                latest_dir / "paper_candidate_shortlist.csv",
                [
                    {
                        "trade_date": trade_date,
                        "shortlist_rank": 3,
                        "registry_id": "strategy_lab:family_router",
                        "candidate_label": "family-specific routing by regime",
                        "paper_route": "candidate_runtime",
                        "registry_status": "validated",
                        "validation_state": "consensus_backed",
                        "runtime_ready": "False",
                        "shortlist_status": "review_only",
                        "blocking_reason": "candidate_runtime_needs_manual_release",
                        "stability_score": 8.6,
                        "priority": 70,
                        "registry_rank": 3,
                        "execution_params_json": "{}",
                        "evidence_json": "{}",
                    }
                ],
            )

            rows, summary = rst.build_and_persist_research_strategy_targets(
                project_root=project_root,
                trade_date=trade_date,
                latest_dir=latest_dir,
            )

            self.assertEqual(summary["blocked"], 1)
            self.assertEqual(rows[0]["target_kind"], "manual_runtime_release")
            self.assertEqual(rows[0]["launch_ready"], "False")
            self.assertEqual(rows[0]["blocking_reason"], "candidate_runtime_needs_manual_release")
