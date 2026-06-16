from __future__ import annotations

from contextlib import ExitStack
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

import daily_autonomy_runner as dar  # noqa: E402


class DailyAutonomyRunnerPersistenceTest(unittest.TestCase):
    def test_main_applies_persisted_promoted_runtime_policy(self) -> None:
        trade_date = "2026-06-16"
        all_rows = [
            {
                "trade_date": trade_date,
                "secid": "GLZ6",
                "portfolio_group": "GL_WATCH",
                "contour": "AGGRESSIVE",
                "group_key": "GL_WATCH/AGGRESSIVE",
                "family": "GL",
                "net_rub": 100.0,
            }
        ]
        overall = {
            "trades": 1,
            "net_rub": 100.0,
            "win_rate_pct": 100.0,
            "expectancy_rub": 100.0,
        }
        auto_policy = {
            "active": {"entry_no_new_after": None, "notes": []},
            "active_base": {"entry_no_new_after": None, "notes": []},
            "summary": {"active_rule_count": 0},
            "proposed": {},
        }
        candidate_gate = {
            "summary": {
                "pending_count": 0,
                "promoted_now_count": 0,
                "rejected_now_count": 0,
            },
            "promoted_now": [],
        }
        promoted_runtime_state = {
            "trade_date": "2026-06-15",
            "updated_at": "2026-06-16 02:44:18",
            "promoted_candidates": [
                {
                    "candidate_id": "entry_no_new_after|entry_window_1015_1159|11:59",
                    "policy_key": "entry_no_new_after",
                    "value": "11:59",
                    "source_scenario": "entry_window_1015_1159",
                    "created_trade_date": "2026-06-15",
                    "resolved_trade_date": "2026-06-15",
                    "promoted_trade_date": "2026-06-15",
                    "evaluation_days": 3,
                    "total_delta_rub": 1800.0,
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            run_dir = project_root / "reports" / "paper_runs" / "v7_live_20260525"
            runtime_dir = project_root / "reports" / "runtime"
            latest_root = project_root / "reports" / "autonomy" / "latest"
            run_dir.mkdir(parents=True)
            runtime_dir.mkdir(parents=True)
            latest_root.mkdir(parents=True)
            (runtime_dir / "v7_paper_supervisor_20260525.log").write_text("ok\n", encoding="utf-8")
            (runtime_dir / "server_watchdog.log").write_text("ok\n", encoding="utf-8")
            (latest_root / "promoted_runtime_policy.json").write_text(
                json.dumps(promoted_runtime_state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            state_path = runtime_dir / "daily_autonomy_state.json"
            argv = [
                "daily_autonomy_runner.py",
                "--project-root",
                str(project_root),
                "--state-path",
                str(state_path),
                "--email-to",
                "ops@example.com",
            ]

            def fake_write_analysis_outputs(analysis_dir: Path, **_: object) -> None:
                analysis_dir.mkdir(parents=True, exist_ok=True)
                (analysis_dir / "daily_summary.md").write_text("# Summary\n", encoding="utf-8")
                (analysis_dir / "auto_policy.md").write_text("# Policy\n", encoding="utf-8")

            def fake_write_research_outputs(research_dir: Path, **_: object) -> dict[str, int]:
                research_dir.mkdir(parents=True, exist_ok=True)
                (research_dir / "research_summary.md").write_text("# Research\n", encoding="utf-8")
                return {"total": 0, "runtime_policy": 0, "shadow_backtest": 0, "research_then_runtime": 0, "autopromote_ready": 0}

            def fake_copy_bundle_outputs(bundle_dir: Path, **_: object) -> None:
                bundle_dir.mkdir(parents=True, exist_ok=True)
                (bundle_dir / "daily_summary.md").write_text("# Summary\n", encoding="utf-8")
                (bundle_dir / "auto_policy.md").write_text("# Policy\n", encoding="utf-8")

            def fake_build_zip(path: Path, bundle_dir: Path) -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"zip")

            with ExitStack() as stack:
                stack.enter_context(patch.object(dar, "load_primary_trades", return_value=all_rows))
                stack.enter_context(patch.object(dar, "load_profiles", return_value={"GLZ6": {"ticker": "GLZ6", "family": "GL"}}))
                stack.enter_context(patch.object(dar, "load_wide_spread_reviews", return_value=[]))
                stack.enter_context(patch.object(dar, "annotate_trade_rows", side_effect=lambda rows, profiles: None))
                stack.enter_context(patch.object(dar, "build_day_history", return_value=[{"trade_date": trade_date, "day_class": "good_day"}]))
                stack.enter_context(patch.object(dar, "build_recurring_killers", return_value=[]))
                stack.enter_context(patch.object(dar, "build_scenario_history", return_value=[{"scenario": "base"}]))
                stack.enter_context(patch.object(dar, "summarize_scenario_history", return_value=[{"scenario": "base"}]))
                stack.enter_context(patch.object(dar, "filter_trade_date", return_value=all_rows))
                stack.enter_context(patch.object(dar, "metrics", return_value=overall))
                stack.enter_context(patch.object(dar, "grouped_metrics", return_value=[]))
                stack.enter_context(patch.object(dar, "metrics_map", return_value={}))
                stack.enter_context(patch.object(dar, "build_microstructure_summary", return_value=[]))
                stack.enter_context(patch.object(dar, "ranked_tail", return_value=[]))
                stack.enter_context(patch.object(dar, "load_open_position_snapshot", return_value=[]))
                stack.enter_context(patch.object(dar, "summarize_open_positions", return_value={}))
                stack.enter_context(patch.object(dar, "load_roll_watch", return_value=[]))
                stack.enter_context(patch.object(dar, "load_margin_timeline", return_value=[]))
                stack.enter_context(patch.object(dar, "load_margin_snapshot_fallback", return_value=[]))
                stack.enter_context(patch.object(dar, "summarize_margin_day", return_value=[]))
                stack.enter_context(
                    patch.object(dar, "load_runtime_trade_model", return_value={"margin_mode": "leveraged_paper", "fee_model": {"broker": "tbank"}})
                )
                stack.enter_context(patch.object(dar, "build_research_scenarios", side_effect=[[], []]))
                stack.enter_context(patch.object(dar, "build_recommendations", return_value=["keep base"]))
                stack.enter_context(patch.object(dar, "build_auto_policy", return_value=auto_policy))
                stack.enter_context(patch.object(dar, "advance_candidate_gate", return_value=candidate_gate))
                stack.enter_context(patch.object(dar, "merge_watchdog_overrides", side_effect=lambda policy, latest: policy))
                stack.enter_context(patch.object(dar, "build_optimizer_candidates", return_value=[]))
                stack.enter_context(patch.object(dar, "build_strategy_lab", return_value=[]))
                stack.enter_context(patch.object(dar, "build_strategy_review", return_value={}, create=True))
                stack.enter_context(patch.object(dar, "apply_strategy_review_candidates", side_effect=lambda policy, review: policy))
                stack.enter_context(patch.object(dar, "build_restriction_rows", return_value=[]))
                stack.enter_context(patch.object(dar, "build_summary_markdown", return_value="# Summary\n"))
                stack.enter_context(patch.object(dar, "pick_best_consensus_scenario", return_value={}))
                stack.enter_context(patch.object(dar, "write_analysis_outputs", side_effect=fake_write_analysis_outputs))
                stack.enter_context(patch.object(dar, "write_research_outputs", side_effect=fake_write_research_outputs))
                stack.enter_context(patch.object(dar, "copy_bundle_outputs", side_effect=fake_copy_bundle_outputs))
                stack.enter_context(patch.object(dar, "build_manifest_payload", return_value={"trade_date": trade_date}))
                stack.enter_context(patch.object(dar, "build_zip", side_effect=fake_build_zip))
                stack.enter_context(patch.object(dar, "send_email", return_value=(True, "sent")))
                stack.enter_context(patch.object(sys, "argv", argv))
                rc = dar.main()

            self.assertEqual(rc, 0)

            latest_policy = json.loads((latest_root / "latest_auto_policy.json").read_text(encoding="utf-8"))
            latest_promoted = json.loads((latest_root / "promoted_runtime_policy.json").read_text(encoding="utf-8"))
            analysis_promoted = json.loads(
                (project_root / "reports" / "autonomy" / "analysis" / trade_date / "promoted_runtime_policy.json").read_text(encoding="utf-8")
            )

            self.assertEqual(latest_policy["active"]["entry_no_new_after"], "11:59")
            self.assertEqual(latest_policy["active_base"]["entry_no_new_after"], "11:59")
            self.assertEqual(latest_policy["promoted_runtime"]["promoted_candidate_count"], 1)
            self.assertEqual(latest_promoted["active_base"]["entry_no_new_after"], "11:59")
            self.assertEqual(analysis_promoted["active_base"]["entry_no_new_after"], "11:59")

    def test_main_persists_status_and_latest_artifacts_with_strategy_review(self) -> None:
        trade_date = "2026-06-15"
        strategy_review_summary = f"reports/autonomy/research/{trade_date}/strategy_review_summary.md"
        strategy_review_candidates = f"reports/autonomy/research/{trade_date}/strategy_review_candidates.csv"

        all_rows = [
            {
                "trade_date": trade_date,
                "secid": "GLZ6",
                "portfolio_group": "GL_WATCH",
                "contour": "AGGRESSIVE",
                "group_key": "GL_WATCH/AGGRESSIVE",
                "family": "GL",
                "net_rub": 1234.5,
            }
        ]
        overall = {
            "trades": 7,
            "net_rub": 1234.5,
            "win_rate_pct": 57.14,
            "expectancy_rub": 176.36,
        }
        research_day = [{"scenario": "base", "net_rub": 1234.5, "trades": 7, "expectancy_rub": 176.36}]
        strategy_lab = [{"candidate": "strict_plus_aggressive", "action_type": "runtime_policy", "autopromote_ready": True}]
        strategy_review = {
            "generated": True,
            "summary_path": strategy_review_summary,
            "artifacts": [strategy_review_summary, strategy_review_candidates],
        }
        candidate_gate = {
            "summary": {
                "pending_count": 1,
                "promoted_now_count": 0,
                "rejected_now_count": 0,
            },
            "promoted_now": [],
        }
        auto_policy = {
            "active": {"entry_no_new_after": "17:45"},
            "active_base": {"entry_no_new_after": "17:45"},
            "summary": {"active_rule_count": 1},
        }

        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            run_dir = project_root / "reports" / "paper_runs" / "v7_live_20260525"
            runtime_dir = project_root / "reports" / "runtime"
            run_dir.mkdir(parents=True)
            runtime_dir.mkdir(parents=True)
            (runtime_dir / "v7_paper_supervisor_20260525.log").write_text("ok\n", encoding="utf-8")
            (runtime_dir / "server_watchdog.log").write_text("ok\n", encoding="utf-8")

            state_path = runtime_dir / "daily_autonomy_state.json"
            argv = [
                "daily_autonomy_runner.py",
                "--project-root",
                str(project_root),
                "--state-path",
                str(state_path),
                "--email-to",
                "ops@example.com",
            ]

            def fake_write_analysis_outputs(analysis_dir: Path, **_: object) -> None:
                analysis_dir.mkdir(parents=True, exist_ok=True)
                (analysis_dir / "daily_summary.md").write_text("# Summary\n", encoding="utf-8")
                (analysis_dir / "auto_policy.md").write_text("# Policy\n", encoding="utf-8")

            def fake_write_research_outputs(research_dir: Path, **_: object) -> dict[str, int]:
                research_dir.mkdir(parents=True, exist_ok=True)
                (research_dir / "research_summary.md").write_text("# Research\n", encoding="utf-8")
                (research_dir / "optimizer_summary.md").write_text("# Optimizer\n", encoding="utf-8")
                (research_dir / "strategy_lab_summary.md").write_text("# Strategy Lab\n", encoding="utf-8")
                (research_dir / "strategy_review_summary.md").write_text("# Strategy Review\n", encoding="utf-8")
                (research_dir / "strategy_review_candidates.csv").write_text("candidate,status\nstrict_plus_aggressive,ready\n", encoding="utf-8")
                return {"total": 1, "runtime_policy": 1, "shadow_backtest": 0, "research_then_runtime": 0, "autopromote_ready": 1}

            def fake_copy_bundle_outputs(bundle_dir: Path, **_: object) -> None:
                bundle_dir.mkdir(parents=True, exist_ok=True)
                (bundle_dir / "daily_summary.md").write_text("# Summary\n", encoding="utf-8")
                (bundle_dir / "auto_policy.md").write_text("# Policy\n", encoding="utf-8")
                (bundle_dir / "research_summary.md").write_text("# Research\n", encoding="utf-8")
                (bundle_dir / "optimizer_summary.md").write_text("# Optimizer\n", encoding="utf-8")
                (bundle_dir / "strategy_lab_summary.md").write_text("# Strategy Lab\n", encoding="utf-8")
                (bundle_dir / "strategy_review_summary.md").write_text("# Strategy Review\n", encoding="utf-8")
                (bundle_dir / "strategy_review_candidates.csv").write_text("candidate,status\nstrict_plus_aggressive,ready\n", encoding="utf-8")

            def fake_build_zip(path: Path, bundle_dir: Path) -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"zip")

            with ExitStack() as stack:
                stack.enter_context(patch.object(dar, "load_primary_trades", return_value=all_rows))
                stack.enter_context(patch.object(dar, "load_profiles", return_value={"GLZ6": {"ticker": "GLZ6", "family": "GL"}}))
                stack.enter_context(patch.object(dar, "load_wide_spread_reviews", return_value=[]))
                stack.enter_context(patch.object(dar, "annotate_trade_rows", side_effect=lambda rows, profiles: None))
                stack.enter_context(patch.object(dar, "build_day_history", return_value=[{"trade_date": trade_date, "day_class": "good_day"}]))
                stack.enter_context(patch.object(dar, "build_recurring_killers", return_value=[]))
                stack.enter_context(patch.object(dar, "build_scenario_history", return_value=[{"scenario": "base"}]))
                stack.enter_context(patch.object(dar, "summarize_scenario_history", return_value=[{"scenario": "base"}]))
                stack.enter_context(patch.object(dar, "filter_trade_date", return_value=all_rows))
                stack.enter_context(patch.object(dar, "metrics", return_value=overall))
                stack.enter_context(patch.object(dar, "grouped_metrics", return_value=[]))
                stack.enter_context(patch.object(dar, "metrics_map", return_value={}))
                stack.enter_context(patch.object(dar, "build_microstructure_summary", return_value=[]))
                stack.enter_context(patch.object(dar, "ranked_tail", return_value=[]))
                stack.enter_context(patch.object(dar, "load_open_position_snapshot", return_value=[]))
                stack.enter_context(patch.object(dar, "summarize_open_positions", return_value={}))
                stack.enter_context(patch.object(dar, "load_roll_watch", return_value=[]))
                stack.enter_context(patch.object(dar, "load_margin_timeline", return_value=[]))
                stack.enter_context(patch.object(dar, "load_margin_snapshot_fallback", return_value=[]))
                stack.enter_context(patch.object(dar, "summarize_margin_day", return_value=[]))
                stack.enter_context(
                    patch.object(dar, "load_runtime_trade_model", return_value={"margin_mode": "leveraged_paper", "fee_model": {"broker": "tbank"}})
                )
                stack.enter_context(patch.object(dar, "build_research_scenarios", side_effect=[research_day, research_day]))
                stack.enter_context(patch.object(dar, "build_recommendations", return_value=["keep base"]))
                stack.enter_context(patch.object(dar, "build_auto_policy", return_value=auto_policy))
                stack.enter_context(patch.object(dar, "advance_candidate_gate", return_value=candidate_gate))
                stack.enter_context(patch.object(dar, "merge_watchdog_overrides", side_effect=lambda policy, latest: policy))
                stack.enter_context(patch.object(dar, "build_optimizer_candidates", return_value=[{"scenario": "no_new_after_1745"}]))
                stack.enter_context(patch.object(dar, "build_strategy_lab", return_value=strategy_lab))
                stack.enter_context(patch.object(dar, "build_strategy_review", return_value=strategy_review, create=True))
                stack.enter_context(patch.object(dar, "build_restriction_rows", return_value=[{"restriction_type": "entry_no_new_after"}]))
                stack.enter_context(patch.object(dar, "build_summary_markdown", return_value="# Summary\n"))
                stack.enter_context(patch.object(dar, "pick_best_consensus_scenario", return_value={"scenario": "base"}))
                stack.enter_context(patch.object(dar, "write_analysis_outputs", side_effect=fake_write_analysis_outputs))
                stack.enter_context(patch.object(dar, "write_research_outputs", side_effect=fake_write_research_outputs))
                stack.enter_context(patch.object(dar, "copy_bundle_outputs", side_effect=fake_copy_bundle_outputs))
                stack.enter_context(patch.object(dar, "build_manifest_payload", return_value={"trade_date": trade_date, "strategy_review": strategy_review}))
                stack.enter_context(patch.object(dar, "build_zip", side_effect=fake_build_zip))
                stack.enter_context(patch.object(dar, "send_email", return_value=(True, "sent")))
                stack.enter_context(patch.object(sys, "argv", argv))
                rc = dar.main()

            self.assertEqual(rc, 0)

            latest_root = project_root / "reports" / "autonomy" / "latest"
            analysis_status_path = project_root / "reports" / "autonomy" / "analysis" / trade_date / "nightly_cycle_status.json"
            bundle_status_path = project_root / "reports" / "autonomy" / "archives" / f"bundle_{trade_date}" / "nightly_cycle_status.json"
            latest_status_path = latest_root / "latest_nightly_cycle_status.json"
            latest_manifest_path = latest_root / "latest_daily_manifest.json"
            latest_email_status_path = latest_root / "latest_email_status.json"
            latest_registry_path = latest_root / "research_strategy_registry.csv"
            latest_registry_summary_path = latest_root / "research_strategy_registry_summary.json"
            latest_shortlist_path = latest_root / "paper_candidate_shortlist.csv"
            latest_shortlist_summary_path = latest_root / "paper_candidate_shortlist_summary.json"

            self.assertTrue(analysis_status_path.exists(), analysis_status_path)
            self.assertTrue(bundle_status_path.exists(), bundle_status_path)
            self.assertTrue(latest_status_path.exists(), latest_status_path)
            self.assertTrue(latest_manifest_path.exists(), latest_manifest_path)
            self.assertTrue(latest_email_status_path.exists(), latest_email_status_path)
            self.assertTrue(latest_registry_path.exists(), latest_registry_path)
            self.assertTrue(latest_registry_summary_path.exists(), latest_registry_summary_path)
            self.assertTrue(latest_shortlist_path.exists(), latest_shortlist_path)
            self.assertTrue(latest_shortlist_summary_path.exists(), latest_shortlist_summary_path)
            self.assertTrue(state_path.exists(), state_path)

            analysis_status = json.loads(analysis_status_path.read_text(encoding="utf-8"))
            bundle_status = json.loads(bundle_status_path.read_text(encoding="utf-8"))
            latest_status = json.loads(latest_status_path.read_text(encoding="utf-8"))
            latest_manifest = json.loads(latest_manifest_path.read_text(encoding="utf-8"))
            latest_email_status = json.loads(latest_email_status_path.read_text(encoding="utf-8"))
            latest_registry_summary = json.loads(latest_registry_summary_path.read_text(encoding="utf-8"))
            latest_shortlist_summary = json.loads(latest_shortlist_summary_path.read_text(encoding="utf-8"))
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))

            self.assertEqual(analysis_status, bundle_status)
            self.assertEqual(bundle_status, latest_status)
            self.assertEqual(latest_manifest["nightly_cycle_status"], latest_status)
            self.assertEqual(latest_status["stages"]["candidate_gate"]["pending"], 1)
            self.assertEqual(latest_status["stages"]["strategy_review"]["status"], "ok")
            self.assertTrue(latest_status["stages"]["strategy_review"]["generated"])
            self.assertEqual(latest_status["stages"]["strategy_review"]["summary_path"], strategy_review_summary)
            self.assertEqual(latest_status["stages"]["strategy_review"]["artifacts"], [strategy_review_summary, strategy_review_candidates])
            self.assertTrue(latest_status["stages"]["summary"]["archive_ready"])
            self.assertEqual(latest_status["stages"]["email"]["status"], "sent")
            self.assertTrue(latest_status["stages"]["email"]["sent"])
            self.assertEqual(latest_status["stages"]["email"]["recipient"], "ops@example.com")
            self.assertEqual(latest_email_status["status"], "sent")
            self.assertTrue(latest_email_status["sent"])
            self.assertEqual(latest_email_status["trade_date"], trade_date)
            self.assertEqual(state_payload["last_trade_date_sent"], trade_date)
            self.assertEqual(state_payload["last_email_status"], "sent")
            self.assertEqual(state_payload["archive"], latest_status["stages"]["summary"]["archive_path"])
            self.assertEqual(latest_manifest["archive"], latest_status["stages"]["summary"]["archive_path"])
            self.assertIn("research_strategy_registry", latest_manifest)
            self.assertIn("research_strategy_registry_top", latest_manifest)
            self.assertIn("paper_candidate_shortlist", latest_manifest)
            self.assertIn("paper_candidate_shortlist_top", latest_manifest)
            self.assertEqual(latest_registry_summary["trade_date"], trade_date)
            self.assertEqual(latest_shortlist_summary["trade_date"], trade_date)


if __name__ == "__main__":
    unittest.main()
