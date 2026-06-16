from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import server_watchdog as sw  # noqa: E402
import watchdog_policy as wp  # noqa: E402


class ServerWatchdogStabilityTest(unittest.TestCase):
    def test_check_latest_autonomy_outputs_accepts_fresh_complete_latest_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            latest_dir = project_root / "reports" / "autonomy" / "latest"
            latest_dir.mkdir(parents=True)
            payloads = {
                "latest_auto_policy.json": {"trade_date": "2026-06-15"},
                "latest_nightly_cycle_status.json": {
                    "trade_date": "2026-06-15",
                    "status": "ok",
                    "stages": {"summary": {"status": "ok", "archive_ready": True}},
                },
                "latest_manifest.json": {
                    "trade_date": "2026-06-15",
                    "nightly_cycle_status": {"status": "ok"},
                    "archive": "reports/autonomy/archives/3pips_daily_2026-06-15.zip",
                    "research_strategy_registry": {"top": 1},
                    "paper_candidate_shortlist": {"top": 1},
                    "research_strategy_targets": {"top": 1},
                },
                "research_strategy_registry_summary.json": {"rows": 3},
                "paper_candidate_shortlist_summary.json": {"rows": 2},
                "research_strategy_targets_summary.json": {"rows": 1},
            }
            for name, payload in payloads.items():
                (latest_dir / name).write_text(__import__("json").dumps(payload), encoding="utf-8")

            issues = sw.check_latest_autonomy_outputs(project_root, "2026-06-15", 7200)

        self.assertEqual(issues, [])

    def test_check_latest_autonomy_outputs_flags_incomplete_latest_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            latest_dir = project_root / "reports" / "autonomy" / "latest"
            latest_dir.mkdir(parents=True)
            (latest_dir / "latest_auto_policy.json").write_text(
                __import__("json").dumps({"trade_date": "2026-06-14"}),
                encoding="utf-8",
            )
            (latest_dir / "latest_nightly_cycle_status.json").write_text(
                __import__("json").dumps(
                    {
                        "trade_date": "2026-06-14",
                        "status": "degraded",
                        "stages": {"summary": {"status": "degraded", "archive_ready": False}},
                    }
                ),
                encoding="utf-8",
            )
            (latest_dir / "latest_manifest.json").write_text(
                __import__("json").dumps({"trade_date": "2026-06-14", "nightly_cycle_status": {"status": "degraded"}}),
                encoding="utf-8",
            )

            issues = sw.check_latest_autonomy_outputs(project_root, "2026-06-15", 7200)

        self.assertIn("latest_auto_policy_trade_date_mismatch[2026-06-14!=2026-06-15]", issues)
        self.assertIn("nightly_trade_date_mismatch[2026-06-14!=2026-06-15]", issues)
        self.assertIn("nightly_status_not_ok[degraded]", issues)
        self.assertIn("nightly_summary_status_not_ok[degraded]", issues)
        self.assertIn("nightly_archive_not_ready", issues)
        self.assertIn("latest_manifest_trade_date_mismatch[2026-06-14!=2026-06-15]", issues)
        self.assertIn("latest_manifest_missing_key[archive]", issues)
        self.assertIn("latest_manifest_missing_key[research_strategy_registry]", issues)
        self.assertIn("latest_manifest_missing_key[paper_candidate_shortlist]", issues)
        self.assertIn("latest_manifest_missing_key[research_strategy_targets]", issues)
        self.assertIn("latest_manifest_nightly_status_not_ok[degraded]", issues)
        self.assertIn("missing_latest_artifact[research_strategy_registry_summary.json]", issues)
        self.assertIn("missing_latest_artifact[paper_candidate_shortlist_summary.json]", issues)
        self.assertIn("missing_latest_artifact[research_strategy_targets_summary.json]", issues)

    def test_check_automation_health_flags_timer_and_dirty_autoupdate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "git_autoupdate_status.json"
            status_path.write_text(
                __import__("json").dumps(
                    {
                        "updated_at": "2026-06-16 04:13:27",
                        "outcome": "skipped",
                        "reason": "dirty_worktree",
                        "pending_restart_exists": False,
                        "rollout_lock_exists": False,
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(sw, "unit_enabled", side_effect=lambda name: name != "3pips-daily-autonomy.timer"), patch.object(
                sw, "service_active", side_effect=lambda name: name != "3pips-daily-autonomy.timer"
            ):
                issues = sw.check_automation_health(
                    ["3pips-watchdog.timer", "3pips-daily-autonomy.timer"],
                    status_path,
                    7200,
                )

        self.assertIn("timer_disabled[3pips-daily-autonomy.timer]", issues)
        self.assertIn("timer_inactive[3pips-daily-autonomy.timer]", issues)
        self.assertIn("git_autoupdate_blocked[dirty_worktree]", issues)

    def test_overrides_notes_stay_stable_when_only_open_pnl_moves(self) -> None:
        closed_rows = [
            {
                "secid": "GLZ6",
                "family": "GL",
                "portfolio_group": "GL_WATCH",
                "contour": "AGGRESSIVE",
                "net_rub": -3941.0,
            }
        ]
        base_active = {
            "observe_only_portfolios": [],
            "observe_only_group_families": [],
            "allow_aggressive_group_families": [],
            "observe_only_tickers": [],
            "observe_only_families": [],
            "strict_only_tickers": [],
            "strict_only_families": [],
            "entry_blackout_windows": [],
            "entry_blackout_group_windows": {"GL_WATCH/AGGRESSIVE": ["12:00-13:59"]},
            "entry_no_trade_before": None,
            "entry_no_new_after": None,
            "entry_max_full_stop_rub": 500,
            "pause_ticker_after_losses": 1,
            "pause_family_after_losses": None,
            "pause_after_loss_minutes": 120,
            "notes": [],
        }
        dashboard_state_a = {
            "open_positions": [
                {
                    "ticker": "GLZ6",
                    "portfolio": "GL_WATCH",
                    "contour": "AGGRESSIVE",
                    "unrealized_net_rub": -84.12,
                }
            ]
        }
        dashboard_state_b = {
            "open_positions": [
                {
                    "ticker": "GLZ6",
                    "portfolio": "GL_WATCH",
                    "contour": "AGGRESSIVE",
                    "unrealized_net_rub": -82.92,
                }
            ]
        }

        with patch.object(wp, "load_closed_trade_rows", return_value=closed_rows):
            overrides_a = sw.compute_intraday_watchdog_overrides(
                Path("."),
                "2026-06-15",
                dashboard_state_a,
                base_active,
            )
            overrides_b = sw.compute_intraday_watchdog_overrides(
                Path("."),
                "2026-06-15",
                dashboard_state_b,
                base_active,
            )

        self.assertEqual(overrides_a, overrides_b)
        self.assertEqual(overrides_a.get("observe_only_tickers"), ["GLZ6"])
        self.assertIn(
            "watchdog intraday: GLZ6 -> observe-only after ticker damage threshold",
            overrides_a.get("notes") or [],
        )

    def test_fresh_rollout_lock_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "git_autoupdate_rollout_lock.json"
            lock_path.write_text('{"reason":"apply_remote_update"}', encoding="utf-8")
            payload, age_sec = sw.load_active_rollout_lock(lock_path, max_age_sec=60)
        self.assertEqual(payload.get("reason"), "apply_remote_update")
        self.assertGreaterEqual(age_sec, 0.0)

    def test_main_exits_early_under_maintenance_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            runtime_dir = project_root / "reports" / "runtime"
            run_dir = project_root / "reports" / "paper_runs" / "v7_live_20260525"
            runtime_dir.mkdir(parents=True)
            run_dir.mkdir(parents=True)
            state_path = runtime_dir / "server_watchdog_state.json"
            log_path = runtime_dir / "server_watchdog.log"

            argv = [
                "server_watchdog.py",
                "--project-root",
                str(project_root),
                "--state-path",
                str(state_path),
                "--log-path",
                str(log_path),
            ]
            with patch.object(
                sw,
                "load_active_rollout_lock",
                return_value=({"reason": "apply_remote_update"}, 3.0),
            ), patch.object(
                sw.subprocess,
                "run",
                return_value=SimpleNamespace(stdout="test-host\n", returncode=0, stderr=""),
            ), patch.object(
                sys,
                "argv",
                argv,
            ):
                rc = sw.main()

            self.assertEqual(rc, 0)
            self.assertTrue(state_path.exists())
            payload = __import__("json").loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload.get("status"), "maintenance_lock")
            self.assertIn("maintenance_lock", payload.get("last_summary", ""))

    def test_dashboard_only_issue_waits_one_cycle_before_incident(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            runtime_dir = project_root / "reports" / "runtime"
            run_dir = project_root / "reports" / "paper_runs" / "v7_live_20260525"
            runtime_dir.mkdir(parents=True)
            run_dir.mkdir(parents=True)
            state_path = runtime_dir / "server_watchdog_state.json"
            log_path = runtime_dir / "server_watchdog.log"
            argv = [
                "server_watchdog.py",
                "--project-root",
                str(project_root),
                "--state-path",
                str(state_path),
                "--log-path",
                str(log_path),
                "--no-remediate",
            ]

            def run_once() -> int:
                with patch.object(sw, "load_active_rollout_lock", return_value=({}, 0.0)), patch.object(
                    sw, "refresh_intraday_killer_policy", return_value=(False, "trade_date=2026-06-15")
                ), patch.object(
                    sw, "should_check_daily_autonomy", return_value=False
                ), patch.object(
                    sw, "service_active", return_value=True
                ), patch.object(
                    sw, "service_age_sec", return_value=999.0
                ), patch.object(
                    sw, "dashboard_ok", return_value=(False, "urlerror:test")
                ), patch.object(
                    sw, "check_run_health", return_value=[]
                ), patch.object(
                    sw, "check_automation_health", return_value=[]
                ), patch.object(
                    sw, "check_latest_autonomy_outputs", return_value=[]
                ), patch.object(
                    sw, "send_email", return_value=(False, "disabled_missing_smtp")
                ), patch.object(
                    sw.subprocess, "run", return_value=SimpleNamespace(stdout="test-host\n", returncode=0, stderr="")
                ), patch.object(
                    sys, "argv", argv
                ):
                    return sw.main()

            rc1 = run_once()
            first = __import__("json").loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(rc1, 0)
            self.assertEqual(first.get("status"), "dashboard_unstable")
            self.assertEqual(first.get("dashboard_fail_count"), 1)

            rc2 = run_once()
            second = __import__("json").loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(rc2, 1)
            self.assertEqual(second.get("status"), "incident")
            self.assertEqual(second.get("dashboard_fail_count"), 2)
            self.assertIn("dashboard_down", second.get("last_summary", ""))

    def test_main_skips_restart_for_automation_only_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            runtime_dir = project_root / "reports" / "runtime"
            run_dir = project_root / "reports" / "paper_runs" / "v7_live_20260525"
            runtime_dir.mkdir(parents=True)
            run_dir.mkdir(parents=True)
            state_path = runtime_dir / "server_watchdog_state.json"
            log_path = runtime_dir / "server_watchdog.log"
            argv = [
                "server_watchdog.py",
                "--project-root",
                str(project_root),
                "--state-path",
                str(state_path),
                "--log-path",
                str(log_path),
            ]

            with patch.object(sw, "load_active_rollout_lock", return_value=({}, 0.0)), patch.object(
                sw, "refresh_intraday_killer_policy", return_value=(False, "trade_date=2026-06-15")
            ), patch.object(
                sw, "should_check_daily_autonomy", return_value=False
            ), patch.object(
                sw, "service_active", return_value=True
            ), patch.object(
                sw, "service_age_sec", return_value=999.0
            ), patch.object(
                sw, "dashboard_ok", return_value=(True, "http_200")
            ), patch.object(
                sw, "check_run_health", return_value=[]
            ), patch.object(
                sw, "check_automation_health", return_value=["timer_disabled[3pips-git-autoupdate.timer]"]
            ), patch.object(
                sw, "check_latest_autonomy_outputs", return_value=[]
            ), patch.object(
                sw, "restart_service"
            ) as restart_mock, patch.object(
                sw, "send_email", return_value=(False, "disabled_missing_smtp")
            ), patch.object(
                sw.subprocess, "run", return_value=SimpleNamespace(stdout="test-host\n", returncode=0, stderr="")
            ), patch.object(
                sys, "argv", argv
            ):
                rc = sw.main()

            self.assertEqual(rc, 1)
            restart_mock.assert_not_called()
            payload = __import__("json").loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload.get("status"), "incident")
            self.assertIn("timer_disabled", payload.get("last_summary", ""))

    def test_main_skips_restart_for_latest_autonomy_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            runtime_dir = project_root / "reports" / "runtime"
            run_dir = project_root / "reports" / "paper_runs" / "v7_live_20260525"
            runtime_dir.mkdir(parents=True)
            run_dir.mkdir(parents=True)
            state_path = runtime_dir / "server_watchdog_state.json"
            log_path = runtime_dir / "server_watchdog.log"
            argv = [
                "server_watchdog.py",
                "--project-root",
                str(project_root),
                "--state-path",
                str(state_path),
                "--log-path",
                str(log_path),
            ]

            with patch.object(sw, "load_active_rollout_lock", return_value=({}, 0.0)), patch.object(
                sw, "refresh_intraday_killer_policy", return_value=(False, "trade_date=2026-06-15")
            ), patch.object(
                sw, "latest_trade_date", return_value="2026-06-15"
            ), patch.object(
                sw, "should_check_daily_autonomy", return_value=False
            ), patch.object(
                sw, "service_active", return_value=True
            ), patch.object(
                sw, "service_age_sec", return_value=999.0
            ), patch.object(
                sw, "dashboard_ok", return_value=(True, "http_200")
            ), patch.object(
                sw, "check_run_health", return_value=[]
            ), patch.object(
                sw, "check_automation_health", return_value=[]
            ), patch.object(
                sw, "check_latest_autonomy_outputs", return_value=["nightly_archive_not_ready"]
            ), patch.object(
                sw, "restart_service"
            ) as restart_mock, patch.object(
                sw, "send_email", return_value=(False, "disabled_missing_smtp")
            ), patch.object(
                sw.subprocess, "run", return_value=SimpleNamespace(stdout="test-host\n", returncode=0, stderr="")
            ), patch.object(
                sys, "argv", argv
            ):
                rc = sw.main()

            self.assertEqual(rc, 1)
            restart_mock.assert_not_called()
            payload = __import__("json").loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload.get("status"), "incident")
            self.assertIn("nightly_archive_not_ready", payload.get("last_summary", ""))


if __name__ == "__main__":
    unittest.main()
