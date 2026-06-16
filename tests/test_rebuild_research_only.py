from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import rebuild_research_only as rro  # noqa: E402


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


class RebuildResearchOnlyTest(unittest.TestCase):
    def test_main_rebuilds_research_without_touching_latest_auto_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            run_dir = project_root / "reports" / "paper_runs" / "v7_live_20260525"
            latest_dir = project_root / "reports" / "autonomy" / "latest"
            run_dir.mkdir(parents=True, exist_ok=True)
            latest_dir.mkdir(parents=True, exist_ok=True)

            write_csv(
                run_dir / "classic_core_multi_futures_paper_trades.csv",
                [
                    {"closed_at": "2026-06-14 11:00:00", "contour": "strict", "secid": "GLZ6", "qty": 1, "net_rub": 100.0},
                    {"closed_at": "2026-06-14 12:00:00", "contour": "aggressive", "secid": "GLM6", "qty": 1, "net_rub": -60.0},
                    {"closed_at": "2026-06-15 10:30:00", "contour": "strict", "secid": "GLZ6", "qty": 1, "net_rub": 250.0},
                    {"closed_at": "2026-06-15 12:30:00", "contour": "aggressive", "secid": "GLM6", "qty": 1, "net_rub": -300.0},
                    {"closed_at": "2026-06-15 16:00:00", "contour": "strict", "secid": "BRQ6", "qty": 1, "net_rub": 150.0},
                ],
            )
            write_csv(
                project_root / "reports" / "futures_scalp_profiles_v7_paper_20260525.csv",
                [
                    {"ticker": "GLZ6", "v7_family": "GL"},
                    {"ticker": "GLM6", "v7_family": "GL"},
                    {"ticker": "BRQ6", "v7_family": "BR"},
                ],
            )

            latest_auto_policy = {
                "active": {"entry_no_new_after": "11:59", "strict_only_families": ["GL"], "allow_aggressive_group_families": []},
                "summary": {"active_rule_count": 1},
            }
            latest_manifest = {"candidate_gate": {"pending_count": 3}, "nightly_cycle_status": {"status": "ok"}}
            (latest_dir / "latest_auto_policy.json").write_text(json.dumps(latest_auto_policy, ensure_ascii=False, indent=2), encoding="utf-8")
            (latest_dir / "latest_daily_manifest.json").write_text(json.dumps(latest_manifest, ensure_ascii=False, indent=2), encoding="utf-8")

            argv = [
                "rebuild_research_only.py",
                "--project-root",
                str(project_root),
                "--trade-date",
                "latest",
            ]

            with patch.object(sys, "argv", argv):
                rc = rro.main()

            self.assertEqual(rc, 0)
            self.assertEqual(
                json.loads((latest_dir / "latest_auto_policy.json").read_text(encoding="utf-8")),
                latest_auto_policy,
            )
            rebuilt_manifest = json.loads((latest_dir / "latest_daily_manifest.json").read_text(encoding="utf-8"))
            rebuilt_manifest_alias = json.loads((latest_dir / "latest_manifest.json").read_text(encoding="utf-8"))
            rebuild_summary = (latest_dir / "latest_research_rebuild.md").read_text(encoding="utf-8")
            self.assertEqual(rebuilt_manifest["candidate_gate"], latest_manifest["candidate_gate"])
            self.assertEqual(rebuilt_manifest["research_rebuild"]["mode"], "research_only")
            self.assertEqual(rebuilt_manifest["trade_date"], "2026-06-15")
            self.assertIn("research_intervention_proposals", rebuilt_manifest)
            self.assertIn("entry_shadow_collection", rebuilt_manifest)
            self.assertEqual(rebuilt_manifest["strategy_review"]["collection_status"], "awaiting_first_close")
            self.assertEqual(rebuilt_manifest["strategy_review"]["entry_shadow_rows_all"], 0)
            self.assertEqual(rebuilt_manifest["strategy_review"]["shadow_rows_all"], 0)
            self.assertEqual(rebuilt_manifest["strategy_review"]["missing_entry_files"], ["CLASSIC_CORE"])
            self.assertIn("- entry_shadow_collection_status: awaiting_first_close", rebuild_summary)
            self.assertIn("- entry_shadow_rows_all: 0", rebuild_summary)
            self.assertIn("- entry_shadow_missing_files: CLASSIC_CORE", rebuild_summary)
            self.assertEqual(rebuilt_manifest["microstructure_gate_research"]["collection_status"], "awaiting_review_rows")
            self.assertEqual(rebuilt_manifest["microstructure_counterfactual"]["collection_status"], "awaiting_entry_shadow_rows")
            self.assertIn("- microstructure_gate_status: awaiting_review_rows", rebuild_summary)
            self.assertIn("- microstructure_gate_source_rows_day: 0", rebuild_summary)
            self.assertIn("- microstructure_gate_next_action: Collect wide-spread review snapshots before evaluating proxy microstructure gates.", rebuild_summary)
            self.assertIn("- microstructure_counterfactual_status: awaiting_entry_shadow_rows", rebuild_summary)
            self.assertIn("- microstructure_counterfactual_source_rows_all: 0", rebuild_summary)
            self.assertIn("- microstructure_counterfactual_next_action: Collect first entry-shadow rows before promoting trade-level microstructure decisions.", rebuild_summary)
            self.assertEqual(rebuilt_manifest_alias, rebuilt_manifest)

            research_dir = project_root / "reports" / "autonomy" / "research" / "2026-06-15"
            self.assertTrue((research_dir / "policy_sweep_latest_day.csv").exists())
            self.assertTrue((latest_dir / "research_strategy_registry.csv").exists())
            self.assertTrue((latest_dir / "paper_candidate_shortlist.csv").exists())
            self.assertTrue((latest_dir / "research_strategy_targets.csv").exists())
            self.assertTrue((latest_dir / "research_intervention_proposals.csv").exists())
            self.assertTrue((latest_dir / "research_intervention_proposals_summary.json").exists())
            self.assertTrue((latest_dir / "microstructure_gate_research.csv").exists())
            self.assertTrue((latest_dir / "microstructure_gate_research_summary.json").exists())
            self.assertTrue((latest_dir / "microstructure_counterfactual.csv").exists())
            self.assertTrue((latest_dir / "microstructure_counterfactual_summary.json").exists())
            self.assertTrue((latest_dir / "entry_shadow_collection.csv").exists())
            self.assertTrue((latest_dir / "entry_shadow_collection_summary.json").exists())

            with (research_dir / "policy_sweep_latest_day.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertIn("avg_win_rub", rows[0])
            self.assertIn("avg_loss_rub", rows[0])
            self.assertIn("top3_loss_rub", rows[0])

            with (latest_dir / "research_strategy_registry.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                registry_rows = list(csv.DictReader(handle))
            self.assertIn("sample_avg_win_rub", registry_rows[0])

            intervention_summary = json.loads((latest_dir / "research_intervention_proposals_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(intervention_summary["runtime_mutation_allowed"], 0)
            self.assertEqual(intervention_summary["live_mode_allowed"], 0)
            self.assertIn("evidence_backed_rows", intervention_summary)
            self.assertIn("filtered_low_evidence_rows", intervention_summary)
            micro_gate_summary = json.loads((latest_dir / "microstructure_gate_research_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(micro_gate_summary["evaluation_state"], "review_event_proxy")
            self.assertEqual(micro_gate_summary["collection_status"], "awaiting_review_rows")
            self.assertEqual(micro_gate_summary["source_review_rows_day"], 0)
            micro_counter_summary = json.loads((latest_dir / "microstructure_counterfactual_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(micro_counter_summary["evaluation_state"], "trade_level_counterfactual")
            self.assertEqual(micro_counter_summary["collection_status"], "awaiting_entry_shadow_rows")
            self.assertEqual(micro_counter_summary["source_entry_shadow_rows_all"], 0)
            entry_shadow_collection_summary = json.loads((latest_dir / "entry_shadow_collection_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(entry_shadow_collection_summary["trade_date"], "2026-06-15")
            self.assertIn("status", entry_shadow_collection_summary)

    def test_registry_build_sees_microstructure_summaries_before_shortlist_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            run_dir = project_root / "reports" / "paper_runs" / "v7_live_20260525"
            latest_dir = project_root / "reports" / "autonomy" / "latest"
            run_dir.mkdir(parents=True, exist_ok=True)
            latest_dir.mkdir(parents=True, exist_ok=True)

            write_csv(
                run_dir / "classic_core_multi_futures_paper_trades.csv",
                [
                    {"closed_at": "2026-06-15 10:30:00", "contour": "strict", "secid": "GLZ6", "qty": 1, "net_rub": 250.0},
                ],
            )
            write_csv(
                project_root / "reports" / "futures_scalp_profiles_v7_paper_20260525.csv",
                [
                    {"ticker": "GLZ6", "v7_family": "GL"},
                ],
            )
            (latest_dir / "latest_auto_policy.json").write_text(
                json.dumps({"active": {}, "summary": {"active_rule_count": 0}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (latest_dir / "latest_daily_manifest.json").write_text(
                json.dumps({"candidate_gate": {"pending_count": 0}, "nightly_cycle_status": {"status": "ok"}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            seen: dict[str, object] = {}
            fake_gate_summary = {
                "trade_date": "2026-06-15",
                "rows": 12,
                "backtest_candidates": 2,
                "monitor_only": 1,
                "collection_status": "proxy_backtest_candidate_ready",
            }
            fake_counter_summary = {
                "trade_date": "2026-06-15",
                "rows": 0,
                "unique_entries": 0,
                "candidate_count": 0,
                "monitor_only": 0,
                "collection_status": "awaiting_entry_shadow_rows",
            }

            def fake_registry(**kwargs):
                manifest_payload = kwargs["manifest_payload"]
                seen["gate"] = manifest_payload.get("microstructure_gate_research")
                seen["counter"] = manifest_payload.get("microstructure_counterfactual")
                return [], {
                    "trade_date": "2026-06-15",
                    "generated_at": "now",
                    "rows": 0,
                    "paper_candidates": 0,
                    "validated": 0,
                    "research_only": 0,
                    "autopromote_ready": 0,
                    "by_source": {},
                    "by_status": {},
                    "by_paper_route": {},
                }

            argv = [
                "rebuild_research_only.py",
                "--project-root",
                str(project_root),
                "--trade-date",
                "latest",
            ]

            with patch.object(rro, "build_and_persist_microstructure_gate_research", return_value=([], fake_gate_summary)), patch.object(
                rro, "build_and_persist_microstructure_counterfactual", return_value=([], fake_counter_summary)
            ), patch.object(
                rro.dar,
                "build_and_persist_entry_shadow_collection",
                return_value=([], {"trade_date": "2026-06-15", "status": "awaiting_first_close"}),
            ), patch.object(
                rro, "build_and_persist_research_strategy_registry", side_effect=fake_registry
            ), patch.object(
                rro,
                "build_and_persist_paper_candidate_shortlist",
                return_value=([], {"trade_date": "2026-06-15", "rows": 0, "runtime_ready": 0, "review_only": 0, "by_state": {}}),
            ), patch.object(
                rro,
                "build_and_persist_research_strategy_targets",
                return_value=([], {"trade_date": "2026-06-15", "rows": 0, "launch_ready": 0, "research_only": 0, "by_decision": {}}),
            ), patch.object(sys, "argv", argv):
                rc = rro.main()

            self.assertEqual(rc, 0)
            self.assertEqual(seen["gate"], fake_gate_summary)
            self.assertEqual(seen["counter"], fake_counter_summary)


if __name__ == "__main__":
    unittest.main()
