from __future__ import annotations

import argparse
import csv
import math
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Literal

import requests


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"

Direction = Literal["long", "short"]


def patch_protobuf_for_tinkoff_stream() -> None:
    try:
        from google.protobuf import message_factory, symbol_database
    except Exception:
        return
    db_cls = symbol_database.SymbolDatabase
    if hasattr(db_cls, "GetPrototype"):
        return

    def get_prototype(self, descriptor):  # noqa: ANN001
        return message_factory.GetMessageClass(descriptor)

    db_cls.GetPrototype = get_prototype  # type: ignore[attr-defined]


@dataclass
class ContractSpec:
    secid: str
    min_step: float
    step_price: float
    last_rub: float


@dataclass
class Position:
    direction: Direction
    entry_price: float
    qty: int
    best_price: float
    stop_price: float
    opened_at: str


@dataclass
class StreamInstrument:
    figi: str
    uid: str
    ticker: str
    class_code: str
    name: str


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def quotation_to_float(value: object) -> float:
    units = getattr(value, "units", 0) or 0
    nano = getattr(value, "nano", 0) or 0
    return float(units) + float(nano) / 1_000_000_000


def round_to_step(value: float, step: float) -> float:
    return round(round(value / step) * step, 10)


def find_tbank_token(*, env_names: list[str] | tuple[str, ...] | None = None, allow_desktop_tokens: bool = True) -> str:
    candidates: list[tuple[str, str]] = []
    for name in env_names or ["TBANK_TOKEN_READONLY", "TBANK_TOKEN", "TINKOFF_TOKEN"]:
        value = os.environ.get(name)
        if value:
            candidates.append((f"env:{name}", value.strip()))
    desktop = Path.home() / "Desktop"
    if allow_desktop_tokens and desktop.exists():
        for path in sorted(desktop.glob("*.txt")):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for token in re.findall(r"(?i)(?:t\.[A-Za-z0-9_-]{20,}|[A-Za-z0-9_-]{40,})", text):
                candidates.append((f"file:{path.name}", token.strip()))

    seen: set[str] = set()
    last_error = "token_not_found"
    try:
        from t_tech.invest import Client
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"tinkoff-investments SDK unavailable: {type(exc).__name__}") from exc

    for source, token in candidates:
        if token in seen:
            continue
        seen.add(token)
        try:
            with Client(token) as client:
                client.users.get_accounts()
            print(f"{now_str()} tbank_token source={source} status=working")
            return token
        except Exception as exc:  # noqa: BLE001
            last_error = f"{source}:{type(exc).__name__}"
    raise RuntimeError(f"No working T-Bank token found ({last_error})")


def find_paper_tbank_token() -> str:
    return find_tbank_token(env_names=["TBANK_TOKEN_READONLY"], allow_desktop_tokens=False)


def iss_table(payload: dict, name: str) -> list[dict]:
    block = payload.get(name, {})
    cols = block.get("columns", [])
    return [dict(zip(cols, row)) for row in block.get("data", [])]


def fetch_market(secid: str) -> tuple[ContractSpec, float]:
    url = f"https://iss.moex.com/iss/engines/futures/markets/forts/securities/{secid}.json"
    params = {"iss.meta": "off", "iss.only": "securities,marketdata"}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    payload = r.json()
    securities = iss_table(payload, "securities")
    marketdata = iss_table(payload, "marketdata")
    if not securities:
        raise RuntimeError(f"MOEX did not return securities for {secid}")
    sec = securities[0]
    md = marketdata[0] if marketdata else {}
    min_step = float(sec["MINSTEP"])
    step_price = float(sec["STEPPRICE"])
    last_rub = float(md.get("LAST_RUB") or 0.0)
    last = md.get("LAST") or md.get("SETTLEPRICE") or sec.get("LASTSETTLEPRICE") or sec.get("PREVSETTLEPRICE")
    if last is None:
        raise RuntimeError(f"MOEX did not return last price for {secid}")
    return ContractSpec(secid=secid, min_step=min_step, step_price=step_price, last_rub=last_rub), float(last)


def fetch_candles(secid: str, interval: int) -> list[dict]:
    url = f"https://iss.moex.com/iss/engines/futures/markets/forts/securities/{secid}/candles.json"
    params = {"iss.meta": "off", "interval": interval}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return [row for row in iss_table(r.json(), "candles") if row.get("close") is not None]


