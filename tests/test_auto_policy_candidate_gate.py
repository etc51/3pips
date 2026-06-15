from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import auto_policy_candidate as apc  # noqa: E402


class AutoPolicyCandidateGateTest(unittest.TestCase):
    def test_promotes_candidate_after_consistent_future_days(self) -> None:
        auto_policy = {
            "active_base": {
                "entry_no_trade_before": None,
                "entry_no_new_after": None,
                "entry_blackout_windows": [],
                "entry_blackout_group_windows": {},
                "entry_max_full_stop_rub": None,
                "notes": [],
            },
            "proposed": {
                "candidate_entry_start": "",
                "candidate_entry_start_anchor": "",
                "candidate_entry_cutoff": "17:45",
                "candidate_entry_cutoff_anchor": "no_new_after_1745",
                "candidate_entry_blackout_windows": [],
                "candidate_entry_blackout_anchor": "",
                "candidate_group_blackout_windows": {},
                "candidate_group_blackout_anchor": "",
                "candidate_stop_cap_rub": None,
                "candidate_stop_cap_anchor": "",
            },
        }
        initial_history = [
            {"scenario": "base", "trade_date": "2026-06-15", "net_rub": 1000},
            {"scenario": "no_new_after_1745", "trade_date": "2026-06-15", "net_rub": 1300},
        ]
        state = apc.advance_candidate_gate({}, auto_policy, initial_history, "2026-06-15")
        self.assertEqual(state["summary"]["pending_count"], 1)
        self.assertEqual(state["summary"]["promoted_now_count"], 0)

        later_history = initial_history + [
            {"scenario": "base", "trade_date": "2026-06-16", "net_rub": 1000},
            {"scenario": "no_new_after_1745", "trade_date": "2026-06-16", "net_rub": 1600},
            {"scenario": "base", "trade_date": "2026-06-17", "net_rub": 900},
            {"scenario": "no_new_after_1745", "trade_date": "2026-06-17", "net_rub": 1400},
            {"scenario": "base", "trade_date": "2026-06-18", "net_rub": 1200},
            {"scenario": "no_new_after_1745", "trade_date": "2026-06-18", "net_rub": 1900},
        ]
        advanced = apc.advance_candidate_gate(state, auto_policy, later_history, "2026-06-18")
        self.assertEqual(advanced["summary"]["pending_count"], 0)
        self.assertEqual(advanced["summary"]["promoted_now_count"], 1)
        promoted = advanced["promoted_now"][0]
        self.assertEqual(promoted["policy_key"], "entry_no_new_after")
        self.assertEqual(promoted["value"], "17:45")
        self.assertEqual(promoted["evaluation_days"], 3)
        self.assertEqual(promoted["beat_base_days"], 3)
        self.assertGreater(promoted["total_delta_rub"], 1000.0)

        promoted_active = apc.apply_promoted_candidates(auto_policy["active_base"], advanced["promoted_now"])
        self.assertEqual(promoted_active["entry_no_new_after"], "17:45")
        self.assertTrue(any("Candidate gate:" in note for note in promoted_active["notes"]))


if __name__ == "__main__":
    unittest.main()
