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
    family: str = ""
    source_secid: str = ""


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
    last_entry_candle_count: int = -1
    cooldown_until: float = 0.0
    shadow_positions: dict[str, Position] = field(default_factory=dict)
    shadow_closed: dict[str, bool] = field(default_factory=dict)


@dataclass
class Portfolio:
    initial_capital: float
    max_total_margin_pct: float
    max_position_margin_pct: float
    closed_net: float = 0.0

    @property
    def equity(self) -> float:
        return self.initial_capital + self.closed_net


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


def paper_qty(portfolio: Portfolio, states: list[State], spec: Spec, direction: Direction | str) -> int:
    per_contract = spec.margin_buy if direction == "long" else spec.margin_sell
    if per_contract <= 0:
        per_contract = max(spec.margin_buy, spec.margin_sell, 0.0)
    if per_contract <= 0:
        return 1
    max_total_margin = portfolio.equity * portfolio.max_total_margin_pct
    free_margin = max(0.0, max_total_margin - used_margin(states))
    per_position_limit = portfolio.equity * portfolio.max_position_margin_pct
    budget = min(free_margin, per_position_limit)
    return max(0, int(budget // per_contract))


def fee_ticks(side_fee: float, spec: Spec) -> float:
    return (2 * side_fee / spec.step_price) if spec.step_price else 999.0


def profile_can_trade(profile: Profile, side_fee: float, spec: Spec, max_fee_to_stop: float = 0.55) -> tuple[bool, str]:
    round_fee_ticks = fee_ticks(side_fee, spec)
    max_allowed = profile.stop_ticks * max_fee_to_stop
    if round_fee_ticks > max_allowed:
        return False, f"startup_fee_filter fee={round_fee_ticks:.1f}t stop={profile.stop_ticks}t max={max_allowed:.1f}t"
    return True, f"fee_ok fee={round_fee_ticks:.1f}t stop={profile.stop_ticks}t"


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
    return Profile(
        secid=ticker,
        stop_ticks=int(row["stop_ticks"]),
        trail_ticks=int(row["trail_ticks"]),
        trail_arm_ticks=int(row["trail_arm_ticks"]),
        target_min_ticks=int(row["target_min_ticks"]),
        max_attempts=int(row["max_attempts"]),
        family=str(row.get("v7_family") or contract_family(ticker)),
        source_secid=source_secid or row["ticker"],
    )


def contract_family(secid: str) -> str:
    if secid.endswith("perpA"):
        return secid
    head = secid.rstrip("0123456789")
    month_codes = set("FGHJKMNQUVXZ")
    if len(head) > 1 and head[-1].upper() in month_codes:
        head = head[:-1]
    return head or secid


def clone_profile_for_contract(profile: Profile, secid: str) -> Profile:
    return Profile(
        secid=secid,
        stop_ticks=profile.stop_ticks,
        trail_ticks=profile.trail_ticks,
        trail_arm_ticks=profile.trail_arm_ticks,
        target_min_ticks=profile.target_min_ticks,
        max_attempts=profile.max_attempts,
        family=profile.family or contract_family(secid),
        source_secid=profile.source_secid or profile.secid,
    )


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
        return None, f"book_filter empty_book bid={bid_qty} ask={ask_qty}"
    vol_ok = avgv > 0 and last_vol >= avgv * vol_mult
    fee_ticks = (2 * state.side_fee / spec.step_price) if spec.step_price else 999
    if fee_ticks > state.profile.stop_ticks * max_fee_to_stop:
        return None, f"fee_filter fee={fee_ticks:.1f}t stop={state.profile.stop_ticks}t"
    if max(float(r["high"]) - float(r["low"]) for r in rows[-5:]) / spec.min_step < fee_ticks + 2:
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


def parse_today_time(value: str | None) -> float | None:
    if not value:
        return None
    target = datetime.strptime(value, "%H:%M").replace(
        year=datetime.now().year,
        month=datetime.now().month,
        day=datetime.now().day,
    )
    return target.timestamp()


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
    if expiry_close_days > 0 and dte is not None and dte <= expiry_close_days:
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
    st.closed += 1
    st.closed_net += net
    portfolio.closed_net += net
    append_trade(
        Path(args.log),
        {
            "closed_at": now_str(),
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
    print(
        f"{now_str()} CLOSE {st.contour} {st.spec.secid} exit={fill_price:g} "
        f"source={fill_source} trigger={actual_trigger_price:g}/{actual_trigger_source} "
        f"ticks={ticks:.1f} net={net:.2f} total={st.closed_net:.2f}"
    )
    st.position = None
    if all_shadow_models_closed(st):
        st.shadow_positions = {}
        st.shadow_closed = {}
    st.cooldown_until = time.monotonic() + 90
    write_open_positions(Path(args.open_positions_log), states)


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
                                process_open_state_exit(
                                    st,
                                    args,
                                    portfolio,
                                    states,
                                    candle_closed=False,
                                    actual_trigger_source_override="polling_fallback",
                                )
                        write_open_positions(Path(args.open_positions_log), states)
                        write_microstructure_snapshot(Path(args.snapshot_log), states, trading_enabled=True)
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


def write_microstructure_snapshot(path: Path, states: list[State], trading_enabled: bool) -> None:
    rows = []
    snapshot_time = now_str()
    for st in states:
        if st.contour != "aggressive":
            continue
        levels = best_levels(st.last_order_book, st.spec)
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
                "signal_status": "listening" if trading_enabled else "warmup_or_no_new_entries",
                "skip_reason": "" if trading_enabled else "time_gate",
                "orderbook_source": "tbank_stream",
                "execution_validation_possible": levels["bid"] is not None and levels["ask"] is not None,
                "session_phase": "stream",
                "can_open_new_paper_trade": trading_enabled,
                "last_reason": st.last_reason,
            }
        )
    for row in rows:
        append_trade(path, row)


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
                "margin_rub": round(position_margin(st.spec, st.position.direction, st.position.qty), 2),
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
    parser.add_argument("--shadow-log", default=str(REPORTS / "paper_shadow_exit_models.csv"))
    parser.add_argument("--health-log", default=str(REPORTS / "paper_bot_health.json"))
    parser.add_argument("--snapshot-sec", type=int, default=10)
    parser.add_argument("--no-trade-before", default="")
    parser.add_argument("--no-new-after", default="")
    parser.add_argument("--paper-capital", type=float, default=200_000.0)
    parser.add_argument("--max-total-margin-pct", type=float, default=0.80)
    parser.add_argument("--max-position-margin-pct", type=float, default=0.20)
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
                print(f"{now_str()} SKIP {secid} instrument_error={exc}", flush=True)
                return None
            spec = spec_from_tbank(client, secid, future, info)
            side_fee = commission_side_rub(spec, 1, 0.00025, None)
            can_trade, fee_reason = profile_can_trade(profile, side_fee, spec)
            if not can_trade:
                print(f"{now_str()} SKIP {secid} {fee_reason} load_reason={load_reason}", flush=True)
                return None
            specs.append(spec)
            loaded_secids.add(secid)
            dte = days_to_expiration(spec)
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
            f"max_position_margin_pct={portfolio.max_position_margin_pct:.2f}"
        )
        started = time.monotonic()
        next_report = started + args.report_sec
        next_snapshot = started
        no_trade_before = parse_today_time(args.no_trade_before)
        no_new_after = parse_today_time(args.no_new_after)
        last_stream_event = [time.monotonic()]
        runtime_state: dict[str, object] = {"reconnect_count": 0, "last_stream_error": ""}
        stop_event = threading.Event()
        lock = threading.RLock()
        restore_closed_totals(Path(args.log), states, portfolio)
        restore_open_positions(Path(args.open_positions_log), states)
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
            wall_now = time.time()
            trading_enabled = (no_trade_before is None or wall_now >= no_trade_before) and (no_new_after is None or wall_now < no_new_after)
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
                    if st.position is None and has_active_shadow(st):
                        process_open_state_exit(st, args, portfolio, states, candle is not None)
                        if has_active_shadow(st):
                            continue
                    if st.position is None and trading_enabled:
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
                            entry_price, entry_source = executable_price(st, direction, "entry")
                            if entry_price is None:
                                st.last_reason = "book_filter no_executable_entry"
                                continue
                            qty = paper_qty(portfolio, states, spec, direction)
                            if qty < 1:
                                st.last_reason = "capital_filter no_free_margin"
                                continue
                            st.attempts += 1
                            st.position = open_position(direction, entry_price, qty, st.profile.stop_ticks, st.profile.trail_ticks, spec)
                            st.shadow_positions = {
                                "stream_stoplimit": clone_position(st.position),
                                "candle_like": clone_position(st.position),
                            }
                            st.shadow_closed = {}
                            st.last_entry_candle_count = len(st.candles)
                            print(
                                f"{now_str()} OPEN {contour} {spec.secid} {direction} qty={qty} "
                                f"entry={entry_price:g} source={entry_source} stop={st.position.stop_price:g} "
                                f"margin={position_margin(spec, direction, qty):.2f} {reason}",
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
                        )
                if now >= next_report:
                    print_report(states, started)
                    print_portfolio_report(states, portfolio)
                    next_report += args.report_sec
                if now >= next_snapshot:
                    write_microstructure_snapshot(Path(args.snapshot_log), states, trading_enabled)
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
