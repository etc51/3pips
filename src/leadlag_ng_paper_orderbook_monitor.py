from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import requests
from pandas.errors import ParserError
import statsmodels.api as sm

from leadlag_ng_moex import MONTH_CODES, REPORTS, ROOT, ensure_dirs, secid_for_month
from ng_scalper_bot import find_tbank_token, quotation_to_float, tbank_find_future


SNAPSHOTS_PATH = REPORTS / "live_orderbook_snapshots.csv"
TRADES_PATH = REPORTS / "paper_execution_trades.csv"
SUMMARY_PATH = REPORTS / "paper_execution_summary.csv"
BY_DAY_PATH = REPORTS / "paper_execution_by_day.csv"
DAILY_MD_PATH = REPORTS / "paper_execution_daily_summary.md"
HEARTBEAT_PATH = REPORTS / "paper_monitor_heartbeat.csv"
CONTRACT_SELECTION_PATH = REPORTS / "paper_contract_selection.csv"
OPEN_POSITIONS_PATH = REPORTS / "paper_open_positions.json"
PAUSE_NEW_ENTRIES_PATH = REPORTS / "paper_pause_new_entries.flag"
SHADOW_ONLY_PATH = REPORTS / "paper_shadow_only.flag"
FEATURES_PATH = ROOT / "data" / "processed" / "leadlag_ng_10m" / "features.csv"
SPECS_PATH = REPORTS / "unit_audit_contract_specs.csv"
FX_PATH = REPORTS / "unit_audit_fx_rates.csv"
SELECTION_LOG_PATH = REPORTS / "third_pass_feature_selection_log.csv"

MIN_STEP = 0.001
TICK_VALUE_USD = 0.1
HORIZON_MINUTES = 30
POLICIES = ["market_now", "passive_mid_or_better", "wait_5s_market", "wait_30s_market"]
TBANK_TOKEN_CACHE: str | None = None
TBANK_INSTRUMENT_CACHE: dict[str, dict] = {}


@dataclass(frozen=True)
class Config:
    once: bool
    interval_seconds: int
    max_spread_ticks: float
    passive_wait_seconds: int
    paper_contracts: int
    request_sleep: float
    stale_signal_minutes: int
    max_orderbook_age_seconds: int
    weekend_session: bool
    shadow_execution: bool
    orderbook_source: str
    heartbeat_seconds: int
    target_contract: str
    plus1_contract: str
    max_target_spread_ticks: float
    max_plus1_spread_ticks: float
    min_touch_size: float
    paper_only: bool
    reset_paper_day: bool
    force_reset_open_positions: bool
    run_id: str


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def read_csv(path: Path, date_cols: list[str] | None = None) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
    except ParserError:
        df = pd.read_csv(path, engine="python", on_bad_lines="skip")
    for col in date_cols or []:
        if col in df:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def append_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    if path.exists() and path.stat().st_size > 0:
        existing = read_csv(path)
        pd.concat([existing, df], ignore_index=True, sort=False).to_csv(path, index=False)
    else:
        df.to_csv(path, index=False)


def reset_paper_day(cfg: Config) -> None:
    if not cfg.reset_paper_day:
        return
    for path in [SNAPSHOTS_PATH, TRADES_PATH, SUMMARY_PATH, BY_DAY_PATH, DAILY_MD_PATH, HEARTBEAT_PATH, CONTRACT_SELECTION_PATH]:
        if path.exists():
            path.unlink()
    if OPEN_POSITIONS_PATH.exists():
        open_positions = json.loads(OPEN_POSITIONS_PATH.read_text(encoding="utf-8") or "[]")
        if open_positions and not cfg.force_reset_open_positions:
            log("Open paper positions exist; not resetting paper_open_positions.json without --force-reset-open-positions")
        else:
            OPEN_POSITIONS_PATH.write_text("[]", encoding="utf-8")


def request_json(url: str, params: dict | None = None) -> dict:
    response = requests.get(url, params=params or {}, timeout=30)
    response.raise_for_status()
    return response.json()


def iss_table(payload: dict, table: str) -> pd.DataFrame:
    block = payload.get(table, {})
    return pd.DataFrame(block.get("data", []), columns=block.get("columns", []))


def contract_months(current: pd.Timestamp) -> tuple[str, str]:
    month = pd.Timestamp(current).to_period("M").to_timestamp()
    return secid_for_month(month), secid_for_month(month + pd.DateOffset(months=1))


def msk_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="Europe/Moscow")


def session_state(cfg: Config, now: pd.Timestamp | None = None) -> dict:
    now = now or msk_now()
    if cfg.weekend_session:
        session_type = "weekend_dsvd"
        start_auction = pd.Timestamp(now.date(), tz=now.tz).replace(hour=9, minute=50, second=0)
        start_cont = pd.Timestamp(now.date(), tz=now.tz).replace(hour=10, minute=0, second=0)
        end_cont = pd.Timestamp(now.date(), tz=now.tz).replace(hour=18, minute=59, second=59)
        if start_auction <= now < start_cont:
            phase, can_open = "auction_open", False
        elif start_cont <= now <= end_cont:
            can_open = now + pd.Timedelta(minutes=HORIZON_MINUTES) <= end_cont
            phase = "continuous" if can_open else "closing_guard"
        else:
            phase, can_open = "closed", False
    else:
        session_type, phase, can_open = "weekday", "continuous", True
    return {"session_type": session_type, "session_phase": phase, "msk_time": now.isoformat(timespec="seconds"), "can_open_new_paper_trade": can_open}


def resolve_contracts(cfg: Config) -> tuple[str, str, str]:
    target_auto, plus1_auto = contract_months(pd.Timestamp.now())
    target = target_auto if cfg.target_contract.lower() == "auto" else cfg.target_contract
    plus1 = plus1_auto if cfg.plus1_contract.lower() == "auto" else cfg.plus1_contract
    method = "auto_front_plus1" if cfg.target_contract.lower() == "auto" and cfg.plus1_contract.lower() == "auto" else "manual_override"
    return target, plus1, method


def business_days_to_last_trade(target_contract: str, today: pd.Timestamp) -> int | None:
    specs = read_csv(SPECS_PATH)
    if specs.empty or "last_trade_date" not in specs:
        return None
    row = specs[specs["secid"] == target_contract]
    if row.empty or pd.isna(row.iloc[0]["last_trade_date"]):
        return None
    last = pd.Timestamp(row.iloc[0]["last_trade_date"]).normalize()
    start = pd.Timestamp(today).normalize()
    if last < start:
        return -1
    return int(np.busday_count(start.date(), (last + pd.Timedelta(days=1)).date()))


def market_snapshot(secid: str) -> dict:
    url = f"https://iss.moex.com/iss/engines/futures/markets/forts/securities/{secid}.json"
    payload = request_json(url, {"iss.meta": "off", "iss.only": "marketdata,securities"})
    md = iss_table(payload, "marketdata")
    sec = iss_table(payload, "securities")
    row = md.iloc[0].to_dict() if not md.empty else {}
    srow = sec.iloc[0].to_dict() if not sec.empty else {}
    bid = pd.to_numeric(row.get("BID"), errors="coerce")
    ask = pd.to_numeric(row.get("OFFER"), errors="coerce")
    last = pd.to_numeric(row.get("LAST"), errors="coerce")
    bid_size = pd.to_numeric(row.get("BIDDEPTH"), errors="coerce")
    ask_size = pd.to_numeric(row.get("OFFERDEPTH"), errors="coerce")
    spread_ticks = (ask - bid) / MIN_STEP if pd.notna(bid) and pd.notna(ask) else np.nan
    snapshot_ts = pd.Timestamp.now()
    trade_date = row.get("TRADEDATE")
    market_time = row.get("TIME") or row.get("UPDATETIME")
    market_dt = pd.NaT
    if trade_date and market_time:
        market_dt = pd.to_datetime(f"{trade_date} {market_time}", errors="coerce")
    age = (snapshot_ts - market_dt).total_seconds() if pd.notna(market_dt) else np.nan
    return {
        "snapshot_ts": snapshot_ts.isoformat(timespec="seconds"),
        "secid": secid,
        "last_price": last,
        "bid": bid,
        "ask": ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "spread_ticks": spread_ticks,
        "market_time": row.get("TIME"),
        "update_time": row.get("UPDATETIME"),
        "trade_date": row.get("TRADEDATE"),
        "market_datetime": market_dt,
        "age_seconds": age,
        "last_trade_date": srow.get("LASTTRADEDATE"),
        "raw_marketdata_json": json.dumps(row, ensure_ascii=False, default=str),
        "orderbook_source": "moex_iss_marketdata_top_of_book",
        "execution_validation_possible": bool(pd.notna(bid) and pd.notna(ask) and pd.notna(age) and age <= 30),
    }