def volume_vwap(rows: list[dict], fallback_price: float) -> tuple[float, float]:
    value_sum = 0.0
    volume_sum = 0.0
    for row in rows:
        volume = float(row.get("volume") or 0.0)
        if volume <= 0:
            continue
        typical = (float(row["high"]) + float(row["low"]) + float(row["close"])) / 3.0
        value_sum += typical * volume
        volume_sum += volume
    if volume_sum <= 0:
        return fallback_price, 0.0
    return value_sum / volume_sum, volume_sum


def rolling_average_volume(rows: list[dict], length: int) -> float:
    vols = [float(row.get("volume") or 0.0) for row in rows[-length:] if row.get("volume") is not None]
    vols = [v for v in vols if v > 0]
    if not vols:
        return 0.0
    return sum(vols) / len(vols)


def fetch_signal(secid: str, args: argparse.Namespace) -> tuple[Direction | None, str]:
    spec, price = fetch_market(secid)
    one_min = fetch_candles(secid, 1)
    five_min = fetch_candles(secid, 5)
    if len(one_min) < max(args.breakout_lookback + 2, args.volume_lookback + 2) or len(five_min) < 4:
        return None, "not_enough_candles"

    closed_1m = one_min[:-1]
    last = closed_1m[-1]
    prev = closed_1m[-2]
    recent = closed_1m[-args.breakout_lookback - 1 : -1]
    avg_volume = rolling_average_volume(closed_1m[:-1], args.volume_lookback)
    last_volume = float(last.get("volume") or 0.0)
    vwap, _ = volume_vwap(closed_1m, price)

    last_close = float(last["close"])
    prev_close = float(prev["close"])
    momentum_ticks = (last_close - prev_close) / spec.min_step
    breakout_high = max(float(row["high"]) for row in recent)
    breakout_low = min(float(row["low"]) for row in recent)

    closed_5m = five_min[:-1]
    fast_5m = sum(float(row["close"]) for row in closed_5m[-2:]) / 2.0
    slow_5m = sum(float(row["close"]) for row in closed_5m[-4:]) / 4.0
    trend_ticks = (fast_5m - slow_5m) / spec.min_step

    volume_ok = avg_volume > 0 and last_volume >= avg_volume * args.volume_multiplier
    long_ok = (
        price > vwap + args.vwap_buffer_ticks * spec.min_step
        and trend_ticks >= args.trend_ticks
        and momentum_ticks >= args.signal_ticks
        and last_close >= breakout_high
        and volume_ok
    )
    short_ok = (
        price < vwap - args.vwap_buffer_ticks * spec.min_step
        and trend_ticks <= -args.trend_ticks
        and momentum_ticks <= -args.signal_ticks
        and last_close <= breakout_low
        and volume_ok
    )

    reason = (
        f"price={price:.3f} vwap={vwap:.3f} mom={momentum_ticks:.1f}t "
        f"trend5m={trend_ticks:.1f}t vol={last_volume:.0f}/{avg_volume:.0f}"
    )
    if long_ok:
        return "long", reason
    if short_ok:
        return "short", reason
    return None, reason


def fetch_impulse_signal(secid: str, signal_ticks: int) -> Direction | None:
    rows = fetch_candles(secid, 1)
    rows = [row for row in rows if row.get("close") is not None]
    if len(rows) < 3:
        return None
    prev = rows[-2]
    last = rows[-1]
    _, price = fetch_market(secid)
    spec, _ = fetch_market(secid)
    change_ticks = (float(last["close"]) - float(prev["close"])) / spec.min_step
    if change_ticks >= signal_ticks and price >= float(last["close"]):
        return "long"
    if change_ticks <= -signal_ticks and price <= float(last["close"]):
        return "short"
    return None


def commission_side_rub(spec: ContractSpec, qty: int, rate: float, override: float | None) -> float:
    if override is not None:
        return override
    if spec.last_rub <= 0:
        return 0.0
    return round(spec.last_rub * qty * rate, 2)


