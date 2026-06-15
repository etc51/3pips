from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import server_watchdog as sw  # noqa: E402


class ServerWatchdogStabilityTest(unittest.TestCase):
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

        with patch.object(sw, "load_closed_trade_rows", return_value=closed_rows):
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


if __name__ == "__main__":
    unittest.main()