def tbank_orderbook_snapshot(secid: str) -> dict:
    global TBANK_TOKEN_CACHE
    base = market_snapshot(secid)
    try:
        from t_tech.invest import Client

        if TBANK_TOKEN_CACHE is None:
            TBANK_TOKEN_CACHE = find_tbank_token()
        with Client(TBANK_TOKEN_CACHE) as client:
            info = TBANK_INSTRUMENT_CACHE.get(secid)
            if info is None:
                inst = tbank_find_future(client, secid)
                info = {"figi": inst.figi, "uid": inst.uid, "ticker": inst.ticker, "name": inst.name}
                TBANK_INSTRUMENT_CACHE[secid] = info
            ob = client.market_data.get_order_book(figi=info["figi"], depth=10)
            bids = list(getattr(ob, "bids", []) or [])
            asks = list(getattr(ob, "asks", []) or [])
            bid = quotation_to_float(bids[0].price) if bids else np.nan
            ask = quotation_to_float(asks[0].price) if asks else np.nan
            bid_size = int(getattr(bids[0], "quantity", 0) or 0) if bids else np.nan
            ask_size = int(getattr(asks[0], "quantity", 0) or 0) if asks else np.nan
            last = base["last_price"]
            try:
                prices = client.market_data.get_last_prices(figi=[info["figi"]]).last_prices
                if prices:
                    last = quotation_to_float(prices[0].price)
            except Exception:
                pass
            book_time = getattr(ob, "time", None)
            snapshot_ts = pd.Timestamp.now(tz="UTC")
            if book_time is not None:
                market_dt = pd.Timestamp(book_time)
                if market_dt.tzinfo is None:
                    market_dt = market_dt.tz_localize("UTC")
                age = float((snapshot_ts - market_dt.tz_convert("UTC")).total_seconds())
                market_time = market_dt.tz_convert("Europe/Moscow").isoformat(timespec="seconds")
            else:
                market_dt = snapshot_ts
                age = 0.0
                market_time = snapshot_ts.tz_convert("Europe/Moscow").isoformat(timespec="seconds")
            spread_ticks = (ask - bid) / MIN_STEP if pd.notna(bid) and pd.notna(ask) else np.nan
            return {
                **base,
                "last_price": last,
                "bid": bid,
                "ask": ask,
                "bid_size": bid_size,
                "ask_size": ask_size,
                "spread_ticks": spread_ticks,
                "market_time": market_time,
                "update_time": market_time,
                "market_datetime": market_dt,
                "age_seconds": age,
                "raw_marketdata_json": json.dumps({"tbank_instrument": info, "bids": len(bids), "asks": len(asks)}, ensure_ascii=False, default=str),
                "orderbook_source": "tbank_stream",
                "execution_validation_possible": bool(pd.notna(bid) and pd.notna(ask) and age <= 30),
            }
    except Exception as exc:
        return {
            **base,
            "orderbook_source": "tbank_stream_unavailable",
            "execution_validation_possible": False,
            "raw_marketdata_json": json.dumps({"tbank_error": f"{type(exc).__name__}: {exc}", "moex_fallback": base.get("raw_marketdata_json")}, ensure_ascii=False, default=str),
        }


def make_pair_stream_requests(instruments: list[dict], depth: int = 10):
    from t_tech.invest import (
        CandleInstrument,
        LastPriceInstrument,
        MarketDataRequest,
        OrderBookInstrument,
        SubscribeCandlesRequest,
        SubscribeLastPriceRequest,
        SubscribeOrderBookRequest,
        SubscriptionAction,
        SubscriptionInterval,
    )

    yield MarketDataRequest(
        subscribe_last_price_request=SubscribeLastPriceRequest(
            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[LastPriceInstrument(figi=x["figi"], instrument_id=x["uid"]) for x in instruments],
        )
    )
    yield MarketDataRequest(
        subscribe_order_book_request=SubscribeOrderBookRequest(
            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[OrderBookInstrument(figi=x["figi"], instrument_id=x["uid"], depth=depth) for x in instruments],
        )
    )
    yield MarketDataRequest(
        subscribe_candles_request=SubscribeCandlesRequest(
            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[
                CandleInstrument(
                    figi=x["figi"],
                    instrument_id=x["uid"],
                    interval=SubscriptionInterval.SUBSCRIPTION_INTERVAL_10_MIN,
                )
                for x in instruments
            ],
            waiting_close=True,
        )
    )
    while True:
        time.sleep(60)


def stream_orderbook_snapshot(secid: str, orderbook: object | None, last_price: float | None = None) -> dict:
    now_utc = pd.Timestamp.now(tz="UTC")
    bid = ask = bid_size = ask_size = np.nan
    book_time = now_utc
    if orderbook is not None:
        bids = list(getattr(orderbook, "bids", []) or [])
        asks = list(getattr(orderbook, "asks", []) or [])
        bid = quotation_to_float(bids[0].price) if bids else np.nan
        ask = quotation_to_float(asks[0].price) if asks else np.nan
        bid_size = int(getattr(bids[0], "quantity", 0) or 0) if bids else np.nan
        ask_size = int(getattr(asks[0], "quantity", 0) or 0) if asks else np.nan
        if getattr(orderbook, "time", None) is not None:
            book_time = pd.Timestamp(orderbook.time)
            if book_time.tzinfo is None:
                book_time = book_time.tz_localize("UTC")
    age = float((now_utc - book_time.tz_convert("UTC")).total_seconds())
    return {
        "secid": secid,
        "last_price": last_price if last_price is not None else np.nan,
        "bid": bid,
        "ask": ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "spread_ticks": (ask - bid) / MIN_STEP if pd.notna(bid) and pd.notna(ask) else np.nan,
        "market_time": book_time.tz_convert("Europe/Moscow").isoformat(timespec="seconds"),
        "update_time": book_time.tz_convert("Europe/Moscow").isoformat(timespec="seconds"),
        "trade_date": book_time.tz_convert("Europe/Moscow").date().isoformat(),
        "market_datetime": book_time,
        "age_seconds": age,
        "last_trade_date": None,
        "raw_marketdata_json": json.dumps({"stream_orderbook": orderbook is not None}, ensure_ascii=False, default=str),
        "orderbook_source": "tbank_stream",
        "execution_validation_possible": bool(pd.notna(bid) and pd.notna(ask) and age <= 30),
    }


def candle_to_stream_row(candle: object) -> dict:
    begin = pd.Timestamp(candle.time)
    if begin.tzinfo is None:
        begin = begin.tz_localize("UTC")
    begin = begin.tz_convert("Europe/Moscow").tz_localize(None)
    return {
        "open": quotation_to_float(candle.open),
        "close": quotation_to_float(candle.close),
        "high": quotation_to_float(candle.high),
        "low": quotation_to_float(candle.low),
        "volume": int(candle.volume),
        "begin": begin,
    }


