from __future__ import annotations

import argparse
import csv
import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from ng_scalper_bot import (
    Direction,
    Position,
    append_trade,
    commission_side_rub,
    find_tbank_token,
    is_stop_hit,
    now_str,
    open_position,
    pnl_rub,
    quotation_to_float,
    round_to_step,
    tbank_find_future,
    update_stop,
    volume_vwap,
)


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
BR_POLICY_MIN_STOP_TICKS = 15
BR_POLICY_MAX_SPREAD_TO_STOP = 0.40
BR_POLICY_LOSS_PAUSE_COUNT = 3
BR_POLICY_LOSS_PAUSE_SECONDS = 2 * 60 * 60
SPREAD_WATCH_RATIO = 0.25
SPREAD_HEAVY_RATIO = 0.40
SPREAD_DOMINATES_RATIO = 1.00
DEFAULT_MAX_FULL_STOP_RUB = 4_000.0
ENTRY_AUDIT_COLUMNS = [
    "event_time",
    "event_type",
    "entry_id",
    "contour",
    "secid",
    "family",
    "direction",
    "qty",
    "entry_price",
    "exit_price",
    "exit_source",
    "net_rub",
    "ticks",
    "last_price",
    "last_close",
    "vwap",
    "directional_vwap_ticks",
    "directional_vwap_to_stop",
    "momentum_ticks",
    "trend_ticks",
    "last_volume",
    "avg_volume",
    "volume_ratio",
    "bid_qty_top3",
    "ask_qty_top3",
    "book_signal_ratio",
    "spread_ticks",
    "spread_to_stop_ratio",
    "fee_ticks",
    "fee_to_stop_ratio",
    "recent_range_ticks",
    "breakout_margin_ticks",
    "stop_ticks",
    "trail_ticks",
    "trail_arm_ticks",
    "full_stop_risk_rub",
    "risk_mode",
    "risk_reason",
    "sizing_reason",
    "soft_entry_allow",
    "soft_entry_reason",
    "hard_entry_allow",
    "hard_entry_reason",
    "raw_signal_reason",
]
GPT_SHADOW_COLUMNS = [
    "event_time",
    "event_type",
    "model",
    "contour",
    "secid",
    "family",
    "signal_family",
    "entry_timing",
    "session_filter",
    "direction",
    "qty",
    "entry_price",
    "exit_price",
    "exit_source",
    "trigger_price",
    "trigger_source",
    "stop_price",
    "best_price",
    "stop_ticks",
    "trail_ticks",
    "trail_arm_ticks",
    "exit_mode",
    "source_stage2_id",
    "match_quality",
    "max_hold_minutes",
    "ticks",
    "gross_rub",
    "fees_rub",
    "net_rub",
    "closed_net_rub",
    "full_stop_risk_rub",
    "risk_mode",
    "risk_reason",
    "sizing_reason",
    "signal_reason",
]


@dataclass
class Spec:
    secid: str
    figi: str
    uid: str
    min_step: float
    step_price: float
    last_rub: float
    last_price: float = 0.0
    expiration_date: datetime | None = None
    margin_buy: float = 0.0
    margin_sell: float = 0.0


@dataclass
class Profile:
    secid: str
    stop_ticks: int
    trail_ticks: int
    trail_arm_ticks: int
    target_min_ticks: int
    max_attempts: int
    allowed_direction: str = "both"
    family: str = ""
    source_secid: str = ""
    signal_family: str = "online_current"
    entry_timing: str = "stream_executable"
    session_filter: str = "all_available_data"
    momentum_pct: float = 0.0
    momentum_ticks: float = 0.0
    breakout_lookback: int = 6
    trend_fast: int = 0
    trend_slow: int = 0
    volume_multiplier: float = 1.0
    volume_window: int = 20
    vwap_mode: str = "disabled"
    vwap_buffer_pct: float = 0.0
    cooldown_minutes: int = 0
    max_hold_minutes: int = 0
    gpt_params_loaded: bool = False
    gpt_exit_loaded: bool = False
    gpt_exit_mode: str = ""
    gpt_stop_ticks: float = 0.0
    gpt_trail_ticks: float = 0.0
    gpt_trail_arm_ticks: float = 0.0
    gpt_stop_pct: float = 0.0
    gpt_trail_pct: float = 0.0
    gpt_trail_arm_pct: float = 0.0
    gpt_source_stage2_id: str = ""
    gpt_match_quality: str = ""


@dataclass
class State:
    spec: Spec
    profile: Profile
    contour: str
    side_fee: float
    candles: deque[dict] = field(default_factory=lambda: deque(maxlen=180))
    position: Position | None = None
    attempts: int = 0
    closed: int = 0
    closed_net: float = 0.0
    last_price: float = 0.0
    last_order_book: object | None = None
    last_reason: str = ""
    last_signal_metrics: dict = field(default_factory=dict)
    last_entry_candle_count: int = -1
    cooldown_until: float = 0.0
    consecutive_losses: int = 0
    risk_mode: str = "normal"
    risk_limit_rub: float = DEFAULT_MAX_FULL_STOP_RUB
    risk_reason: str = ""
    shadow_positions: dict[str, Position] = field(default_factory=dict)
    shadow_closed: dict[str, bool] = field(default_factory=dict)
    gpt_shadow_position: Position | None = None
    gpt_shadow_attempts: int = 0
    gpt_shadow_closed: int = 0
    gpt_shadow_net: float = 0.0
    gpt_shadow_last_reason: str = ""
    gpt_shadow_entry_reason: str = ""
    gpt_shadow_entry_sizing_reason: str = ""
    gpt_shadow_risk_mode: str = ""
    gpt_shadow_risk_reason: str = ""
    gpt_shadow_last_entry_candle_count: int = -1
    gpt_shadow_cooldown_until: float = 0.0
    gpt_shadow_layers: dict[str, "GptShadowRuntime"] = field(default_factory=dict)


@dataclass
class GptShadowRuntime:
    position: Position | None = None
    attempts: int = 0
    closed: int = 0
    net: float = 0.0
    last_reason: str = ""
    entry_reason: str = ""
    entry_sizing_reason: str = ""
    risk_mode: str = ""
    risk_reason: str = ""
    last_entry_candle_count: int = -1
    cooldown_until: float = 0.0
    active_stop_ticks: int = 0
    active_trail_ticks: int = 0
    active_trail_arm_ticks: int = 0
    active_exit_mode: str = ""


@dataclass(frozen=True)
class GptShadowLayerConfig:
    name: str
    threshold_mult: float
    volume_mult: float
    trend_tolerance_ticks: float
    trend_tolerance_mult: float
    breakout_tolerance_ticks: float
    vwap_buffer_mult: float
    spread_stop_ratio: float
    range_cost_mult: float


GPT_SHADOW_LAYER_CONFIGS = {
    "full": GptShadowLayerConfig(
        name="full",
        threshold_mult=1.0,
        volume_mult=1.0,
        trend_tolerance_ticks=0.0,
        trend_tolerance_mult=0.0,
        breakout_tolerance_ticks=0.0,
        vwap_buffer_mult=1.0,
        spread_stop_ratio=SPREAD_DOMINATES_RATIO,
        range_cost_mult=1.0,
    ),
    "relaxed": GptShadowLayerConfig(
        name="relaxed",
        threshold_mult=0.75,
        volume_mult=0.75,
        trend_tolerance_ticks=1.0,
        trend_tolerance_mult=0.20,
        breakout_tolerance_ticks=1.0,
        vwap_buffer_mult=0.50,
        spread_stop_ratio=1.15,
        range_cost_mult=0.85,
    ),
    "loose": GptShadowLayerConfig(
        name="loose",
        threshold_mult=0.55,
        volume_mult=0.50,
        trend_tolerance_ticks=2.0,
        trend_tolerance_mult=0.35,
        breakout_tolerance_ticks=2.0,
        vwap_buffer_mult=0.0,
        spread_stop_ratio=1.30,
        range_cost_mult=0.70,
    ),
}


@dataclass
class Portfolio:
    initial_capital: float
    max_total_margin_pct: float
    max_position_margin_pct: float
    closed_net: float = 0.0

    @property
    def equity(self) -> float:
        return self.initial_capital + self.closed_net


@dataclass
class SizingDecision:
    qty: int
    margin_qty: int
    risk_qty: int | None
    gross_stop_per_contract_rub: float
    round_turn_fee_per_contract_rub: float
    full_stop_per_contract_rub: float
    full_stop_rub: float
    reason: str


@dataclass(frozen=True)
class GptExitTicks:
    stop_ticks: int
    trail_ticks: int
    trail_arm_ticks: int
    exit_mode: str


@dataclass
class RiskDecision:
    allowed: bool
    mode: str
    max_full_stop_rub: float
    reason: str
    median_win_rub: float | None = None
    stop_to_median_win: float | None = None
    median_stop_cap_rub: float | None = None
    median_win_source: str = ""
    family_day_net: float = 0.0
    family_day_high: float = 0.0


