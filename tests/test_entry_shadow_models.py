from __future__ import annotations

import json
import os
import stat
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

    @patch("multi_futures_paper.clock_seconds_now", return_value=10 * 3600 + 30 * 60)
    def test_actual_close_writes_entry_shadow_rows(self, _mock_clock: object) -> None:
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
        state.position = mfp.open_position("long", 110.0, 1, state.profile.stop_ticks, state.profile.trail_ticks, state.spec)
        portfolio = mfp.Portfolio(initial_capital=200_000.0, max_total_margin_pct=0.8, max_position_margin_pct=0.2)
        state.entry_shadow_decisions = decisions

        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(
                log=str(Path(tmp) / "classic_core_multi_futures_paper_trades.csv"),
                shadow_log=str(Path(tmp) / "classic_core_shadow_exit_models.csv"),
                entry_shadow_log=str(Path(tmp) / "classic_core_entry_shadow_models.csv"),
                open_positions_log=str(Path(tmp) / "classic_core_paper_open_positions.json"),
                stop_limit_emergency_ticks=2.0,
                actual_exit_model="candle_like",
                expiry_force_close_days=3.0,
            )
            mfp.process_open_state_exit(
                state,
                args,
                portfolio,
                [state],
                candle_closed=True,
                actual_trigger_override=107.9,
                actual_trigger_source_override="closed_1m_candle",
            )

            path = Path(tmp) / "classic_core_entry_shadow_models.csv"
            self.assertTrue(path.exists())
            rows = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertGreater(len(rows), 1)
            text = path.read_text(encoding="utf-8")
            self.assertIn("actual_candle_like_stop_fill", text)
            self.assertEqual(state.entry_shadow_decisions, [])

    def test_write_entry_shadow_decisions_emits_runtime_log(self) -> None:
        decisions = [
            {
                "entry_id": "e1",
                "portfolio_group": "CLASSIC_CORE",
                "contour": "STRICT",
                "secid": "BRQ6",
                "model": "tv_ema_rsi_adx_trend",
                "allow": "true",
            },
            {
                "entry_id": "e1",
                "portfolio_group": "CLASSIC_CORE",
                "contour": "STRICT",
                "secid": "BRQ6",
                "model": "forum_chop_donchian_guard",
                "allow": "false",
            },
        ]

        with tempfile.TemporaryDirectory() as tmp, patch("multi_futures_paper.now_str", return_value="2026-06-16 10:45:00"), patch(
            "builtins.print"
        ) as mock_print:
            path = Path(tmp) / "classic_core_entry_shadow_models.csv"
            mfp.write_entry_shadow_decisions(
                path,
                decisions,
                closed_at="2026-06-16 10:44:30",
                minutes_held=15,
                exit_price=111.2,
                exit_source="actual_candle_like_stop_fill",
                net_rub=123.45,
                ticks=12.0,
            )

            self.assertTrue(path.exists())
            text = path.read_text(encoding="utf-8")
            self.assertIn("tv_ema_rsi_adx_trend", text)
            mock_print.assert_called_once()
            message = mock_print.call_args.args[0]
            self.assertIn("ENTRY_SHADOW", message)
            self.assertIn("rows=2", message)
            self.assertIn("path=classic_core_entry_shadow_models.csv", message)
            self.assertIn("exit_source=actual_candle_like_stop_fill", message)
            self.assertIn("secids=BRQ6", message)
            self.assertIn("models=forum_chop_donchian_guard,tv_ema_rsi_adx_trend", message)
            self.assertTrue(mock_print.call_args.kwargs.get("flush"))

    @patch("multi_futures_paper.clock_seconds_now", return_value=10 * 3600 + 30 * 60)
    def test_open_position_shadow_state_round_trips_through_restore(self, _mock_clock: object) -> None:
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
        state.position = mfp.open_position("long", 110.0, 1, state.profile.stop_ticks, state.profile.trail_ticks, state.spec)
        state.shadow_positions = {
            "stream_stoplimit": mfp.clone_position(state.position),
            "candle_like": mfp.clone_position(state.position),
        }
        state.shadow_closed = {"stream_stoplimit": True}
        state.shadow_close_details = {
            "stream_stoplimit": {
                "closed_at": "2026-06-16 10:45:00",
                "minutes_held": 15,
                "exit_price": 111.2,
                "exit_source": "candle_like_stop_fill",
                "net_rub": 123.45,
                "ticks": 12.0,
            }
        }
        state.entry_shadow_decisions = decisions

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "classic_core_paper_open_positions.json"
            mfp.write_open_positions(path, [state])
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload[0]["_kind"], "shadow_state")
            self.assertFalse(path.with_name(f"{path.stem}_shadow_state{path.suffix}").exists())

            restored = self.make_state()
            count = mfp.restore_open_positions(path, [restored])

            self.assertEqual(count, 1)
            self.assertIsNotNone(restored.position)
            self.assertEqual(len(restored.entry_shadow_decisions), len(decisions))
            self.assertEqual(set(restored.shadow_positions), {"stream_stoplimit", "candle_like"})
            self.assertTrue(restored.shadow_closed.get("stream_stoplimit"))
            self.assertEqual(
                restored.shadow_close_details["stream_stoplimit"]["net_rub"],
                123.45,
            )

    @patch("multi_futures_paper.clock_seconds_now", return_value=10 * 3600 + 30 * 60)
    def test_blocked_entry_shadow_only_round_trips_through_restore(self, _mock_clock: object) -> None:
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
        mfp.activate_blocked_entry_shadow_tracking(
            state,
            direction="long",
            entry_price=110.0,
            qty=1,
            spec=state.spec,
            actual_exit_model="candle_like",
            decisions=decisions,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "classic_core_paper_open_positions.json"
            mfp.write_open_positions(path, [state])
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload[0]["_kind"], "shadow_state")
            self.assertFalse(path.with_name(f"{path.stem}_shadow_state{path.suffix}").exists())

            restored = self.make_state()
            count = mfp.restore_open_positions(path, [restored])

            self.assertEqual(count, 0)
            self.assertIsNone(restored.position)
            self.assertEqual(restored.shadow_entry_mode, "blocked_entry")
            self.assertEqual(restored.shadow_entry_anchor_model, "candle_like")
            self.assertEqual(len(restored.entry_shadow_decisions), len(decisions))
            self.assertEqual(set(restored.shadow_positions), {"stream_stoplimit", "candle_like"})

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits are required")
    def test_write_open_positions_sets_shared_read_mode(self) -> None:
        state = self.make_state()
        state.position = mfp.open_position("long", 110.0, 1, state.profile.stop_ticks, state.profile.trail_ticks, state.spec)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "classic_core_paper_open_positions.json"

            mfp.write_open_positions(path, [state])

            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, mfp.DEFAULT_SHARED_FILE_MODE)


if __name__ == "__main__":
    unittest.main()
