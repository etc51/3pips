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

import ubuntu_paper_supervisor as ups  # noqa: E402


def make_args(project_root: str) -> SimpleNamespace:
    return SimpleNamespace(
        project_root=project_root,
        python="python",
        dashboard_host="127.0.0.1",
        dashboard_port="8768",
        loop_sec=15,
        stale_sec=90,
        startup_grace_sec=180,
        once=True,
    )


class UbuntuPaperSupervisorDashboardTest(unittest.TestCase):
    def test_restart_bot_launches_multi_futures_in_paper_only_mode(self) -> None:
        class DummyProc:
            pid = 4321

        with tempfile.TemporaryDirectory() as tmp:
            sup = ups.Supervisor(make_args(tmp))
            sup.log = lambda _msg: None
            sup.setup()
            calls: list[dict] = []

            def fake_popen(cmd, cwd, stdout, stderr, start_new_session, close_fds):  # noqa: ANN001
                calls.append(
                    {
                        "cmd": list(cmd),
                        "cwd": cwd,
                        "stdout": getattr(stdout, "name", ""),
                        "stderr": getattr(stderr, "name", ""),
                        "start_new_session": start_new_session,
                        "close_fds": close_fds,
                    }
                )
                stdout.close()
                stderr.close()
                return DummyProc()

            with patch.object(sup, "stop_pid"), patch("ubuntu_paper_supervisor.subprocess.Popen", side_effect=fake_popen):
                for name, secids in ups.PORTFOLIOS.items():
                    del secids
                    sup.restart_bot(name, "unit_test")

            self.assertEqual(len(calls), len(ups.PORTFOLIOS))
            for call, name in zip(calls, ups.PORTFOLIOS):
                cmd = call["cmd"]
                self.assertEqual(cmd[0], "python")
                self.assertEqual(cmd[1], "src/multi_futures_paper.py")
                self.assertIn("--paper-only", cmd)
                self.assertIn("--entry-shadow-log", cmd)
                entry_shadow_idx = cmd.index("--entry-shadow-log")
                self.assertEqual(
                    cmd[entry_shadow_idx + 1],
                    f"reports/paper_runs/{ups.RUN_NAME}/{name}_entry_shadow_models.csv",
                )
                self.assertFalse(any(str(part).lower().startswith("--live") for part in cmd))
                self.assertEqual(call["cwd"], sup.root)
                self.assertTrue(call["start_new_session"])
                self.assertTrue(call["close_fds"])

    def test_first_dashboard_http_failure_waits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sup = ups.Supervisor(make_args(tmp))
            logs: list[str] = []
            sup.log = logs.append
            with patch.object(sup, "read_pid", return_value=123), patch.object(
                sup, "process_alive", return_value=True
            ), patch.object(sup, "dashboard_http_ok", return_value=False), patch.object(
                sup, "restart_dashboard"
            ) as restart:
                sup.check_dashboard()

        restart.assert_not_called()
        self.assertEqual(sup.dashboard_failures, 1)
        self.assertTrue(any("wait dashboard" in line for line in logs))

    def test_second_consecutive_dashboard_http_failure_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sup = ups.Supervisor(make_args(tmp))
            sup.log = lambda _msg: None
            with patch.object(sup, "read_pid", return_value=123), patch.object(
                sup, "process_alive", return_value=True
            ), patch.object(sup, "dashboard_http_ok", return_value=False), patch.object(
                sup, "restart_dashboard"
            ) as restart:
                sup.check_dashboard()
                sup.check_dashboard()

        restart.assert_called_once_with("http_check_failed_2x")
        self.assertEqual(sup.dashboard_failures, 2)

    def test_success_resets_dashboard_failure_counter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sup = ups.Supervisor(make_args(tmp))
            sup.dashboard_failures = 1
            sup.log = lambda _msg: None
            with patch.object(sup, "read_pid", return_value=123), patch.object(
                sup, "process_alive", return_value=True
            ), patch.object(sup, "dashboard_http_ok", return_value=True):
                sup.check_dashboard()

        self.assertEqual(sup.dashboard_failures, 0)


if __name__ == "__main__":
    unittest.main()
