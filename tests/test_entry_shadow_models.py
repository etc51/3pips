from __future__ import annotations

import sys
import tempfile
import types
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
sys.modules.setdefault("requests", types.SimpleNamespace())

import multi_futures_paper as mfp  # noqa: E402


def make_orderbook(bid: float, ask: float, bid_qty: int, ask_qty: int) -> object:
    return SimpleNamespace(
        bids=[SimpleNamespace(price=bid, quantity=bid_qty)],
        asks=[SimpleNamespace(price=ask, quantity=ask_qty)],
    )


def make_trend_candles(count: int = 40, start: float = 100.0, step: float = 0.25) -> deque[dict]:
    rows: deque[dict] = deque(maxlen=180)
    price = start
    for idx in range(count):
        open_price = price
        close_price = price + step
        high_price = close_price + 0.05
        low_price = open_price - 0.05
        volume = 100 + idx
        if idx == count - 1:
            close_price += 0.5
            high_price = close_price + 0.02
            volume = 400
        rows.append(
            {
                "open": round(open_price, 4),
                "high": round(high_price, 4),
                "low": round(low_price, 4),
                "close": round(close_price, 4),
                "volume": volume,
            }
        )
        price = close_price
    return rows


class EntryShadowModelsTest(unittest.TestCase):
    def make_state(self) -> mfp.State:
        spec = mfp.Spec(secid="TEST", figi="F", uid="U", min_step=0.1, step_price=1.0, last_rub=0.0, last_price=110.0)
        profile = mfp.Profile(
            secid="TEST",
            stop_ticks=20,
            trail_ticks=5,
            trail_arm_ticks=8,
            target_min_ticks=10,
            max_attempts=5,
            family="TS",
        )
        state = mfp.State(
            spec=spec,
            profile=profile,
            contour="strict",
            side_fee=0.2,
            candles=make_trend_candles(),
            last_price=110.0,
            last_order_book=make_orderbook(109.9, 110.0, 500, 200),
        )
        return state

    @patch("multi_futures_paper.clock_seconds_now", return_value=10 * 3600 + 30 * 60)
    def test_strong_uptrend_allows_multiple_shadow_models(self, _mock_clock: object) -> None:
        state = self.make_state()
        sizing = mfp.SizingDecision(
            qty=2,
            margin_qty=2,
            risk_qty=2,
            gross_stop_per_contract_rub=20.0,
            round_turn_fee_per_contract_rub=0.4,
            full_stop_per_contract_rub=20.4,
            full_stop_rub=40.8,
            reason="test",
        )

        decisions = mfp.evaluate_entry_shadow_models(
            state=state,
            portfolio_group="classic_core",
            direction="long",
            entry_price=110.0,
            qty=2,
            sizing=sizing,
            aggressive=False,
        )
        by_model = {row["model"]: row for row in decisions}

        self.assertTrue(by_model["tv_early_vwap_volume_breakout"]["allow"])
        self.assertTrue(by_model["tv_ema_rsi_adx_trend"]["allow"])
        self.assertFalse(by_model["forum_chop_donchian_guard"]["allow"])
        self.assertIn("atr_too_small", by_model["forum_chop_donchian_guard"]["decision_reason"])

    @patch("multi_futures_paper.clock_seconds_now", return_value=13 * 3600 + 30 * 60)
    def test_early_window_model_blocks_late_signal(self, _mock_clock: object) -> None:
        state = self.make_state()
        sizing = mfp.SizingDecision(
            qty=1,
            margin_qty=1,
            risk_qty=1,
            gross_stop_per_contract_rub=20.0,
            round_turn_fee_per_contract_rub=0.4,
            full_stop_per_contract_rub=20.4,
            full_stop_rub=20.4,
            reason="test",
        )

        decisions = mfp.evaluate_entry_shadow_models(
            state=state,
            portfolio_group="classic_core",
            direction="long",
            entry_price=110.0,
            qty=1,
            sizing=sizing,
            aggressive=False,
        )
        by_model = {row["model"]: row for row in decisions}

        self.assertFalse(by_model["tv_early_vwap_volume_breakout"]["allow"])
        self.assertIn("outside_1015_1159", by_model["tv_early_vwap_volume_breakout"]["decision_reason"])

    @patch("multi_futures_paper.clock_seconds_now", return_value=13 * 3600 + 30 * 60)
    def test_entry_shadow_gate_blocks_when_required_model_denies(self, _mock_clock: object) -> None:
        state = self.make_state()
        sizing = mfp.SizingDecision(
            qty=1,
            margin_qty=1,
            risk_qty=1,
            gross_stop_per_contract_rub=20.0,
            round_turn_fee_per_contract_rub=0.4,
            full_stop_per_contract_rub=20.4,
            full_stop_rub=20.4,
            reason="test",
        )

        decisions = mfp.evaluate_entry_shadow_models(
            state=state,
            portfolio_group="classic_core",
            direction="long",
            entry_price=110.0,
            qty=1,
            sizing=sizing,
            aggressive=False,
        )
        reason = mfp.entry_shadow_gate_block_reason(
            decisions,
            {"entry_shadow_gate_group_models": {"CLASSIC_CORE/STRICT": "tv_early_vwap_volume_breakout"}},
            "classic_core",
            "strict",
        )

        self.assertIsNotNone(reason)
        self.assertIn("entry_shadow_gate", str(reason))

    @patch("multi_futures_paper.clock_seconds_now", return_value=10 * 3600 + 30 * 60)
    def test_entry_shadow_gate_allows_when_required_model_passes(self, _mock_clock: object) -> None:
        state = self.make_state()
        sizing = mfp.SizingDecision(
            qty=1,
            margin_qty=1,
            risk_qty=1,
            gross_stop_per_contract_rub=20.0,
            round_turn_fee_per_contract_rub=0.4,
            full_stop_per_contract_rub=20.4,
            full_stop_rub=20.4,
            reason="test",
        )

        decisions = mfp.evaluate_entry_shadow_models(
            state=state,
            portfolio_group="classic_core",
            direction="long",
            entry_price=110.0,
            qty=1,
            sizing=sizing,
            aggressive=False,
        )
        reason = mfp.entry_shadow_gate_block_reason(
            decisions,
            {"entry_shadow_gate_group_models": {"CLASSIC_CORE/STRICT": "tv_early_vwap_volume_breakout"}},
            "classic_core",
            "strict",
        )

        self.assertIsNone(reason)

    @patch("multi_futures_paper.clock_seconds_now", return_value=10 * 3600 + 30 * 60)
    def test_blocked_entry_shadow_tracking_writes_outcome_rows(self, _mock_clock: object) -> None:
        state = self.make_state()
        sizing = mfp.SizingDecision(
            qty=1,
            margin_qty=1,
            risk_qty=1,
            gross_stop_per_contract_rub=20.0,
            round_turn_fee_per_contract_rub=0.4,
            full_stop_per_contract_rub=20.4,
            full_stop_rub=20.4,
            reason="test",
        )
        decisions = mfp.evaluate_entry_shadow_models(
            state=state,
            portfolio_group="classic_core",
            direction="long",
            entry_price=110.0,
            qty=1,
            sizing=sizing,
            aggressive=False,
        )

        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(
                log=str(Path(tmp) / "classic_core_multi_futures_paper_trades.csv"),
                entry_shadow_log="",
                actual_exit_model="candle_like",
            )
            mfp.activate_blocked_entry_shadow_tracking(
                state,
                direction="long",
                entry_price=110.0,
                qty=1,
                spec=state.spec,
                actual_exit_model="candle_like",
                decisions=decisions,
            )
            state.shadow_close_details["candle_like"] = {
                "closed_at": "2026-06-16 10:45:00",
                "minutes_held": 15,
                "exit_price": 111.2,
                "exit_source": "candle_like_stop_fill",
                "net_rub": 123.45,
                "ticks": 12.0,
            }

            mfp.finalize_shadow_only_entry_tracking(state, args)

            path = Path(tmp) / "classic_core_entry_shadow_models.csv"
            self.assertTrue(path.exists())
            rows = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertGreater(len(rows), 1)
            self.assertEqual(state.entry_shadow_decisions, [])
            self.assertEqual(state.shadow_entry_mode, "")
            self.assertEqual(state.shadow_entry_anchor_model, "")
            self.assertEqual(state.shadow_close_details, {})
            self.assertIn("shadow_only::candle_like_stop_fill", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
