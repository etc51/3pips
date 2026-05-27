from __future__ import annotations

import argparse
import json
import math
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from ng_scalper_bot import find_tbank_token, now_str, quotation_to_float, round_to_step, tbank_find_future


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"

Direction = Literal["long", "short"]


@dataclass
class Instrument:
    ticker: str
    figi: str
    uid: str
    min_step: float
    step_price: float


@dataclass
class SmokePosition:
    direction: Direction
    qty: int
    entry_price: float
    best_price: float
    stop_price: float
    stop_order_id: str = ""
    stop_client_id: str = ""


def log_event(path: Path, event: str, **fields: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": now_str(), "event": event}
    row.update(fields)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def price_to_quotation(price: float):
    from t_tech.invest import Quotation

    units = math.floor(price)
    nano = int(round((price - units) * 1_000_000_000))
    if nano >= 1_000_000_000:
        units += 1
        nano -= 1_000_000_000
    return Quotation(units=units, nano=nano)


def order_id(prefix: str, tag: str) -> str:
    clean = "".join(ch for ch in prefix.lower() if ch.isalnum() or ch == "-")[:14]
    return f"{clean}-{tag}-{uuid.uuid4().hex[:12]}"[:36]


def enum_name(value: object) -> str:
    return str(getattr(value, "name", value))


def best_levels(book: object, step: float) -> dict:
    bids = getattr(book, "bids", []) or []
    asks = getattr(book, "asks", []) or []
    bid = quotation_to_float(bids[0].price) if bids else 0.0
    ask = quotation_to_float(asks[0].price) if asks else 0.0
    bid_qty = int(getattr(bids[0], "quantity", 0) or 0) if bids else 0
    ask_qty = int(getattr(asks[0], "quantity", 0) or 0) if asks else 0
    spread_ticks = round((ask - bid) / step, 3) if bid and ask and step else None
    return {"bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty, "spread_ticks": spread_ticks}


def select_account(client: object, requested: str) -> object:
    accounts = list(getattr(client.users.get_accounts(), "accounts", []) or [])
    if requested:
        for account in accounts:
            if str(getattr(account, "id", "")) == requested:
                return account
        raise RuntimeError(f"account_id_not_found {requested}")
    open_accounts = [a for a in accounts if "OPEN" in enum_name(getattr(a, "status", "")).upper()]
    if len(open_accounts) != 1:
        ids = [str(getattr(a, "id", "")) for a in open_accounts]
        raise RuntimeError(f"account_id_required open_accounts={ids}")
    return open_accounts[0]


def load_instrument(client: object, ticker: str) -> Instrument:
    from t_tech.invest import InstrumentIdType

    future = tbank_find_future(client, ticker)
    info = client.instruments.future_by(
        id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
        id=future.uid,
    ).instrument
    return Instrument(
        ticker=ticker,
        figi=future.figi,
        uid=future.uid,
        min_step=quotation_to_float(info.min_price_increment),
        step_price=quotation_to_float(info.min_price_increment_amount),
    )


def active_regular_orders(client: object, account_id: str, instrument: Instrument) -> list[object]:
    from t_tech.invest import OrderExecutionReportStatus

    now = datetime.now(timezone.utc)
    resp = client.orders.get_orders(
        account_id=account_id,
        from_=now - timedelta(minutes=30),
        to=now,
        execution_status=[
            OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW,
            OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_PARTIALLYFILL,
        ],
    )
    result = []
    for order in getattr(resp, "orders", []) or []:
        if getattr(order, "figi", "") == instrument.figi or getattr(order, "instrument_uid", "") == instrument.uid:
            result.append(order)
    return result


def active_stop_orders(client: object, account_id: str, instrument: Instrument) -> list[object]:
    from t_tech.invest import StopOrderStatusOption

    resp = client.stop_orders.get_stop_orders(
        account_id=account_id,
        status=StopOrderStatusOption.STOP_ORDER_STATUS_ACTIVE,
    )
    result = []
    for order in getattr(resp, "stop_orders", []) or []:
        if (
            getattr(order, "figi", "") == instrument.figi
            or getattr(order, "instrument_uid", "") == instrument.uid
            or str(getattr(order, "ticker", "")).upper() == instrument.ticker.upper()
        ):
            result.append(order)
    return result


def wait_order_fill(client: object, account_id: str, order_id_value: str, timeout_sec: int, log_path: Path) -> tuple[int, float, str]:
    from t_tech.invest import OrderExecutionReportStatus, PriceType

    deadline = time.monotonic() + timeout_sec
    last_status = ""
    while time.monotonic() <= deadline:
        state = client.orders.get_order_state(
            account_id=account_id,
            order_id=order_id_value,
            price_type=PriceType.PRICE_TYPE_POINT,
        )
        status = enum_name(getattr(state, "execution_report_status", ""))
        lots = int(getattr(state, "lots_executed", 0) or 0)
        price = quotation_to_float(getattr(state, "executed_order_price", None))
        if status != last_status:
            log_event(log_path, "order_state", order_id=order_id_value, status=status, lots_executed=lots, executed_price=price)
            last_status = status
        if getattr(state, "execution_report_status", None) == OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_FILL:
            return lots, price, status
        if getattr(state, "execution_report_status", None) in (
            OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_CANCELLED,
            OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_REJECTED,
        ):
            return lots, price, status
        time.sleep(1)
    return 0, 0.0, f"timeout last_status={last_status}"


def post_market_order(
    client: object,
    account_id: str,
    instrument: Instrument,
    direction: Direction,
    qty: int,
    client_id_prefix: str,
    tag: str,
    log_path: Path,
) -> tuple[str, str]:
    from t_tech.invest import OrderDirection, OrderType, PriceType

    order_direction = OrderDirection.ORDER_DIRECTION_BUY if direction == "long" else OrderDirection.ORDER_DIRECTION_SELL
    client_order_id = order_id(client_id_prefix, tag)
    response = client.orders.post_order(
        figi=instrument.figi,
        instrument_id=instrument.uid,
        quantity=int(qty),
        direction=order_direction,
        account_id=account_id,
        order_type=OrderType.ORDER_TYPE_MARKET,
        order_id=client_order_id,
        price_type=PriceType.PRICE_TYPE_POINT,
    )
    broker_order_id = str(getattr(response, "order_id", "") or client_order_id)
    log_event(
        log_path,
        "post_market_order",
        client_order_id=client_order_id,
        broker_order_id=broker_order_id,
        direction=direction,
        qty=qty,
        status=enum_name(getattr(response, "execution_report_status", "")),
        lots_executed=int(getattr(response, "lots_executed", 0) or 0),
        executed_price=quotation_to_float(getattr(response, "executed_order_price", None)),
        message=str(getattr(response, "message", "") or ""),
    )
    return client_order_id, broker_order_id


def protective_stop_prices(position: SmokePosition, instrument: Instrument, limit_offset_ticks: int) -> tuple[float, float]:
    if position.direction == "long":
        limit_price = position.stop_price - limit_offset_ticks * instrument.min_step
    else:
        limit_price = position.stop_price + limit_offset_ticks * instrument.min_step
    return round_to_step(position.stop_price, instrument.min_step), round_to_step(limit_price, instrument.min_step)


def post_stop_limit(
    client: object,
    account_id: str,
    instrument: Instrument,
    position: SmokePosition,
    client_id_prefix: str,
    limit_offset_ticks: int,
    log_path: Path,
) -> str:
    from t_tech.invest import (
        ExchangeOrderType,
        PriceType,
        StopOrderDirection,
        StopOrderExpirationType,
        StopOrderType,
    )

    stop_direction = StopOrderDirection.STOP_ORDER_DIRECTION_SELL if position.direction == "long" else StopOrderDirection.STOP_ORDER_DIRECTION_BUY
    stop_price, limit_price = protective_stop_prices(position, instrument, limit_offset_ticks)
    client_stop_id = order_id(client_id_prefix, "stop")
    response = client.stop_orders.post_stop_order(
        figi=instrument.figi,
        instrument_id=instrument.uid,
        quantity=int(position.qty),
        price=price_to_quotation(limit_price),
        stop_price=price_to_quotation(stop_price),
        direction=stop_direction,
        account_id=account_id,
        expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
        stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LIMIT,
        exchange_order_type=ExchangeOrderType.EXCHANGE_ORDER_TYPE_LIMIT,
        price_type=PriceType.PRICE_TYPE_POINT,
        order_id=client_stop_id,
    )
    stop_order_id = str(getattr(response, "stop_order_id", ""))
    log_event(
        log_path,
        "post_stop_limit",
        client_stop_id=client_stop_id,
        stop_order_id=stop_order_id,
        position_direction=position.direction,
        qty=position.qty,
        stop_price=stop_price,
        limit_price=limit_price,
    )
    position.stop_order_id = stop_order_id
    position.stop_client_id = client_stop_id
    return stop_order_id


def cancel_stop_if_any(client: object, account_id: str, stop_order_id: str, log_path: Path, reason: str) -> None:
    if not stop_order_id:
        return
    try:
        response = client.stop_orders.cancel_stop_order(account_id=account_id, stop_order_id=stop_order_id)
        log_event(log_path, "cancel_stop_order", stop_order_id=stop_order_id, reason=reason, time=getattr(response, "time", None))
    except Exception as exc:  # noqa: BLE001
        log_event(log_path, "cancel_stop_order_error", stop_order_id=stop_order_id, reason=reason, error=f"{type(exc).__name__}: {exc}")


def cancel_regular_if_any(client: object, account_id: str, order_id_value: str, log_path: Path, reason: str) -> None:
    if not order_id_value:
        return
    try:
        response = client.orders.cancel_order(account_id=account_id, order_id=order_id_value)
        log_event(log_path, "cancel_order", order_id=order_id_value, reason=reason, time=getattr(response, "time", None))
    except Exception as exc:  # noqa: BLE001
        log_event(log_path, "cancel_order_error", order_id=order_id_value, reason=reason, error=f"{type(exc).__name__}: {exc}")


def audit_duplicates(client: object, account_id: str, instrument: Instrument, log_path: Path, fail_on_duplicate: bool = True) -> tuple[int, int]:
    regular = active_regular_orders(client, account_id, instrument)
    stops = active_stop_orders(client, account_id, instrument)
    log_event(
        log_path,
        "broker_duplicate_audit",
        ticker=instrument.ticker,
        active_regular=len(regular),
        active_stop=len(stops),
        regular_ids=[str(getattr(x, "order_id", "")) for x in regular],
        stop_ids=[str(getattr(x, "stop_order_id", "")) for x in stops],
    )
    if fail_on_duplicate and (len(regular) > 1 or len(stops) > 1):
        raise RuntimeError(f"duplicate_broker_orders regular={len(regular)} stop={len(stops)}")
    return len(regular), len(stops)


def update_stop_from_price(position: SmokePosition, instrument: Instrument, price: float, trail_ticks: int) -> bool:
    if position.direction == "long":
        position.best_price = max(position.best_price, price)
        new_stop = round_to_step(position.best_price - trail_ticks * instrument.min_step, instrument.min_step)
        if new_stop > position.stop_price:
            position.stop_price = new_stop
            return True
    else:
        position.best_price = min(position.best_price, price)
        new_stop = round_to_step(position.best_price + trail_ticks * instrument.min_step, instrument.min_step)
        if new_stop < position.stop_price:
            position.stop_price = new_stop
            return True
    return False


def emergency_stop_hit(position: SmokePosition, instrument: Instrument, last_price: float, emergency_ticks: int) -> bool:
    if position.direction == "long":
        return last_price <= position.stop_price - emergency_ticks * instrument.min_step
    return last_price >= position.stop_price + emergency_ticks * instrument.min_step


def run(args: argparse.Namespace) -> int:
    from t_tech.invest import Client

    if args.real_orders:
        if os.environ.get("LIVE_SMOKE_ENABLE") != "1" or args.confirm_real_orders != "YES":
            raise RuntimeError("real_orders_blocked set LIVE_SMOKE_ENABLE=1 and --confirm-real-orders YES")

    log_path = Path(args.log)
    token = find_tbank_token()
    with Client(token) as client:
        account = select_account(client, args.account_id)
        account_id = str(getattr(account, "id", ""))
        instrument = load_instrument(client, args.ticker)
        book = client.market_data.get_order_book(figi=instrument.figi, depth=int(args.orderbook_depth))
        levels = best_levels(book, instrument.min_step)
        log_event(
            log_path,
            "preflight",
            real_orders=bool(args.real_orders),
            account_id=account_id,
            account_name=str(getattr(account, "name", "")),
            ticker=instrument.ticker,
            figi=instrument.figi,
            uid=instrument.uid,
            min_step=instrument.min_step,
            step_price=instrument.step_price,
            **levels,
        )
        if not levels["bid"] or not levels["ask"]:
            raise RuntimeError("empty_orderbook")
        if levels["spread_ticks"] is not None and levels["spread_ticks"] > float(args.max_spread_ticks):
            raise RuntimeError(f"spread_too_wide {levels['spread_ticks']} > {args.max_spread_ticks}")
        regular_count, stop_count = audit_duplicates(client, account_id, instrument, log_path, fail_on_duplicate=False)
        if (regular_count or stop_count) and not args.allow_existing_instrument_orders:
            raise RuntimeError(f"existing_instrument_orders regular={regular_count} stop={stop_count}")
        if not args.real_orders:
            log_event(log_path, "dry_run_done", note="No real order was sent")
            return 0

        direction: Direction = args.direction
        _, broker_order_id = post_market_order(
            client,
            account_id,
            instrument,
            direction,
            int(args.qty),
            args.client_id_prefix,
            "entry",
            log_path,
        )
        lots, executed_price, status = wait_order_fill(client, account_id, broker_order_id, int(args.order_fill_timeout_sec), log_path)
        if lots <= 0:
            cancel_regular_if_any(client, account_id, broker_order_id, log_path, "entry_not_filled")
            raise RuntimeError(f"entry_not_filled status={status}")
        if lots != int(args.qty):
            log_event(log_path, "partial_fill_warning", requested=int(args.qty), filled=lots)

        stop_price = (
            executed_price - int(args.stop_ticks) * instrument.min_step
            if direction == "long"
            else executed_price + int(args.stop_ticks) * instrument.min_step
        )
        position = SmokePosition(
            direction=direction,
            qty=lots,
            entry_price=round_to_step(executed_price, instrument.min_step),
            best_price=round_to_step(executed_price, instrument.min_step),
            stop_price=round_to_step(stop_price, instrument.min_step),
        )
        log_event(log_path, "entry_filled", direction=direction, qty=lots, entry_price=position.entry_price, initial_stop=position.stop_price)
        post_stop_limit(client, account_id, instrument, position, args.client_id_prefix, int(args.stop_limit_offset_ticks), log_path)
        audit_duplicates(client, account_id, instrument, log_path, fail_on_duplicate=True)

        start = time.monotonic()
        last_replace = 0.0
        forced_replace_done = False
        while time.monotonic() - start < int(args.duration_sec):
            book = client.market_data.get_order_book(figi=instrument.figi, depth=int(args.orderbook_depth))
            levels = best_levels(book, instrument.min_step)
            mark = levels["bid"] if position.direction == "long" else levels["ask"]
            if not mark:
                time.sleep(1)
                continue
            moved = update_stop_from_price(position, instrument, float(mark), int(args.trail_ticks))
            if (
                not forced_replace_done
                and time.monotonic() - start >= int(args.force_replace_after_sec)
                and int(args.force_replace_after_sec) >= 0
            ):
                if position.direction == "long":
                    candidate = min(position.stop_price + instrument.min_step, float(mark) - instrument.min_step)
                    if candidate > position.stop_price:
                        position.stop_price = round_to_step(candidate, instrument.min_step)
                        moved = True
                else:
                    candidate = max(position.stop_price - instrument.min_step, float(mark) + instrument.min_step)
                    if candidate < position.stop_price:
                        position.stop_price = round_to_step(candidate, instrument.min_step)
                        moved = True
                forced_replace_done = True
                log_event(log_path, "forced_replace_test", mark=mark, new_stop=position.stop_price)
            if moved and time.monotonic() - last_replace >= float(args.min_stop_replace_interval_sec):
                old_stop_id = position.stop_order_id
                cancel_stop_if_any(client, account_id, old_stop_id, log_path, "trail_replace")
                post_stop_limit(client, account_id, instrument, position, args.client_id_prefix, int(args.stop_limit_offset_ticks), log_path)
                audit_duplicates(client, account_id, instrument, log_path, fail_on_duplicate=True)
                last_replace = time.monotonic()
            if emergency_stop_hit(position, instrument, float(mark), int(args.emergency_ticks)):
                log_event(log_path, "emergency_market_close", mark=mark, stop=position.stop_price)
                cancel_stop_if_any(client, account_id, position.stop_order_id, log_path, "emergency_market_close")
                close_direction: Direction = "short" if position.direction == "long" else "long"
                _, close_order_id = post_market_order(
                    client,
                    account_id,
                    instrument,
                    close_direction,
                    position.qty,
                    args.client_id_prefix,
                    "emerg",
                    log_path,
                )
                wait_order_fill(client, account_id, close_order_id, int(args.order_fill_timeout_sec), log_path)
                audit_duplicates(client, account_id, instrument, log_path, fail_on_duplicate=False)
                return 2
            log_event(log_path, "heartbeat", mark=mark, stop=position.stop_price, best=position.best_price, **levels)
            time.sleep(float(args.heartbeat_sec))

        cancel_stop_if_any(client, account_id, position.stop_order_id, log_path, "scheduled_smoke_end")
        close_direction = "short" if position.direction == "long" else "long"
        _, close_order_id = post_market_order(
            client,
            account_id,
            instrument,
            close_direction,
            position.qty,
            args.client_id_prefix,
            "exit",
            log_path,
        )
        wait_order_fill(client, account_id, close_order_id, int(args.order_fill_timeout_sec), log_path)
        audit_duplicates(client, account_id, instrument, log_path, fail_on_duplicate=False)
        log_event(log_path, "smoke_done", status="completed", duration_sec=int(args.duration_sec))
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="15-minute T-Bank real-order smoke test with hard safeguards.")
    parser.add_argument("--ticker", default="BRN6")
    parser.add_argument("--account-id", default="")
    parser.add_argument("--direction", choices=["long", "short"], default="long")
    parser.add_argument("--qty", type=int, default=1)
    parser.add_argument("--duration-sec", type=int, default=900)
    parser.add_argument("--stop-ticks", type=int, default=20)
    parser.add_argument("--trail-ticks", type=int, default=3)
    parser.add_argument("--stop-limit-offset-ticks", type=int, default=0)
    parser.add_argument("--emergency-ticks", type=int, default=2)
    parser.add_argument("--force-replace-after-sec", type=int, default=20)
    parser.add_argument("--min-stop-replace-interval-sec", type=float, default=3.0)
    parser.add_argument("--heartbeat-sec", type=float, default=2.0)
    parser.add_argument("--orderbook-depth", type=int, default=10)
    parser.add_argument("--max-spread-ticks", type=float, default=5.0)
    parser.add_argument("--order-fill-timeout-sec", type=int, default=20)
    parser.add_argument("--client-id-prefix", default="3pips-smoke")
    parser.add_argument("--log", default=str(REPORTS / "runtime" / f"live_smoke_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"))
    parser.add_argument("--allow-existing-instrument-orders", action="store_true")
    parser.add_argument("--real-orders", action="store_true")
    parser.add_argument("--confirm-real-orders", default="")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
