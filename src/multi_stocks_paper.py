from __future__ import annotations

import argparse
import json
import math
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from ng_scalper_bot import (
    Direction,
    commission_side_rub,
    find_tbank_token,
    now_str,
    open_position,
    quotation_to_float,
    round_to_step,
    update_stop,
    volume_vwap,
)
from multi_futures_paper import (
    DEFAULT_MAX_FULL_STOP_RUB,
    Portfolio,
    Profile,
    RiskGovernor,
    Spec,
    State,
    avg_volume,
    best_levels,
    clone_position,
    daily_force_close_due,
    daily_trading_enabled,
    executable_price,
    has_open_ticker,
    make_stream_requests,
    paper_sizing,
    parse_clock_time,
    poll_market_fallback,
    print_portfolio_report,
    print_report,
    process_open_state_exit,
    profile_can_trade,
    restore_closed_totals,
    restore_open_positions,
    seed_candles,
    write_bot_health,
    write_instrument_specs,
    write_microstructure_snapshot,
    write_open_positions,
    write_startup_status,
)


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"


def price_param_to_ticks(mode: object, value: object, spec: Spec, fallback: int) -> int:
    try:
        raw = float(value)
    except Exception:
        return max(1, int(fallback))
    if str(mode or "").lower() == "pct":
        base = spec.last_price or 0.0
        if base <= 0 or spec.min_step <= 0:
            return max(1, int(fallback))
        return max(1, int(math.ceil(base * raw / spec.min_step)))
    return max(1, int(round(raw)))


def param_threshold_ticks(profile: Profile, price: float, spec: Spec) -> float:
    mode = str(getattr(profile, "threshold_mode", "ticks") or "ticks").lower()
    try:
        value = float(getattr(profile, "threshold_value", 1.0))
    except Exception:
        value = 1.0
    if mode == "pct":
        return max(1.0, price * value / spec.min_step)
    return max(1.0, value)


def tbank_find_share(client: object, ticker: str):
    from t_tech.invest import InstrumentType

    resp = client.instruments.find_instrument(
        query=ticker,
        instrument_kind=InstrumentType.INSTRUMENT_TYPE_SHARE,
    )
    matches = [
        item
        for item in resp.instruments
        if str(getattr(item, "ticker", "")).upper() == ticker.upper()
        and str(getattr(item, "class_code", "")).upper() == "TQBR"
    ]
    if not matches:
        matches = [item for item in resp.instruments if str(getattr(item, "ticker", "")).upper() == ticker.upper()]
    if not matches:
        raise RuntimeError(f"T-Bank did not find share instrument {ticker}")
    return matches[0]


def stock_spec_from_tbank(client: object, ticker: str, share_ref: object, info: object) -> Spec:
    try:
        lp = client.market_data.get_last_prices(figi=[share_ref.figi]).last_prices
        last = quotation_to_float(lp[0].price) if lp else 0.0
    except Exception:
        last = 0.0
    tick = quotation_to_float(info.min_price_increment)
    lot = int(getattr(info, "lot", 1) or 1)
    tick_rub = tick * lot
    notional = last * lot if last else 0.0
    return Spec(
        secid=ticker,
        figi=share_ref.figi,
        uid=share_ref.uid,
        min_step=tick,
        step_price=tick_rub,
        last_rub=notional,
        last_price=last,
        expiration_date=None,
        margin_buy=notional,
        margin_sell=notional,
    )


def load_watchlist_profiles(path: Path, tickers: list[str] | None = None) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    wanted = {t.upper() for t in tickers or []}
    rows = []
    for item in raw:
        ticker = str(item.get("ticker") or "")
        if not ticker:
            continue
        if wanted and ticker.upper() not in wanted:
            continue
        if str(item.get("status") or "").upper() != "WATCHLIST":
            continue
        rows.append(item)
    return rows


