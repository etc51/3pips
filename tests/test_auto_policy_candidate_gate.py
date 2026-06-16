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
                "entry_shadow_gate_group_models": {},
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

    def test_promotes_entry_shadow_gate_after_positive_future_days(self) -> None:
        auto_policy = {
            "active_base": {
                "entry_no_trade_before": None,
                "entry_no_new_after": None,
                "entry_blackout_windows": [],
                "entry_blackout_group_windows": {},
                "entry_shadow_gate_group_models": {},
                "entry_max_full_stop_rub": None,
                "notes": [],
            },
            "proposed": {
                "candidate_entry_start": "",
                "candidate_entry_start_anchor": "",
                "candidate_entry_cutoff": "",
                "candidate_entry_cutoff_anchor": "",
                "candidate_entry_blackout_windows": [],
                "candidate_entry_blackout_anchor": "",
                "candidate_group_blackout_windows": {},
                "candidate_group_blackout_anchor": "",
                "candidate_stop_cap_rub": None,
                "candidate_stop_cap_anchor": "",
                "candidate_entry_shadow_gate_rows": [
                    {
                        "candidate": "entry_shadow_gate::CLASSIC_CORE/STRICT/tv_ema_rsi_adx_trend",
                        "portfolio_group": "CLASSIC_CORE",
                        "contour": "STRICT",
                        "model": "tv_ema_rsi_adx_trend",
                        "promote_after_days": 2,
                        "min_total_delta_rub": 900.0,
                        "note": "positive entry-shadow gate candidate",
                    }
                ],
            },
        }

        initial = apc.advance_candidate_gate({}, auto_policy, [], "2026-06-15", strategy_review_history=[])
        self.assertEqual(initial["summary"]["pending_count"], 1)

        future_history = [
            {
                "trade_date": "2026-06-16",
                "candidate": "entry_shadow_gate::CLASSIC_CORE/STRICT/tv_ema_rsi_adx_trend",
                "portfolio_group": "CLASSIC_CORE",
                "contour": "STRICT",
                "model": "tv_ema_rsi_adx_trend",
                "delta_vs_base_rub": 650.0,
                "model_net_rub": 1200.0,
            },
            {
                "trade_date": "2026-06-17",
                "candidate": "entry_shadow_gate::CLASSIC_CORE/STRICT/tv_ema_rsi_adx_trend",
                "portfolio_group": "CLASSIC_CORE",
                "contour": "STRICT",
                "model": "tv_ema_rsi_adx_trend",
                "delta_vs_base_rub": 700.0,
                "model_net_rub": 1100.0,
            },
        ]
        advanced = apc.advance_candidate_gate(
            initial,
            auto_policy,
            [],
            "2026-06-17",
            strategy_review_history=future_history,
        )

        self.assertEqual(advanced["summary"]["promoted_now_count"], 1)
        promoted = advanced["promoted_now"][0]
        self.assertEqual(promoted["policy_key"], "entry_shadow_gate_group_models")
        self.assertEqual(promoted["evaluation_days"], 2)
        self.assertEqual(promoted["beat_base_days"], 2)
        self.assertGreaterEqual(promoted["total_delta_rub"], 900.0)

        promoted_active = apc.apply_promoted_candidates(auto_policy["active_base"], advanced["promoted_now"])
        self.assertEqual(
            promoted_active["entry_shadow_gate_group_models"],
            {"CLASSIC_CORE/STRICT": "tv_ema_rsi_adx_trend"},
        )

    def test_build_promoted_runtime_policy_state_keeps_promoted_candidates_across_days(self) -> None:
        promoted_now = [
            {
                "candidate_id": "entry_no_new_after|entry_window_1015_1159|11:59",
                "policy_key": "entry_no_new_after",
                "value": "11:59",
                "source_scenario": "entry_window_1015_1159",
                "created_trade_date": "2026-06-15",
                "resolved_trade_date": "2026-06-18",
                "evaluation_days": 3,
                "total_delta_rub": 1200.0,
            }
        ]
        state = apc.build_promoted_runtime_policy_state({}, promoted_now, "2026-06-18")
        self.assertEqual(state["summary"]["promoted_candidate_count"], 1)
        self.assertEqual(state["active_base"]["entry_no_new_after"], "11:59")

        later_state = apc.build_promoted_runtime_policy_state(state, [], "2026-06-19")
        self.assertEqual(later_state["summary"]["promoted_candidate_count"], 1)
        self.assertEqual(later_state["active_base"]["entry_no_new_after"], "11:59")
        self.assertEqual(
            later_state["promoted_candidates"][0]["promoted_trade_date"],
            "2026-06-18",
        )


if __name__ == "__main__":
    unittest.main()
