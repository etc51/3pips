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