def build_profile(candidate: dict, spec: Spec) -> Profile:
    params = candidate.get("params") or {}
    stop_ticks = price_param_to_ticks(params.get("stop_mode"), params.get("stop_value"), spec, 10)
    trail_ticks = price_param_to_ticks(params.get("trail_mode"), params.get("trail_value"), spec, max(1, stop_ticks // 2))
    arm_ticks = price_param_to_ticks(params.get("activation_mode"), params.get("activation_value"), spec, stop_ticks)
    direction = str(candidate.get("direction") or "both").lower()
    profile = Profile(
        secid=str(candidate["ticker"]),
        stop_ticks=stop_ticks,
        trail_ticks=trail_ticks,
        trail_arm_ticks=arm_ticks,
        target_min_ticks=1,
        max_attempts=10_000,
        allowed_direction=direction,
        family=str(candidate.get("family") or params.get("family") or candidate["ticker"]),
        source_secid=str(candidate["ticker"]),
    )
    profile.signal_family = str(candidate.get("family") or params.get("family") or "momentum_breakout")
    profile.exit_model_preferred = str(candidate.get("exit_model_preferred") or "hard_tick_model")
    profile.lookback = int(params.get("lookback") or 5)
    profile.trend_fast = int(params.get("trend_fast") or 5)
    profile.trend_slow = int(params.get("trend_slow") or 21)
    profile.volume_multiplier = float(params.get("volume_multiplier") or 1.0)
    profile.volume_window = int(params.get("volume_window") or 40)
    profile.vwap_mode = str(params.get("vwap_mode") or "disabled")
    profile.vwap_buffer_pct = float(params.get("vwap_buffer_pct") or 0.0)
    profile.threshold_mode = str(params.get("threshold_mode") or "ticks")
    profile.threshold_value = float(params.get("threshold_value") or 1.0)
    profile.cooldown_minutes = int(params.get("cooldown_minutes") or 5)
    profile.max_hold_minutes = int(params.get("max_hold_minutes") or 30)
    profile.session_filter = str(params.get("session_filter") or "main_session")
    profile.policy_spread_ticks = float((candidate.get("microstructure") or {}).get("policy_spread_ticks") or 0.0)
    return profile


def clock_seconds_now() -> int:
    current = datetime.now()
    return current.hour * 3600 + current.minute * 60 + current.second


def profile_session_allows(profile: Profile) -> tuple[bool, str]:
    now_sec = clock_seconds_now()
    session = str(getattr(profile, "session_filter", "main_session") or "main_session")
    if session == "morning_only":
        start, end = 10 * 3600, 14 * 3600
    elif session == "exclude_first_last_10min":
        start, end = 10 * 3600 + 10 * 60, 18 * 3600 + 35 * 60
    else:
        start, end = 10 * 3600, 18 * 3600 + 45 * 60
    if now_sec < start or now_sec >= end:
        return False, f"time_gate stock_session={session}"
    return True, "ok"


def holding_expired(st: State) -> bool:
    if st.position is None:
        return False
    minutes = int(getattr(st.profile, "max_hold_minutes", 0) or 0)
    if minutes <= 0:
        return False
    try:
        opened = datetime.strptime(st.position.opened_at, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return False
    return (datetime.now() - opened).total_seconds() >= minutes * 60


def stock_signal(state: State) -> tuple[Direction | None, str]:
    profile = state.profile
    spec = state.spec
    rows = list(state.candles)
    lookback = max(2, int(getattr(profile, "lookback", 5) or 5))
    trend_fast = max(2, int(getattr(profile, "trend_fast", 5) or 5))
    trend_slow = max(trend_fast + 1, int(getattr(profile, "trend_slow", 21) or 21))
    volume_window = max(5, int(getattr(profile, "volume_window", 40) or 40))
    need = max(lookback + 2, trend_slow + 1, volume_window + 2)
    if len(rows) < need:
        return None, f"warmup={len(rows)}"

    ok_session, session_reason = profile_session_allows(profile)
    if not ok_session:
        return None, session_reason

    levels = best_levels(state.last_order_book, spec)
    bid_qty = int(levels.get("bid_size") or 0)
    ask_qty = int(levels.get("ask_size") or 0)
    spread_ticks = levels.get("spread_ticks")
    if bid_qty <= 0 or ask_qty <= 0:
        return None, f"book_filter empty_book bid={bid_qty} ask={ask_qty}"
    if spread_ticks is None:
        return None, "book_filter no_spread"
    max_spread = max(1.0, min(state.profile.stop_ticks * 0.35, float(getattr(profile, "policy_spread_ticks", 0.0) or 999) * 1.5))
    if float(spread_ticks) > max_spread:
        return None, f"stock_spread_filter spread={float(spread_ticks):.1f}t stop={state.profile.stop_ticks}t max={max_spread:.1f}t"

    last = rows[-1]
    prev = rows[-2]
    recent = rows[-lookback - 1 : -1]
    last_close = float(last["close"])
    prev_close = float(prev["close"])
    price = state.last_price or last_close
    threshold = param_threshold_ticks(profile, price, spec)
    mom = (last_close - prev_close) / spec.min_step
    fast = sum(float(r["close"]) for r in rows[-trend_fast:]) / trend_fast
    slow = sum(float(r["close"]) for r in rows[-trend_slow:]) / trend_slow
    trend = (fast - slow) / spec.min_step
    high = max(float(r["high"]) for r in recent)
    low = min(float(r["low"]) for r in recent)
    candle_range = (float(last["high"]) - float(last["low"])) / spec.min_step
    last_vol = float(last.get("volume") or 0)
    avgv = avg_volume(rows[:-1], volume_window)
    vol_mult = float(getattr(profile, "volume_multiplier", 1.0) or 1.0)
    vol_ok = avgv > 0 and last_vol >= avgv * vol_mult
    vwap_ok_long = vwap_ok_short = True
    if str(getattr(profile, "vwap_mode", "disabled")) != "disabled":
        vwap, _ = volume_vwap(rows[-max(20, min(len(rows), 60)) :], price)
        buffer_ticks = max(0.0, price * float(getattr(profile, "vwap_buffer_pct", 0.0) or 0.0) / spec.min_step)
        vwap_ok_long = price >= vwap + buffer_ticks * spec.min_step
        vwap_ok_short = price <= vwap - buffer_ticks * spec.min_step
    family = str(getattr(profile, "signal_family", "momentum_breakout") or "momentum_breakout")

    if family == "range_expansion":
        long_core = candle_range >= threshold and last_close >= high and mom > 0
        short_core = candle_range >= threshold and last_close <= low and mom < 0
    else:
        long_core = mom >= threshold and last_close >= high
        short_core = mom <= -threshold and last_close <= low

    long_ok = long_core and trend >= 0 and vol_ok and vwap_ok_long
    short_ok = short_core and trend <= 0 and vol_ok and vwap_ok_short
    reason = (
        f"stock p={price:g} mom={mom:.1f}t need={threshold:.1f}t "
        f"trend={trend:.1f}t vol={last_vol:.0f}/{avgv:.0f} book={bid_qty}/{ask_qty}"
    )
    if long_ok:
        return "long", f"entry_signal long {reason}"
    if short_ok:
        return "short", f"entry_signal short {reason}"
    return None, f"watch_conditions {reason}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles-json", default=str(REPORTS / "stock_moex_scalp_results_review" / "stock_final_live_paper_profiles.json"))
    parser.add_argument("--secids", nargs="+", default=[])
    parser.add_argument("--runtime-sec", type=int, default=86400)
    parser.add_argument("--report-sec", type=int, default=600)
    parser.add_argument("--seed-minutes", type=int, default=240)
    parser.add_argument("--orderbook-depth", type=int, default=10)
    parser.add_argument("--log", default=str(REPORTS / "stock_watch_multi_futures_paper_trades.csv"))
    parser.add_argument("--snapshot-log", default=str(REPORTS / "stock_watch_live_orderbook_snapshots.csv"))
    parser.add_argument("--open-positions-log", default=str(REPORTS / "stock_watch_paper_open_positions.json"))
    parser.add_argument("--instrument-specs-log", default=str(REPORTS / "stock_watch_instrument_specs.csv"))
    parser.add_argument("--startup-status-log", default=str(REPORTS / "stock_watch_startup_status.csv"))
    parser.add_argument("--shadow-log", default=str(REPORTS / "stock_watch_shadow_exit_models.csv"))
    parser.add_argument("--health-log", default=str(REPORTS / "stock_watch_health.json"))
    parser.add_argument("--risk-state-log", default=str(REPORTS / "stock_watch_risk_policy_state.json"))
    parser.add_argument("--snapshot-sec", type=int, default=10)
    parser.add_argument("--paper-capital", type=float, default=800_000.0)
    parser.add_argument("--max-total-margin-pct", type=float, default=0.80)
    parser.add_argument("--max-position-margin-pct", type=float, default=0.20)
    parser.add_argument("--max-full-stop-rub", type=float, default=500.0)
    parser.add_argument("--risk-reduced-full-stop-rub", type=float, default=250.0)
    parser.add_argument("--risk-micro-full-stop-rub", type=float, default=100.0)
    parser.add_argument("--risk-profit-guard-min-rub", type=float, default=1_500.0)
    parser.add_argument("--risk-profit-guard-drawdown-pct", type=float, default=0.35)
    parser.add_argument("--risk-profit-guard-drawdown-min-rub", type=float, default=500.0)
    parser.add_argument("--risk-stop-to-median-reduced", type=float, default=7.0)
    parser.add_argument("--risk-stop-to-median-micro", type=float, default=10.0)
    parser.add_argument("--risk-stop-to-median-cap", type=float, default=4.0)
    parser.add_argument("--risk-observe-trades", type=int, default=5)
    parser.add_argument("--risk-probation-trades", type=int, default=30)
    parser.add_argument("--stop-limit-emergency-ticks", type=float, default=2.0)
    parser.add_argument("--actual-exit-model", choices=["stream_stoplimit", "candle_like"], default="stream_stoplimit")
    parser.add_argument("--stream-stale-sec", type=float, default=15.0)
    parser.add_argument("--fallback-poll-sec", type=float, default=2.0)
    parser.add_argument("--no-trade-before", default="10:00")
    parser.add_argument("--no-new-after", default="18:35")
    parser.add_argument("--force-close-at", default="18:45")
    args = parser.parse_args()

    from t_tech.invest import Client, InstrumentIdType

    candidates = load_watchlist_profiles(Path(args.profiles_json), args.secids or None)
    if not candidates:
        print(f"{now_str()} stock_paper no WATCHLIST profiles found", flush=True)
        return

    token = find_tbank_token()
    portfolio = Portfolio(
        initial_capital=float(args.paper_capital),
        max_total_margin_pct=float(args.max_total_margin_pct),
        max_position_margin_pct=float(args.max_position_margin_pct),
    )
    states: list[State] = []
    specs: list[Spec] = []
    startup_status: list[dict] = []

    with Client(token) as client:
        for candidate in candidates:
            ticker = str(candidate["ticker"])
            try:
                share_ref = tbank_find_share(client, ticker)
                info = client.instruments.share_by(
                    id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
                    id=share_ref.uid,
                ).instrument
                if not bool(getattr(info, "api_trade_available_flag", True)):
                    raise RuntimeError("api_trade_unavailable")
                if str(candidate.get("direction") or "").lower() == "short" and not bool(getattr(info, "short_enabled_flag", False)):
                    raise RuntimeError("short_unavailable")
                spec = stock_spec_from_tbank(client, ticker, share_ref, info)
                profile = build_profile(candidate, spec)
                side_fee = commission_side_rub(spec, 1, 0.00025, None)
                can_trade, fee_reason = profile_can_trade(profile, side_fee, spec, max_fee_to_stop=0.55)
                if not can_trade:
                    raise RuntimeError(fee_reason)
                seeded = seed_candles(client, spec.figi, args.seed_minutes)
                states.append(
                    State(
                        spec=spec,
                        profile=profile,
                        contour="aggressive",
                        side_fee=side_fee,
                        candles=deque(seeded, maxlen=240),
                        last_price=spec.last_price,
                    )
                )
                specs.append(spec)
                startup_status.append(
                    {
                        "timestamp": now_str(),
                        "ticker": ticker,
                        "status": "loaded",
                        "reason": f"{fee_reason} stock_watch stop={profile.stop_ticks}t trail={profile.trail_ticks}t arm={profile.trail_arm_ticks}t",
                        "load_reason": "stock_watchlist",
                        "profile_source": ticker,
                        "family": profile.family,
                        "tick": spec.min_step,
                        "tick_rub": spec.step_price,
                        "last_price": round(spec.last_price, 6),
                        "go_buy": round(spec.margin_buy, 2),
                        "go_sell": round(spec.margin_sell, 2),
                    }
                )
                print(f"{now_str()} STOCK LOAD {ticker} {startup_status[-1]['reason']}", flush=True)
            except Exception as exc:
                startup_status.append(
                    {
                        "timestamp": now_str(),
                        "ticker": ticker,
                        "status": "skipped",
                        "reason": f"instrument_error={exc}",
                        "load_reason": "stock_watchlist",
                        "profile_source": ticker,
                        "family": str(candidate.get("family") or ticker),
                    }
                )
                print(f"{now_str()} STOCK SKIP {ticker} error={exc}", flush=True)

        write_startup_status(Path(args.startup_status_log), startup_status)
        if not specs:
            print(f"{now_str()} stock_paper no tradable shares after startup filters", flush=True)
            return
        write_instrument_specs(Path(args.instrument_specs_log), specs)

        state_by_uid = {(st.spec.uid, st.contour): st for st in states}
        by_uid = {s.uid: s for s in specs}
        started = time.monotonic()
        next_report = started + float(args.report_sec)
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
            observe_trades=int(args.risk_observe_trades),
            probation_trades=int(args.risk_probation_trades),
        )
        setattr(args, "risk_governor", risk_governor)
        restore_closed_totals(Path(args.log), states, portfolio)
        risk_governor.rebuild_from_trade_log(Path(args.log), states)
        restore_open_positions(Path(args.open_positions_log), states)
        write_open_positions(Path(args.open_positions_log), states)
        write_bot_health(Path(args.health_log), states, portfolio, started, last_stream_event, 0, "", "starting")

        poll_thread = threading.Thread(
            target=poll_market_fallback,
            args=(token, specs, state_by_uid, states, args, portfolio, last_stream_event, started, runtime_state, stop_event, lock),
            daemon=True,
        )
        poll_thread.start()

        print(
            f"{now_str()} stock_paper start instruments={len(specs)} runtime_sec={args.runtime_sec} "
            f"capital={portfolio.initial_capital:.0f} max_full_stop_rub={float(args.max_full_stop_rub):.0f} "
            f"no_new_after={args.no_new_after} force_close_at={args.force_close_at}",
            flush=True,
        )

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
                st = state_by_uid[(uid, "aggressive")]
                if price is not None:
                    st.last_price = round_to_step(price, spec.min_step)
                    spec.last_price = st.last_price
                    spec.last_rub = st.last_price / spec.min_step * spec.step_price if spec.min_step else spec.last_rub
                    spec.margin_buy = spec.last_rub
                    spec.margin_sell = spec.last_rub
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

                force_reason = None
                if force_close_due:
                    force_reason = "scheduled_force_close"
                elif holding_expired(st):
                    force_reason = "max_hold_close"

                if st.position is not None:
                    before = st.position
                    process_open_state_exit(st, args, portfolio, states, candle is not None, force_exit_reason=force_reason)
                    if before is not None and st.position is None:
                        st.cooldown_until = max(st.cooldown_until, time.monotonic() + int(getattr(st.profile, "cooldown_minutes", 5)) * 60)
                elif st.position is None and trading_enabled and not force_close_due:
                    ok_session, session_reason = profile_session_allows(st.profile)
                    if not ok_session:
                        st.last_reason = session_reason
                    elif now < st.cooldown_until:
                        st.last_reason = "cooldown_filter wait_after_close"
                    elif has_open_ticker(states, spec.secid):
                        st.last_reason = "duplicate_filter ticker_already_open"
                    else:
                        direction, reason = stock_signal(st)
                        st.last_reason = reason
                        if direction:
                            allowed_direction = (st.profile.allowed_direction or "both").lower()
                            if allowed_direction in {"long", "short"} and direction != allowed_direction:
                                st.last_reason = f"direction_filter profile={allowed_direction} signal={direction}"
                            else:
                                entry_price, entry_source = executable_price(st, direction, "entry")
                                if entry_price is None:
                                    st.last_reason = "book_filter no_executable_entry"
                                else:
                                    risk_decision = risk_governor.decide(st, float(args.max_full_stop_rub))
                                    st.risk_mode = risk_decision.mode
                                    st.risk_limit_rub = risk_decision.max_full_stop_rub
                                    st.risk_reason = risk_decision.reason
                                    if not risk_decision.allowed:
                                        st.last_reason = f"risk_governor {risk_decision.mode} {risk_decision.reason}"
                                    else:
                                        sizing = paper_sizing(
                                            portfolio,
                                            states,
                                            spec,
                                            st.profile,
                                            direction,
                                            st.side_fee,
                                            risk_decision.max_full_stop_rub,
                                        )
                                        if sizing.qty < 1:
                                            st.last_reason = f"risk_filter full_stop_gt_limit {sizing.reason}"
                                        else:
                                            st.attempts += 1
                                            st.position = open_position(direction, entry_price, sizing.qty, st.profile.stop_ticks, st.profile.trail_ticks, spec)
                                            st.shadow_positions = {
                                                "stream_stoplimit": clone_position(st.position),
                                                "candle_like": clone_position(st.position),
                                            }
                                            st.shadow_closed = {}
                                            print(
                                                f"{now_str()} STOCK OPEN {spec.secid} {direction} qty={sizing.qty} "
                                                f"entry={entry_price:g} source={entry_source} stop={st.position.stop_price:g} "
                                                f"full_stop_risk={sizing.full_stop_rub:.2f} {reason}",
                                                flush=True,
                                            )
                                            write_open_positions(Path(args.open_positions_log), states)

                if now >= next_report:
                    print_report(states, started)
                    print_portfolio_report(states, portfolio)
                    next_report += float(args.report_sec)
                if now >= next_snapshot:
                    write_microstructure_snapshot(
                        Path(args.snapshot_log),
                        states,
                        trading_enabled,
                        risk=risk_governor,
                        base_max_full_stop_rub=float(args.max_full_stop_rub),
                    )
                    write_open_positions(Path(args.open_positions_log), states)
                    write_bot_health(
                        Path(args.health_log),
                        states,
                        portfolio,
                        started,
                        last_stream_event,
                        int(runtime_state.get("reconnect_count", 0)),
                        str(runtime_state.get("last_stream_error", "")),
                        "running",
                    )
                    next_snapshot += float(args.snapshot_sec)

        try:
            while time.monotonic() < started + float(args.runtime_sec):
                try:
                    for response in client.market_data_stream.market_data_stream(make_stream_requests(specs, args.orderbook_depth)):
                        if time.monotonic() >= started + float(args.runtime_sec):
                            break
                        handle_stream_response(response)
                except Exception as exc:
                    if time.monotonic() >= started + float(args.runtime_sec):
                        break
                    runtime_state["reconnect_count"] = int(runtime_state["reconnect_count"]) + 1
                    runtime_state["last_stream_error"] = f"{type(exc).__name__}: {exc}"
                    print(f"{now_str()} STOCK STREAM reconnect_after_error {runtime_state['last_stream_error']}", flush=True)
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
        print(f"{now_str()} stock_paper done", flush=True)


if __name__ == "__main__":
    main()