def open_position(direction: Direction, price: float, qty: int, stop_ticks: int, trail_ticks: int, spec: ContractSpec) -> Position:
    if direction == "long":
        stop = price - stop_ticks * spec.min_step
    else:
        stop = price + stop_ticks * spec.min_step
    return Position(
        direction=direction,
        entry_price=price,
        qty=qty,
        best_price=price,
        stop_price=round_to_step(stop, spec.min_step),
        opened_at=now_str(),
    )


def update_stop(pos: Position, price: float, trail_ticks: int, spec: ContractSpec) -> None:
    if pos.direction == "long":
        pos.best_price = max(pos.best_price, price)
        trailed = pos.best_price - trail_ticks * spec.min_step
        pos.stop_price = max(pos.stop_price, round_to_step(trailed, spec.min_step))
    else:
        pos.best_price = min(pos.best_price, price)
        trailed = pos.best_price + trail_ticks * spec.min_step
        pos.stop_price = min(pos.stop_price, round_to_step(trailed, spec.min_step))


def is_stop_hit(pos: Position, price: float) -> bool:
    if pos.direction == "long":
        return price <= pos.stop_price
    return price >= pos.stop_price


def pnl_rub(pos: Position, exit_price: float, spec: ContractSpec, side_fee: float) -> tuple[float, float, float]:
    sign = 1 if pos.direction == "long" else -1
    ticks = sign * (exit_price - pos.entry_price) / spec.min_step
    gross = ticks * spec.step_price * pos.qty
    net = gross - 2 * side_fee * pos.qty
    return ticks, gross, net


def append_trade(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row))
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def tbank_find_future(client: object, secid: str) -> StreamInstrument:
    from t_tech.invest import InstrumentType

    resp = client.instruments.find_instrument(
        query=secid,
        instrument_kind=InstrumentType.INSTRUMENT_TYPE_FUTURES,
    )
    matches = [
        item
        for item in resp.instruments
        if getattr(item, "ticker", "").upper() == secid.upper()
        or getattr(item, "name", "").upper().startswith(secid.upper())
    ]
    if not matches:
        raise RuntimeError(f"T-Bank did not find futures instrument {secid}")
    item = matches[0]
    return StreamInstrument(
        figi=item.figi,
        uid=item.uid,
        ticker=item.ticker,
        class_code=item.class_code,
        name=item.name,
    )


def seed_tbank_candles(client: object, instrument: StreamInstrument, minutes: int) -> deque[dict]:
    from t_tech.invest import CandleInterval

    to = datetime.now(timezone.utc)
    from_ = to - timedelta(minutes=minutes)
    resp = client.market_data.get_candles(
        figi=instrument.figi,
        from_=from_,
        to=to,
        interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
    )
    rows: deque[dict] = deque(maxlen=max(minutes, 60))
    for candle in resp.candles:
        rows.append(
            {
                "open": quotation_to_float(candle.open),
                "high": quotation_to_float(candle.high),
                "low": quotation_to_float(candle.low),
                "close": quotation_to_float(candle.close),
                "volume": int(candle.volume),
                "time": candle.time.isoformat(),
            }
        )
    return rows


def stream_requests(instrument: StreamInstrument, depth: int):
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
            instruments=[LastPriceInstrument(figi=instrument.figi, instrument_id=instrument.uid)],
        )
    )
    yield MarketDataRequest(
        subscribe_trades_request=SubscribeTradesRequest(
            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[TradeInstrument(figi=instrument.figi, instrument_id=instrument.uid)],
        )
    )
    yield MarketDataRequest(
        subscribe_order_book_request=SubscribeOrderBookRequest(
            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[OrderBookInstrument(figi=instrument.figi, instrument_id=instrument.uid, depth=depth)],
        )
    )
    yield MarketDataRequest(
        subscribe_candles_request=SubscribeCandlesRequest(
            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[
                CandleInstrument(
                    figi=instrument.figi,
                    instrument_id=instrument.uid,
                    interval=SubscriptionInterval.SUBSCRIPTION_INTERVAL_ONE_MINUTE,
                )
            ],
            waiting_close=True,
        )
    )
    while True:
        time.sleep(60)


