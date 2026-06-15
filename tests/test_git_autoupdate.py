from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import git_autoupdate as gau  # noqa: E402


class _FakeDateTime:
    current: datetime

    @classmethod
    def now(cls, tz=None) -> datetime:
        if tz is not None:
            return cls.current.astimezone(tz)
        return cls.current


class GitAutoupdateWindowTest(unittest.TestCase):
    def test_blocks_during_entry_window(self) -> None:
        _FakeDateTime.current = datetime(2026, 6, 15, 11, 0, tzinfo=gau.MSK)
        with patch.object(gau, "datetime", _FakeDateTime):
            allowed, reason = gau.restart_allowed_now(ROOT, "v7_live_20260525")
        self.assertFalse(allowed)
        self.assertTrue(reason.startswith("entry_window "))

    def test_blocks_when_open_positions_remain(self) -> None:
        _FakeDateTime.current = datetime(2026, 6, 15, 19, 30, tzinfo=gau.MSK)
        with patch.object(gau, "datetime", _FakeDateTime), patch.object(
            gau,
            "runtime_open_positions",
            return_value=(2, ["classic_core=1", "neo=1"]),
        ):
            allowed, reason = gau.restart_allowed_now(ROOT, "v7_live_20260525")
        self.assertFalse(allowed)
        self.assertIn("open_positions=2", reason)

    def test_allows_restart_after_window_without_positions(self) -> None:
        _FakeDateTime.current = datetime(2026, 6, 15, 20, 30, tzinfo=gau.MSK)
        with patch.object(gau, "datetime", _FakeDateTime), patch.object(
            gau,
            "runtime_open_positions",
            return_value=(0, []),
        ):
            allowed, reason = gau.restart_allowed_now(ROOT, "v7_live_20260525")
        self.assertTrue(allowed)
        self.assertTrue(reason.startswith("safe_window "))

    def test_rollout_lock_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "rollout_lock.json"
            gau.write_rollout_lock(lock_path, {"reason": "apply_remote_update", "new_head": "abc123"})
            self.assertTrue(lock_path.exists())
            gau.clear_rollout_lock(lock_path)
            self.assertFalse(lock_path.exists())

    def test_verify_runtime_ready_reports_dashboard_and_artifact_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            run_dir = project_root / "reports" / "paper_runs" / "v7_live_20260525"
            run_dir.mkdir(parents=True)
            (run_dir / "classic_core_health.json").write_text('{"timestamp":"2026-06-15T17:00:00+03:00"}', encoding="utf-8")
            (run_dir / "classic_core_paper_open_positions.json").write_text("{}", encoding="utf-8")
            with patch.object(gau, "dashboard_ok", return_value=(False, "urlerror:test")):
                issues = gau.verify_runtime_ready(project_root, "v7_live_20260525", "http://127.0.0.1:8768/", 999999)
        self.assertTrue(any(item.startswith("dashboard_down[") for item in issues))
        self.assertTrue(any(item.startswith("bad_open_positions[") for item in issues))

    def test_main_fails_when_post_restart_verification_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            runtime_dir = project_root / "reports" / "runtime"
            runtime_dir.mkdir(parents=True)
            pending_path = runtime_dir / "git_autoupdate_pending_restart.json"
            status_path = runtime_dir / "git_autoupdate_status.json"
            calls: list[tuple[list[str], Path]] = []

            def fake_run(cmd: list[str], cwd: Path):
                calls.append((cmd, cwd))
                if cmd[:4] == ["git", "-c", f"safe.directory={project_root}", "status"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if cmd[:4] == ["git", "-c", f"safe.directory={project_root}", "fetch"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if cmd[:4] == ["git", "-c", f"safe.directory={project_root}", "rev-parse"]:
                    return SimpleNamespace(returncode=0, stdout="newsha\n", stderr="")
                if cmd[:2] == ["systemctl", "restart"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if cmd[:2] == ["systemctl", "is-active"]:
                    return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
                raise AssertionError(f"unexpected command: {cmd}")

            pending_path.write_text(
                __import__("json").dumps(
                    {
                        "old_head": "oldsha",
                        "new_head": "newsha",
                        "reason": "pending_restart old=oldsha new=newsha",
                        "deferred_because": "entry_window 17:20",
                        "deps_ready": True,
                        "merged": True,
                    }
                ),
                encoding="utf-8",
            )
            argv = [
                "git_autoupdate.py",
                "--project-root",
                str(project_root),
                "--pending-restart-path",
                str(pending_path),
                "--restart-wait-sec",
                "0",
            ]
            with patch.object(gau, "run", side_effect=fake_run), patch.object(
                gau, "restart_allowed_now", return_value=(True, "safe_window 18:10")
            ), patch.object(
                gau, "verify_runtime_ready", return_value=["dashboard_down[urlerror:test]"]
            ), patch.object(sys, "argv", argv):
                rc = gau.main()

            self.assertEqual(rc, 1)
            self.assertTrue(pending_path.exists())
            self.assertTrue(any(cmd[:2] == ["systemctl", "restart"] for cmd, _cwd in calls))
            payload = __import__("json").loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(payload.get("outcome"), "failed")
            self.assertEqual(payload.get("reason"), "post_restart_verification_failed")
            self.assertEqual(payload.get("new_head"), "newsha")

    def test_main_defers_before_merge_when_window_is_unsafe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            runtime_dir = project_root / "reports" / "runtime"
            runtime_dir.mkdir(parents=True)
            pending_path = runtime_dir / "git_autoupdate_pending_restart.json"
            status_path = runtime_dir / "git_autoupdate_status.json"

            calls: list[tuple[list[str], Path]] = []

            def fake_run(cmd: list[str], cwd: Path):
                calls.append((cmd, cwd))
                if cmd[:4] == ["git", "-c", f"safe.directory={project_root}", "status"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if cmd[:4] == ["git", "-c", f"safe.directory={project_root}", "fetch"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if cmd[:4] == ["git", "-c", f"safe.directory={project_root}", "rev-parse"]:
                    if cmd[-1] == "HEAD":
                        return SimpleNamespace(returncode=0, stdout="oldsha\n", stderr="")
                    if cmd[-1] == "origin/rollback-20260525-a26cf99":
                        return SimpleNamespace(returncode=0, stdout="newsha\n", stderr="")
                if cmd[:4] == ["git", "-c", f"safe.directory={project_root}", "diff"]:
                    return SimpleNamespace(returncode=0, stdout="scripts/server_watchdog.py\n", stderr="")
                raise AssertionError(f"unexpected command: {cmd}")

            argv = [
                "git_autoupdate.py",
                "--project-root",
                str(project_root),
                "--branch",
                "rollback-20260525-a26cf99",
                "--pending-restart-path",
                str(pending_path),
            ]
            with patch.object(gau, "run", side_effect=fake_run), patch.object(
                gau,
                "restart_allowed_now",
                return_value=(False, "entry_window 11:00"),
            ), patch.object(sys, "argv", argv):
                rc = gau.main()

            self.assertEqual(rc, 0)
            self.assertTrue(pending_path.exists())
            payload = __import__("json").loads(pending_path.read_text(encoding="utf-8"))
            self.assertEqual(payload.get("old_head"), "oldsha")
            self.assertEqual(payload.get("new_head"), "newsha")
            self.assertEqual(payload.get("merged"), False)
            self.assertEqual(payload.get("deferred_because"), "entry_window 11:00")
            self.assertFalse(any("merge" in cmd for cmd, _cwd in calls))
            status_payload = __import__("json").loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status_payload.get("outcome"), "deferred")
            self.assertEqual(status_payload.get("reason"), "remote_update_available")
            self.assertEqual(status_payload.get("deferred_because"), "entry_window 11:00")
            self.assertEqual(status_payload.get("new_head"), "newsha")

    def test_main_writes_status_when_worktree_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            runtime_dir = project_root / "reports" / "runtime"
            runtime_dir.mkdir(parents=True)
            status_path = runtime_dir / "git_autoupdate_status.json"

            def fake_run(cmd: list[str], cwd: Path):
                if cmd[:4] == ["git", "-c", f"safe.directory={project_root}", "status"]:
                    return SimpleNamespace(returncode=0, stdout=" M src/multi_futures_paper.py\n", stderr="")
                raise AssertionError(f"unexpected command: {cmd}")

            argv = [
                "git_autoupdate.py",
                "--project-root",
                str(project_root),
            ]
            with patch.object(gau, "run", side_effect=fake_run), patch.object(sys, "argv", argv):
                rc = gau.main()

            self.assertEqual(rc, 0)
            payload = __import__("json").loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(payload.get("outcome"), "skipped")
            self.assertEqual(payload.get("reason"), "dirty_worktree")


if __name__ == "__main__":
    unittest.main()
