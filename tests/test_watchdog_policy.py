from __future__ import annotations

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

import watchdog_policy as wp  # noqa: E402


class WatchdogPolicyTest(unittest.TestCase):
    def test_diff_reasons_ignore_note_only_drift(self) -> None:
        current_overrides = {
            "observe_only_tickers": ["glz6"],
            "notes": ["second", "first"],
        }
        next_overrides = {
            "observe_only_tickers": ["GLZ6"],
            "notes": ["first", "second", "first"],
        }
        current_active = {
            "observe_only_tickers": ["GLZ6"],
            "notes": ["alpha", "beta"],
            "debug_temp": "ignore-me",
        }
        next_active = {
            "observe_only_tickers": ["glz6"],
            "notes": ["beta", "alpha"],
        }

        reasons = wp.diff_watchdog_policy_reasons(
            current_overrides,
            next_overrides,
            current_active,
            next_active,
            current_active,
            next_active,
            include_notes=False,
        )

        self.assertEqual(reasons, [])

    def test_refresh_ignores_note_only_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            policy_dir = project_root / "reports" / "autonomy" / "latest"
            run_dir = project_root / "reports" / "paper_runs" / "v7_live_20260525"
            policy_dir.mkdir(parents=True)
            run_dir.mkdir(parents=True)
            policy_path = policy_dir / "latest_auto_policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "trade_date": "2026-06-15",
                        "watchdog_overrides": {
                            "trade_date": "2026-06-15",
                            "observe_only_tickers": ["GLZ6"],
                            "observe_only_families": [],
                            "observe_only_group_families": [],
                            "entry_blackout_group_windows": {},
                            "notes": ["second", "first"],
                        },
                        "active_base": {
                            "observe_only_portfolios": [],
                            "observe_only_group_families": [],
                            "allow_aggressive_group_families": [],
                            "observe_only_tickers": [],
                            "observe_only_families": [],
                            "strict_only_tickers": [],
                            "strict_only_families": [],
                            "entry_blackout_windows": [],
                            "entry_blackout_group_windows": {},
                            "entry_no_trade_before": None,
                            "entry_no_new_after": None,
                            "entry_max_full_stop_rub": 1000,
                            "pause_ticker_after_losses": 1,
                            "pause_family_after_losses": None,
                            "pause_after_loss_minutes": 120,
                            "notes": ["base note"],
                        },
                        "active": {
                            "observe_only_portfolios": [],
                            "observe_only_group_families": [],
                            "allow_aggressive_group_families": [],
                            "observe_only_tickers": ["GLZ6"],
                            "observe_only_families": [],
                            "strict_only_tickers": [],
                            "strict_only_families": [],
                            "entry_blackout_windows": [],
                            "entry_blackout_group_windows": {},
                            "entry_no_trade_before": None,
                            "entry_no_new_after": None,
                            "entry_max_full_stop_rub": 1000,
                            "pause_ticker_after_losses": 1,
                            "pause_family_after_losses": None,
                            "pause_after_loss_minutes": 120,
                            "notes": ["base note", "second", "first"],
                            "transient_debug": "ignore-me",
                        },
                        "summary": {},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            next_overrides = {
                "trade_date": "2026-06-15",
                "observe_only_tickers": ["glz6"],
                "observe_only_families": [],
                "observe_only_group_families": [],
                "entry_blackout_group_windows": {},
                "notes": ["first", "second", "first"],
            }

            with patch.object(wp, "latest_trade_date", return_value="2026-06-15"), patch.object(
                wp, "compute_intraday_watchdog_overrides", return_value=next_overrides
            ), patch.object(wp, "load_dashboard_state", return_value={}):
                changed, summary = wp.refresh_intraday_killer_policy(project_root, run_dir, "http://127.0.0.1:8768/")

            self.assertFalse(changed)
            self.assertIn("reasons=-", summary)
            self.assertIn("note_drift=no", summary)


if __name__ == "__main__":
    unittest.main()