def stream_signal(candles: deque[dict], last_price: float, order_book: object | None, args: argparse.Namespace) -> tuple[Direction | None, str]:
    if len(candles) < max(args.breakout_lookback + 2, args.volume_lookback + 2, 8):
        return None, f"warmup candles={len(candles)}"

    rows = list(candles)
    last = rows[-1]
    prev = rows[-2]
    recent = rows[-args.breakout_lookback - 1 : -1]
    avg_volume = rolling_average_volume(rows[:-1], args.volume_lookback)
    last_volume = float(last.get("volume") or 0.0)
    vwap, _ = volume_vwap(rows, last_price)

    last_close = float(last["close"])
    prev_close = float(prev["close"])
    momentum_ticks = (last_close - prev_close) / args.min_step
    breakout_high = max(float(row["high"]) for row in recent)
    breakout_low = min(float(row["low"]) for row in recent)

    fast_5m = sum(float(row["close"]) for row in rows[-2:]) / 2.0
    slow_5m = sum(float(row["close"]) for row in rows[-5:]) / 5.0
    trend_ticks = (fast_5m - slow_5m) / args.min_step

    bid_qty = ask_qty = 0
    if order_book is not None:
        bid_qty = sum(int(getattr(level, "quantity", 0) or 0) for level in getattr(order_book, "bids", [])[: args.book_levels])
        ask_qty = sum(int(getattr(level, "quantity", 0) or 0) for level in getattr(order_book, "asks", [])[: args.book_levels])
    book_long_ok = bid_qty >= ask_qty * args.book_imbalance if ask_qty > 0 else True
    book_short_ok = ask_qty >= bid_qty * args.book_imbalance if bid_qty > 0 else True

    volume_ok = avg_volume > 0 and last_volume >= avg_volume * args.volume_multiplier
    long_ok = (
        last_price > vwap + args.vwap_buffer_ticks * args.min_step
        and trend_ticks >= args.trend_ticks
        and momentum_ticks >= args.signal_ticks
        and last_close >= breakout_high
        and volume_ok
        and book_long_ok
    )
    short_ok = (
        last_price < vwap - args.vwap_buffer_ticks * args.min_step
        and trend_ticks <= -args.trend_ticks
        and momentum_ticks <= -args.signal_ticks
        and last_close <= breakout_low
        and volume_ok
        and book_short_ok
    )
    reason = (
        f"price={last_price:.3f} vwap={vwap:.3f} mom={momentum_ticks:.1f}t "
        f"trend={trend_ticks:.1f}t vol={last_volume:.0f}/{avg_volume:.0f} book={bid_qty}/{ask_qty}"
    )
    if long_ok:
        return "long", reason
    if short_ok:
        return "short", reason
    return None, reason


