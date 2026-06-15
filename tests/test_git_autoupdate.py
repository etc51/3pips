from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