class RiskGovernor:
    def __init__(
        self,
        path: Path,
        base_max_full_stop_rub: float,
        reduced_full_stop_rub: float,
        micro_full_stop_rub: float,
        profit_guard_min_rub: float,
        profit_guard_drawdown_pct: float,
        profit_guard_drawdown_min_rub: float,
        stop_to_median_reduced: float,
        stop_to_median_micro: float,
        stop_to_median_cap: float,
        probation_trades: int,
    ) -> None:
        self.path = path
        self.base_max_full_stop_rub = float(base_max_full_stop_rub)
        self.reduced_full_stop_rub = float(reduced_full_stop_rub)
        self.micro_full_stop_rub = float(micro_full_stop_rub)
        self.profit_guard_min_rub = float(profit_guard_min_rub)
        self.profit_guard_drawdown_pct = float(profit_guard_drawdown_pct)
        self.profit_guard_drawdown_min_rub = float(profit_guard_drawdown_min_rub)
        self.stop_to_median_reduced = float(stop_to_median_reduced)
        self.stop_to_median_micro = float(stop_to_median_micro)
        self.stop_to_median_cap = float(stop_to_median_cap)
        self.probation_trades = int(probation_trades)
        self.profile_stats: dict[str, dict] = {}
        self.family_stats: dict[str, dict] = {}
        self.global_stats: dict = self._empty_stats()
        self.family_days: dict[str, dict] = {}
        self.global_days: dict[str, dict] = {}
        self._shared_day_cache_at = 0.0
        self._shared_day_cache: dict[str, dict] = {}

    @staticmethod
    def _empty_stats() -> dict:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "net": 0.0,
            "gross": 0.0,
            "fees": 0.0,
            "gross_profit": 0.0,
            "gross_loss_abs": 0.0,
            "best_win": 0.0,
            "worst_loss": 0.0,
            "positive_nets": [],
            "recent_nets": [],
        }

    @staticmethod
    def _date_from_closed_at(closed_at: object) -> str:
        text = str(closed_at or "")
        if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
            return text[:10]
        return datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _record_stats(stats: dict, net: float, gross: float, fees: float) -> None:
        stats["trades"] = int(stats.get("trades") or 0) + 1
        stats["net"] = float(stats.get("net") or 0.0) + net
        stats["gross"] = float(stats.get("gross") or 0.0) + gross
        stats["fees"] = float(stats.get("fees") or 0.0) + fees
        recent = list(stats.get("recent_nets") or [])
        recent.append(round(net, 2))
        stats["recent_nets"] = recent[-20:]
        if net > 0:
            stats["wins"] = int(stats.get("wins") or 0) + 1
            stats["gross_profit"] = float(stats.get("gross_profit") or 0.0) + net
            stats["best_win"] = max(float(stats.get("best_win") or 0.0), net)
            positives = list(stats.get("positive_nets") or [])
            positives.append(round(net, 2))
            stats["positive_nets"] = positives[-200:]
        elif net < 0:
            stats["losses"] = int(stats.get("losses") or 0) + 1
            stats["gross_loss_abs"] = float(stats.get("gross_loss_abs") or 0.0) + abs(net)
            stats["worst_loss"] = min(float(stats.get("worst_loss") or 0.0), net)

    @staticmethod
    def _median_win(stats: dict | None) -> float | None:
        values = [float(x) for x in (stats or {}).get("positive_nets", []) if float(x) > 0]
        if not values:
            return None
        return float(median(values))

    @staticmethod
    def _median_win_with_count(stats: dict | None) -> tuple[float | None, int]:
        values = [float(x) for x in (stats or {}).get("positive_nets", []) if float(x) > 0]
        if not values:
            return None, 0
        return float(median(values)), len(values)

    @staticmethod
    def profile_key(st: State) -> str:
        family = state_family(st)
        source = st.profile.source_secid or st.profile.secid
        profile_bits = (
            st.profile.allowed_direction,
            st.profile.stop_ticks,
            st.profile.trail_ticks,
            st.profile.trail_arm_ticks,
            source,
        )
        return "|".join(str(x) for x in (st.contour, family, st.spec.secid, *profile_bits))

    @staticmethod
    def family_key(st: State) -> str:
        return state_family(st)

    def _day_key(self, family: str, date_text: str | None = None) -> str:
        return f"{family}|{date_text or datetime.now().strftime('%Y-%m-%d')}"

    def _record_day(self, family: str, date_text: str, net: float) -> None:
        key = self._day_key(family, date_text)
        day = self.family_days.setdefault(key, {"net": 0.0, "high": 0.0, "paused": False, "pause_reason": ""})
        day["net"] = float(day.get("net") or 0.0) + net
        day["high"] = max(float(day.get("high") or 0.0), float(day["net"]))
        self._apply_profit_guard_to_day(day)

    def _record_global_day(self, date_text: str, net: float) -> None:
        day = self.global_days.setdefault(date_text, {"net": 0.0, "high": 0.0, "protect_active": False, "paused": False, "pause_reason": ""})
        day["net"] = float(day.get("net") or 0.0) + net
        day["high"] = max(float(day.get("high") or 0.0), float(day["net"]))
        self._apply_global_profit_guard_to_day(day)

    def _shared_day_from_trade_logs(self, date_text: str) -> dict:
        now = time.monotonic()
        if now - self._shared_day_cache_at <= 3.0 and date_text in self._shared_day_cache:
            return dict(self._shared_day_cache[date_text])

        trades: list[tuple[str, float]] = []
        for path in sorted(self.path.parent.glob("*_multi_futures_paper_trades.csv")):
            if not path.exists() or path.stat().st_size == 0:
                continue
            try:
                with path.open(newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        closed_at = str(row.get("closed_at") or row.get("time") or "")
                        if not closed_at.startswith(date_text):
                            continue
                        raw_net = row.get("net_rub")
                        if raw_net in (None, ""):
                            raw_net = row.get("net_pnl_rub")
                        trades.append((closed_at, float(raw_net or 0.0)))
            except Exception:
                continue

        net = 0.0
        high = 0.0
        for _, value in sorted(trades, key=lambda x: x[0]):
            net += value
            high = max(high, net)
        day = {"net": net, "high": high, "protect_active": False, "paused": False, "pause_reason": ""}
        self._apply_global_profit_guard_to_day(day)
        self._shared_day_cache = {date_text: dict(day)}
        self._shared_day_cache_at = now
        return day

    def _apply_profit_guard_to_day(self, day: dict) -> None:
        high = float(day.get("high") or 0.0)
        net = float(day.get("net") or 0.0)
        drawdown = high - net
        min_drawdown = max(self.profit_guard_drawdown_min_rub, high * self.profit_guard_drawdown_pct)
        if high >= self.profit_guard_min_rub and drawdown >= min_drawdown:
            day["paused"] = True
            day["pause_reason"] = (
                f"daily_profit_guard high={high:.0f} net={net:.0f} "
                f"drawdown={drawdown:.0f}"
            )

    def _apply_global_profit_guard_to_day(self, day: dict) -> None:
        high = float(day.get("high") or 0.0)
        net = float(day.get("net") or 0.0)
        protect = self.profit_guard_min_rub
        if high >= protect:
            day["protect_active"] = True
        if bool(day.get("protect_active")) and net < protect:
            day["paused"] = True
            day["pause_reason"] = (
                f"daily_profit_guard_floor high={high:.0f} net={net:.0f} "
                f"protect={protect:.0f}"
            )

    def record_trade(self, st: State, net: float, gross: float, fees: float, closed_at: object | None = None) -> None:
        family = self.family_key(st)
        date_text = self._date_from_closed_at(closed_at)
        profile_key = self.profile_key(st)
        profile_stats = self.profile_stats.setdefault(profile_key, self._empty_stats())
        family_stats = self.family_stats.setdefault(family, self._empty_stats())
        self._record_stats(profile_stats, net, gross, fees)
        self._record_stats(family_stats, net, gross, fees)
        self._record_stats(self.global_stats, net, gross, fees)
        self._record_day(family, date_text, net)
        self._record_global_day(date_text, net)
        self.write()

    def rebuild_from_trade_log(self, path: Path, states: list[State]) -> None:
        self.profile_stats = {}
        self.family_stats = {}
        self.global_stats = self._empty_stats()
        self.family_days = {}
        self.global_days = {}
        if not path.exists() or path.stat().st_size == 0:
            self.write()
            return
        by_key = {(st.contour, st.spec.secid): st for st in states}
        try:
            with path.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    st = by_key.get((str(row.get("contour") or ""), str(row.get("secid") or row.get("ticker") or "")))
                    if st is None:
                        continue
                    try:
                        net = float(row.get("net_rub") or 0.0)
                        gross = float(row.get("gross_rub") or 0.0)
                        fees = float(row.get("fees_rub") or 0.0)
                    except Exception:
                        continue
                    self.record_trade(st, net, gross, fees, row.get("closed_at"))
        except Exception as exc:
            print(f"{now_str()} RISK restore_error={exc}", flush=True)
        self.write()

    def decide(self, st: State, base_max_full_stop_rub: float | None = None) -> RiskDecision:
        base_limit = float(base_max_full_stop_rub or self.base_max_full_stop_rub)
        family = self.family_key(st)
        today = datetime.now().strftime("%Y-%m-%d")
        local_global_day = self.global_days.setdefault(today, {"net": 0.0, "high": 0.0, "protect_active": False, "paused": False, "pause_reason": ""})
        self._apply_global_profit_guard_to_day(local_global_day)
        global_day = self._shared_day_from_trade_logs(today)
        if float(global_day.get("high") or 0.0) <= 0 and float(local_global_day.get("high") or 0.0) > 0:
            global_day = local_global_day
        self._apply_global_profit_guard_to_day(global_day)
        global_guard_active = bool(global_day.get("protect_active"))
        if bool(global_day.get("paused")):
            return RiskDecision(
                allowed=False,
                mode="paused_today",
                max_full_stop_rub=0.0,
                reason=str(global_day.get("pause_reason") or "daily_profit_guard_floor"),
                family_day_net=float(global_day.get("net") or 0.0),
                family_day_high=float(global_day.get("high") or 0.0),
            )
        if global_guard_active and st.contour == "aggressive":
            return RiskDecision(
                allowed=False,
                mode="profit_guard_aggressive_off",
                max_full_stop_rub=0.0,
                reason=(
                    f"daily_profit_guard_aggressive_off high={float(global_day.get('high') or 0.0):.0f} "
                    f"net={float(global_day.get('net') or 0.0):.0f} protect={self.profit_guard_min_rub:.0f}"
                ),
                family_day_net=float(global_day.get("net") or 0.0),
                family_day_high=float(global_day.get("high") or 0.0),
            )
        day = self.family_days.setdefault(self._day_key(family), {"net": 0.0, "high": 0.0, "paused": False, "pause_reason": ""})
        self._apply_profit_guard_to_day(day)
        if bool(day.get("paused")):
            return RiskDecision(
                allowed=False,
                mode="paused_today",
                max_full_stop_rub=0.0,
                reason=str(day.get("pause_reason") or "daily_profit_guard"),
                family_day_net=float(day.get("net") or 0.0),
                family_day_high=float(day.get("high") or 0.0),
            )

        profile_stats = self.profile_stats.get(self.profile_key(st), self._empty_stats())
        family_stats = self.family_stats.get(family, self._empty_stats())
        profile_median, profile_positive_count = self._median_win_with_count(profile_stats)
        family_median, family_positive_count = self._median_win_with_count(family_stats)
        global_median, global_positive_count = self._median_win_with_count(self.global_stats)
        median_win = profile_median if profile_median is not None else family_median
        if median_win is None:
            median_win = global_median
        median_source = ""
        if profile_median is not None:
            median_source = f"profile_wins={profile_positive_count}"
        elif family_median is not None:
            median_source = f"family_wins={family_positive_count}"
        elif global_median is not None:
            median_source = f"portfolio_wins={global_positive_count}"
        stop_to_median = (base_limit / median_win) if median_win and median_win > 0 else None
        mode = "normal"
        reasons: list[str] = []

        profile_trades = int(profile_stats.get("trades") or 0)
        family_trades = int(family_stats.get("trades") or 0)
        family_profit = float(family_stats.get("gross_profit") or 0.0)
        family_net = float(family_stats.get("net") or 0.0)
        family_losses = int(family_stats.get("losses") or 0)
        family_worst_abs = abs(float(family_stats.get("worst_loss") or 0.0))
        profile_profit = float(profile_stats.get("gross_profit") or 0.0)
        profile_net = float(profile_stats.get("net") or 0.0)
        profile_losses = int(profile_stats.get("losses") or 0)
        profile_worst_abs = abs(float(profile_stats.get("worst_loss") or 0.0))
        source = st.profile.source_secid or st.profile.secid

        if source != st.spec.secid and profile_trades < self.probation_trades:
            mode = "micro"
            reasons.append(f"probation_new_contract trades={profile_trades}/{self.probation_trades}")
        if global_guard_active:
            mode = "micro"
            reasons.append(
                f"daily_profit_guard_active high={float(global_day.get('high') or 0.0):.0f} "
                f"net={float(global_day.get('net') or 0.0):.0f} protect={self.profit_guard_min_rub:.0f}"
            )
        if profile_trades >= 2 and profile_losses >= 2 and profile_net < 0:
            mode = "micro"
            reasons.append(f"profile_loss_cluster net={profile_net:.0f}")
        elif profile_trades >= 2 and profile_net <= -base_limit * 0.50 and mode != "micro":
            mode = "reduced"
            reasons.append(f"profile_negative net={profile_net:.0f}")
        if family_trades >= 2 and family_net <= -base_limit and family_losses >= 2:
            mode = "micro"
            reasons.append(f"family_negative net={family_net:.0f}")
        elif family_trades >= 2 and family_net <= -base_limit * 0.50 and mode != "micro":
            mode = "reduced"
            reasons.append(f"family_negative net={family_net:.0f}")
        if stop_to_median is not None:
            if stop_to_median >= self.stop_to_median_micro:
                mode = "micro"
                reasons.append(f"stop_to_median={stop_to_median:.1f}")
            elif stop_to_median >= self.stop_to_median_reduced and mode != "micro":
                mode = "reduced"
                reasons.append(f"stop_to_median={stop_to_median:.1f}")
        if profile_trades >= 3 and profile_profit > 0:
            profile_tail = profile_worst_abs / profile_profit
            if profile_tail >= 0.90:
                mode = "micro"
                reasons.append(f"profile_tail={profile_tail:.2f}")
            elif profile_tail >= 0.50 and mode != "micro":
                mode = "reduced"
                reasons.append(f"profile_tail={profile_tail:.2f}")
        if family_trades >= 5 and family_profit > 0:
            family_tail = family_worst_abs / family_profit
            if family_tail >= 0.90:
                mode = "micro"
                reasons.append(f"family_tail={family_tail:.2f}")
            elif family_tail >= 0.55 and mode != "micro":
                mode = "reduced"
                reasons.append(f"family_tail={family_tail:.2f}")
        recent = [float(x) for x in profile_stats.get("recent_nets", [])]
        if len(recent) >= 3 and all(x < 0 for x in recent[-3:]):
            mode = "micro"
            reasons.append("recent_losses=3")

        if mode == "micro":
            limit = min(base_limit, self.micro_full_stop_rub)
        elif mode == "reduced":
            limit = min(base_limit, self.reduced_full_stop_rub)
        else:
            limit = base_limit
            reasons.append("risk_ok")

        median_stop_cap = None
        if median_win is not None and median_win > 0 and self.stop_to_median_cap > 0:
            median_stop_cap = median_win * self.stop_to_median_cap
            if median_stop_cap < limit:
                limit = max(0.0, median_stop_cap)
                if mode == "normal":
                    mode = "median_cap"
                reasons.append(
                    f"median_cap median={median_win:.0f} cap_x={self.stop_to_median_cap:.1f} "
                    f"max_stop={limit:.0f} {median_source}"
                )
        stop_to_median = (limit / median_win) if median_win and median_win > 0 else None

        return RiskDecision(
            allowed=True,
            mode=mode,
            max_full_stop_rub=limit,
            reason="; ".join(reasons),
            median_win_rub=median_win,
            stop_to_median_win=stop_to_median,
            median_stop_cap_rub=median_stop_cap,
            median_win_source=median_source,
            family_day_net=float(day.get("net") or 0.0),
            family_day_high=float(day.get("high") or 0.0),
        )

    def write(self) -> None:
        payload = {
            "updated_at": now_str(),
            "base_max_full_stop_rub": self.base_max_full_stop_rub,
            "reduced_full_stop_rub": self.reduced_full_stop_rub,
            "micro_full_stop_rub": self.micro_full_stop_rub,
            "profit_guard_min_rub": self.profit_guard_min_rub,
            "profit_guard_drawdown_pct": self.profit_guard_drawdown_pct,
            "profit_guard_drawdown_min_rub": self.profit_guard_drawdown_min_rub,
            "stop_to_median_reduced": self.stop_to_median_reduced,
            "stop_to_median_micro": self.stop_to_median_micro,
            "stop_to_median_cap": self.stop_to_median_cap,
            "probation_trades": self.probation_trades,
            "profile_stats": self.profile_stats,
            "family_stats": self.family_stats,
            "global_stats": self.global_stats,
            "family_days": self.family_days,
            "global_days": self.global_days,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def position_margin(spec: Spec, direction: Direction | str, qty: int) -> float:
    per_contract = spec.margin_buy if direction == "long" else spec.margin_sell
    if per_contract <= 0:
        per_contract = max(spec.margin_buy, spec.margin_sell, 0.0)
    return per_contract * qty


def used_margin(states: list[State]) -> float:
    total = 0.0
    for st in states:
        if st.position is not None:
            total += position_margin(st.spec, st.position.direction, st.position.qty)
    return total


def has_open_ticker(states: list[State], secid: str) -> bool:
    return any(st.spec.secid == secid and st.position is not None for st in states)


def state_family(st: State) -> str:
    return st.profile.family or contract_family(st.spec.secid)


def has_roll_family_conflict(states: list[State], spec: Spec, family: str, roll_window_days: float) -> str | None:
    if roll_window_days <= 0:
        return None
    current_dte = days_to_expiration(spec)
    current_in_roll = current_dte is not None and current_dte <= roll_window_days
    for st in states:
        if st.position is None:
            continue
        if st.spec.secid == spec.secid:
            continue
        if state_family(st) != family:
            continue
        other_dte = days_to_expiration(st.spec)
        other_in_roll = other_dte is not None and other_dte <= roll_window_days
        if current_in_roll or other_in_roll:
            return st.spec.secid
    return None


def paper_sizing(
    portfolio: Portfolio,
    states: list[State],
    spec: Spec,
    profile: Profile,
    direction: Direction | str,
    side_fee: float,
    max_full_stop_rub: float,
) -> SizingDecision:
    per_contract = spec.margin_buy if direction == "long" else spec.margin_sell
    if per_contract <= 0:
        per_contract = max(spec.margin_buy, spec.margin_sell, 0.0)
    if per_contract <= 0:
        margin_qty = 1
    else:
        max_total_margin = portfolio.equity * portfolio.max_total_margin_pct
        free_margin = max(0.0, max_total_margin - used_margin(states))
        per_position_limit = portfolio.equity * portfolio.max_position_margin_pct
        budget = min(free_margin, per_position_limit)
        margin_qty = max(0, int(budget // per_contract))

    gross_stop_per_contract = max(0.0, profile.stop_ticks * spec.step_price)
    round_turn_fee_per_contract = max(0.0, 2 * side_fee)
    full_stop_per_contract = gross_stop_per_contract + round_turn_fee_per_contract
    risk_qty = None
    if max_full_stop_rub > 0 and full_stop_per_contract > 0:
        risk_qty = int(max_full_stop_rub // full_stop_per_contract)

    qty = margin_qty
    if risk_qty is not None:
        qty = min(qty, risk_qty)
    qty = max(0, qty)
    full_stop_rub = qty * full_stop_per_contract
    reason = (
        f"sizing margin_qty={margin_qty} risk_qty={risk_qty if risk_qty is not None else '-'} "
        f"full_stop_1lot={full_stop_per_contract:.2f} full_stop={full_stop_rub:.2f} "
        f"max_full_stop={max_full_stop_rub:.0f}"
    )
    return SizingDecision(
        qty=qty,
        margin_qty=margin_qty,
        risk_qty=risk_qty,
        gross_stop_per_contract_rub=gross_stop_per_contract,
        round_turn_fee_per_contract_rub=round_turn_fee_per_contract,
        full_stop_per_contract_rub=full_stop_per_contract,
        full_stop_rub=full_stop_rub,
        reason=reason,
    )


def full_stop_risk_rub(profile: Profile, spec: Spec, side_fee: float, qty: int) -> float:
    return max(0.0, (profile.stop_ticks * spec.step_price + 2 * side_fee) * qty)


def full_stop_risk_rub_ticks(stop_ticks: int, spec: Spec, side_fee: float, qty: int) -> float:
    return max(0.0, (max(1, int(stop_ticks)) * spec.step_price + 2 * side_fee) * qty)


def percent_to_ticks(percent: float, reference_price: float, spec: Spec) -> int:
    if percent <= 0 or reference_price <= 0 or spec.min_step <= 0:
        return 1
    return max(1, int(round(abs(percent) * reference_price / spec.min_step)))


def gpt_shadow_exit_ticks(profile: Profile, spec: Spec, reference_price: float | None = None) -> GptExitTicks:
    ref = float(reference_price or 0.0)
    if ref <= 0:
        ref = float(spec.last_price or spec.last_rub or 0.0)
    mode = (profile.gpt_exit_mode or "").lower()
    if profile.gpt_exit_loaded and mode == "percent":
        return GptExitTicks(
            stop_ticks=percent_to_ticks(float(profile.gpt_stop_pct or 0.0), ref, spec),
            trail_ticks=percent_to_ticks(float(profile.gpt_trail_pct or 0.0), ref, spec),
            trail_arm_ticks=percent_to_ticks(float(profile.gpt_trail_arm_pct or 0.0), ref, spec),
            exit_mode="percent",
        )
    if profile.gpt_exit_loaded:
        return GptExitTicks(
            stop_ticks=max(1, int(round(float(profile.gpt_stop_ticks or profile.stop_ticks or 1)))),
            trail_ticks=max(1, int(round(float(profile.gpt_trail_ticks or profile.trail_ticks or 1)))),
            trail_arm_ticks=max(1, int(round(float(profile.gpt_trail_arm_ticks or profile.trail_arm_ticks or 1)))),
            exit_mode=mode or "direct_ticks",
        )
    return GptExitTicks(
        stop_ticks=max(1, int(profile.stop_ticks or 1)),
        trail_ticks=max(1, int(profile.trail_ticks or 1)),
        trail_arm_ticks=max(1, int(profile.trail_arm_ticks or 1)),
        exit_mode="profile_ticks",
    )


def fee_ticks(side_fee: float, spec: Spec) -> float:
    return (2 * side_fee / spec.step_price) if spec.step_price else 999.0


def spread_to_stop_metrics(spread_ticks: float | None, stop_ticks: int) -> tuple[float | None, str, bool]:
    if spread_ticks is None or stop_ticks <= 0:
        return None, "NO_BOOK", False
    ratio = float(spread_ticks) / float(stop_ticks)
    if ratio > SPREAD_DOMINATES_RATIO:
        return ratio, "SPREAD_DOMINATES", True
    if ratio > SPREAD_HEAVY_RATIO:
        return ratio, "SPREAD_HEAVY", True
    if ratio > SPREAD_WATCH_RATIO:
        return ratio, "SPREAD_WATCH", True
    return ratio, "SPREAD_OK", False


def profile_can_trade(profile: Profile, side_fee: float, spec: Spec, max_fee_to_stop: float = 0.55) -> tuple[bool, str]:
    round_fee_ticks = fee_ticks(side_fee, spec)
    max_allowed = profile.stop_ticks * max_fee_to_stop
    if round_fee_ticks > max_allowed:
        return False, f"startup_fee_filter fee={round_fee_ticks:.1f}t stop={profile.stop_ticks}t max={max_allowed:.1f}t"
    return True, f"fee_ok fee={round_fee_ticks:.1f}t stop={profile.stop_ticks}t"


def apply_br_loss_pause(st: State, states: list[State], net: float) -> None:
    if not br_small_stop_policy_applies(st.profile):
        return
    related = [
        other
        for other in states
        if br_small_stop_policy_applies(other.profile) and state_family(other) == state_family(st)
    ]
    if net >= 0:
        for other in related:
            other.consecutive_losses = 0
        return

    st.consecutive_losses += 1
    loss_count = sum(other.consecutive_losses for other in related)
    if loss_count < BR_POLICY_LOSS_PAUSE_COUNT:
        return

    pause_until = time.monotonic() + BR_POLICY_LOSS_PAUSE_SECONDS
    minutes = int(BR_POLICY_LOSS_PAUSE_SECONDS // 60)
    for other in related:
        other.cooldown_until = max(other.cooldown_until, pause_until)
        other.last_reason = f"brq6_loss_pause losses={loss_count} minutes={minutes}"


def days_to_expiration(spec: Spec) -> float | None:
    if spec.expiration_date is None:
        return None
    exp = spec.expiration_date
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return (exp.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds() / 86400


def expiry_new_entry_block_reason(spec: Spec, threshold_days: float) -> str | None:
    if threshold_days <= 0:
        return None
    dte = days_to_expiration(spec)
    if dte is None or dte > threshold_days:
        return None
    exp = spec.expiration_date.isoformat() if spec.expiration_date else ""
    return f"expiry_filter no_new_entries dte={dte:.1f}d threshold={threshold_days:.1f}d exp={exp}"


def profile_from_row(row: dict, secid: str | None = None, source_secid: str | None = None) -> Profile:
    ticker = secid or row["ticker"]
    def as_int(name: str, default: int = 0) -> int:
        try:
            raw = row.get(name)
            return int(float(raw)) if raw not in (None, "") else default
        except Exception:
            return default

    def as_float(name: str, default: float = 0.0) -> float:
        try:
            raw = row.get(name)
            return float(raw) if raw not in (None, "") else default
        except Exception:
            return default

    profile = Profile(
        secid=ticker,
        stop_ticks=int(row["stop_ticks"]),
        trail_ticks=int(row["trail_ticks"]),
        trail_arm_ticks=int(row["trail_arm_ticks"]),
        target_min_ticks=int(row["target_min_ticks"]),
        max_attempts=int(row["max_attempts"]),
        allowed_direction=str(row.get("v7_direction") or "both").lower(),
        family=str(row.get("v7_family") or contract_family(ticker)),
        source_secid=source_secid or row["ticker"],
        signal_family=str(row.get("signal_family") or row.get("v7_signal_family") or "online_current"),
        entry_timing=str(row.get("entry_timing") or row.get("v7_entry_timing") or "stream_executable"),
        session_filter=str(row.get("session_filter") or "all_available_data"),
        momentum_pct=as_float("momentum_pct"),
        momentum_ticks=as_float("momentum_ticks"),
        breakout_lookback=as_int("breakout_lookback", 6),
        trend_fast=as_int("trend_fast"),
        trend_slow=as_int("trend_slow"),
        volume_multiplier=as_float("volume_multiplier", 1.0),
        volume_window=as_int("volume_window", 20),
        vwap_mode=str(row.get("vwap_mode") or "disabled"),
        vwap_buffer_pct=as_float("vwap_buffer_pct"),
        cooldown_minutes=as_int("cooldown_minutes"),
        max_hold_minutes=as_int("max_hold_minutes"),
        gpt_params_loaded=bool(row.get("signal_family") or row.get("momentum_ticks") or row.get("max_hold_minutes")),
    )
    apply_profile_policy_overrides(profile)
    return profile


def contract_family(secid: str) -> str:
    if secid.endswith("perpA"):
        return secid
    head = secid.rstrip("0123456789")
    month_codes = set("FGHJKMNQUVXZ")
    if len(head) > 1 and head[-1].upper() in month_codes:
        head = head[:-1]
    return head or secid


def br_small_stop_policy_applies(profile: Profile) -> bool:
    source = (profile.source_secid or profile.secid).upper()
    family = (profile.family or contract_family(profile.secid)).upper()
    return source == "BRQ6" or (family == "BR" and profile.stop_ticks <= BR_POLICY_MIN_STOP_TICKS)


def apply_profile_policy_overrides(profile: Profile) -> None:
    if br_small_stop_policy_applies(profile) and profile.stop_ticks < BR_POLICY_MIN_STOP_TICKS:
        profile.stop_ticks = BR_POLICY_MIN_STOP_TICKS


def clone_profile_for_contract(profile: Profile, secid: str) -> Profile:
    cloned = Profile(
        secid=secid,
        stop_ticks=profile.stop_ticks,
        trail_ticks=profile.trail_ticks,
        trail_arm_ticks=profile.trail_arm_ticks,
        target_min_ticks=profile.target_min_ticks,
        max_attempts=profile.max_attempts,
        allowed_direction=profile.allowed_direction,
        family=profile.family or contract_family(secid),
        source_secid=profile.source_secid or profile.secid,
        signal_family=profile.signal_family,
        entry_timing=profile.entry_timing,
        session_filter=profile.session_filter,
        momentum_pct=profile.momentum_pct,
        momentum_ticks=profile.momentum_ticks,
        breakout_lookback=profile.breakout_lookback,
        trend_fast=profile.trend_fast,
        trend_slow=profile.trend_slow,
        volume_multiplier=profile.volume_multiplier,
        volume_window=profile.volume_window,
        vwap_mode=profile.vwap_mode,
        vwap_buffer_pct=profile.vwap_buffer_pct,
        cooldown_minutes=profile.cooldown_minutes,
        max_hold_minutes=profile.max_hold_minutes,
        gpt_params_loaded=profile.gpt_params_loaded,
        gpt_exit_loaded=profile.gpt_exit_loaded,
        gpt_exit_mode=profile.gpt_exit_mode,
        gpt_stop_ticks=profile.gpt_stop_ticks,
        gpt_trail_ticks=profile.gpt_trail_ticks,
        gpt_trail_arm_ticks=profile.gpt_trail_arm_ticks,
        gpt_stop_pct=profile.gpt_stop_pct,
        gpt_trail_pct=profile.gpt_trail_pct,
        gpt_trail_arm_pct=profile.gpt_trail_arm_pct,
        gpt_source_stage2_id=profile.gpt_source_stage2_id,
        gpt_match_quality=profile.gpt_match_quality,
    )
    apply_profile_policy_overrides(cloned)
    return cloned


def load_profiles(path: Path, secids: list[str] | None = None) -> dict[str, Profile]:
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    wanted = set(secids or [])
    out: dict[str, Profile] = {}
    for row in rows:
        if wanted and row["ticker"] not in wanted:
            continue
        if row["quality"] == "skip":
            continue
        out[row["ticker"]] = profile_from_row(row)
    return out


def apply_gpt_shadow_params(profiles: dict[str, Profile], path: Path) -> None:
    if not path or not path.exists():
        print(f"{now_str()} GPT_SHADOW params_missing path={path}", flush=True)
        return
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    by_ticker = {str(row.get("ticker") or "").upper(): row for row in rows if row.get("ticker")}
    source_cache: dict[Path, dict[str, dict]] = {}

    def as_int(row: dict, name: str, current: int) -> int:
        try:
            raw = row.get(name)
            return int(float(raw)) if raw not in (None, "") else current
        except Exception:
            return current

    def as_float(row: dict, name: str, current: float) -> float:
        try:
            raw = row.get(name)
            return float(raw) if raw not in (None, "") else current
        except Exception:
            return current

    def as_float_any(row: dict | None, name: str, current: float = 0.0) -> float:
        if not row:
            return current
        try:
            raw = row.get(name)
            return float(raw) if raw not in (None, "") else current
        except Exception:
            return current

    def exact_source_row(row: dict) -> dict | None:
        source_file = str(row.get("source_file") or "")
        stage2_id = str(row.get("source_stage2_id") or "")
        if not source_file or not stage2_id:
            return None
        source_path = Path(source_file)
        if not source_path.is_absolute():
            source_path = ROOT / source_path
        if source_path not in source_cache:
            source_cache[source_path] = {}
            try:
                with source_path.open(newline="", encoding="utf-8-sig") as f:
                    source_cache[source_path] = {str(r.get("stage2_id") or ""): r for r in csv.DictReader(f)}
            except Exception as exc:
                print(f"{now_str()} GPT_SHADOW source_read_error path={source_path} error={exc}", flush=True)
        return source_cache.get(source_path, {}).get(stage2_id)

    def apply_gpt_exit(profile: Profile, row: dict, source_row: dict | None) -> bool:
        source = source_row or row
        mode = str(source.get("exit_mode") or "").lower()
        profile.gpt_source_stage2_id = str(row.get("source_stage2_id") or source.get("stage2_id") or "")
        profile.gpt_match_quality = str(row.get("match_quality") or "")
        if mode == "percent":
            profile.gpt_exit_loaded = True
            profile.gpt_exit_mode = "percent"
            profile.gpt_stop_pct = as_float_any(source, "stop_pct")
            profile.gpt_trail_pct = as_float_any(source, "trail_pct")
            profile.gpt_trail_arm_pct = as_float_any(source, "activation_pct")
            profile.gpt_stop_ticks = as_float_any(source, "avg_stop_ticks", as_float_any(row, "source_stop_ticks"))
            profile.gpt_trail_ticks = as_float_any(source, "avg_trail_ticks", as_float_any(row, "source_trail_ticks"))
            profile.gpt_trail_arm_ticks = as_float_any(source, "avg_activation_ticks", as_float_any(row, "source_activation_ticks"))
            return profile.gpt_stop_pct > 0 and profile.gpt_trail_pct > 0 and profile.gpt_trail_arm_pct > 0

        stop_ticks = as_float_any(source, "stop_ticks", as_float_any(row, "source_stop_ticks"))
        trail_ticks = as_float_any(source, "trail_ticks", as_float_any(row, "source_trail_ticks"))
        arm_ticks = as_float_any(source, "activation_ticks", as_float_any(row, "source_activation_ticks"))
        if stop_ticks > 0 and trail_ticks > 0 and arm_ticks > 0:
            profile.gpt_exit_loaded = True
            profile.gpt_exit_mode = mode or "direct_ticks"
            profile.gpt_stop_ticks = stop_ticks
            profile.gpt_trail_ticks = trail_ticks
            profile.gpt_trail_arm_ticks = arm_ticks
            return True
        return False

    applied = 0
    exit_applied = 0
    for ticker, profile in profiles.items():
        row = by_ticker.get(ticker.upper())
        if row is None:
            continue
        source_row = exact_source_row(row)
        profile.signal_family = str(row.get("signal_family") or profile.signal_family)
        profile.entry_timing = str(row.get("entry_timing") or profile.entry_timing)
        profile.session_filter = str(row.get("session_filter") or profile.session_filter)
        profile.momentum_pct = as_float(row, "momentum_pct", profile.momentum_pct)
        profile.momentum_ticks = as_float(row, "momentum_ticks", profile.momentum_ticks)
        profile.breakout_lookback = as_int(row, "breakout_lookback", profile.breakout_lookback)
        profile.trend_fast = as_int(row, "trend_fast", profile.trend_fast)
        profile.trend_slow = as_int(row, "trend_slow", profile.trend_slow)
        profile.volume_multiplier = as_float(row, "volume_multiplier", profile.volume_multiplier)
        profile.volume_window = as_int(row, "volume_window", profile.volume_window)
        profile.vwap_mode = str(row.get("vwap_mode") or profile.vwap_mode)
        profile.vwap_buffer_pct = as_float(row, "vwap_buffer_pct", profile.vwap_buffer_pct)
        profile.cooldown_minutes = as_int(row, "cooldown_minutes", profile.cooldown_minutes)
        profile.max_hold_minutes = as_int(row, "max_hold_minutes", profile.max_hold_minutes)
        profile.gpt_params_loaded = True
        if apply_gpt_exit(profile, row, source_row):
            exit_applied += 1
        applied += 1
    print(f"{now_str()} GPT_SHADOW params_loaded applied={applied} exits={exit_applied} path={path}", flush=True)


def seed_candles(client: object, figi: str, minutes: int) -> deque[dict]:
    from t_tech.invest import CandleInterval

    to = datetime.now(timezone.utc)
    from_ = to - timedelta(minutes=minutes)
    resp = client.market_data.get_candles(
        figi=figi,
        from_=from_,
        to=to,
        interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
    )
    rows: deque[dict] = deque(maxlen=max(minutes, 180))
    for candle in resp.candles:
        rows.append(
            {
                "open": quotation_to_float(candle.open),
                "high": quotation_to_float(candle.high),
                "low": quotation_to_float(candle.low),
                "close": quotation_to_float(candle.close),
                "volume": int(candle.volume),
            }
        )
    return rows


def spec_from_tbank(client: object, secid: str, future: object, info: object) -> Spec:
    try:
        lp = client.market_data.get_last_prices(figi=[future.figi]).last_prices
        last = quotation_to_float(lp[0].price) if lp else 0.0
    except Exception:
        last = 0.0
    spec = Spec(
        secid=secid,
        figi=future.figi,
        uid=future.uid,
        min_step=quotation_to_float(info.min_price_increment),
        step_price=quotation_to_float(info.min_price_increment_amount),
        last_rub=0.0,
        last_price=last,
        expiration_date=getattr(info, "expiration_date", None),
        margin_buy=quotation_to_float(getattr(info, "initial_margin_on_buy", None)),
        margin_sell=quotation_to_float(getattr(info, "initial_margin_on_sell", None)),
    )
    spec.last_rub = last / spec.min_step * spec.step_price if last and spec.min_step else 0.0
    return spec


def future_candidates_by_family(client: object, family: str) -> list[object]:
    from t_tech.invest import InstrumentStatus

    now = datetime.now(timezone.utc)
    family_upper = family.upper()
    instruments = client.instruments.futures(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE).instruments
    candidates = []
    for item in instruments:
        ticker = str(getattr(item, "ticker", ""))
        exp = getattr(item, "expiration_date", None)
        if not ticker.upper().startswith(family_upper):
            continue
        if exp is None or exp.astimezone(timezone.utc) <= now:
            continue
        candidates.append(item)
    return sorted(candidates, key=lambda item: getattr(item, "expiration_date", datetime.max.replace(tzinfo=timezone.utc)))


def select_roll_contract(
    client: object,
    current_spec: Spec,
    current_profile: Profile,
    all_profiles: dict[str, Profile],
    args: argparse.Namespace,
) -> tuple[str | None, Profile | None, dict]:
    from t_tech.invest import InstrumentIdType

    family = current_profile.family or contract_family(current_spec.secid)
    current_dte = days_to_expiration(current_spec)
    event = {
        "ticker": current_spec.secid,
        "family": family,
        "expiration": current_spec.expiration_date.isoformat() if current_spec.expiration_date else "",
        "days_to_expiration": round(current_dte, 3) if current_dte is not None else None,
        "status": "not_near_roll",
        "selected": "",
        "selected_profile_source": "",
        "reason": "",
        "candidates": [],
    }
    if current_spec.expiration_date is None or current_dte is None:
        event["status"] = "no_expiration"
        return None, None, event
    if current_spec.expiration_date.year >= 2099 or family.endswith("perpA"):
        event["status"] = "perpetual_or_far_expiration"
        return None, None, event
    if current_dte > float(args.roll_observe_days):
        event["reason"] = f"dte_above_observe_window {current_dte:.1f}>{float(args.roll_observe_days):.1f}"
        return None, None, event

    event["status"] = "searching_next"
    for item in future_candidates_by_family(client, family):
        ticker = str(getattr(item, "ticker", ""))
        exp = getattr(item, "expiration_date", None)
        if ticker == current_spec.secid or exp is None:
            continue
        if exp.astimezone(timezone.utc) <= current_spec.expiration_date.astimezone(timezone.utc):
            continue
        dte = (exp.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds() / 86400
        candidate_event = {
            "ticker": ticker,
            "expiration": exp.isoformat(),
            "days_to_expiration": round(dte, 3),
            "status": "checking",
            "profile_source": "",
            "reason": "",
        }
        event["candidates"].append(candidate_event)
        if dte <= float(args.no_new_expiry_days):
            candidate_event["status"] = "blocked"
            candidate_event["reason"] = "candidate_too_close_to_expiry"
            continue
        try:
            info = client.instruments.future_by(
                id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
                id=getattr(item, "uid"),
            ).instrument
            candidate_spec = spec_from_tbank(client, ticker, item, info)
        except Exception as exc:
            candidate_event["status"] = "blocked"
            candidate_event["reason"] = f"instrument_error {type(exc).__name__}: {exc}"
            continue

        profile = all_profiles.get(ticker)
        if profile is not None:
            candidate_event["profile_source"] = ticker
        else:
            profile = clone_profile_for_contract(current_profile, ticker)
            candidate_event["profile_source"] = current_profile.secid
        can_trade, fee_reason = profile_can_trade(profile, commission_side_rub(candidate_spec, 1, 0.00025, None), candidate_spec)
        candidate_event["reason"] = fee_reason
        if not can_trade:
            candidate_event["status"] = "blocked"
            continue
        try:
            book = client.market_data.get_order_book(figi=candidate_spec.figi, depth=int(args.orderbook_depth))
            if not getattr(book, "bids", None) or not getattr(book, "asks", None):
                candidate_event["status"] = "blocked"
                candidate_event["reason"] = "no_live_book"
                continue
        except Exception as exc:
            candidate_event["status"] = "blocked"
            candidate_event["reason"] = f"book_error {type(exc).__name__}: {exc}"
            continue
        candidate_event["status"] = "selected"
        event["status"] = "roll_ready"
        event["selected"] = ticker
        event["selected_profile_source"] = candidate_event["profile_source"]
        event["reason"] = f"current_dte={current_dte:.1f}d candidate_ok {fee_reason}"
        return ticker, profile, event

    event["status"] = "blocked_no_candidate"
    event["reason"] = "no viable next contract found"
    return None, None, event


def make_stream_requests(specs: list[Spec], depth: int):
    from t_tech.invest import (
        CandleInstrument,
        LastPriceInstrument,
        MarketDataRequest,
        OrderBookInstrument,
        SubscribeCandlesRequest,
        SubscribeLastPriceRequest,
        SubscribeOrderBookRequest,
        SubscribeTradesRequest,
        SubscriptionAction,
        SubscriptionInterval,
        TradeInstrument,
    )

    yield MarketDataRequest(
        subscribe_last_price_request=SubscribeLastPriceRequest(
            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[LastPriceInstrument(figi=s.figi, instrument_id=s.uid) for s in specs],
        )
    )
    yield MarketDataRequest(
        subscribe_trades_request=SubscribeTradesRequest(
            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[TradeInstrument(figi=s.figi, instrument_id=s.uid) for s in specs],
        )
    )
    yield MarketDataRequest(
        subscribe_order_book_request=SubscribeOrderBookRequest(
            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[OrderBookInstrument(figi=s.figi, instrument_id=s.uid, depth=depth) for s in specs],
        )
    )
    yield MarketDataRequest(
        subscribe_candles_request=SubscribeCandlesRequest(
            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[
                CandleInstrument(
                    figi=s.figi,
                    instrument_id=s.uid,
                    interval=SubscriptionInterval.SUBSCRIPTION_INTERVAL_ONE_MINUTE,
                )
                for s in specs
            ],
            waiting_close=True,
        )
    )
    while True:
        time.sleep(60)


def avg_volume(rows: list[dict], n: int) -> float:
    vols = [float(r.get("volume") or 0) for r in rows[-n:] if float(r.get("volume") or 0) > 0]
    return sum(vols) / len(vols) if vols else 0.0


def signal(state: State, aggressive: bool) -> tuple[Direction | None, str]:
    rows = list(state.candles)
    state.last_signal_metrics = {}
    if len(rows) < 12:
        return None, f"warmup={len(rows)}"
    spec = state.spec
    lookback = 4 if aggressive else 6
    vol_mult = 0.7 if aggressive else 1.15
    vwap_buffer = 0 if aggressive else 1
    trend_need = 0.5 if aggressive else 1.0
    signal_need = 1 if aggressive else 2
    book_imbalance = 1.2 if aggressive else 1.35
    max_fee_to_stop = 0.55 if aggressive else 0.40

    last = rows[-1]
    prev = rows[-2]
    recent = rows[-lookback - 1 : -1]
    last_close = float(last["close"])
    prev_close = float(prev["close"])
    last_vol = float(last.get("volume") or 0)
    avgv = avg_volume(rows[:-1], 20)
    vwap, _ = volume_vwap(rows, state.last_price)
    mom = (last_close - prev_close) / spec.min_step
    fast = sum(float(r["close"]) for r in rows[-2:]) / 2
    slow = sum(float(r["close"]) for r in rows[-5:]) / 5
    trend = (fast - slow) / spec.min_step
    high = max(float(r["high"]) for r in recent)
    low = min(float(r["low"]) for r in recent)

    bid_qty = ask_qty = 0
    if state.last_order_book is not None:
        bid_qty = sum(int(getattr(x, "quantity", 0) or 0) for x in getattr(state.last_order_book, "bids", [])[:3])
        ask_qty = sum(int(getattr(x, "quantity", 0) or 0) for x in getattr(state.last_order_book, "asks", [])[:3])
    if bid_qty <= 0 or ask_qty <= 0:
        state.last_signal_metrics = {
            "last_price": state.last_price,
            "last_close": last_close,
            "vwap": vwap,
            "mom": mom,
            "trend": trend,
            "last_vol": last_vol,
            "avgv": avgv,
            "bid_qty": bid_qty,
            "ask_qty": ask_qty,
        }
        return None, f"book_filter empty_book bid={bid_qty} ask={ask_qty}"
    levels = best_levels(state.last_order_book, spec)
    spread_ticks = levels.get("spread_ticks")
    fee_ticks = (2 * state.side_fee / spec.step_price) if spec.step_price else 999
    recent_range_ticks = max(float(r["high"]) - float(r["low"]) for r in rows[-5:]) / spec.min_step
    state.last_signal_metrics = {
        "last_price": state.last_price,
        "last_close": last_close,
        "prev_close": prev_close,
        "vwap": vwap,
        "mom": mom,
        "trend": trend,
        "last_vol": last_vol,
        "avgv": avgv,
        "vol_ratio": (last_vol / avgv) if avgv else None,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "spread_ticks": spread_ticks,
        "fee_ticks": fee_ticks,
        "recent_range_ticks": recent_range_ticks,
        "recent_high": high,
        "recent_low": low,
        "long_breakout_margin_ticks": (last_close - high) / spec.min_step,
        "short_breakout_margin_ticks": (low - last_close) / spec.min_step,
    }
    max_general_spread = state.profile.stop_ticks * SPREAD_DOMINATES_RATIO
    if spread_ticks is None or float(spread_ticks) > max_general_spread:
        spread_txt = "-" if spread_ticks is None else f"{float(spread_ticks):.1f}t"
        return None, f"spread_filter spread={spread_txt} stop={state.profile.stop_ticks}t max={max_general_spread:.1f}t"
    if br_small_stop_policy_applies(state.profile):
        max_spread = state.profile.stop_ticks * BR_POLICY_MAX_SPREAD_TO_STOP
        if spread_ticks is None or float(spread_ticks) > max_spread:
            spread_txt = "-" if spread_ticks is None else f"{float(spread_ticks):.1f}t"
            return None, f"brq6_spread_filter spread={spread_txt} stop={state.profile.stop_ticks}t max={max_spread:.1f}t"
    vol_ok = avgv > 0 and last_vol >= avgv * vol_mult
    if fee_ticks > state.profile.stop_ticks * max_fee_to_stop:
        return None, f"fee_filter fee={fee_ticks:.1f}t stop={state.profile.stop_ticks}t"
    if recent_range_ticks < fee_ticks + 2:
        return None, f"range_filter fee={fee_ticks:.1f}t"
    long_book = bid_qty >= ask_qty * book_imbalance if ask_qty > 0 else True
    short_book = ask_qty >= bid_qty * book_imbalance if bid_qty > 0 else True

    long_ok = (
        state.last_price >= vwap + vwap_buffer * spec.min_step
        and trend >= trend_need
        and mom >= signal_need
        and last_close >= high
        and vol_ok
        and long_book
    )
    short_ok = (
        state.last_price <= vwap - vwap_buffer * spec.min_step
        and trend <= -trend_need
        and mom <= -signal_need
        and last_close <= low
        and vol_ok
        and short_book
    )
    reason = f"p={state.last_price:g} vwap={vwap:g} mom={mom:.1f} trend={trend:.1f} vol={last_vol:.0f}/{avgv:.0f} book={bid_qty}/{ask_qty}"
    if long_ok:
        return "long", f"entry_signal long {reason}"
    if short_ok:
        return "short", f"entry_signal short {reason}"
    return None, f"watch_conditions {reason}"


def mean_close(rows: list[dict], n: int) -> float:
    subset = rows[-max(1, n) :]
    return sum(float(r["close"]) for r in subset) / len(subset) if subset else 0.0


def rolling_vwap(rows: list[dict], n: int | None) -> float:
    subset = rows[-n:] if n else rows
    volume_sum = sum(float(r.get("volume") or 0) for r in subset)
    if volume_sum <= 0:
        return float(subset[-1]["close"]) if subset else 0.0
    return sum(float(r["close"]) * float(r.get("volume") or 0) for r in subset) / volume_sum


def gpt_session_allowed(profile: Profile) -> bool:
    mode = (profile.session_filter or "all_available_data").lower()
    if mode in {"", "all_available_data", "all"}:
        return True
    now_sec = clock_seconds_now()
    start = 10 * 3600
    end = 18 * 3600 + 45 * 60
    if mode == "exclude_first_last_10_minutes":
        start += 10 * 60
        end -= 10 * 60
    return start <= now_sec < end


def gpt_profile_signal(state: State, layer: GptShadowLayerConfig | None = None) -> tuple[Direction | None, str]:
    layer = layer or GPT_SHADOW_LAYER_CONFIGS["full"]
    profile = state.profile
    rows = list(state.candles)
    if not profile.gpt_params_loaded:
        return None, f"gpt_shadow {layer.name} params_missing"
    if not gpt_session_allowed(profile):
        return None, f"gpt_shadow {layer.name} session_filter {profile.session_filter}"

    lookback = max(2, int(profile.breakout_lookback or 6))
    trend_fast = max(1, int(profile.trend_fast or 3))
    trend_slow = max(trend_fast + 1, int(profile.trend_slow or max(8, trend_fast + 1)))
    vol_window = max(2, int(profile.volume_window or 20))
    needed = max(lookback + 2, trend_slow + 1, vol_window + 2, 12)
    if len(rows) < needed:
        return None, f"gpt_shadow {layer.name} warmup={len(rows)}/{needed}"

    spec = state.spec
    last = rows[-1]
    prev = rows[-2]
    recent = rows[-lookback - 1 : -1]
    last_close = float(last["close"])
    prev_close = float(prev["close"])
    last_vol = float(last.get("volume") or 0)
    avgv = avg_volume(rows[:-1], vol_window)
    mom = (last_close - prev_close) / spec.min_step
    threshold = float(profile.momentum_ticks or 0.0)
    if profile.momentum_pct:
        threshold = max(threshold, abs(float(profile.momentum_pct) * last_close / spec.min_step))
    threshold *= max(0.05, float(layer.threshold_mult))
    if threshold <= 0:
        threshold = 2.0
    fast = mean_close(rows, trend_fast)
    slow = mean_close(rows, trend_slow)
    trend = (fast - slow) / spec.min_step
    high = max(float(r["high"]) for r in recent)
    low = min(float(r["low"]) for r in recent)
    recent_range_ticks = max(float(r["high"]) - float(r["low"]) for r in rows[-5:]) / spec.min_step

    vwap_mode = (profile.vwap_mode or "disabled").lower()
    if vwap_mode == "rolling20":
        vwap = rolling_vwap(rows, 20)
    elif vwap_mode == "rolling60":
        vwap = rolling_vwap(rows, 60)
    elif vwap_mode == "session":
        vwap = rolling_vwap(rows, None)
    else:
        vwap, _ = volume_vwap(rows, state.last_price)
    vwap_buffer_ticks = abs(float(profile.vwap_buffer_pct or 0.0) * last_close / spec.min_step)
    vwap_buffer_ticks *= max(0.0, float(layer.vwap_buffer_mult))

    bid_qty = ask_qty = 0
    if state.last_order_book is not None:
        bid_qty = sum(int(getattr(x, "quantity", 0) or 0) for x in getattr(state.last_order_book, "bids", [])[:3])
        ask_qty = sum(int(getattr(x, "quantity", 0) or 0) for x in getattr(state.last_order_book, "asks", [])[:3])
    if bid_qty <= 0 or ask_qty <= 0:
        return None, f"gpt_shadow {layer.name} book_filter empty_book bid={bid_qty} ask={ask_qty}"
    levels = best_levels(state.last_order_book, spec)
    spread_ticks = levels.get("spread_ticks")
    exit_ticks = gpt_shadow_exit_ticks(profile, spec, state.last_price or last_close)
    max_general_spread = exit_ticks.stop_ticks * max(0.1, float(layer.spread_stop_ratio))
    if spread_ticks is None or float(spread_ticks) > max_general_spread:
        spread_txt = "-" if spread_ticks is None else f"{float(spread_ticks):.1f}t"
        return None, f"gpt_shadow {layer.name} spread_filter spread={spread_txt} stop={exit_ticks.stop_ticks}t max={max_general_spread:.1f}t"

    fee_t = fee_ticks(state.side_fee, spec)
    vol_mult = float(profile.volume_multiplier if profile.volume_multiplier is not None else 1.0)
    vol_mult *= max(0.0, float(layer.volume_mult))
    vol_ok = vol_mult <= 0 or (avgv > 0 and last_vol >= avgv * vol_mult)
    if not vol_ok:
        return None, f"gpt_shadow {layer.name} volume_filter vol={last_vol:.0f}/{avgv:.0f} mult={vol_mult:g}"

    family = (profile.signal_family or "pure_trailing_after_impulse").lower()
    allowed = (profile.allowed_direction or "both").lower()
    vwap_long_ok = vwap_mode == "disabled" or (last_close - vwap) / spec.min_step >= vwap_buffer_ticks
    vwap_short_ok = vwap_mode == "disabled" or (vwap - last_close) / spec.min_step >= vwap_buffer_ticks
    break_tol = max(0.0, float(layer.breakout_tolerance_ticks)) * spec.min_step
    long_break = last_close >= high - break_tol
    short_break = last_close <= low + break_tol
    long_mom = mom >= threshold
    short_mom = mom <= -threshold
    trend_tol = max(float(layer.trend_tolerance_ticks), abs(threshold) * float(layer.trend_tolerance_mult))
    long_trend = trend >= -trend_tol
    short_trend = trend <= trend_tol

    if family == "momentum_breakout":
        long_ok = long_mom and long_break and long_trend and vwap_long_ok
        short_ok = short_mom and short_break and short_trend and vwap_short_ok
    elif family == "vwap_impulse":
        long_ok = long_mom and long_trend and vwap_long_ok
        short_ok = short_mom and short_trend and vwap_short_ok
    elif family == "range_expansion":
        min_range = max(threshold, (fee_t + float(spread_ticks or 0) + 2.0) * max(0.1, float(layer.range_cost_mult)))
        long_ok = recent_range_ticks >= min_range and long_break and mom > 0 and vwap_long_ok
        short_ok = recent_range_ticks >= min_range and short_break and mom < 0 and vwap_short_ok
    elif family == "trend_pullback":
        long_ok = long_trend and mom >= max(1.0, threshold * 0.5) and vwap_long_ok
        short_ok = short_trend and mom <= -max(1.0, threshold * 0.5) and vwap_short_ok
    else:
        long_ok = long_mom and long_trend and vwap_long_ok
        short_ok = short_mom and short_trend and vwap_short_ok

    reason = (
        f"gpt_shadow layer={layer.name} family={family} p={state.last_price:g} close={last_close:g} "
        f"mom={mom:.1f}/{threshold:.1f} trend={trend:.1f} "
        f"vol={last_vol:.0f}/{avgv:.0f} vwap={vwap:g} spread={spread_ticks} book={bid_qty}/{ask_qty}"
    )
    if long_ok and allowed in {"long", "both"}:
        return "long", f"gpt_entry_signal long {reason}"
    if short_ok and allowed in {"short", "both"}:
        return "short", f"gpt_entry_signal short {reason}"
    return None, f"gpt_watch_conditions {reason}"


def entry_shadow_flags(st: State, direction: Direction) -> dict:
    metrics = st.last_signal_metrics or {}
    stop_ticks = max(int(st.profile.stop_ticks or 0), 1)
    bid_qty = float(metrics.get("bid_qty") or 0)
    ask_qty = float(metrics.get("ask_qty") or 0)
    if direction == "long":
        book_signal_ratio = bid_qty / ask_qty if ask_qty > 0 else None
        directional_vwap_ticks = (
            (float(metrics.get("last_price") or 0) - float(metrics.get("vwap") or 0)) / st.spec.min_step
            if metrics.get("vwap") is not None and st.spec.min_step
            else None
        )
        breakout_margin_ticks = metrics.get("long_breakout_margin_ticks")
    else:
        book_signal_ratio = ask_qty / bid_qty if bid_qty > 0 else None
        directional_vwap_ticks = (
            (float(metrics.get("vwap") or 0) - float(metrics.get("last_price") or 0)) / st.spec.min_step
            if metrics.get("vwap") is not None and st.spec.min_step
            else None
        )
        breakout_margin_ticks = metrics.get("short_breakout_margin_ticks")
    spread_ticks = metrics.get("spread_ticks")
    fee_ticks = metrics.get("fee_ticks")
    vol_ratio = metrics.get("vol_ratio")
    mom = abs(float(metrics.get("mom") or 0))
    trend = abs(float(metrics.get("trend") or 0))
    directional_vwap_to_stop = (
        abs(float(directional_vwap_ticks)) / stop_ticks if directional_vwap_ticks is not None else None
    )
    spread_to_stop = float(spread_ticks) / stop_ticks if spread_ticks is not None else None
    fee_to_stop = float(fee_ticks) / stop_ticks if fee_ticks is not None else None

    soft_reasons: list[str] = []
    hard_reasons: list[str] = []
    if vol_ratio is not None and book_signal_ratio is not None and vol_ratio < 1.2 and book_signal_ratio < 1.5:
        soft_reasons.append("low_volume_and_weak_book")
    if mom < 15 and trend < 10:
        soft_reasons.append("weak_momentum_trend")
    if directional_vwap_to_stop is not None and directional_vwap_to_stop > 1.25:
        soft_reasons.append("late_chase_from_vwap")

    if vol_ratio is None or vol_ratio < 1.2:
        hard_reasons.append("volume_lt_1_2x")
    if book_signal_ratio is None or book_signal_ratio < 1.5:
        hard_reasons.append("book_lt_1_5x")
    if mom < 20 and trend < 10:
        hard_reasons.append("weak_momentum_trend")
    if spread_to_stop is not None and spread_to_stop > 0.40:
        hard_reasons.append("spread_gt_40pct_stop")
    if directional_vwap_to_stop is not None and directional_vwap_to_stop > 1.25:
        hard_reasons.append("late_chase_from_vwap")

    return {
        "book_signal_ratio": book_signal_ratio,
        "directional_vwap_ticks": directional_vwap_ticks,
        "directional_vwap_to_stop": directional_vwap_to_stop,
        "breakout_margin_ticks": breakout_margin_ticks,
        "spread_to_stop_ratio": spread_to_stop,
        "fee_to_stop_ratio": fee_to_stop,
        "soft_entry_allow": not soft_reasons,
        "soft_entry_reason": "ok" if not soft_reasons else ";".join(soft_reasons),
        "hard_entry_allow": not hard_reasons,
        "hard_entry_reason": "ok" if not hard_reasons else ";".join(hard_reasons),
    }


def append_entry_audit(path: Path, row: dict) -> None:
    if not str(path):
        return
    stable = {key: row.get(key) for key in ENTRY_AUDIT_COLUMNS}
    append_schema_stable_csv(path, stable)


def write_entry_audit_open(
    path: Path,
    st: State,
    direction: Direction,
    entry_price: float,
    qty: int,
    sizing: SizingDecision,
    risk_decision: RiskDecision,
    raw_signal_reason: str,
) -> None:
    metrics = st.last_signal_metrics or {}
    flags = entry_shadow_flags(st, direction)
    opened_at = st.position.opened_at if st.position is not None else now_str()
    entry_id = f"{opened_at}|{st.contour}|{st.spec.secid}|{direction}|{entry_price:g}"
    append_entry_audit(
        path,
        {
            "event_time": opened_at,
            "event_type": "entry",
            "entry_id": entry_id,
            "contour": st.contour,
            "secid": st.spec.secid,
            "family": state_family(st),
            "direction": direction,
            "qty": qty,
            "entry_price": entry_price,
            "last_price": metrics.get("last_price"),
            "last_close": metrics.get("last_close"),
            "vwap": metrics.get("vwap"),
            "directional_vwap_ticks": flags.get("directional_vwap_ticks"),
            "directional_vwap_to_stop": flags.get("directional_vwap_to_stop"),
            "momentum_ticks": metrics.get("mom"),
            "trend_ticks": metrics.get("trend"),
            "last_volume": metrics.get("last_vol"),
            "avg_volume": metrics.get("avgv"),
            "volume_ratio": metrics.get("vol_ratio"),
            "bid_qty_top3": metrics.get("bid_qty"),
            "ask_qty_top3": metrics.get("ask_qty"),
            "book_signal_ratio": flags.get("book_signal_ratio"),
            "spread_ticks": metrics.get("spread_ticks"),
            "spread_to_stop_ratio": flags.get("spread_to_stop_ratio"),
            "fee_ticks": metrics.get("fee_ticks"),
            "fee_to_stop_ratio": flags.get("fee_to_stop_ratio"),
            "recent_range_ticks": metrics.get("recent_range_ticks"),
            "breakout_margin_ticks": flags.get("breakout_margin_ticks"),
            "stop_ticks": st.profile.stop_ticks,
            "trail_ticks": st.profile.trail_ticks,
            "trail_arm_ticks": st.profile.trail_arm_ticks,
            "full_stop_risk_rub": sizing.full_stop_rub,
            "risk_mode": risk_decision.mode,
            "risk_reason": risk_decision.reason,
            "sizing_reason": sizing.reason,
            "soft_entry_allow": flags.get("soft_entry_allow"),
            "soft_entry_reason": flags.get("soft_entry_reason"),
            "hard_entry_allow": flags.get("hard_entry_allow"),
            "hard_entry_reason": flags.get("hard_entry_reason"),
            "raw_signal_reason": raw_signal_reason,
        },
    )


def write_entry_audit_close(path: Path, st: State, fill_price: float, fill_source: str, ticks: float, net: float) -> None:
    if st.position is None:
        return
    entry_id = f"{st.position.opened_at}|{st.contour}|{st.spec.secid}|{st.position.direction}|{st.position.entry_price:g}"
    append_entry_audit(
        path,
        {
            "event_time": now_str(),
            "event_type": "exit",
            "entry_id": entry_id,
            "contour": st.contour,
            "secid": st.spec.secid,
            "family": state_family(st),
            "direction": st.position.direction,
            "qty": st.position.qty,
            "entry_price": st.position.entry_price,
            "exit_price": fill_price,
            "exit_source": fill_source,
            "net_rub": round(net, 2),
            "ticks": round(ticks, 3),
            "stop_ticks": st.profile.stop_ticks,
            "trail_ticks": st.profile.trail_ticks,
            "trail_arm_ticks": st.profile.trail_arm_ticks,
        },
    )


def print_report(states: list[State], started: float) -> None:
    elapsed = int((time.monotonic() - started) // 60)
    print(f"{now_str()} REPORT minute={elapsed}")
    for contour in ["strict", "aggressive"]:
        subset = [s for s in states if s.contour == contour]
        closed = sum(s.closed for s in subset)
        attempts = sum(s.attempts for s in subset)
        net = sum(s.closed_net for s in subset)
        open_count = sum(1 for s in subset if s.position is not None)
        leaders = sorted(subset, key=lambda s: s.closed_net, reverse=True)[:5]
        leader_txt = " ".join(f"{s.spec.secid}:{s.closed_net:.0f}/{s.attempts}" for s in leaders)
        print(f"{now_str()} {contour} attempts={attempts} closed={closed} open={open_count} net={net:.2f} leaders={leader_txt}")


def print_portfolio_report(states: list[State], portfolio: Portfolio) -> None:
    margin = used_margin(states)
    open_count = sum(1 for s in states if s.position is not None)
    print(
        f"{now_str()} PORTFOLIO capital={portfolio.initial_capital:.0f} "
        f"equity={portfolio.equity:.2f} closed_net={portfolio.closed_net:.2f} "
        f"used_margin={margin:.2f} open={open_count}"
    )


def parse_clock_time(value: str | None) -> int | None:
    if not value:
        return None
    target = datetime.strptime(value, "%H:%M")
    return target.hour * 3600 + target.minute * 60


def clock_seconds_now() -> int:
    current = datetime.now()
    return current.hour * 3600 + current.minute * 60 + current.second


def daily_trading_enabled(no_trade_before: int | None, no_new_after: int | None) -> bool:
    now_sec = clock_seconds_now()
    if no_trade_before is not None and now_sec < no_trade_before:
        return False
    if no_new_after is not None and now_sec >= no_new_after:
        return False
    return True


def daily_force_close_due(force_close_at: int | None) -> bool:
    return force_close_at is not None and clock_seconds_now() >= force_close_at


def best_levels(orderbook: object | None, spec: Spec) -> dict:
    if orderbook is None:
        return {
            "bid": None,
            "ask": None,
            "bid_size": None,
            "ask_size": None,
            "spread_ticks": None,
        }
    bids = getattr(orderbook, "bids", []) or []
    asks = getattr(orderbook, "asks", []) or []
    bid = quotation_to_float(bids[0].price) if bids else None
    ask = quotation_to_float(asks[0].price) if asks else None
    bid_size = int(getattr(bids[0], "quantity", 0) or 0) if bids else None
    ask_size = int(getattr(asks[0], "quantity", 0) or 0) if asks else None
    spread_ticks = round((ask - bid) / spec.min_step, 3) if bid is not None and ask is not None else None
    return {
        "bid": bid,
        "ask": ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "spread_ticks": spread_ticks,
    }


def executable_price(state: State, direction: Direction | str, action: str) -> tuple[float | None, str]:
    levels = best_levels(state.last_order_book, state.spec)
    bid = levels["bid"]
    ask = levels["ask"]
    if action == "entry":
        price = ask if direction == "long" else bid
        source = "ask_entry" if direction == "long" else "bid_entry"
    else:
        price = bid if direction == "long" else ask
        source = "bid_exit" if direction == "long" else "ask_exit"
    if price is None:
        return None, "no_book"
    return round_to_step(float(price), state.spec.min_step), source


def stop_limit_available_qty(state: State, pos: Position) -> int:
    if state.last_order_book is None:
        return 0
    levels = (
        getattr(state.last_order_book, "bids", []) or []
        if pos.direction == "long"
        else getattr(state.last_order_book, "asks", []) or []
    )
    qty = 0
    for level in levels:
        price = quotation_to_float(level.price)
        if pos.direction == "long":
            if price < pos.stop_price:
                continue
        else:
            if price > pos.stop_price:
                continue
        qty += int(getattr(level, "quantity", 0) or 0)
    return qty


def stop_adverse_overrun_ticks(pos: Position, trigger_price: float, spec: Spec) -> float:
    if pos.direction == "long":
        return max(0.0, (pos.stop_price - trigger_price) / spec.min_step)
    return max(0.0, (trigger_price - pos.stop_price) / spec.min_step)


def stop_limit_fill_price(
    state: State,
    pos: Position,
    trigger_price: float,
    emergency_ticks: float,
) -> tuple[float | None, str, int, float]:
    spec = state.spec
    if not is_stop_hit(pos, trigger_price):
        return None, "stop_limit_waiting", 0, 0.0
    available_qty = stop_limit_available_qty(state, pos)
    if available_qty >= pos.qty:
        return round_to_step(pos.stop_price, spec.min_step), "broker_stop_limit_fill", available_qty, 0.0
    overrun = stop_adverse_overrun_ticks(pos, trigger_price, spec)
    if overrun >= emergency_ticks:
        return round_to_step(trigger_price, spec.min_step), "emergency_market_after_missed_limit", available_qty, overrun
    return None, "stop_limit_touched_waiting_fill", available_qty, overrun


def clone_position(pos: Position) -> Position:
    return Position(
        direction=pos.direction,
        entry_price=pos.entry_price,
        qty=pos.qty,
        best_price=pos.best_price,
        stop_price=pos.stop_price,
        opened_at=pos.opened_at,
    )


def has_active_shadow(state: State) -> bool:
    return any(not state.shadow_closed.get(model) for model in state.shadow_positions)


def all_shadow_models_closed(state: State) -> bool:
    return bool(state.shadow_positions) and all(state.shadow_closed.get(model) for model in state.shadow_positions)


def close_shadow(
    path: Path,
    state: State,
    model: str,
    pos: Position,
    exit_price: float,
    exit_source: str,
    trigger_price: float | None,
    trigger_source: str,
    stop_limit_qty: int | None = None,
    stop_overrun_ticks: float | None = None,
) -> None:
    ticks, gross, net = pnl_rub(pos, exit_price, state.spec, state.side_fee)
    append_trade(
        path,
        {
            "closed_at": now_str(),
            "model": model,
            "contour": state.contour,
            "secid": state.spec.secid,
            "direction": pos.direction,
            "qty": pos.qty,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "exit_source": exit_source,
            "trigger_price": trigger_price,
            "trigger_source": trigger_source,
            "stop_limit_qty": stop_limit_qty,
            "stop_overrun_ticks": stop_overrun_ticks,
            "ticks": round(ticks, 3),
            "gross_rub": round(gross, 2),
            "fees_rub": round(2 * state.side_fee * pos.qty, 2),
            "net_rub": round(net, 2),
        },
    )
    state.shadow_closed[model] = True


def update_shadow_models(
    state: State,
    path: Path,
    exit_price: float,
    exit_source: str,
    candle_closed: bool,
    emergency_ticks: float,
) -> None:
    if not state.shadow_positions:
        return
    stream_pos = state.shadow_positions.get("stream_stoplimit")
    if stream_pos is not None and not state.shadow_closed.get("stream_stoplimit"):
        move_ticks = (
            (exit_price - stream_pos.entry_price) / state.spec.min_step
            if stream_pos.direction == "long"
            else (stream_pos.entry_price - exit_price) / state.spec.min_step
        )
        fee_ticks = (2 * state.side_fee / state.spec.step_price) if state.spec.step_price else 999
        if move_ticks >= max(state.profile.trail_arm_ticks, fee_ticks + 1):
            update_stop(stream_pos, exit_price, state.profile.trail_ticks, state.spec)
            min_net_stop = stream_pos.entry_price + (fee_ticks + 0.5) * state.spec.min_step if stream_pos.direction == "long" else stream_pos.entry_price - (fee_ticks + 0.5) * state.spec.min_step
            if stream_pos.direction == "long":
                stream_pos.stop_price = max(stream_pos.stop_price, round_to_step(min_net_stop, state.spec.min_step))
            else:
                stream_pos.stop_price = min(stream_pos.stop_price, round_to_step(min_net_stop, state.spec.min_step))
        fill_price, fill_source, stop_qty, overrun = stop_limit_fill_price(state, stream_pos, exit_price, emergency_ticks)
        if fill_price is not None:
            close_shadow(path, state, "stream_stoplimit", stream_pos, fill_price, fill_source, exit_price, exit_source, stop_qty, overrun)

    candle_pos = state.shadow_positions.get("candle_like")
    if candle_pos is not None and not state.shadow_closed.get("candle_like") and candle_closed:
        candle_exit = state.last_price
        move_ticks = (
            (candle_exit - candle_pos.entry_price) / state.spec.min_step
            if candle_pos.direction == "long"
            else (candle_pos.entry_price - candle_exit) / state.spec.min_step
        )
        fee_ticks = (2 * state.side_fee / state.spec.step_price) if state.spec.step_price else 999
        if move_ticks >= max(state.profile.trail_arm_ticks, fee_ticks + 1):
            update_stop(candle_pos, candle_exit, state.profile.trail_ticks, state.spec)
            min_net_stop = candle_pos.entry_price + (fee_ticks + 0.5) * state.spec.min_step if candle_pos.direction == "long" else candle_pos.entry_price - (fee_ticks + 0.5) * state.spec.min_step
            if candle_pos.direction == "long":
                candle_pos.stop_price = max(candle_pos.stop_price, round_to_step(min_net_stop, state.spec.min_step))
            else:
                candle_pos.stop_price = min(candle_pos.stop_price, round_to_step(min_net_stop, state.spec.min_step))
        if is_stop_hit(candle_pos, candle_exit):
            close_shadow(path, state, "candle_like", candle_pos, candle_pos.stop_price, "candle_like_stop_fill", candle_exit, "closed_1m_candle")


def process_open_state_exit(
    st: State,
    args: argparse.Namespace,
    portfolio: Portfolio,
    states: list[State],
    candle_closed: bool,
    actual_trigger_override: float | None = None,
    actual_trigger_source_override: str | None = None,
    force_exit_reason: str | None = None,
) -> None:
    if st.position is None:
        if has_active_shadow(st):
            shadow_pos = next(iter(st.shadow_positions.values()))
            shadow_exit_price, shadow_exit_source = executable_price(st, shadow_pos.direction, "exit")
            if shadow_exit_price is not None:
                update_shadow_models(
                    st,
                    Path(args.shadow_log),
                    shadow_exit_price,
                    shadow_exit_source,
                    candle_closed,
                    float(args.stop_limit_emergency_ticks),
                )
            st.last_reason = "shadow_compare_active"
            if all_shadow_models_closed(st):
                st.shadow_positions = {}
                st.shadow_closed = {}
        return

    pos = st.position
    exit_price, exit_source = executable_price(st, pos.direction, "exit")
    if exit_price is None:
        st.last_reason = "book_filter no_executable_exit"
        return
    update_shadow_models(
        st,
        Path(args.shadow_log),
        exit_price,
        exit_source,
        candle_closed,
        float(args.stop_limit_emergency_ticks),
    )
    fill_price = None
    fill_source = "actual_waiting"
    stop_limit_qty = None
    stop_overrun_ticks = None
    actual_trigger_price = actual_trigger_override if actual_trigger_override is not None else exit_price
    actual_trigger_source = actual_trigger_source_override or exit_source
    fee_ticks = (2 * st.side_fee / st.spec.step_price) if st.spec.step_price else 999
    expiry_close_days = float(getattr(args, "expiry_force_close_days", 0.0))
    dte = days_to_expiration(st.spec)
    if force_exit_reason:
        fill_price = round_to_step(exit_price, st.spec.min_step)
        fill_source = force_exit_reason
        actual_trigger_price = fill_price
        actual_trigger_source = force_exit_reason
    elif expiry_close_days > 0 and dte is not None and dte <= expiry_close_days:
        fill_price = round_to_step(exit_price, st.spec.min_step)
        fill_source = "expiry_force_close"
        actual_trigger_price = fill_price
        actual_trigger_source = f"expiry_dte_{dte:.1f}d"
    elif args.actual_exit_model == "candle_like":
        if candle_closed:
            actual_trigger_price = round_to_step(actual_trigger_price, st.spec.min_step)
            move_ticks = (
                (actual_trigger_price - pos.entry_price) / st.spec.min_step
                if pos.direction == "long"
                else (pos.entry_price - actual_trigger_price) / st.spec.min_step
            )
            if move_ticks >= max(st.profile.trail_arm_ticks, fee_ticks + 1):
                update_stop(pos, actual_trigger_price, st.profile.trail_ticks, st.spec)
                min_net_stop = pos.entry_price + (fee_ticks + 0.5) * st.spec.min_step if pos.direction == "long" else pos.entry_price - (fee_ticks + 0.5) * st.spec.min_step
                if pos.direction == "long":
                    pos.stop_price = max(pos.stop_price, round_to_step(min_net_stop, st.spec.min_step))
                else:
                    pos.stop_price = min(pos.stop_price, round_to_step(min_net_stop, st.spec.min_step))
            if is_stop_hit(pos, actual_trigger_price):
                fill_price = round_to_step(pos.stop_price, st.spec.min_step)
                fill_source = "actual_candle_like_stop_fill"
    else:
        move_ticks = (
            (exit_price - pos.entry_price) / st.spec.min_step
            if pos.direction == "long"
            else (pos.entry_price - exit_price) / st.spec.min_step
        )
        if move_ticks >= max(st.profile.trail_arm_ticks, fee_ticks + 1):
            update_stop(pos, exit_price, st.profile.trail_ticks, st.spec)
            min_net_stop = pos.entry_price + (fee_ticks + 0.5) * st.spec.min_step if pos.direction == "long" else pos.entry_price - (fee_ticks + 0.5) * st.spec.min_step
            if pos.direction == "long":
                pos.stop_price = max(pos.stop_price, round_to_step(min_net_stop, st.spec.min_step))
            else:
                pos.stop_price = min(pos.stop_price, round_to_step(min_net_stop, st.spec.min_step))
        fill_price, fill_source, stop_limit_qty, stop_overrun_ticks = stop_limit_fill_price(
            st,
            st.position,
            exit_price,
            float(args.stop_limit_emergency_ticks),
        )
        if fill_price is None and fill_source != "stop_limit_waiting":
            st.last_reason = (
                f"{fill_source} stop={st.position.stop_price:g} "
                f"trigger={exit_price:g} book_qty={stop_limit_qty} "
                f"overrun={stop_overrun_ticks:.1f}t"
            )
    if fill_price is None:
        return
    if not st.shadow_closed.get("actual"):
        close_shadow(
            Path(args.shadow_log),
            st,
            "actual",
            st.position,
            fill_price,
            fill_source,
            actual_trigger_price,
            actual_trigger_source,
            stop_limit_qty,
            stop_overrun_ticks,
        )
    ticks, gross, net = pnl_rub(st.position, fill_price, st.spec, st.side_fee)
    write_entry_audit_close(Path(getattr(args, "entry_audit_log", "")), st, fill_price, fill_source, ticks, net)
    st.closed += 1
    st.closed_net += net
    portfolio.closed_net += net
    closed_at = now_str()
    append_schema_stable_csv(
        Path(args.log),
        {
            "closed_at": closed_at,
            "opened_at": st.position.opened_at,
            "contour": st.contour,
            "secid": st.spec.secid,
            "direction": st.position.direction,
            "qty": st.position.qty,
            "entry_price": st.position.entry_price,
            "exit_price": fill_price,
            "exit_source": fill_source,
            "trigger_price": actual_trigger_price,
            "trigger_source": actual_trigger_source,
            "stop_limit_qty": stop_limit_qty,
            "stop_overrun_ticks": round(stop_overrun_ticks, 3) if stop_overrun_ticks is not None else None,
            "ticks": round(ticks, 3),
            "gross_rub": round(gross, 2),
            "fees_rub": round(2 * st.side_fee * st.position.qty, 2),
            "net_rub": round(net, 2),
            "closed_net_rub": round(st.closed_net, 2),
        },
    )
    risk = getattr(args, "risk_governor", None)
    if risk is not None:
        risk.record_trade(st, net, gross, 2 * st.side_fee * st.position.qty, closed_at)
    print(
        f"{now_str()} CLOSE {st.contour} {st.spec.secid} exit={fill_price:g} "
        f"source={fill_source} trigger={actual_trigger_price:g}/{actual_trigger_source} "
        f"ticks={ticks:.1f} net={net:.2f} total={st.closed_net:.2f}"
    )
    apply_br_loss_pause(st, states, net)
    st.position = None
    if all_shadow_models_closed(st):
        st.shadow_positions = {}
        st.shadow_closed = {}
    st.cooldown_until = max(st.cooldown_until, time.monotonic() + 90)
    write_open_positions(Path(args.open_positions_log), states)


def gpt_shadow_layer_names(args: argparse.Namespace) -> list[str]:
    raw = str(getattr(args, "gpt_shadow_layers", "full,relaxed,loose") or "")
    names: list[str] = []
    for item in raw.replace(";", ",").split(","):
        name = item.strip().lower()
        if not name:
            continue
        if name == "strict":
            name = "full"
        if name in GPT_SHADOW_LAYER_CONFIGS and name not in names:
            names.append(name)
    return names or ["full"]


def gpt_shadow_layer_from_model(model: str) -> str:
    name = (model or "").strip().lower()
    if name in {"", "gpt_profile_shadow"}:
        return "full"
    if name.startswith("gpt_"):
        name = name[4:]
    if name in {"strict", "profile_shadow"}:
        return "full"
    return name if name in GPT_SHADOW_LAYER_CONFIGS else "full"


def gpt_shadow_model_name(layer_name: str) -> str:
    layer_name = gpt_shadow_layer_from_model(layer_name)
    return f"gpt_{layer_name}"


def gpt_shadow_runtime(st: State, layer_name: str) -> GptShadowRuntime:
    layer_name = gpt_shadow_layer_from_model(layer_name)
    if layer_name not in st.gpt_shadow_layers:
        st.gpt_shadow_layers[layer_name] = GptShadowRuntime()
    return st.gpt_shadow_layers[layer_name]


def gpt_shadow_used_margin(states: list[State], layer_name: str) -> float:
    total = 0.0
    for st in states:
        rt = gpt_shadow_runtime(st, layer_name)
        if rt.position is not None:
            total += position_margin(st.spec, rt.position.direction, rt.position.qty)
    return total


def gpt_shadow_sizing(
    args: argparse.Namespace,
    states: list[State],
    spec: Spec,
    profile: Profile,
    exit_ticks: GptExitTicks,
    direction: Direction | str,
    side_fee: float,
    max_full_stop_rub: float,
    layer_name: str,
) -> SizingDecision:
    initial_capital = float(getattr(args, "paper_capital", 200_000.0))
    equity = initial_capital + sum(gpt_shadow_runtime(st, layer_name).net for st in states)
    per_contract = spec.margin_buy if direction == "long" else spec.margin_sell
    if per_contract <= 0:
        per_contract = max(spec.margin_buy, spec.margin_sell, 0.0)
    if per_contract <= 0:
        margin_qty = 1
    else:
        max_total_margin = equity * float(getattr(args, "max_total_margin_pct", 0.80))
        free_margin = max(0.0, max_total_margin - gpt_shadow_used_margin(states, layer_name))
        per_position_limit = equity * float(getattr(args, "max_position_margin_pct", 0.20))
        budget = min(free_margin, per_position_limit)
        margin_qty = max(0, int(budget // per_contract))

    gross_stop_per_contract = max(0.0, exit_ticks.stop_ticks * spec.step_price)
    round_turn_fee_per_contract = max(0.0, 2 * side_fee)
    full_stop_per_contract = gross_stop_per_contract + round_turn_fee_per_contract
    risk_qty = None
    if max_full_stop_rub > 0 and full_stop_per_contract > 0:
        risk_qty = int(max_full_stop_rub // full_stop_per_contract)
    qty = margin_qty if risk_qty is None else min(margin_qty, risk_qty)
    qty = max(0, qty)
    full_stop_rub = qty * full_stop_per_contract
    reason = (
        f"gpt_shadow_sizing layer={layer_name} exit={exit_ticks.exit_mode} "
        f"stop={exit_ticks.stop_ticks}t trail={exit_ticks.trail_ticks}t arm={exit_ticks.trail_arm_ticks}t "
        f"margin_qty={margin_qty} risk_qty={risk_qty if risk_qty is not None else '-'} "
        f"full_stop_1lot={full_stop_per_contract:.2f} full_stop={full_stop_rub:.2f} "
        f"max_full_stop={max_full_stop_rub:.0f}"
    )
    return SizingDecision(
        qty=qty,
        margin_qty=margin_qty,
        risk_qty=risk_qty,
        gross_stop_per_contract_rub=gross_stop_per_contract,
        round_turn_fee_per_contract_rub=round_turn_fee_per_contract,
        full_stop_per_contract_rub=full_stop_per_contract,
        full_stop_rub=full_stop_rub,
        reason=reason,
    )


def append_gpt_shadow_event(path: Path, st: State, layer_name: str, rt: GptShadowRuntime, event_type: str, row: dict) -> None:
    if not str(path):
        return
    layer_name = gpt_shadow_layer_from_model(layer_name)
    exit_ticks = gpt_shadow_exit_ticks(st.profile, st.spec, st.last_price)
    active_stop_ticks = rt.active_stop_ticks or exit_ticks.stop_ticks
    active_trail_ticks = rt.active_trail_ticks or exit_ticks.trail_ticks
    active_trail_arm_ticks = rt.active_trail_arm_ticks or exit_ticks.trail_arm_ticks
    base = {
        "event_time": now_str(),
        "event_type": event_type,
        "model": gpt_shadow_model_name(layer_name),
        "contour": st.contour,
        "secid": st.spec.secid,
        "family": state_family(st),
        "signal_family": st.profile.signal_family,
        "entry_timing": st.profile.entry_timing,
        "session_filter": st.profile.session_filter,
        "direction": "",
        "qty": "",
        "entry_price": "",
        "exit_price": "",
        "exit_source": "",
        "trigger_price": "",
        "trigger_source": "",
        "stop_price": "",
        "best_price": "",
        "stop_ticks": active_stop_ticks,
        "trail_ticks": active_trail_ticks,
        "trail_arm_ticks": active_trail_arm_ticks,
        "exit_mode": rt.active_exit_mode or exit_ticks.exit_mode,
        "source_stage2_id": st.profile.gpt_source_stage2_id,
        "match_quality": st.profile.gpt_match_quality,
        "max_hold_minutes": st.profile.max_hold_minutes,
        "ticks": "",
        "gross_rub": "",
        "fees_rub": "",
        "net_rub": "",
        "closed_net_rub": round(rt.net, 2),
        "full_stop_risk_rub": "",
        "risk_mode": rt.risk_mode,
        "risk_reason": rt.risk_reason,
        "sizing_reason": rt.entry_sizing_reason,
        "signal_reason": rt.entry_reason or rt.last_reason,
    }
    base.update(row)
    append_schema_stable_csv(path, {key: base.get(key) for key in GPT_SHADOW_COLUMNS})


def opened_minutes_ago(pos: Position) -> float:
    try:
        opened = datetime.strptime(str(pos.opened_at), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return 0.0
    return max(0.0, (datetime.now() - opened).total_seconds() / 60.0)


def close_gpt_shadow(
    st: State,
    args: argparse.Namespace,
    layer_name: str,
    fill_price: float,
    fill_source: str,
    trigger_price: float,
    trigger_source: str,
) -> None:
    layer_name = gpt_shadow_layer_from_model(layer_name)
    rt = gpt_shadow_runtime(st, layer_name)
    pos = rt.position
    if pos is None:
        return
    ticks, gross, net = pnl_rub(pos, fill_price, st.spec, st.side_fee)
    rt.closed += 1
    rt.net += net
    append_gpt_shadow_event(
        Path(args.gpt_shadow_log),
        st,
        layer_name,
        rt,
        "close",
        {
            "direction": pos.direction,
            "qty": pos.qty,
            "entry_price": pos.entry_price,
            "exit_price": fill_price,
            "exit_source": fill_source,
            "trigger_price": trigger_price,
            "trigger_source": trigger_source,
            "stop_price": pos.stop_price,
            "best_price": pos.best_price,
            "ticks": round(ticks, 3),
            "gross_rub": round(gross, 2),
            "fees_rub": round(2 * st.side_fee * pos.qty, 2),
            "net_rub": round(net, 2),
            "closed_net_rub": round(rt.net, 2),
            "stop_ticks": rt.active_stop_ticks or st.profile.stop_ticks,
            "trail_ticks": rt.active_trail_ticks or st.profile.trail_ticks,
            "trail_arm_ticks": rt.active_trail_arm_ticks or st.profile.trail_arm_ticks,
            "exit_mode": rt.active_exit_mode or st.profile.gpt_exit_mode or "profile_ticks",
            "full_stop_risk_rub": round(
                full_stop_risk_rub_ticks(rt.active_stop_ticks or st.profile.stop_ticks, st.spec, st.side_fee, pos.qty),
                2,
            ),
        },
    )
    print(
        f"{now_str()} GPT_SHADOW_CLOSE layer={layer_name} {st.spec.secid} {pos.direction} "
        f"exit={fill_price:g} source={fill_source} ticks={ticks:.1f} "
        f"net={net:.2f} total={rt.net:.2f}",
        flush=True,
    )
    rt.position = None
    rt.active_stop_ticks = 0
    rt.active_trail_ticks = 0
    rt.active_trail_arm_ticks = 0
    rt.active_exit_mode = ""
    cooldown = max(0, int(st.profile.cooldown_minutes or 0)) * 60
    rt.cooldown_until = max(rt.cooldown_until, time.monotonic() + max(60, cooldown))


def process_gpt_shadow(
    st: State,
    args: argparse.Namespace,
    portfolio: Portfolio,
    states: list[State],
    trading_enabled: bool,
    force_close_due: bool,
    candle_closed: bool,
) -> None:
    if not bool(getattr(args, "enable_gpt_shadow", False)):
        return
    if st.contour != str(getattr(args, "gpt_shadow_contour", "strict")):
        return

    for layer_name in gpt_shadow_layer_names(args):
        layer = GPT_SHADOW_LAYER_CONFIGS[layer_name]
        rt = gpt_shadow_runtime(st, layer_name)
        pos = rt.position
        if pos is not None:
            active_exit_ticks = GptExitTicks(
                stop_ticks=rt.active_stop_ticks or st.profile.stop_ticks,
                trail_ticks=rt.active_trail_ticks or st.profile.trail_ticks,
                trail_arm_ticks=rt.active_trail_arm_ticks or st.profile.trail_arm_ticks,
                exit_mode=rt.active_exit_mode or st.profile.gpt_exit_mode or "profile_ticks",
            )
            exit_price, exit_source = executable_price(st, pos.direction, "exit")
            if exit_price is None:
                rt.last_reason = f"gpt_shadow {layer_name} book_filter no_executable_exit"
                continue
            force_reason = None
            max_hold = int(st.profile.max_hold_minutes or 0)
            if force_close_due:
                force_reason = "gpt_scheduled_force_close"
            elif max_hold > 0 and opened_minutes_ago(pos) >= max_hold:
                force_reason = "gpt_max_hold_exit"

            fee_t = fee_ticks(st.side_fee, st.spec)
            if force_reason:
                close_gpt_shadow(st, args, layer_name, round_to_step(exit_price, st.spec.min_step), force_reason, exit_price, exit_source)
                continue

            move_ticks = (
                (exit_price - pos.entry_price) / st.spec.min_step
                if pos.direction == "long"
                else (pos.entry_price - exit_price) / st.spec.min_step
            )
            if move_ticks >= max(active_exit_ticks.trail_arm_ticks, fee_t + 1):
                update_stop(pos, exit_price, active_exit_ticks.trail_ticks, st.spec)
                min_net_stop = (
                    pos.entry_price + (fee_t + 0.5) * st.spec.min_step
                    if pos.direction == "long"
                    else pos.entry_price - (fee_t + 0.5) * st.spec.min_step
                )
                if pos.direction == "long":
                    pos.stop_price = max(pos.stop_price, round_to_step(min_net_stop, st.spec.min_step))
                else:
                    pos.stop_price = min(pos.stop_price, round_to_step(min_net_stop, st.spec.min_step))

            fill_price, fill_source, _stop_qty, _overrun = stop_limit_fill_price(
                st,
                pos,
                exit_price,
                float(args.stop_limit_emergency_ticks),
            )
            if fill_price is not None:
                close_gpt_shadow(st, args, layer_name, fill_price, fill_source, exit_price, exit_source)
            continue

        if not trading_enabled or force_close_due:
            rt.last_reason = f"gpt_shadow {layer_name} time_gate"
            continue
        if not candle_closed:
            continue
        if rt.attempts >= st.profile.max_attempts:
            rt.last_reason = f"gpt_shadow {layer_name} attempt_filter max_attempts_reached"
            continue
        if time.monotonic() < rt.cooldown_until:
            rt.last_reason = f"gpt_shadow {layer_name} cooldown_filter wait_after_close"
            continue
        if rt.last_entry_candle_count == len(st.candles):
            rt.last_reason = f"gpt_shadow {layer_name} candle_filter wait_new_candle"
            continue
        expiry_reason = expiry_new_entry_block_reason(st.spec, float(args.no_new_expiry_days))
        if expiry_reason:
            rt.last_reason = f"gpt_shadow {layer_name} {expiry_reason}"
            continue

        direction, reason = gpt_profile_signal(st, layer)
        rt.last_reason = reason
        if direction is None:
            continue
        entry_price, entry_source = executable_price(st, direction, "entry")
        if entry_price is None:
            rt.last_reason = f"gpt_shadow {layer_name} book_filter no_executable_entry"
            continue
        if (st.profile.entry_timing or "").lower() == "adverse_1tick":
            entry_price += st.spec.min_step if direction == "long" else -st.spec.min_step
            entry_price = round_to_step(entry_price, st.spec.min_step)

        risk_decision = args.risk_governor.decide(st, float(args.max_full_stop_rub))
        rt.risk_mode = risk_decision.mode
        rt.risk_reason = risk_decision.reason
        if not risk_decision.allowed:
            rt.last_reason = f"gpt_shadow {layer_name} risk_governor {risk_decision.mode} {risk_decision.reason}"
            continue
        exit_ticks = gpt_shadow_exit_ticks(st.profile, st.spec, entry_price)
        sizing = gpt_shadow_sizing(
            args,
            states,
            st.spec,
            st.profile,
            exit_ticks,
            direction,
            st.side_fee,
            risk_decision.max_full_stop_rub,
            layer_name,
        )
        if sizing.qty < 1:
            rt.last_reason = f"gpt_shadow {layer_name} risk_filter full_stop_gt_limit {sizing.reason}"
            continue

        rt.attempts += 1
        rt.entry_reason = reason
        rt.entry_sizing_reason = sizing.reason
        rt.active_stop_ticks = exit_ticks.stop_ticks
        rt.active_trail_ticks = exit_ticks.trail_ticks
        rt.active_trail_arm_ticks = exit_ticks.trail_arm_ticks
        rt.active_exit_mode = exit_ticks.exit_mode
        rt.position = open_position(direction, entry_price, sizing.qty, exit_ticks.stop_ticks, exit_ticks.trail_ticks, st.spec)
        rt.last_entry_candle_count = len(st.candles)
        append_gpt_shadow_event(
            Path(args.gpt_shadow_log),
            st,
            layer_name,
            rt,
            "open",
            {
                "event_time": rt.position.opened_at,
                "direction": direction,
                "qty": sizing.qty,
                "entry_price": entry_price,
                "exit_source": entry_source,
                "stop_price": rt.position.stop_price,
                "best_price": rt.position.best_price,
                "stop_ticks": exit_ticks.stop_ticks,
                "trail_ticks": exit_ticks.trail_ticks,
                "trail_arm_ticks": exit_ticks.trail_arm_ticks,
                "exit_mode": exit_ticks.exit_mode,
                "full_stop_risk_rub": round(sizing.full_stop_rub, 2),
                "risk_mode": risk_decision.mode,
                "risk_reason": risk_decision.reason,
                "sizing_reason": sizing.reason,
                "signal_reason": reason,
            },
        )
        print(
            f"{now_str()} GPT_SHADOW_OPEN layer={layer_name} {st.spec.secid} {direction} qty={sizing.qty} "
            f"entry={entry_price:g} source={entry_source} stop={rt.position.stop_price:g} "
            f"exit={exit_ticks.exit_mode} {exit_ticks.stop_ticks}/{exit_ticks.trail_ticks}/{exit_ticks.trail_arm_ticks}t "
            f"risk={risk_decision.mode}/{risk_decision.max_full_stop_rub:.0f} {reason}",
            flush=True,
        )


def poll_market_fallback(
    token: str,
    specs: list[Spec],
    state_by_uid: dict[tuple[str, str], State],
    states: list[State],
    args: argparse.Namespace,
    portfolio: Portfolio,
    last_stream_event: list[float],
    started: float,
    runtime_state: dict[str, object],
    stop_event: threading.Event,
    lock: threading.RLock,
) -> None:
    from t_tech.invest import Client

    while not stop_event.is_set():
        try:
            with Client(token) as poll_client:
                while not stop_event.is_set():
                    time.sleep(max(0.5, float(args.fallback_poll_sec)))
                    if time.monotonic() - last_stream_event[0] < float(args.stream_stale_sec):
                        continue
                    try:
                        prices = poll_client.market_data.get_last_prices(figi=[s.figi for s in specs]).last_prices
                        price_by_uid = {p.instrument_uid: quotation_to_float(p.price) for p in prices}
                    except Exception as exc:
                        print(f"{now_str()} POLL last_price_error={exc}", flush=True)
                        price_by_uid = {}
                    with lock:
                        for spec in specs:
                            try:
                                orderbook = poll_client.market_data.get_order_book(figi=spec.figi, depth=int(args.orderbook_depth))
                            except Exception as exc:
                                orderbook = None
                                print(f"{now_str()} POLL orderbook_error {spec.secid} {exc}", flush=True)
                            for contour in ["strict", "aggressive"]:
                                st = state_by_uid.get((spec.uid, contour))
                                if st is None:
                                    continue
                                if spec.uid in price_by_uid:
                                    st.last_price = round_to_step(price_by_uid[spec.uid], spec.min_step)
                                if orderbook is not None:
                                    st.last_order_book = orderbook
                                trading_enabled = daily_trading_enabled(
                                    parse_clock_time(getattr(args, "no_trade_before", "")),
                                    parse_clock_time(getattr(args, "no_new_after", "")),
                                )
                                force_due = daily_force_close_due(parse_clock_time(getattr(args, "force_close_at", "")))
                                process_gpt_shadow(
                                    st,
                                    args,
                                    portfolio,
                                    states,
                                    trading_enabled=trading_enabled,
                                    force_close_due=force_due,
                                    candle_closed=False,
                                )
                                force_reason = "scheduled_force_close" if st.position is not None and force_due else None
                                process_open_state_exit(
                                    st,
                                    args,
                                    portfolio,
                                    states,
                                    candle_closed=False,
                                    actual_trigger_source_override="polling_fallback",
                                    force_exit_reason=force_reason,
                                )
                        write_open_positions(Path(args.open_positions_log), states)
                        write_microstructure_snapshot(
                            Path(args.snapshot_log),
                            states,
                            trading_enabled=True,
                            risk=getattr(args, "risk_governor", None),
                            base_max_full_stop_rub=float(args.max_full_stop_rub),
                        )
                        write_bot_health(
                            Path(args.health_log),
                            states,
                            portfolio,
                            started,
                            last_stream_event,
                            int(runtime_state.get("reconnect_count", 0)),
                            str(runtime_state.get("last_stream_error", "")),
                            "polling_fallback",
                        )
                    print(f"{now_str()} POLL fallback active stale_sec={time.monotonic() - last_stream_event[0]:.1f}", flush=True)
        except Exception as exc:
            if not stop_event.is_set():
                print(f"{now_str()} POLL reconnect_after_error {type(exc).__name__}: {exc}", flush=True)
                time.sleep(3)


def write_microstructure_snapshot(
    path: Path,
    states: list[State],
    trading_enabled: bool,
    risk: RiskGovernor | None = None,
    base_max_full_stop_rub: float = DEFAULT_MAX_FULL_STOP_RUB,
) -> None:
    rows = []
    spread_review_rows = []
    snapshot_time = now_str()
    for st in states:
        if st.contour != "aggressive":
            continue
        levels = best_levels(st.last_order_book, st.spec)
        spread_ratio, spread_class, spread_review = spread_to_stop_metrics(
            levels["spread_ticks"],
            st.profile.stop_ticks,
        )
        fee_to_stop = fee_ticks(st.side_fee, st.spec) / st.profile.stop_ticks if st.profile.stop_ticks > 0 else None
        risk_decision = risk.decide(st, base_max_full_stop_rub) if risk is not None else None
        if risk_decision is not None:
            st.risk_mode = risk_decision.mode
            st.risk_limit_rub = risk_decision.max_full_stop_rub
            st.risk_reason = risk_decision.reason
        rows.append(
            {
                "snapshot_time": snapshot_time,
                "target_contract": st.spec.secid,
                "last_price_target": st.last_price,
                "bid_target": levels["bid"],
                "ask_target": levels["ask"],
                "bid_size_target": levels["bid_size"],
                "ask_size_target": levels["ask_size"],
                "spread_ticks_target": levels["spread_ticks"],
                "stop_ticks": st.profile.stop_ticks,
                "trail_ticks": st.profile.trail_ticks,
                "trail_arm_ticks": st.profile.trail_arm_ticks,
                "spread_to_stop_ratio": round(spread_ratio, 4) if spread_ratio is not None else None,
                "spread_class": spread_class,
                "spread_review_flag": spread_review,
                "fee_to_stop_ratio": round(fee_to_stop, 4) if fee_to_stop is not None else None,
                "signal_status": "listening" if trading_enabled else "warmup_or_no_new_entries",
                "skip_reason": "" if trading_enabled else "time_gate",
                "orderbook_source": "tbank_stream",
                "execution_validation_possible": levels["bid"] is not None and levels["ask"] is not None,
                "session_phase": "stream",
                "can_open_new_paper_trade": trading_enabled,
                "last_reason": st.last_reason,
                "risk_mode": risk_decision.mode if risk_decision is not None else st.risk_mode,
                "risk_limit_rub": round(risk_decision.max_full_stop_rub, 2) if risk_decision is not None else round(st.risk_limit_rub, 2),
                "risk_reason": risk_decision.reason if risk_decision is not None else st.risk_reason,
                "family_day_net": round(risk_decision.family_day_net, 2) if risk_decision is not None else None,
                "family_day_high": round(risk_decision.family_day_high, 2) if risk_decision is not None else None,
                "stop_to_median_win": round(risk_decision.stop_to_median_win, 2) if risk_decision is not None and risk_decision.stop_to_median_win is not None else None,
                "median_win_rub": round(risk_decision.median_win_rub, 2) if risk_decision is not None and risk_decision.median_win_rub is not None else None,
                "median_stop_cap_rub": round(risk_decision.median_stop_cap_rub, 2) if risk_decision is not None and risk_decision.median_stop_cap_rub is not None else None,
                "median_win_source": risk_decision.median_win_source if risk_decision is not None else "",
            }
        )
        if spread_review:
            spread_review_rows.append(
                {
                    "snapshot_time": snapshot_time,
                    "ticker": st.spec.secid,
                    "family": state_family(st),
                    "last_price": st.last_price,
                    "bid": levels["bid"],
                    "ask": levels["ask"],
                    "bid_size": levels["bid_size"],
                    "ask_size": levels["ask_size"],
                    "spread_ticks": levels["spread_ticks"],
                    "stop_ticks": st.profile.stop_ticks,
                    "spread_to_stop_ratio": round(spread_ratio, 4) if spread_ratio is not None else None,
                    "spread_class": spread_class,
                    "last_reason": st.last_reason,
                    "risk_mode": risk_decision.mode if risk_decision is not None else st.risk_mode,
                    "risk_reason": risk_decision.reason if risk_decision is not None else st.risk_reason,
                }
            )
    for row in rows:
        append_schema_stable_csv(path, row)
    if spread_review_rows:
        review_name = path.name.replace("live_orderbook_snapshots", "wide_spread_review")
        if review_name == path.name:
            review_name = f"{path.stem}_wide_spread_review{path.suffix}"
        review_path = path.with_name(review_name)
        for row in spread_review_rows:
            append_schema_stable_csv(review_path, row)


def write_instrument_specs(path: Path, specs: list[Spec]) -> None:
    rows = []
    for spec in specs:
        dte = days_to_expiration(spec)
        rows.append(
            {
                "ticker": spec.secid,
                "figi": spec.figi,
                "uid": spec.uid,
                "expiration": spec.expiration_date.isoformat() if spec.expiration_date else "",
                "days_to_expiration": round(dte, 3) if dte is not None else "",
                "tick": spec.min_step,
                "tick_rub": spec.step_price,
                "last_price": round(spec.last_price, 6),
                "last_rub": round(spec.last_rub, 6),
                "go_buy": round(spec.margin_buy, 2),
                "go_sell": round(spec.margin_sell, 2),
                "source": "tbank_api",
                "updated_at": now_str(),
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["ticker"])
        writer.writeheader()
        writer.writerows(rows)


def write_startup_status(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "timestamp",
        "ticker",
        "status",
        "reason",
        "load_reason",
        "profile_source",
        "family",
        "expiration",
        "days_to_expiration",
        "tick",
        "tick_rub",
        "last_price",
        "go_buy",
        "go_sell",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_roll_state(path: Path, roll_events: list[dict], specs: list[Spec], args: argparse.Namespace) -> None:
    payload = {
        "updated_at": now_str(),
        "auto_roll_enabled": not bool(getattr(args, "disable_auto_roll", False)),
        "roll_observe_days": float(getattr(args, "roll_observe_days", 0.0)),
        "no_new_expiry_days": float(getattr(args, "no_new_expiry_days", 0.0)),
        "expiry_force_close_days": float(getattr(args, "expiry_force_close_days", 0.0)),
        "loaded_contracts": [
            {
                "ticker": spec.secid,
                "family": contract_family(spec.secid),
                "expiration": spec.expiration_date.isoformat() if spec.expiration_date else "",
                "days_to_expiration": round(days_to_expiration(spec), 3) if days_to_expiration(spec) is not None else None,
            }
            for spec in specs
        ],
        "roll_events": roll_events,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_open_positions(path: Path, states: list[State]) -> None:
    rows = []
    for st in states:
        if st.position is None:
            continue
        mark_price, mark_source = executable_price(st, st.position.direction, "exit")
        rows.append(
            {
                "contour": st.contour,
                "ticker": st.spec.secid,
                "direction": st.position.direction,
                "qty": st.position.qty,
                "entry_price": st.position.entry_price,
                "best_price": st.position.best_price,
                "last_price": st.last_price,
                "mark_price": mark_price,
                "mark_source": mark_source,
                "stop_price": st.position.stop_price,
                "full_stop_risk_rub": round(full_stop_risk_rub(st.profile, st.spec, st.side_fee, st.position.qty), 2),
                "full_stop_gross_rub": round(st.profile.stop_ticks * st.spec.step_price * st.position.qty, 2),
                "margin_rub": round(position_margin(st.spec, st.position.direction, st.position.qty), 2),
                "risk_mode": st.risk_mode,
                "risk_limit_rub": round(st.risk_limit_rub, 2),
                "risk_reason": st.risk_reason,
                "opened_at": st.position.opened_at,
            }
        )
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def restore_open_positions(path: Path, states: list[State]) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"{now_str()} RESTORE open_positions_read_error={exc}", flush=True)
        return 0
    if not isinstance(rows, list):
        return 0
    by_key = {(st.contour, st.spec.secid): st for st in states}
    restored = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("contour") or ""), str(row.get("ticker") or ""))
        st = by_key.get(key)
        if st is None or st.position is not None:
            continue
        try:
            direction = str(row["direction"])
            entry = float(row["entry_price"])
            qty = int(float(row["qty"]))
            stop = float(row["stop_price"])
            best_raw = row.get("best_price")
            best = float(best_raw) if best_raw not in (None, "") else entry
            opened_at = str(row.get("opened_at") or now_str())
        except Exception as exc:
            print(f"{now_str()} RESTORE skip_bad_position {key} error={exc}", flush=True)
            continue
        st.position = Position(
            direction=direction,
            entry_price=entry,
            qty=qty,
            best_price=best,
            stop_price=stop,
            opened_at=opened_at,
        )
        st.attempts = max(st.attempts, 1)
        st.last_reason = "restored_open_position"
        restored += 1
    if restored:
        print(f"{now_str()} RESTORE open_positions count={restored} source={path}", flush=True)
    return restored


def restore_closed_totals(path: Path, states: list[State], portfolio: Portfolio) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    by_key = {(st.contour, st.spec.secid): st for st in states}
    restored = 0
    restored_net = 0.0
    try:
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                contour = str(row.get("contour") or "")
                secid = str(row.get("secid") or row.get("ticker") or "")
                st = by_key.get((contour, secid))
                if st is None:
                    continue
                try:
                    net = float(row.get("net_rub") or 0.0)
                except Exception:
                    net = 0.0
                st.closed += 1
                st.closed_net += net
                st.attempts += 1
                portfolio.closed_net += net
                restored += 1
                restored_net += net
    except Exception as exc:
        print(f"{now_str()} RESTORE closed_trades_read_error={exc}", flush=True)
        return 0
    if restored:
        print(f"{now_str()} RESTORE closed_trades count={restored} net={restored_net:.2f} source={path}", flush=True)
    return restored


def restore_gpt_shadow_from_log(path: Path, states: list[State]) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    by_key = {(st.contour, st.spec.secid): st for st in states}
    open_rows: dict[tuple[str, str, str], dict] = {}
    restored_net: dict[tuple[str, str, str], float] = {}
    restored_closed: dict[tuple[str, str, str], int] = {}
    restored_attempts: dict[tuple[str, str, str], int] = {}
    try:
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                state_key = (str(row.get("contour") or ""), str(row.get("secid") or ""))
                if state_key not in by_key:
                    continue
                layer_name = gpt_shadow_layer_from_model(str(row.get("model") or ""))
                key = (state_key[0], state_key[1], layer_name)
                event = str(row.get("event_type") or "").lower()
                if event == "open":
                    open_rows[key] = row
                    restored_attempts[key] = restored_attempts.get(key, 0) + 1
                elif event == "close":
                    open_rows.pop(key, None)
                    restored_closed[key] = restored_closed.get(key, 0) + 1
                    try:
                        restored_net[key] = restored_net.get(key, 0.0) + float(row.get("net_rub") or 0.0)
                    except Exception:
                        pass
    except Exception as exc:
        print(f"{now_str()} GPT_SHADOW restore_error={exc}", flush=True)
        return 0

    restored_open = 0
    for key, row in open_rows.items():
        state_key = (key[0], key[1])
        layer_name = key[2]
        st = by_key.get(state_key)
        if st is None:
            continue
        rt = gpt_shadow_runtime(st, layer_name)
        if rt.position is not None:
            continue
        rt.net += restored_net.get(key, 0.0)
        rt.closed += restored_closed.get(key, 0)
        rt.attempts += restored_attempts.get(key, 0)
        try:
            direction = str(row["direction"])
            entry = float(row["entry_price"])
            qty = int(float(row["qty"]))
            stop = float(row["stop_price"])
            best_raw = row.get("best_price")
            best = float(best_raw) if best_raw not in (None, "") else entry
            opened_at = str(row.get("event_time") or now_str())
            stop_ticks = int(float(row.get("stop_ticks") or 0))
            trail_ticks = int(float(row.get("trail_ticks") or 0))
            trail_arm_ticks = int(float(row.get("trail_arm_ticks") or 0))
        except Exception as exc:
            print(f"{now_str()} GPT_SHADOW restore_skip_bad {key} error={exc}", flush=True)
            continue
        rt.position = Position(
            direction=direction,
            entry_price=entry,
            qty=qty,
            best_price=best,
            stop_price=stop,
            opened_at=opened_at,
        )
        rt.entry_reason = str(row.get("signal_reason") or "")
        rt.entry_sizing_reason = str(row.get("sizing_reason") or "")
        rt.risk_mode = str(row.get("risk_mode") or "")
        rt.risk_reason = str(row.get("risk_reason") or "")
        rt.active_stop_ticks = stop_ticks or st.profile.stop_ticks
        rt.active_trail_ticks = trail_ticks or st.profile.trail_ticks
        rt.active_trail_arm_ticks = trail_arm_ticks or st.profile.trail_arm_ticks
        rt.active_exit_mode = str(row.get("exit_mode") or st.profile.gpt_exit_mode or "profile_ticks")
        rt.last_reason = "gpt_shadow_restored_open_position"
        restored_open += 1

    for key, net in restored_net.items():
        if key in open_rows:
            continue
        st = by_key.get((key[0], key[1]))
        if st is None:
            continue
        rt = gpt_shadow_runtime(st, key[2])
        rt.net += net
        rt.closed += restored_closed.get(key, 0)
        rt.attempts += restored_attempts.get(key, 0)
    if restored_open or restored_closed:
        print(
            f"{now_str()} GPT_SHADOW restore open={restored_open} "
            f"closed={sum(restored_closed.values())} net={sum(restored_net.values()):.2f} source={path}",
            flush=True,
        )
    return restored_open


def write_bot_health(
    path: Path,
    states: list[State],
    portfolio: Portfolio,
    started: float,
    last_stream_event: list[float],
    reconnect_count: int,
    last_stream_error: str,
    status: str,
) -> None:
    now = time.monotonic()
    payload = {
        "timestamp": now_str(),
        "pid": os.getpid(),
        "status": status,
        "uptime_sec": round(now - started, 1),
        "last_stream_age_sec": round(now - last_stream_event[0], 1),
        "reconnect_count": reconnect_count,
        "last_stream_error": last_stream_error,
        "open_positions": sum(1 for st in states if st.position is not None),
        "closed_trades": sum(st.closed for st in states),
        "closed_net": round(portfolio.closed_net, 2),
        "used_margin": round(used_margin(states), 2),
        "contours": {
            contour: {
                "open_positions": sum(1 for st in states if st.contour == contour and st.position is not None),
                "attempts": sum(st.attempts for st in states if st.contour == contour),
                "closed": sum(st.closed for st in states if st.contour == contour),
                "closed_net": round(sum(st.closed_net for st in states if st.contour == contour), 2),
            }
            for contour in ["strict", "aggressive"]
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def append_schema_stable_csv(path: Path, row: dict) -> None:
    expected = list(row)
    if path.exists() and path.stat().st_size > 0:
        try:
            with path.open(newline="", encoding="utf-8") as f:
                header = next(csv.reader(f), [])
        except Exception:
            header = []
        if header != expected:
            backup = path.with_name(f"{path.stem}_schema_backup_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
            path.replace(backup)
            print(f"{now_str()} SNAPSHOT schema_changed backup={backup}", flush=True)
    append_trade(path, row)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--secids", nargs="+", default=["NGK6", "NGM6", "BMM6", "VBM6", "GZM6", "S1M6"])
    parser.add_argument("--runtime-sec", type=int, default=3600)
    parser.add_argument("--report-sec", type=int, default=600)
    parser.add_argument("--seed-minutes", type=int, default=180)
    parser.add_argument("--orderbook-depth", type=int, default=10)
    parser.add_argument("--profiles", default=str(REPORTS / "futures_scalp_profiles.csv"))
    parser.add_argument("--log", default=str(REPORTS / "multi_futures_paper_trades.csv"))
    parser.add_argument("--snapshot-log", default=str(REPORTS / "live_orderbook_snapshots.csv"))
    parser.add_argument("--open-positions-log", default=str(REPORTS / "paper_open_positions.json"))
    parser.add_argument("--instrument-specs-log", default=str(REPORTS / "paper_instrument_specs.csv"))
    parser.add_argument("--startup-status-log", default=str(REPORTS / "paper_startup_status.csv"))
    parser.add_argument("--shadow-log", default=str(REPORTS / "paper_shadow_exit_models.csv"))
    parser.add_argument("--entry-audit-log", default=str(REPORTS / "paper_entry_audit.csv"))
    parser.add_argument("--enable-gpt-shadow", action="store_true")
    parser.add_argument("--gpt-shadow-contour", default="strict")
    parser.add_argument("--gpt-shadow-layers", default="full,relaxed,loose")
    parser.add_argument("--gpt-shadow-params", default=str(REPORTS / "futures_scalp_profiles_v7_paper_20260525_gpt_shadow_params.csv"))
    parser.add_argument("--gpt-shadow-log", default=str(REPORTS / "paper_gpt_shadow_trades.csv"))
    parser.add_argument("--health-log", default=str(REPORTS / "paper_bot_health.json"))
    parser.add_argument("--snapshot-sec", type=int, default=10)
    parser.add_argument("--no-trade-before", default="")
    parser.add_argument("--no-new-after", default="")
    parser.add_argument("--force-close-at", default="")
    parser.add_argument("--paper-capital", type=float, default=200_000.0)
    parser.add_argument("--max-total-margin-pct", type=float, default=0.80)
    parser.add_argument("--max-position-margin-pct", type=float, default=0.20)
    parser.add_argument("--max-full-stop-rub", type=float, default=DEFAULT_MAX_FULL_STOP_RUB)
    parser.add_argument("--risk-state-log", default=str(REPORTS / "paper_risk_policy_state.json"))
    parser.add_argument("--risk-reduced-full-stop-rub", type=float, default=2_000.0)
    parser.add_argument("--risk-micro-full-stop-rub", type=float, default=1_000.0)
    parser.add_argument("--risk-profit-guard-min-rub", type=float, default=3_000.0)
    parser.add_argument("--risk-profit-guard-drawdown-pct", type=float, default=0.35)
    parser.add_argument("--risk-profit-guard-drawdown-min-rub", type=float, default=1_500.0)
    parser.add_argument("--risk-stop-to-median-reduced", type=float, default=7.0)
    parser.add_argument("--risk-stop-to-median-micro", type=float, default=10.0)
    parser.add_argument("--risk-stop-to-median-cap", type=float, default=4.0)
    parser.add_argument("--risk-probation-trades", type=int, default=30)
    parser.add_argument("--stop-limit-emergency-ticks", type=float, default=2.0)
    parser.add_argument("--actual-exit-model", choices=["stream_stoplimit", "candle_like"], default="stream_stoplimit")
    parser.add_argument("--stream-stale-sec", type=float, default=15.0)
    parser.add_argument("--fallback-poll-sec", type=float, default=2.0)
    parser.add_argument("--no-new-expiry-days", type=float, default=5.0)
    parser.add_argument("--expiry-force-close-days", type=float, default=3.0)
    parser.add_argument("--roll-observe-days", type=float, default=10.0)
    parser.add_argument("--roll-state-log", default=str(REPORTS / "paper_roll_state.json"))
    parser.add_argument("--disable-auto-roll", action="store_true")
    args = parser.parse_args()

    from t_tech.invest import Client, InstrumentIdType

    all_profiles = load_profiles(Path(args.profiles))
    if args.enable_gpt_shadow:
        apply_gpt_shadow_params(all_profiles, Path(args.gpt_shadow_params))
    profiles = {secid: all_profiles[secid] for secid in args.secids if secid in all_profiles}
    portfolio = Portfolio(
        initial_capital=float(args.paper_capital),
        max_total_margin_pct=float(args.max_total_margin_pct),
        max_position_margin_pct=float(args.max_position_margin_pct),
    )
    token = find_tbank_token()
    states: list[State] = []
    specs: list[Spec] = []
    with Client(token) as client:
        roll_events: list[dict] = []
        loaded_secids: set[str] = set()
        processed_secids: set[str] = set()
        configured_secids = set(args.secids)
        queued_roll_profiles: dict[str, Profile] = {}
        startup_status: list[dict] = []

        def load_contract(secid: str, profile: Profile, load_reason: str) -> Spec | None:
            if secid in loaded_secids:
                return None
            try:
                future = tbank_find_future(client, secid)
                info = client.instruments.future_by(
                    id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
                    id=future.uid,
                ).instrument
            except Exception as exc:
                startup_status.append(
                    {
                        "timestamp": now_str(),
                        "ticker": secid,
                        "status": "skipped",
                        "reason": f"instrument_error={exc}",
                        "load_reason": load_reason,
                        "profile_source": profile.source_secid or profile.secid,
                        "family": profile.family or contract_family(secid),
                    }
                )
                print(f"{now_str()} SKIP {secid} instrument_error={exc}", flush=True)
                return None
            spec = spec_from_tbank(client, secid, future, info)
            side_fee = commission_side_rub(spec, 1, 0.00025, None)
            can_trade, fee_reason = profile_can_trade(profile, side_fee, spec)
            if not can_trade:
                startup_status.append(
                    {
                        "timestamp": now_str(),
                        "ticker": secid,
                        "status": "skipped",
                        "reason": fee_reason,
                        "load_reason": load_reason,
                        "profile_source": profile.source_secid or profile.secid,
                        "family": profile.family or contract_family(secid),
                        "expiration": spec.expiration_date.isoformat() if spec.expiration_date else "",
                        "days_to_expiration": round(days_to_expiration(spec), 3) if days_to_expiration(spec) is not None else "",
                        "tick": spec.min_step,
                        "tick_rub": spec.step_price,
                        "last_price": round(spec.last_price, 6),
                        "go_buy": round(spec.margin_buy, 2),
                        "go_sell": round(spec.margin_sell, 2),
                    }
                )
                print(f"{now_str()} SKIP {secid} {fee_reason} load_reason={load_reason}", flush=True)
                return None
            specs.append(spec)
            loaded_secids.add(secid)
            dte = days_to_expiration(spec)
            startup_status.append(
                {
                    "timestamp": now_str(),
                    "ticker": secid,
                    "status": "loaded",
                    "reason": fee_reason,
                    "load_reason": load_reason,
                    "profile_source": profile.source_secid or profile.secid,
                    "family": profile.family or contract_family(secid),
                    "expiration": spec.expiration_date.isoformat() if spec.expiration_date else "",
                    "days_to_expiration": round(dte, 3) if dte is not None else "",
                    "tick": spec.min_step,
                    "tick_rub": spec.step_price,
                    "last_price": round(spec.last_price, 6),
                    "go_buy": round(spec.margin_buy, 2),
                    "go_sell": round(spec.margin_sell, 2),
                }
            )
            dte_text = "" if dte is None else f" dte={dte:.1f}d"
            print(
                f"{now_str()} LOAD {secid} {fee_reason} load_reason={load_reason} "
                f"profile_source={profile.source_secid or profile.secid}{dte_text}",
                flush=True,
            )
            seeded = seed_candles(client, spec.figi, args.seed_minutes)
            for contour in ["strict", "aggressive"]:
                states.append(
                    State(
                        spec=spec,
                        profile=profile,
                        contour=contour,
                        side_fee=side_fee,
                        candles=deque(seeded, maxlen=180),
                        last_price=spec.last_price,
                    )
                )
            return spec

        for secid in args.secids:
            processed_secids.add(secid)
            if secid not in profiles:
                startup_status.append(
                    {
                        "timestamp": now_str(),
                        "ticker": secid,
                        "status": "skipped",
                        "reason": "no_profile",
                        "load_reason": "configured",
                        "profile_source": "",
                        "family": contract_family(secid),
                    }
                )
                print(f"{now_str()} SKIP {secid} no_profile", flush=True)
                continue
            profile = profiles[secid]
            spec = load_contract(secid, profile, "configured")
            if spec is None or args.disable_auto_roll:
                continue
            roll_secid, roll_profile, roll_event = select_roll_contract(client, spec, profile, all_profiles, args)
            if roll_secid and roll_profile:
                if roll_secid in loaded_secids:
                    roll_event["status"] = "selected_already_loaded"
                    roll_event["reason"] = f"{roll_event.get('reason', '')}; selected already loaded"
                elif roll_secid in configured_secids and roll_secid not in processed_secids:
                    roll_event["status"] = "selected_will_load_later"
                    roll_event["reason"] = f"{roll_event.get('reason', '')}; selected is configured later in this run"
                else:
                    queued_roll_profiles[roll_secid] = roll_profile
                    roll_event["status"] = "queued_roll_contract"
                    roll_event["reason"] = f"{roll_event.get('reason', '')}; queued for auto load"
            roll_events.append(roll_event)

        for secid, profile in queued_roll_profiles.items():
            load_contract(secid, profile, "auto_roll")

        write_startup_status(Path(args.startup_status_log), startup_status)
        if not specs:
            print(f"{now_str()} multi_paper no tradable instruments after startup filters")
            return
        write_instrument_specs(Path(args.instrument_specs_log), specs)
        write_roll_state(Path(args.roll_state_log), roll_events, specs, args)
        by_figi = {s.figi: s for s in specs}
        by_uid = {s.uid: s for s in specs}
        state_by_uid = {(s.spec.uid, s.contour): s for s in states}
        print(
            f"{now_str()} multi_paper start instruments={len(specs)} contours=2 "
            f"runtime_sec={args.runtime_sec} paper_capital={portfolio.initial_capital:.0f} "
            f"max_total_margin_pct={portfolio.max_total_margin_pct:.2f} "
            f"max_position_margin_pct={portfolio.max_position_margin_pct:.2f} "
            f"max_full_stop_rub={float(args.max_full_stop_rub):.0f} "
            f"no_new_after={args.no_new_after or '-'} force_close_at={args.force_close_at or '-'}"
        )
        started = time.monotonic()
        next_report = started + args.report_sec
        next_snapshot = started
        no_trade_before = parse_clock_time(args.no_trade_before)
        no_new_after = parse_clock_time(args.no_new_after)
        force_close_at = parse_clock_time(args.force_close_at)
        last_stream_event = [time.monotonic()]
        runtime_state: dict[str, object] = {"reconnect_count": 0, "last_stream_error": ""}
        stop_event = threading.Event()
        lock = threading.RLock()
        risk_governor = RiskGovernor(
            Path(args.risk_state_log),
            base_max_full_stop_rub=float(args.max_full_stop_rub),
            reduced_full_stop_rub=float(args.risk_reduced_full_stop_rub),
            micro_full_stop_rub=float(args.risk_micro_full_stop_rub),
            profit_guard_min_rub=float(args.risk_profit_guard_min_rub),
            profit_guard_drawdown_pct=float(args.risk_profit_guard_drawdown_pct),
            profit_guard_drawdown_min_rub=float(args.risk_profit_guard_drawdown_min_rub),
            stop_to_median_reduced=float(args.risk_stop_to_median_reduced),
            stop_to_median_micro=float(args.risk_stop_to_median_micro),
            stop_to_median_cap=float(args.risk_stop_to_median_cap),
            probation_trades=int(args.risk_probation_trades),
        )
        setattr(args, "risk_governor", risk_governor)
        restore_closed_totals(Path(args.log), states, portfolio)
        risk_governor.rebuild_from_trade_log(Path(args.log), states)
        restore_open_positions(Path(args.open_positions_log), states)
        if args.enable_gpt_shadow:
            restore_gpt_shadow_from_log(Path(args.gpt_shadow_log), states)
        write_open_positions(Path(args.open_positions_log), states)
        write_bot_health(
            Path(args.health_log),
            states,
            portfolio,
            started,
            last_stream_event,
            int(runtime_state["reconnect_count"]),
            str(runtime_state["last_stream_error"]),
            "starting",
        )
        poll_thread = threading.Thread(
            target=poll_market_fallback,
            args=(token, specs, state_by_uid, states, args, portfolio, last_stream_event, started, runtime_state, stop_event, lock),
            daemon=True,
        )
        poll_thread.start()

        def handle_stream_response(response) -> None:
            nonlocal next_report, next_snapshot
            now = time.monotonic()
            trading_enabled = daily_trading_enabled(no_trade_before, no_new_after)
            force_close_due = daily_force_close_due(force_close_at)
            uid = ""
            price = None
            candle = None
            orderbook = None
            if response.last_price is not None:
                uid = response.last_price.instrument_uid
                price = quotation_to_float(response.last_price.price)
            elif response.trade is not None:
                uid = response.trade.instrument_uid
                price = quotation_to_float(response.trade.price)
            elif response.orderbook is not None:
                uid = response.orderbook.instrument_uid
                orderbook = response.orderbook
            elif response.candle is not None:
                uid = response.candle.instrument_uid
                candle = response.candle
            if uid not in by_uid:
                return
            last_stream_event[0] = time.monotonic()
            spec = by_uid[uid]
            with lock:
                for contour in ["strict", "aggressive"]:
                    st = state_by_uid[(uid, contour)]
                    if price is not None:
                        st.last_price = round_to_step(price, spec.min_step)
                    if orderbook is not None:
                        st.last_order_book = orderbook
                    if candle is not None:
                        st.candles.append(
                            {
                                "open": quotation_to_float(candle.open),
                                "high": quotation_to_float(candle.high),
                                "low": quotation_to_float(candle.low),
                                "close": quotation_to_float(candle.close),
                                "volume": int(candle.volume),
                            }
                        )
                    process_gpt_shadow(
                        st,
                        args,
                        portfolio,
                        states,
                        trading_enabled=trading_enabled,
                        force_close_due=force_close_due,
                        candle_closed=candle is not None,
                    )
                    if st.position is None and has_active_shadow(st):
                        process_open_state_exit(st, args, portfolio, states, candle is not None)
                        if has_active_shadow(st):
                            continue
                    if st.position is None and trading_enabled and not force_close_due:
                        if st.attempts >= st.profile.max_attempts:
                            st.last_reason = "attempt_filter max_attempts_reached"
                            continue
                        if now < st.cooldown_until:
                            st.last_reason = "cooldown_filter wait_after_close"
                            continue
                        if st.last_entry_candle_count == len(st.candles):
                            st.last_reason = "candle_filter wait_new_candle"
                            continue
                        if has_open_ticker(states, spec.secid):
                            st.last_reason = "duplicate_filter ticker_already_open"
                            continue
                        family_conflict = has_roll_family_conflict(
                            states,
                            spec,
                            state_family(st),
                            float(args.roll_observe_days),
                        )
                        if family_conflict:
                            st.last_reason = f"roll_family_filter open_family_position {family_conflict}"
                            continue
                        expiry_reason = expiry_new_entry_block_reason(spec, float(args.no_new_expiry_days))
                        if expiry_reason:
                            st.last_reason = expiry_reason
                            continue
                        direction, reason = signal(st, contour == "aggressive")
                        st.last_reason = reason
                        if direction:
                            allowed_direction = (st.profile.allowed_direction or "both").lower()
                            if allowed_direction in {"long", "short"} and direction != allowed_direction:
                                st.last_reason = f"direction_filter profile={allowed_direction} signal={direction}"
                                continue
                            entry_price, entry_source = executable_price(st, direction, "entry")
                            if entry_price is None:
                                st.last_reason = "book_filter no_executable_entry"
                                continue
                            risk_decision = args.risk_governor.decide(st, float(args.max_full_stop_rub))
                            st.risk_mode = risk_decision.mode
                            st.risk_limit_rub = risk_decision.max_full_stop_rub
                            st.risk_reason = risk_decision.reason
                            if not risk_decision.allowed:
                                st.last_reason = f"risk_governor {risk_decision.mode} {risk_decision.reason}"
                                continue
                            sizing = paper_sizing(
                                portfolio,
                                states,
                                spec,
                                st.profile,
                                direction,
                                st.side_fee,
                                risk_decision.max_full_stop_rub,
                            )
                            qty = sizing.qty
                            if qty < 1:
                                if sizing.margin_qty < 1:
                                    st.last_reason = f"capital_filter no_free_margin {sizing.reason}"
                                else:
                                    st.last_reason = f"risk_filter full_stop_gt_limit {sizing.reason}"
                                continue
                            st.attempts += 1
                            st.position = open_position(direction, entry_price, qty, st.profile.stop_ticks, st.profile.trail_ticks, spec)
                            st.shadow_positions = {
                                "stream_stoplimit": clone_position(st.position),
                                "candle_like": clone_position(st.position),
                            }
                            st.shadow_closed = {}
                            st.last_entry_candle_count = len(st.candles)
                            write_entry_audit_open(
                                Path(getattr(args, "entry_audit_log", "")),
                                st,
                                direction,
                                entry_price,
                                qty,
                                sizing,
                                risk_decision,
                                reason,
                            )
                            print(
                                f"{now_str()} OPEN {contour} {spec.secid} {direction} qty={qty} "
                                f"entry={entry_price:g} source={entry_source} stop={st.position.stop_price:g} "
                                f"margin={position_margin(spec, direction, qty):.2f} "
                                f"full_stop_risk={sizing.full_stop_rub:.2f} "
                                f"risk={risk_decision.mode}/{risk_decision.max_full_stop_rub:.0f} "
                                f"{risk_decision.reason} {sizing.reason} {reason}",
                                flush=True,
                            )
                            write_open_positions(Path(args.open_positions_log), states)
                    elif st.position is not None:
                        trigger_override = round_to_step(float(st.candles[-1]["close"]), spec.min_step) if candle is not None else None
                        process_open_state_exit(
                            st,
                            args,
                            portfolio,
                            states,
                            candle is not None,
                            actual_trigger_override=trigger_override,
                            actual_trigger_source_override="closed_1m_candle" if candle is not None else None,
                            force_exit_reason="scheduled_force_close" if force_close_due else None,
                        )
                if now >= next_report:
                    print_report(states, started)
                    print_portfolio_report(states, portfolio)
                    next_report += args.report_sec
                if now >= next_snapshot:
                    write_microstructure_snapshot(
                        Path(args.snapshot_log),
                        states,
                        trading_enabled,
                        risk=args.risk_governor,
                        base_max_full_stop_rub=float(args.max_full_stop_rub),
                    )
                    write_open_positions(Path(args.open_positions_log), states)
                    write_bot_health(
                        Path(args.health_log),
                        states,
                        portfolio,
                        started,
                        last_stream_event,
                        int(runtime_state["reconnect_count"]),
                        str(runtime_state["last_stream_error"]),
                        "running",
                    )
                    next_snapshot += args.snapshot_sec

        try:
            while time.monotonic() < started + args.runtime_sec:
                try:
                    for response in client.market_data_stream.market_data_stream(make_stream_requests(specs, args.orderbook_depth)):
                        if time.monotonic() >= started + args.runtime_sec:
                            break
                        handle_stream_response(response)
                except Exception as exc:
                    if time.monotonic() >= started + args.runtime_sec:
                        break
                    runtime_state["reconnect_count"] = int(runtime_state["reconnect_count"]) + 1
                    runtime_state["last_stream_error"] = f"{type(exc).__name__}: {exc}"
                    print(f"{now_str()} STREAM reconnect_after_error {runtime_state['last_stream_error']}", flush=True)
                    with lock:
                        write_bot_health(
                            Path(args.health_log),
                            states,
                            portfolio,
                            started,
                            last_stream_event,
                            int(runtime_state["reconnect_count"]),
                            str(runtime_state["last_stream_error"]),
                            "stream_reconnecting",
                        )
                    time.sleep(3)
        finally:
            with lock:
                write_bot_health(
                    Path(args.health_log),
                    states,
                    portfolio,
                    started,
                    last_stream_event,
                    int(runtime_state["reconnect_count"]),
                    str(runtime_state["last_stream_error"]),
                    "stopping",
                )
            stop_event.set()
        print_report(states, started)
        print_portfolio_report(states, portfolio)
        print(f"{now_str()} multi_paper done")


if __name__ == "__main__":
    main()