def run_tbank_stream(args: argparse.Namespace) -> None:
    patch_protobuf_for_tinkoff_stream()
    from t_tech.invest import Client

    token = find_paper_tbank_token()
    spec, moex_price = fetch_market(args.secid)
    args.min_step = spec.min_step
    side_fee = commission_side_rub(spec, args.qty, args.commission_rate, args.commission_side_rub)
    log_path = Path(args.log)
    position: Position | None = None
    attempts = 0
    closed_net = 0.0
    last_price = round_to_step(moex_price, spec.min_step)
    last_order_book = None
    started = time.monotonic()

    with Client(token) as client:
        instrument = tbank_find_future(client, args.secid)
        candles = seed_tbank_candles(client, instrument, args.seed_minutes)
        print(
            f"{now_str()} tbank_stream start {instrument.ticker} {instrument.class_code} "
            f"figi={instrument.figi} uid={instrument.uid} candles={len(candles)} "
            f"qty={args.qty} tick={spec.min_step} tick_rub={spec.step_price:.4f} side_fee={side_fee:.2f}"
        )
        for response in client.market_data_stream.market_data_stream(stream_requests(instrument, args.orderbook_depth)):
            if args.max_runtime_sec and time.monotonic() - started >= args.max_runtime_sec:
                if position is not None:
                    ticks, gross, net = pnl_rub(position, last_price, spec, side_fee)
                    print(
                        f"{now_str()} runtime_limit open_position {position.direction} entry={position.entry_price:.3f} "
                        f"last={last_price:.3f} stop={position.stop_price:.3f} unrealized_gross={gross:.2f} "
                        f"unrealized_after_roundtrip_fee={net:.2f}"
                    )
                break

            if response.last_price is not None:
                last_price = round_to_step(quotation_to_float(response.last_price.price), spec.min_step)
            if response.orderbook is not None:
                last_order_book = response.orderbook
            if response.trade is not None:
                last_price = round_to_step(quotation_to_float(response.trade.price), spec.min_step)
            if response.candle is not None:
                candle = response.candle
                candles.append(
                    {
                        "open": quotation_to_float(candle.open),
                        "high": quotation_to_float(candle.high),
                        "low": quotation_to_float(candle.low),
                        "close": quotation_to_float(candle.close),
                        "volume": int(candle.volume),
                        "time": candle.time.isoformat(),
                    }
                )
                print(f"{now_str()} candle close={candles[-1]['close']:.3f} volume={candles[-1]['volume']}")

            if position is None and attempts < args.max_attempts:
                if args.direction in {"long", "short"}:
                    direction = args.direction
                    reason = "manual_direction"
                else:
                    direction, reason = stream_signal(candles, last_price, last_order_book, args)
                if direction in {"long", "short"}:
                    attempts += 1
                    position = open_position(direction, last_price, args.qty, args.stop_ticks, args.trail_ticks, spec)
                    print(
                        f"{now_str()} open #{attempts} {position.direction} entry={position.entry_price:.3f} "
                        f"stop={position.stop_price:.3f} reason={reason}"
                    )
                elif response.candle is not None:
                    print(f"{now_str()} wait_filter {reason}")
            elif position is not None:
                old_stop = position.stop_price
                update_stop(position, last_price, args.trail_ticks, spec)
                if position.stop_price != old_stop:
                    print(f"{now_str()} trail price={last_price:.3f} stop={position.stop_price:.3f}")
                if is_stop_hit(position, last_price):
                    ticks, gross, net = pnl_rub(position, last_price, spec, side_fee)
                    closed_net += net
                    row = {
                        "closed_at": now_str(),
                        "secid": spec.secid,
                        "direction": position.direction,
                        "qty": position.qty,
                        "entry_price": position.entry_price,
                        "exit_price": last_price,
                        "ticks": round(ticks, 3),
                        "gross_rub": round(gross, 2),
                        "fees_rub": round(2 * side_fee, 2),
                        "net_rub": round(net, 2),
                        "closed_net_rub": round(closed_net, 2),
                    }
                    append_trade(log_path, row)
                    print(
                        f"{now_str()} close stop exit={last_price:.3f} ticks={ticks:.1f} "
                        f"gross={gross:.2f} net={net:.2f} total={closed_net:.2f}"
                    )
                    position = None
                    if args.cooldown_sec > 0:
                        time.sleep(args.cooldown_sec)
            if position is None and attempts >= args.max_attempts:
                break

    print(f"{now_str()} done attempts={attempts} closed_net={closed_net:.2f} log={log_path}")