def latest_10m_candles(secid: str, lookback_days: int = 10) -> pd.DataFrame:
    till = pd.Timestamp.today().strftime("%Y-%m-%d")
    start = (pd.Timestamp.today() - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    url = f"https://iss.moex.com/iss/engines/futures/markets/forts/securities/{secid}/candles.json"
    pages = []
    offset = 0
    while True:
        payload = request_json(url, {"interval": 10, "from": start, "till": till, "start": offset, "iss.meta": "off"})
        page = iss_table(payload, "candles")
        if page.empty:
            break
        pages.append(page)
        offset += len(page)
    df = pd.concat(pages, ignore_index=True) if pages else pd.DataFrame()
    if df.empty:
        return df
    df["begin"] = pd.to_datetime(df["begin"])
    for col in ["open", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("begin")


def latest_tbank_10m_candles(secid: str, lookback_hours: int = 8) -> pd.DataFrame:
    global TBANK_TOKEN_CACHE
    from t_tech.invest import CandleInterval, Client

    if TBANK_TOKEN_CACHE is None:
        TBANK_TOKEN_CACHE = find_tbank_token()
    to = datetime.now(timezone.utc)
    from_ = to - pd.Timedelta(hours=lookback_hours)
    with Client(TBANK_TOKEN_CACHE) as client:
        info = TBANK_INSTRUMENT_CACHE.get(secid)
        if info is None:
            inst = tbank_find_future(client, secid)
            info = {"figi": inst.figi, "uid": inst.uid, "ticker": inst.ticker, "name": inst.name}
            TBANK_INSTRUMENT_CACHE[secid] = info
        resp = client.market_data.get_candles(
            figi=info["figi"],
            from_=from_,
            to=to,
            interval=CandleInterval.CANDLE_INTERVAL_10_MIN,
        )
    rows = []
    for candle in resp.candles:
        begin = pd.Timestamp(candle.time)
        if begin.tzinfo is None:
            begin = begin.tz_localize("UTC")
        begin = begin.tz_convert("Europe/Moscow").tz_localize(None)
        rows.append(
            {
                "open": quotation_to_float(candle.open),
                "close": quotation_to_float(candle.close),
                "high": quotation_to_float(candle.high),
                "low": quotation_to_float(candle.low),
                "value": np.nan,
                "volume": int(candle.volume),
                "begin": begin,
                "end": begin + pd.Timedelta(minutes=10) - pd.Timedelta(seconds=1),
            }
        )
    return pd.DataFrame(rows).sort_values("begin") if rows else pd.DataFrame()


def train_plus1_model(target_month: str) -> tuple[sm.regression.linear_model.RegressionResultsWrapper, float, str]:
    features = read_csv(FEATURES_PATH, ["begin"])
    if features.empty:
        raise RuntimeError("Missing features.csv. Run lead-lag pipeline first.")
    train = features[features["target_month"] < target_month][["ret_front_30m", "ret_plus1_lag0"]].dropna()
    if len(train) < 100:
        raise RuntimeError(f"Too few train rows before {target_month}: {len(train)}")
    model = sm.OLS(train["ret_front_30m"], sm.add_constant(train[["ret_plus1_lag0"]], has_constant="add")).fit()
    threshold = 0.0
    threshold_type = "zero_fallback"
    sel = read_csv(SELECTION_LOG_PATH)
    if not sel.empty:
        row = sel[
            (sel["strategy_mode"] == "fixed_plus1_only")
            & (sel["test_month"] == target_month)
            & (sel["cost_scenario"] == "2ticks_2rub")
            & (sel["threshold_objective"] == "train_mean")
        ]
        if not row.empty:
            threshold = float(row.iloc[0]["selected_threshold"])
            threshold_type = str(row.iloc[0]["selected_threshold_type"])
    return model, threshold, threshold_type


def compute_signal(target_contract: str, plus1_contract: str, target_month: str, candle_source: str = "moex-iss") -> dict:
    if candle_source == "tbank-stream":
        try:
            target = latest_tbank_10m_candles(target_contract)
            plus1 = latest_tbank_10m_candles(plus1_contract)
        except Exception as exc:
            return {"signal_status": "skipped", "skip_reason": "tbank_candles_unavailable", "candle_source": "tbank", "candle_error": f"{type(exc).__name__}: {exc}"}
    else:
        target = latest_10m_candles(target_contract)
        plus1 = latest_10m_candles(plus1_contract)
    if target.empty or plus1.empty:
        return {"signal_status": "skipped", "skip_reason": "missing_10m_candles", "candle_source": "tbank" if candle_source == "tbank-stream" else "moex_iss"}
    merged = target[["begin", "close", "volume"]].rename(columns={"close": "close_front", "volume": "volume_front"}).merge(
        plus1[["begin", "open", "close", "volume"]].rename(columns={"open": "open_plus1", "close": "close_plus1", "volume": "volume_plus1"}),
        on="begin",
        how="inner",
    )
    merged = merged[(merged["volume_front"] > 0) & (merged["volume_plus1"] > 0)].sort_values("begin")
    if merged.empty:
        return {"signal_status": "skipped", "skip_reason": "no_aligned_nonzero_10m_candle"}
    now_naive = pd.Timestamp.now().tz_localize(None)
    merged["candle_end"] = pd.to_datetime(merged["begin"]) + pd.Timedelta(minutes=10)
    closed = merged[merged["candle_end"] <= now_naive].copy()
    if closed.empty:
        row = merged.iloc[-1]
        candle_end = pd.Timestamp(row["begin"]) + pd.Timedelta(minutes=10)
        candle_age = (now_naive - candle_end).total_seconds()
        return {"signal_status": "skipped", "skip_reason": "incomplete_candle", "candle_begin": row["begin"], "candle_end": candle_end, "candle_age_seconds": candle_age, "candle_source": "tbank" if candle_source == "tbank-stream" else "moex_iss"}
    row = closed.iloc[-1]
    candle_end = pd.Timestamp(row["candle_end"])
    candle_age = (now_naive - candle_end).total_seconds()
    if candle_age > 90:
        return {"signal_status": "skipped", "skip_reason": "stale_candle", "candle_begin": row["begin"], "candle_end": candle_end, "candle_age_seconds": candle_age, "candle_source": "tbank" if candle_source == "tbank-stream" else "moex_iss"}
    model, threshold, threshold_type = train_plus1_model(target_month)
    x = pd.DataFrame({"const": [1.0], "ret_plus1_lag0": [math.log(row["close_plus1"] / row["open_plus1"])]})
    prediction = float(model.predict(x)[0])
    direction = 1 if prediction > threshold else (-1 if prediction < -threshold else 0)
    return {
        "signal_status": "ok" if direction != 0 else "flat",
        "skip_reason": "" if direction != 0 else "below_threshold",
        "timestamp_signal": pd.Timestamp(row["begin"]) + pd.Timedelta(minutes=10),
        "candle_begin": row["begin"],
        "candle_end": candle_end,
        "signal_compute_time": pd.Timestamp.now().isoformat(timespec="seconds"),
        "candle_age_seconds": candle_age,
        "candle_source": "tbank" if candle_source == "tbank-stream" else "moex_iss",
        "target_contract": target_contract,
        "plus1_contract": plus1_contract,
        "prediction": prediction,
        "signal_direction": direction,
        "selected_threshold": threshold,
        "selected_threshold_type": threshold_type,
        "signal_passed_threshold": bool(direction != 0),
        "abs_prediction": abs(prediction),
        "threshold_ratio": abs(prediction) / threshold if threshold else np.inf,
        "last_close_target_10m": row["close_front"],
    }


def usd_rub_rate() -> tuple[float, str]:
    fx = read_csv(REPORTS / "unit_audit_fx_rates.csv", ["trade_date"])
    if fx.empty:
        return np.nan, "missing_fx"
    row = fx.sort_values("trade_date").iloc[-1]
    return float(row["usd_rub_rate"]), str(row["fx_source"])


def current_margin(target_contract: str) -> tuple[float, str]:
    specs = read_csv(SPECS_PATH)
    row = specs[specs["secid"] == target_contract] if not specs.empty else pd.DataFrame()
    if row.empty:
        return 15_000.0, "fallback_15000"
    return float(row.iloc[0].get("initial_margin_rub", 15_000.0)), str(row.iloc[0].get("spec_source", "unknown"))


def snapshot_pair(target_contract: str, plus1_contract: str, orderbook_source: str = "moex-iss") -> tuple[dict, dict, dict]:
    if orderbook_source == "tbank-stream":
        target = tbank_orderbook_snapshot(target_contract)
        plus1 = tbank_orderbook_snapshot(plus1_contract)
    else:
        target = market_snapshot(target_contract)
        plus1 = market_snapshot(plus1_contract)
    row = {
        "snapshot_group_ts": datetime.now().isoformat(timespec="seconds"),
        "target_contract": target_contract,
        "plus1_contract": plus1_contract,
        "last_price_target": target["last_price"],
        "bid_target": target["bid"],
        "ask_target": target["ask"],
        "bid_size_target": target["bid_size"],
        "ask_size_target": target["ask_size"],
        "spread_ticks_target": target["spread_ticks"],
        "last_price_plus1": plus1["last_price"],
        "bid_plus1": plus1["bid"],
        "ask_plus1": plus1["ask"],
        "bid_size_plus1": plus1["bid_size"],
        "ask_size_plus1": plus1["ask_size"],
        "spread_ticks_plus1": plus1["spread_ticks"],
        "target_market_time": target["market_time"],
        "plus1_market_time": plus1["market_time"],
        "snapshot_time": datetime.now().isoformat(timespec="seconds"),
        "target_age_seconds": target["age_seconds"],
        "plus1_age_seconds": plus1["age_seconds"],
        "orderbook_source": target["orderbook_source"] if target["orderbook_source"] == plus1["orderbook_source"] else f"{target['orderbook_source']}/{plus1['orderbook_source']}",
        "execution_validation_possible": bool(target["execution_validation_possible"] and plus1["execution_validation_possible"]),
    }
    return target, plus1, row


def write_contract_selection(target_contract: str, plus1_contract: str, method: str, target_snap: dict, plus1_snap: dict, cfg: Config) -> None:
    source = cfg.orderbook_source
    if cfg.orderbook_source == "tbank-stream" and not (
        pd.notna(target_snap.get("bid")) and pd.notna(target_snap.get("ask")) and pd.notna(plus1_snap.get("bid")) and pd.notna(plus1_snap.get("ask"))
    ):
        source = "tbank_stream_unavailable"
    pd.DataFrame([{
        "run_date": pd.Timestamp.now().date().isoformat(),
        "target_contract": target_contract,
        "plus1_contract": plus1_contract,
        "selection_method": method,
        "dsvd_access_target": "unavailable",
        "dsvd_access_plus1": "unavailable",
        "dsvd_access_source": "unavailable",
        "relying_on_live_orderbook": cfg.orderbook_source == "tbank-stream",
        "last_price_target": target_snap.get("last_price"),
        "last_price_plus1": plus1_snap.get("last_price"),
        "has_orderbook_target": bool(pd.notna(target_snap.get("bid")) and pd.notna(target_snap.get("ask"))),
        "has_orderbook_plus1": bool(pd.notna(plus1_snap.get("bid")) and pd.notna(plus1_snap.get("ask"))),
        "selected_ok": True,
        "warning": "" if source == "tbank-stream" else "NO LIVE ORDERBOOK - execution test not meaningful",
        "orderbook_source_effective": source,
        "run_id": cfg.run_id,
    }]).to_csv(CONTRACT_SELECTION_PATH, index=False)


def entry_price_for_policy(policy: str, direction: int, snap: dict, cfg: Config) -> tuple[float, str, float, bool, bool]:
    if cfg.orderbook_source != "tbank-stream":
        return np.nan, "ORDERBOOK_SOURCE_NOT_EXECUTABLE", 0.0, False, False
    bid = snap["bid"]
    ask = snap["ask"]
    if pd.isna(bid) or pd.isna(ask):
        return np.nan, "missing_bid_ask", 0.0, False, False
    bid_size = 0.0 if pd.isna(snap["bid_size"]) else float(snap["bid_size"])
    ask_size = 0.0 if pd.isna(snap["ask_size"]) else float(snap["ask_size"])
    if policy == "market_now":
        price = ask if direction > 0 else bid
        size = ask_size if direction > 0 else bid_size
        return float(price), "FILLED", 0.0, size >= 1, size >= cfg.paper_contracts
    if policy == "passive_mid_or_better":
        mid = (bid + ask) / 2
        limit = min(mid, bid) if direction > 0 else max(mid, ask)
        time.sleep(max(0, cfg.passive_wait_seconds))
        later = snap
        would_fill = (later["ask"] <= limit) if direction > 0 else (later["bid"] >= limit)
        status = "FILLED" if would_fill else "UNFILLED"
        size = ask_size if direction > 0 else bid_size
        return float(limit), status, float(cfg.passive_wait_seconds), bool(would_fill and size >= 1), bool(would_fill and size >= cfg.paper_contracts)
    if policy in {"wait_5s_market", "wait_30s_market"}:
        wait = 5 if policy == "wait_5s_market" else 30
        time.sleep(wait)
        later = tbank_orderbook_snapshot(snap["secid"]) if cfg.orderbook_source == "tbank-stream" else market_snapshot(snap["secid"])
        if pd.isna(later["bid"]) or pd.isna(later["ask"]):
            return np.nan, "missing_bid_ask_after_wait", float(wait), False, False
        price = later["ask"] if direction > 0 else later["bid"]
        size = later["ask_size"] if direction > 0 else later["bid_size"]
        size = 0.0 if pd.isna(size) else float(size)
        return float(price), "FILLED", float(wait), size >= 1, size >= cfg.paper_contracts
    return np.nan, "unknown_policy", 0.0, False, False


def close_due_trades(cfg: Config) -> int:
    trades = read_csv(TRADES_PATH, ["timestamp_signal", "entry_time", "planned_exit_time", "exit_time"])
    if trades.empty or "fill_status" not in trades:
        return 0
    if "planned_exit_time" not in trades:
        return 0
    now = pd.Timestamp.now()
    due = trades[(trades["fill_status"] == "OPEN") & (trades["planned_exit_time"] <= now)].copy()
    if due.empty:
        return 0
    updates = []
    for idx, row in due.iterrows():
        snap = tbank_orderbook_snapshot(row["target_contract"]) if cfg.orderbook_source == "tbank-stream" else market_snapshot(row["target_contract"])
        direction = int(row["signal_direction"])
        if pd.isna(snap["bid"]) or pd.isna(snap["ask"]):
            continue
        exit_price = snap["bid"] if direction > 0 else snap["ask"]
        raw_ticks = (exit_price - row["entry_price"]) / MIN_STEP
        signed_ticks = direction * raw_ticks
        tick_value_rub = TICK_VALUE_USD * row["usd_rub_rate"]
        gross_pnl = signed_ticks * tick_value_rub
        spread_paid_ticks = float(row.get("spread_paid_ticks", 0.0))
        slippage_ticks = float(row.get("slippage_ticks", 0.0))
        fee = 2.0
        net_pnl = gross_pnl - (spread_paid_ticks + slippage_ticks) * tick_value_rub - fee
        trades.loc[idx, "exit_time"] = pd.Timestamp.now()
        trades.loc[idx, "exit_price"] = exit_price
        trades.loc[idx, "exit_bid"] = snap["bid"]
        trades.loc[idx, "exit_ask"] = snap["ask"]
        trades.loc[idx, "exit_orderbook_age_seconds"] = snap["age_seconds"]
        trades.loc[idx, "gross_ticks"] = signed_ticks
        trades.loc[idx, "net_ticks"] = net_pnl / tick_value_rub if tick_value_rub else np.nan
        trades.loc[idx, "gross_pnl_rub"] = gross_pnl
        trades.loc[idx, "net_pnl_rub"] = net_pnl
        trades.loc[idx, "return_on_go"] = net_pnl / row["initial_margin_rub"]
        trades.loc[idx, "fill_status"] = "CLOSED"
        trades.loc[idx, "exit_reason"] = "scheduled_paper_exit"
        trades.loc[idx, "real_order_sent"] = False
        updates.append(idx)
    if updates:
        trades.to_csv(TRADES_PATH, index=False)
    return len(updates)


def signal_from_stream_candles(target_contract: str, plus1_contract: str, target_month: str, target_row: dict, plus1_row: dict) -> dict:
    if pd.Timestamp(target_row["begin"]) != pd.Timestamp(plus1_row["begin"]):
        return {"signal_status": "skipped", "skip_reason": "stream_candle_not_aligned", "candle_source": "tbank_stream"}
    candle_end = pd.Timestamp(target_row["begin"]) + pd.Timedelta(minutes=10)
    now_naive = pd.Timestamp.now().tz_localize(None)
    candle_age = (now_naive - candle_end).total_seconds()
    if candle_age < -5:
        return {"signal_status": "skipped", "skip_reason": "incomplete_candle", "candle_begin": target_row["begin"], "candle_end": candle_end, "candle_age_seconds": candle_age, "candle_source": "tbank_stream"}
    if candle_age > 90:
        return {"signal_status": "skipped", "skip_reason": "stale_candle", "candle_begin": target_row["begin"], "candle_end": candle_end, "candle_age_seconds": candle_age, "candle_source": "tbank_stream"}
    model, threshold, threshold_type = train_plus1_model(target_month)
    x = pd.DataFrame({"const": [1.0], "ret_plus1_lag0": [math.log(float(plus1_row["close"]) / float(plus1_row["open"]))]})
    prediction = float(model.predict(x)[0])
    direction = 1 if prediction > threshold else (-1 if prediction < -threshold else 0)
    return {
        "signal_status": "ok" if direction != 0 else "flat",
        "skip_reason": "" if direction != 0 else "below_threshold",
        "timestamp_signal": candle_end,
        "candle_begin": target_row["begin"],
        "candle_end": candle_end,
        "signal_compute_time": pd.Timestamp.now().isoformat(timespec="seconds"),
        "candle_age_seconds": candle_age,
        "candle_source": "tbank_stream",
        "target_contract": target_contract,
        "plus1_contract": plus1_contract,
        "prediction": prediction,
        "signal_direction": direction,
        "selected_threshold": threshold,
        "selected_threshold_type": threshold_type,
        "signal_passed_threshold": bool(direction != 0),
        "abs_prediction": abs(prediction),
        "threshold_ratio": abs(prediction) / threshold if threshold else np.inf,
    }


def create_signal_trades(signal: dict, target_snap: dict, plus1_snap: dict, snapshot_row: dict, cfg: Config) -> list[dict]:
    rows = []
    signal_id = signal.get("signal_id")
    execution_base = {**signal, **snapshot_row, "signal_id": signal_id}
    is_shadow = bool(cfg.shadow_execution and not signal.get("signal_passed_threshold", False) and signal.get("signal_status") in {"flat", "ok", "shadow"})
    if SHADOW_ONLY_PATH.exists() and cfg.shadow_execution and signal.get("signal_status") == "ok":
        signal = {**signal, "signal_passed_threshold": False, "shadow_only_mode": True}
        is_shadow = True
    if signal["signal_status"] != "ok":
        if is_shadow and not signal.get("stale_signal", False):
            signal = {**signal, "signal_status": "ok", "signal_direction": 1 if signal.get("prediction", 0) >= 0 else -1}
        else:
            base = {**signal, **snapshot_row, "signal_id": signal_id, "execution_policy": "none", "entry_time": pd.NaT, "planned_exit_time": pd.NaT, "exit_time": pd.NaT, "fill_status": "SKIPPED", "skip_reason": signal["skip_reason"], "is_shadow": False}
            rows.append(base)
            return rows
    if signal.get("stale_signal", False):
        rows.append({**signal, **snapshot_row, "signal_id": signal_id, "execution_policy": "none", "fill_status": "SKIPPED", "skip_reason": "stale_signal", "is_shadow": False})
        return rows
    if snapshot_row.get("session_phase") == "auction_open":
        rows.append({**signal, **snapshot_row, "signal_id": signal_id, "execution_policy": "none", "fill_status": "SKIPPED", "skip_reason": "auction_open_no_entry", "is_shadow": is_shadow})
        return rows
    if not bool(snapshot_row.get("can_open_new_paper_trade", True)):
        rows.append({**signal, **snapshot_row, "signal_id": signal_id, "execution_policy": "none", "fill_status": "SKIPPED", "skip_reason": "exit_after_session_end", "is_shadow": is_shadow})
        return rows
    target_missing = pd.isna(target_snap["bid"]) or pd.isna(target_snap["ask"])
    plus1_missing = pd.isna(plus1_snap["bid"]) or pd.isna(plus1_snap["ask"])
    if target_missing and plus1_missing:
        rows.append({**signal, **snapshot_row, "signal_id": signal_id, "execution_policy": "none", "fill_status": "SKIPPED", "skip_reason": "no_orderbook", "orderbook_source": "unavailable", "execution_validation_possible": False, "is_shadow": is_shadow})
        return rows
    if target_missing:
        rows.append({**signal, **snapshot_row, "signal_id": signal_id, "execution_policy": "none", "fill_status": "SKIPPED", "skip_reason": "no_target_bid_ask", "orderbook_source": "unavailable", "execution_validation_possible": False, "is_shadow": is_shadow})
        return rows
    if plus1_missing:
        rows.append({**signal, **snapshot_row, "signal_id": signal_id, "execution_policy": "none", "fill_status": "SKIPPED", "skip_reason": "no_plus1_bid_ask", "orderbook_source": "unavailable", "execution_validation_possible": False, "is_shadow": is_shadow})
        return rows
    if pd.isna(target_snap["age_seconds"]) or pd.isna(plus1_snap["age_seconds"]) or target_snap["age_seconds"] > cfg.max_orderbook_age_seconds or plus1_snap["age_seconds"] > cfg.max_orderbook_age_seconds:
        rows.append({**signal, **snapshot_row, "signal_id": signal_id, "execution_policy": "none", "fill_status": "SKIPPED", "skip_reason": "stale_orderbook", "execution_validation_possible": False, "is_shadow": is_shadow})
        return rows
    if target_snap["spread_ticks"] > cfg.max_target_spread_ticks or plus1_snap["spread_ticks"] > cfg.max_plus1_spread_ticks:
        rows.append({**signal, **snapshot_row, "signal_id": signal_id, "execution_policy": "none", "fill_status": "SKIPPED", "skip_reason": "spread_too_wide", "is_shadow": is_shadow})
        return rows
    if pd.isna(target_snap["bid"]) or pd.isna(target_snap["ask"]) or pd.isna(plus1_snap["bid"]) or pd.isna(plus1_snap["ask"]):
        base = {**signal, **snapshot_row, "execution_policy": "none", "entry_time": pd.NaT, "planned_exit_time": pd.NaT, "exit_time": pd.NaT, "fill_status": "SKIPPED", "skip_reason": "missing_bid_ask_or_empty_book"}
        rows.append(base)
        return rows
    if target_snap["spread_ticks"] > cfg.max_spread_ticks:
        base = {**signal, **snapshot_row, "execution_policy": "none", "entry_time": pd.NaT, "planned_exit_time": pd.NaT, "exit_time": pd.NaT, "fill_status": "SKIPPED", "skip_reason": "target_spread_too_wide"}
        rows.append(base)
        return rows
    if PAUSE_NEW_ENTRIES_PATH.exists():
        rows.append({
            **execution_base,
            "execution_policy": "none",
            "execution_decision_id": f"{signal_id}|none",
            "entry_time": pd.NaT,
            "planned_exit_time": pd.NaT,
            "exit_time": pd.NaT,
            "fill_status": "SKIPPED",
            "skip_reason": "paper_new_entries_paused",
            "paper_new_entries_paused": True,
            "paper_only": True,
            "is_shadow": is_shadow,
        })
        return rows
    margin, margin_source = current_margin(signal["target_contract"])
    fx, fx_source = usd_rub_rate()
    mid = (target_snap["bid"] + target_snap["ask"]) / 2
    for policy in POLICIES:
        entry_price, status, wait_seconds, fill1, filln = entry_price_for_policy(policy, int(signal["signal_direction"]), target_snap, cfg)
        spread_paid = abs(entry_price - mid) / MIN_STEP if pd.notna(entry_price) and pd.notna(mid) else np.nan
        row = {
            **signal,
            **snapshot_row,
            "execution_policy": policy,
            "execution_decision_id": f"{signal_id}|{policy}",
            "entry_time": pd.Timestamp.now(),
            "planned_exit_time": pd.Timestamp.now() + pd.Timedelta(minutes=HORIZON_MINUTES),
            "entry_price": entry_price,
            "exit_time": pd.NaT,
            "exit_price": np.nan,
            "gross_ticks": np.nan,
            "spread_paid_ticks": spread_paid,
            "slippage_ticks": 0.0,
            "net_ticks": np.nan,
            "usd_rub_rate": fx,
            "fx_source": fx_source,
            "gross_pnl_rub": np.nan,
            "net_pnl_rub": np.nan,
            "initial_margin_rub": margin,
            "margin_source": margin_source,
            "return_on_go": np.nan,
            "fill_status": "OPEN" if status == "FILLED" else status,
            "time_to_fill_seconds": wait_seconds,
            "available_size_at_touch": target_snap["ask_size"] if int(signal["signal_direction"]) > 0 else target_snap["bid_size"],
            "would_fill_1_contract": fill1,
            "would_fill_N_contracts": filln,
            "skip_reason": "" if status == "FILLED" else status,
            "paper_only": True,
            "is_shadow": is_shadow,
            "paper_new_entries_paused": False,
        }
        rows.append(row)
    return rows


def summarize() -> None:
    trades = read_csv(TRADES_PATH, ["timestamp_signal", "entry_time", "planned_exit_time", "exit_time"])
    if "skip_reason" in trades:
        diagnostic_mask = trades["skip_reason"].fillna("").isin(["market_closed_or_no_live_book"]) | trades.get("timestamp_signal", pd.Series(index=trades.index)).isna()
        if diagnostic_mask.any():
            trades = trades[~diagnostic_mask].copy()
            trades.to_csv(TRADES_PATH, index=False)
    snapshots = read_csv(SNAPSHOTS_PATH)
    raw_snapshots = len(snapshots)
    snapshot_orderbook_missing = 0
    if not snapshots.empty:
        snapshot_orderbook_missing = int(
            (
                snapshots.get("bid_target", pd.Series(index=snapshots.index, dtype=float)).isna()
                | snapshots.get("ask_target", pd.Series(index=snapshots.index, dtype=float)).isna()
                | snapshots.get("bid_plus1", pd.Series(index=snapshots.index, dtype=float)).isna()
                | snapshots.get("ask_plus1", pd.Series(index=snapshots.index, dtype=float)).isna()
            ).sum()
        )
    fresh_bidask = raw_snapshots - snapshot_orderbook_missing
    live_orderbook_coverage_pct = float(fresh_bidask / raw_snapshots) if raw_snapshots else 0.0
    if trades.empty:
        empty_trade_cols = [
            "timestamp_signal",
            "signal_id",
            "target_contract",
            "plus1_contract",
            "execution_policy",
            "fill_status",
            "skip_reason",
            "is_shadow",
        ]
        pd.DataFrame(columns=empty_trade_cols).to_csv(TRADES_PATH, index=False)
        snapshot_reason = snapshots.get("skip_reason", pd.Series("", index=snapshots.index)).fillna("") if not snapshots.empty else pd.Series(dtype=str)
        meta = {
            "execution_policy": "__overall__",
            "raw_snapshots": raw_snapshots,
            "unique_signal_ids": 0,
            "valid_live_signals": 0,
            "below_threshold_signals": 0,
            "stale_signals": int((snapshot_reason == "stale_signal").sum()),
            "orderbook_missing": snapshot_orderbook_missing,
            "live_orderbook_coverage_pct": live_orderbook_coverage_pct,
            "executable_signals": 0,
            "opened_trades": 0,
            "closed_trades": 0,
            "strategy_signals": 0,
            "strategy_opened_trades": 0,
            "strategy_closed_trades": 0,
            "shadow_signals": 0,
            "shadow_opened_trades": 0,
            "shadow_closed_trades": 0,
            "net_pnl_rub": 0.0,
            "execution_test_meaningful": False,
        }
        pd.DataFrame([meta]).to_csv(SUMMARY_PATH, index=False)
        pd.DataFrame(columns=["day", "execution_policy", "closed_trades", "net_pnl_rub", "avg_net_ticks", "positive_trades"]).to_csv(BY_DAY_PATH, index=False)
        lines = [
            "# Paper execution daily summary",
            "",
            "NO LIVE ORDERBOOK - execution test not meaningful" if live_orderbook_coverage_pct < 0.8 else "No executable paper trades yet.",
            "",
            f"Updated: {datetime.now().isoformat(timespec='seconds')}",
            "",
            f"- raw snapshots: {raw_snapshots}",
            "- unique signal ids: 0",
            "- valid live signals: 0",
            "- below threshold: 0",
            f"- stale signals: {int((snapshot_reason == 'stale_signal').sum())}",
            f"- missing orderbook: {snapshot_orderbook_missing}",
            f"- live orderbook coverage pct: {live_orderbook_coverage_pct:.2%}",
            "- executable signals: 0",
            "- opened trades: 0",
            "- closed trades: 0",
            "- strategy signals: 0",
            "- strategy opened trades: 0",
            "- strategy closed trades: 0",
            "- shadow signals: 0",
            "- shadow opened trades: 0",
            "- shadow closed trades: 0",
            "- net PnL RUB closed: 0.00",
            "- execution test meaningful: False",
            "",
            "Market-closed/no-live-book diagnostics are written only to snapshots/heartbeat, not to paper trade rows.",
            "Paper only. No real orders are sent.",
        ]
        DAILY_MD_PATH.write_text("\n".join(lines), encoding="utf-8")
        return
    if "signal_id" not in trades and {"target_contract", "plus1_contract", "candle_begin", "timestamp_signal"}.issubset(trades.columns):
        trades["signal_id"] = trades["target_contract"].astype(str) + "|" + trades["plus1_contract"].astype(str) + "|" + trades["candle_begin"].astype(str) + "|" + trades["timestamp_signal"].astype(str)
    if "execution_policy" not in trades:
        trades["execution_policy"] = "none"
    trades["execution_policy"] = trades["execution_policy"].fillna("none")
    closed = trades[trades["fill_status"] == "CLOSED"].copy()
    grouped = []
    for policy, g in trades.groupby("execution_policy", dropna=False):
        c = closed[closed["execution_policy"] == policy]
        grouped.append(
            {
                "execution_policy": policy,
                "signals": int(g["timestamp_signal"].nunique()) if "timestamp_signal" in g else 0,
                "fills": int((g["fill_status"].isin(["OPEN", "CLOSED"])).sum()),
                "skipped": int((g["fill_status"].astype(str).str.startswith("SKIPPED") | g["fill_status"].isin(["UNFILLED", "missing_bid_ask_after_wait"])).sum()),
                "avg_spread_ticks": float(g["spread_ticks_target"].mean()) if "spread_ticks_target" in g else np.nan,
                "avg_net_ticks": float(c["net_ticks"].mean()) if not c.empty else np.nan,
                "net_pnl_rub": float(c["net_pnl_rub"].sum()) if not c.empty else 0.0,
                "positive_trades": int((c["net_pnl_rub"] > 0).sum()) if not c.empty else 0,
                "closed_trades": len(c),
                "passes_2ticks_2rub_fee": bool(not c.empty and c["net_pnl_rub"].sum() > 0 and (c["net_pnl_rub"] > 0).mean() > 0.5),
            }
        )
    summary_df = pd.DataFrame(grouped)
    reason = trades.get("skip_reason", pd.Series("", index=trades.index)).fillna("")
    valid_signal_id_mask = pd.Series(False, index=trades.index)
    if "signal_id" in trades and "timestamp_signal" in trades:
        valid_signal_id_mask = trades["signal_id"].notna() & trades["timestamp_signal"].notna() & ~reason.isin(["market_closed_or_no_live_book"])
    unique_signal_ids = int(trades.loc[valid_signal_id_mask, "signal_id"].nunique()) if "signal_id" in trades else 0
    valid_live_signals = int(((reason == "") & trades["fill_status"].isin(["OPEN", "CLOSED"])).sum())
    is_shadow = trades.get("is_shadow", pd.Series(False, index=trades.index)).fillna(False).astype(bool)
    meta = {
        "execution_policy": "__overall__",
        "run_id": str(trades.get("run_id", pd.Series([""])).dropna().iloc[-1]) if "run_id" in trades and not trades.get("run_id", pd.Series(dtype=str)).dropna().empty else "",
        "raw_snapshots": raw_snapshots,
        "unique_signal_ids": unique_signal_ids,
        "valid_live_signals": valid_live_signals,
        "below_threshold_signals": int((reason == "below_threshold").sum()),
        "stale_signals": int((reason == "stale_signal").sum()),
        "orderbook_missing": snapshot_orderbook_missing,
        "live_orderbook_coverage_pct": live_orderbook_coverage_pct,
        "executable_signals": int(trades["fill_status"].isin(["OPEN", "CLOSED"]).sum()),
        "opened_trades": int((trades["fill_status"] == "OPEN").sum()),
        "closed_trades": int((trades["fill_status"] == "CLOSED").sum()),
        "strategy_signals": int((~is_shadow & trades["signal_status"].eq("ok")).sum()) if "signal_status" in trades else 0,
        "strategy_opened_trades": int((~is_shadow & (trades["fill_status"] == "OPEN")).sum()),
        "strategy_closed_trades": int((~is_shadow & (trades["fill_status"] == "CLOSED")).sum()),
        "shadow_signals": int(is_shadow.sum()),
        "shadow_opened_trades": int((is_shadow & (trades["fill_status"] == "OPEN")).sum()),
        "shadow_closed_trades": int((is_shadow & (trades["fill_status"] == "CLOSED")).sum()),
        "net_pnl_rub": float(closed["net_pnl_rub"].sum()) if not closed.empty else 0.0,
        "execution_test_meaningful": bool(raw_snapshots >= 10 and (is_shadow.any() or ((reason == "") & trades["fill_status"].isin(["OPEN", "CLOSED"])).any()) and snapshot_orderbook_missing <= 0.2 * max(raw_snapshots, 1) and (trades["fill_status"] == "CLOSED").sum() > 0),
        "paper_new_entries_paused": PAUSE_NEW_ENTRIES_PATH.exists(),
    }
    summary_df = pd.concat([pd.DataFrame([meta]), summary_df], ignore_index=True, sort=False)
    summary_df.to_csv(SUMMARY_PATH, index=False)
    if not closed.empty:
        closed["day"] = pd.to_datetime(closed["exit_time"]).dt.date.astype(str)
        by_day = (
            closed.groupby(["day", "execution_policy"], as_index=False)
            .agg(
                closed_trades=("net_pnl_rub", "size"),
                net_pnl_rub=("net_pnl_rub", "sum"),
                avg_net_ticks=("net_ticks", "mean"),
                positive_trades=("net_pnl_rub", lambda x: int((x > 0).sum())),
            )
        )
    else:
        by_day = pd.DataFrame(columns=["day", "execution_policy", "closed_trades", "net_pnl_rub", "avg_net_ticks", "positive_trades"])
    by_day.to_csv(BY_DAY_PATH, index=False)
    best = summary_df.sort_values("net_pnl_rub", ascending=False).head(1)
    lines = [
        "# Paper execution daily summary",
        "",
        f"Updated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"- raw snapshots: {raw_snapshots}",
        f"- unique signal ids: {unique_signal_ids}",
        f"- valid live signals: {valid_live_signals}",
        f"- below threshold: {int((reason == 'below_threshold').sum())}",
        f"- stale signals: {int((reason == 'stale_signal').sum())}",
        f"- missing orderbook: {snapshot_orderbook_missing}",
        f"- live orderbook coverage pct: {live_orderbook_coverage_pct:.2%}",
        f"- executable signals: {int(trades['fill_status'].isin(['OPEN', 'CLOSED']).sum())}",
        f"- opened trades: {int((trades['fill_status'] == 'OPEN').sum())}",
        f"- closed trades: {int((trades['fill_status'] == 'CLOSED').sum())}",
        f"- strategy signals: {meta['strategy_signals']}",
        f"- strategy opened trades: {meta['strategy_opened_trades']}",
        f"- strategy closed trades: {meta['strategy_closed_trades']}",
        f"- shadow signals: {meta['shadow_signals']}",
        f"- shadow opened trades: {meta['shadow_opened_trades']}",
        f"- shadow closed trades: {meta['shadow_closed_trades']}",
        f"- avg spread ticks: {trades['spread_ticks_target'].mean() if 'spread_ticks_target' in trades else np.nan:.4f}",
        f"- net PnL RUB closed: {closed['net_pnl_rub'].sum() if not closed.empty else 0.0:.2f}",
        f"- positive trades closed: {int((closed['net_pnl_rub'] > 0).sum()) if not closed.empty else 0}",
        f"- positive days: {int((by_day.groupby('day')['net_pnl_rub'].sum() > 0).sum()) if not by_day.empty else 0}",
        f"- best execution policy: {best.iloc[0]['execution_policy'] if not best.empty else 'n/a'}",
        f"- execution test meaningful: {bool(meta['execution_test_meaningful'])}",
        f"- NO LIVE ORDERBOOK - execution test not meaningful: {not bool(meta['execution_test_meaningful']) and live_orderbook_coverage_pct < 0.8}",
        "- adverse-like execution: use `wait_30s_market` and compare against `market_now`; historical bid/ask/order book depth is not available here.",
        "",
        "Paper only. No real orders are sent.",
    ]
    DAILY_MD_PATH.write_text("\n".join(lines), encoding="utf-8")


def write_open_positions_json() -> None:
    trades = read_csv(TRADES_PATH, ["timestamp_signal", "entry_time", "planned_exit_time", "exit_time"])
    if trades.empty or "fill_status" not in trades:
        OPEN_POSITIONS_PATH.write_text("[]", encoding="utf-8")
        return
    open_trades = trades[trades["fill_status"] == "OPEN"].copy()
    OPEN_POSITIONS_PATH.write_text(open_trades.to_json(orient="records", force_ascii=False, date_format="iso"), encoding="utf-8")


def heartbeat_row(target_contract: str, plus1_contract: str, target_snap: dict, plus1_snap: dict, sess: dict) -> dict:
    trades = read_csv(TRADES_PATH, ["planned_exit_time"])
    active = int((trades.get("fill_status", pd.Series(dtype=str)) == "OPEN").sum()) if not trades.empty else 0
    now = pd.Timestamp.now()
    next_signal = (now.floor("10min") + pd.Timedelta(minutes=10)).isoformat(timespec="seconds")
    missing = pd.isna(target_snap.get("bid")) or pd.isna(target_snap.get("ask")) or pd.isna(plus1_snap.get("bid")) or pd.isna(plus1_snap.get("ask"))
    stale = (
        pd.isna(target_snap.get("age_seconds"))
        or pd.isna(plus1_snap.get("age_seconds"))
        or float(target_snap.get("age_seconds")) > 30
        or float(plus1_snap.get("age_seconds")) > 30
    )
    status = "orderbook_down" if missing else ("stale_orderbook" if stale else "ok")
    return {
        "local_time": now.isoformat(timespec="seconds"),
        "msk_time": sess["msk_time"],
        "session_phase": sess["session_phase"],
        "target_contract": target_contract,
        "plus1_contract": plus1_contract,
        "target_bid": target_snap.get("bid"),
        "target_ask": target_snap.get("ask"),
        "plus1_bid": plus1_snap.get("bid"),
        "plus1_ask": plus1_snap.get("ask"),
        "target_spread_ticks": target_snap.get("spread_ticks"),
        "plus1_spread_ticks": plus1_snap.get("spread_ticks"),
        "target_age_seconds": target_snap.get("age_seconds"),
        "plus1_age_seconds": plus1_snap.get("age_seconds"),
        "orderbook_ok": not missing and not stale,
        "candles_ok": True,
        "active_paper_positions": active,
        "next_signal_time": next_signal,
        "status": status,
    }


def monitor_once(cfg: Config) -> dict:
    close_due_trades(cfg)
    now = pd.Timestamp.now()
    target_contract, plus1_contract, selection_method = resolve_contracts(cfg)
    target_month = now.strftime("%Y-%m")
    sess = session_state(cfg)
    bdays = business_days_to_last_trade(target_contract, now)
    if bdays is not None and bdays < 3:
        signal = {
            "signal_status": "skipped",
            "skip_reason": "less_than_3_business_days_to_expiry",
            "timestamp_signal": now,
            "target_contract": target_contract,
            "plus1_contract": plus1_contract,
            "prediction": np.nan,
            "signal_direction": 0,
        }
    else:
        signal = compute_signal(target_contract, plus1_contract, target_month, cfg.orderbook_source)
    target_snap, plus1_snap, snapshot_row = snapshot_pair(target_contract, plus1_contract, cfg.orderbook_source)
    write_contract_selection(target_contract, plus1_contract, selection_method, target_snap, plus1_snap, cfg)
    snapshot_row.update(sess)
    snapshot_row["run_id"] = cfg.run_id
    hb_row = heartbeat_row(target_contract, plus1_contract, target_snap, plus1_snap, sess)
    hb_row["run_id"] = cfg.run_id
    append_csv(HEARTBEAT_PATH, [hb_row])
    if cfg.orderbook_source != "tbank-stream":
        snapshot_row["orderbook_source"] = "unavailable" if cfg.orderbook_source == "auto" else cfg.orderbook_source
        snapshot_row["execution_validation_possible"] = False
    elif pd.isna(target_snap["bid"]) or pd.isna(target_snap["ask"]) or pd.isna(plus1_snap["bid"]) or pd.isna(plus1_snap["ask"]):
        snapshot_row["orderbook_source"] = "tbank_stream_unavailable"
        snapshot_row["execution_validation_possible"] = False
    if signal.get("timestamp_signal") is not None and pd.notna(signal.get("timestamp_signal")):
        signal_ts = pd.Timestamp(signal["timestamp_signal"])
        signal["stale_signal"] = bool((pd.Timestamp.now() - signal_ts) > pd.Timedelta(minutes=cfg.stale_signal_minutes))
        if signal["stale_signal"]:
            signal["signal_status"] = "skipped"
            signal["skip_reason"] = "stale_signal"
    else:
        signal["stale_signal"] = False
    diagnostic_only = False
    if (pd.isna(target_snap["bid"]) and pd.isna(target_snap["ask"])) and (pd.isna(plus1_snap["bid"]) and pd.isna(plus1_snap["ask"])):
        snapshot_row["orderbook_source"] = "tbank_stream_unavailable" if cfg.orderbook_source == "tbank-stream" else "unavailable"
        snapshot_row["execution_validation_possible"] = False
        if not signal["stale_signal"]:
            signal["signal_status"] = "skipped"
            signal["skip_reason"] = "market_closed_or_no_live_book"
            diagnostic_only = True
    if pd.isna(signal.get("timestamp_signal", pd.NaT)) or signal.get("skip_reason") == "market_closed_or_no_live_book":
        signal["signal_id"] = pd.NA
        diagnostic_only = True
    else:
        signal["signal_id"] = (
            str(signal.get("target_contract", target_contract))
            + "|"
            + str(signal.get("plus1_contract", plus1_contract))
            + "|"
            + str(signal.get("candle_begin", ""))
            + "|"
            + str(signal.get("timestamp_signal", ""))
        )
    append_csv(SNAPSHOTS_PATH, [{**signal, **snapshot_row}])
    existing = read_csv(TRADES_PATH)
    if diagnostic_only:
        rows = []
    elif not existing.empty and "signal_id" in existing and signal["signal_id"] in set(existing["signal_id"].dropna().astype(str)):
        rows = []
    else:
        rows = create_signal_trades(signal, target_snap, plus1_snap, snapshot_row, cfg)
        append_csv(TRADES_PATH, rows)
    summarize()
    write_open_positions_json()
    return {"signal_status": signal.get("signal_status"), "skip_reason": signal.get("skip_reason", ""), "rows_written": len(rows)}


def stream_monitor_loop(cfg: Config) -> int:
    from t_tech.invest import Client

    if TBANK_TOKEN_CACHE is None:
        token = find_tbank_token()
    else:
        token = TBANK_TOKEN_CACHE
    target_contract, plus1_contract, selection_method = resolve_contracts(cfg)
    target_month = pd.Timestamp.now().strftime("%Y-%m")
    last_orderbook: dict[str, object] = {target_contract: None, plus1_contract: None}
    last_price: dict[str, float | None] = {target_contract: None, plus1_contract: None}
    last_candle: dict[str, dict | None] = {target_contract: None, plus1_contract: None}
    last_signal_begin: pd.Timestamp | None = None
    last_heartbeat = 0.0
    with Client(token) as client:
        instruments = []
        for secid in [target_contract, plus1_contract]:
            inst = tbank_find_future(client, secid)
            info = {"secid": secid, "figi": inst.figi, "uid": inst.uid, "ticker": inst.ticker, "name": inst.name}
            TBANK_INSTRUMENT_CACHE[secid] = info
            instruments.append(info)
        by_uid = {x["uid"]: x["secid"] for x in instruments}
        log(f"tbank_stream_loop start target={target_contract} plus1={plus1_contract}")
        for response in client.market_data_stream.market_data_stream(make_pair_stream_requests(instruments, depth=10)):
            closed_due_count = close_due_trades(cfg)
            if closed_due_count:
                summarize()
                write_open_positions_json()
                log(json.dumps({"paper_state_repair": "closed_due_trades", "closed_due_count": closed_due_count}, ensure_ascii=False))
            uid = ""
            candle_event = False
            if response.last_price is not None:
                uid = response.last_price.instrument_uid
                if uid in by_uid:
                    last_price[by_uid[uid]] = quotation_to_float(response.last_price.price)
            elif response.orderbook is not None:
                uid = response.orderbook.instrument_uid
                if uid in by_uid:
                    last_orderbook[by_uid[uid]] = response.orderbook
            elif response.candle is not None:
                uid = response.candle.instrument_uid
                if uid in by_uid:
                    last_candle[by_uid[uid]] = candle_to_stream_row(response.candle)
                    candle_event = True
            if not uid or uid not in by_uid:
                continue
            now_mono = time.monotonic()
            sess = session_state(cfg)
            target_snap = stream_orderbook_snapshot(target_contract, last_orderbook[target_contract], last_price[target_contract])
            plus1_snap = stream_orderbook_snapshot(plus1_contract, last_orderbook[plus1_contract], last_price[plus1_contract])
            _, _, snapshot_row = snapshot_pair_from_snaps(target_contract, plus1_contract, target_snap, plus1_snap)
            snapshot_row.update(sess)
            snapshot_row["run_id"] = cfg.run_id
            if now_mono - last_heartbeat >= max(1, cfg.heartbeat_seconds):
                write_contract_selection(target_contract, plus1_contract, selection_method, target_snap, plus1_snap, cfg)
                hb_row = heartbeat_row(target_contract, plus1_contract, target_snap, plus1_snap, sess)
                hb_row["run_id"] = cfg.run_id
                append_csv(HEARTBEAT_PATH, [hb_row])
                last_heartbeat = now_mono
            if not candle_event:
                continue
            if sess["session_phase"] != "continuous":
                continue
            if last_candle[target_contract] is None or last_candle[plus1_contract] is None:
                continue
            begin = pd.Timestamp(last_candle[target_contract]["begin"])
            if last_signal_begin is not None and begin <= last_signal_begin:
                continue
            if pd.Timestamp(last_candle[plus1_contract]["begin"]) != begin:
                continue
            signal = signal_from_stream_candles(target_contract, plus1_contract, target_month, last_candle[target_contract], last_candle[plus1_contract])
            last_signal_begin = begin
            if signal.get("signal_status") == "flat" and cfg.shadow_execution:
                signal["signal_status"] = "shadow"
            signal["stale_signal"] = False
            signal["signal_id"] = (
                f"{signal.get('target_contract', target_contract)}|{signal.get('plus1_contract', plus1_contract)}|"
                f"{signal.get('candle_begin', '')}|{signal.get('timestamp_signal', '')}"
            ) if pd.notna(signal.get("timestamp_signal", pd.NaT)) else pd.NA
            append_csv(SNAPSHOTS_PATH, [{**signal, **snapshot_row}])
            existing = read_csv(TRADES_PATH)
            if pd.isna(signal.get("signal_id", pd.NA)):
                rows = []
            elif not existing.empty and "signal_id" in existing and signal["signal_id"] in set(existing["signal_id"].dropna().astype(str)):
                rows = []
            else:
                rows = create_signal_trades(signal, target_snap, plus1_snap, snapshot_row, cfg)
                append_csv(TRADES_PATH, rows)
            summarize()
            write_open_positions_json()
            log(json.dumps({"signal_status": signal.get("signal_status"), "skip_reason": signal.get("skip_reason", ""), "rows_written": len(rows), "source": "tbank_stream"}, ensure_ascii=False))
    return 0


def snapshot_pair_from_snaps(target_contract: str, plus1_contract: str, target: dict, plus1: dict) -> tuple[dict, dict, dict]:
    row = {
        "snapshot_group_ts": datetime.now().isoformat(timespec="seconds"),
        "target_contract": target_contract,
        "plus1_contract": plus1_contract,
        "last_price_target": target["last_price"],
        "bid_target": target["bid"],
        "ask_target": target["ask"],
        "bid_size_target": target["bid_size"],
        "ask_size_target": target["ask_size"],
        "spread_ticks_target": target["spread_ticks"],
        "last_price_plus1": plus1["last_price"],
        "bid_plus1": plus1["bid"],
        "ask_plus1": plus1["ask"],
        "bid_size_plus1": plus1["bid_size"],
        "ask_size_plus1": plus1["ask_size"],
        "spread_ticks_plus1": plus1["spread_ticks"],
        "target_market_time": target["market_time"],
        "plus1_market_time": plus1["market_time"],
        "snapshot_time": datetime.now().isoformat(timespec="seconds"),
        "target_age_seconds": target["age_seconds"],
        "plus1_age_seconds": plus1["age_seconds"],
        "orderbook_source": "tbank_stream",
        "execution_validation_possible": bool(target["execution_validation_possible"] and plus1["execution_validation_possible"]),
    }
    return target, plus1, row


def parse_args(argv: Iterable[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(description="Paper/order-book monitor for selected MOEX NG lead-lag candidate.")
    parser.add_argument("--once", action="store_true", default=True)
    parser.add_argument("--loop", dest="once", action="store_false")
    parser.add_argument("--interval-seconds", type=int, default=600)
    parser.add_argument("--max-spread-ticks", type=float, default=4.0)
    parser.add_argument("--passive-wait-seconds", type=int, default=5)
    parser.add_argument("--paper-contracts", type=int, default=1)
    parser.add_argument("--request-sleep", type=float, default=0.08)
    parser.add_argument("--stale-signal-minutes", type=int, default=15)
    parser.add_argument("--max-orderbook-age-seconds", type=int, default=30)
    parser.add_argument("--weekend-session", action="store_true")
    parser.add_argument("--shadow-execution", action="store_true")
    parser.add_argument("--orderbook-source", choices=["auto", "tbank-stream", "moex-iss", "unavailable"], default="auto")
    parser.add_argument("--heartbeat-seconds", type=int, default=5)
    parser.add_argument("--target-contract", default="auto")
    parser.add_argument("--plus1-contract", default="auto")
    parser.add_argument("--max-target-spread-ticks", type=float, default=4.0)
    parser.add_argument("--max-plus1-spread-ticks", type=float, default=6.0)
    parser.add_argument("--min-touch-size", type=float, default=1.0)
    parser.add_argument("--paper-only", action="store_true", default=True)
    parser.add_argument("--reset-paper-day", action="store_true")
    parser.add_argument("--force-reset-open-positions", action="store_true")
    parser.add_argument("--run-id", default="")
    args = parser.parse_args(list(argv) if argv is not None else None)
    return Config(**vars(args))


def main(argv: Iterable[str] | None = None) -> int:
    cfg = parse_args(argv)
    ensure_dirs()
    reset_paper_day(cfg)
    if cfg.once:
        result = monitor_once(cfg)
        log(json.dumps(result, ensure_ascii=False))
        return 0
    if cfg.orderbook_source == "tbank-stream":
        return stream_monitor_loop(cfg)
    while True:
        result = monitor_once(cfg)
        log(json.dumps(result, ensure_ascii=False))
        time.sleep(cfg.heartbeat_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