def run(args: argparse.Namespace) -> None:
    if args.source == "tbank-stream":
        run_tbank_stream(args)
        return

    spec, price = fetch_market(args.secid)
    side_fee = commission_side_rub(spec, args.qty, args.commission_rate, args.commission_side_rub)
    log_path = Path(args.log)
    position: Position | None = None
    attempts = 0
    closed_net = 0.0
    started = time.monotonic()

    print(
        f"{now_str()} start secid={spec.secid} price={price:.3f} qty={args.qty} "
        f"tick={spec.min_step} tick_rub={spec.step_price:.4f} side_fee={side_fee:.2f}"
    )

    while attempts < args.max_attempts or position is not None:
        if args.max_runtime_sec and time.monotonic() - started >= args.max_runtime_sec:
            if position is not None:
                ticks, gross, net = pnl_rub(position, price, spec, side_fee)
                print(
                    f"{now_str()} runtime_limit open_position {position.direction} entry={position.entry_price:.3f} "
                    f"last={price:.3f} stop={position.stop_price:.3f} unrealized_gross={gross:.2f} "
                    f"unrealized_after_roundtrip_fee={net:.2f}"
                )
            break
        try:
            spec, price = fetch_market(args.secid)
            price = round_to_step(price, spec.min_step)
        except Exception as exc:  # noqa: BLE001
            print(f"{now_str()} market_error {type(exc).__name__}: {exc}")
            time.sleep(args.poll_sec)
            continue

        if position is None:
            direction = args.direction
            if direction == "auto":
                direction, reason = fetch_signal(args.secid, args)
                if direction is None:
                    print(f"{now_str()} wait_filter {reason}")
            elif direction == "impulse":
                direction = fetch_impulse_signal(args.secid, args.signal_ticks)
            if direction in {"long", "short"}:
                attempts += 1
                position = open_position(direction, price, args.qty, args.stop_ticks, args.trail_ticks, spec)
                print(
                    f"{now_str()} open #{attempts} {position.direction} entry={position.entry_price:.3f} "
                    f"stop={position.stop_price:.3f}"
                )
            else:
                print(f"{now_str()} wait_signal price={price:.3f}")
        else:
            old_stop = position.stop_price
            update_stop(position, price, args.trail_ticks, spec)
            if position.stop_price != old_stop:
                print(f"{now_str()} trail price={price:.3f} stop={position.stop_price:.3f}")
            if is_stop_hit(position, price):
                ticks, gross, net = pnl_rub(position, price, spec, side_fee)
                closed_net += net
                row = {
                    "closed_at": now_str(),
                    "secid": spec.secid,
                    "direction": position.direction,
                    "qty": position.qty,
                    "entry_price": position.entry_price,
                    "exit_price": price,
                    "ticks": round(ticks, 3),
                    "gross_rub": round(gross, 2),
                    "fees_rub": round(2 * side_fee, 2),
                    "net_rub": round(net, 2),
                    "closed_net_rub": round(closed_net, 2),
                }
                append_trade(log_path, row)
                print(
                    f"{now_str()} close stop exit={price:.3f} ticks={ticks:.1f} "
                    f"gross={gross:.2f} net={net:.2f} total={closed_net:.2f}"
                )
                position = None
                if args.cooldown_sec > 0:
                    time.sleep(args.cooldown_sec)

        time.sleep(args.poll_sec)

    print(f"{now_str()} done attempts={attempts} closed_net={closed_net:.2f} log={log_path}")


def require_paper_only(paper_only: bool, script_name: str) -> None:
    if paper_only:
        return
    raise SystemExit(f"{script_name} is paper-only. Pass --paper-only to confirm paper execution.")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Paper scalper for MOEX NG futures with fixed trailing stop.")
    parser.add_argument("--paper-only", action="store_true", required=True)
    parser.add_argument("--source", choices=["moex-iss", "tbank-stream"], default="moex-iss")
    parser.add_argument("--secid", default="NGK6")
    parser.add_argument("--direction", choices=["long", "short", "auto", "impulse"], default="auto")
    parser.add_argument("--qty", type=int, default=30)
    parser.add_argument("--stop-ticks", type=int, default=3)
    parser.add_argument("--trail-ticks", type=int, default=3)
    parser.add_argument("--signal-ticks", type=int, default=2)
    parser.add_argument("--trend-ticks", type=int, default=1)
    parser.add_argument("--vwap-buffer-ticks", type=int, default=1)
    parser.add_argument("--breakout-lookback", type=int, default=6)
    parser.add_argument("--volume-lookback", type=int, default=20)
    parser.add_argument("--volume-multiplier", type=float, default=1.2)
    parser.add_argument("--seed-minutes", type=int, default=90)
    parser.add_argument("--orderbook-depth", type=int, default=10)
    parser.add_argument("--book-levels", type=int, default=3)
    parser.add_argument("--book-imbalance", type=float, default=1.05)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--poll-sec", type=float, default=1.0)
    parser.add_argument("--cooldown-sec", type=float, default=0.0)
    parser.add_argument("--commission-rate", type=float, default=0.00025)
    parser.add_argument("--commission-side-rub", type=float, default=None)
    parser.add_argument("--max-runtime-sec", type=float, default=0.0)
    parser.add_argument("--log", default=str(REPORTS / "ng_scalper_paper_trades.csv"))
    args = parser.parse_args(list(argv) if argv is not None else None)
    require_paper_only(bool(args.paper_only), "ng_scalper_bot.py")
    return args


if __name__ == "__main__":
    run(parse_args())
