from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from t_tech.invest import CandleInterval, Client, InstrumentStatus

from ng_scalper_bot import find_tbank_token, quotation_to_float


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"


def score_row(row: dict) -> float:
    spread = row["spread_ticks"]
    if spread is None:
        return -1_000_000.0
    score = 0.0
    score += max(0.0, 8.0 - spread) * 12.0
    score += min(row["vol_180m"] / 1000.0, 70.0)
    score += min((row["bid_qty3"] + row["ask_qty3"]) / 500.0, 35.0)
    score += min(row["avg_abs_1m_ticks"], 20.0) * 1.5
    score -= min(row["round_fee_ticks"], 80.0) * 1.3
    if row["tick_rub"] <= 0 or row["notional_rub"] <= 0:
        score -= 10_000
    if row["vol_180m"] < 50:
        score -= 200
    if spread > 20:
        score -= 100
    return score


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--lookback-min", type=int, default=180)
    parser.add_argument("--out", default=str(REPORTS / "scalp_futures_top30.csv"))
    args = parser.parse_args()

    token = find_tbank_token()
    rows = []
    with Client(token) as client:
        futures = client.instruments.futures(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE).instruments
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=args.lookback_min)
        for idx, f in enumerate(futures, start=1):
            if not f.api_trade_available_flag or not f.buy_available_flag or not f.sell_available_flag:
                continue
            if f.expiration_date and f.expiration_date < now:
                continue
            tick = quotation_to_float(f.min_price_increment)
            tick_rub = quotation_to_float(f.min_price_increment_amount)
            if tick <= 0 or tick_rub <= 0:
                continue
            try:
                last_prices = client.market_data.get_last_prices(figi=[f.figi]).last_prices
                last = quotation_to_float(last_prices[0].price) if last_prices else 0.0
            except Exception:
                last = 0.0
            try:
                ob = client.market_data.get_order_book(figi=f.figi, depth=10)
                bid = quotation_to_float(ob.bids[0].price) if ob.bids else None
                ask = quotation_to_float(ob.asks[0].price) if ob.asks else None
                bid_qty3 = sum(level.quantity for level in ob.bids[:3]) if ob.bids else 0
                ask_qty3 = sum(level.quantity for level in ob.asks[:3]) if ob.asks else 0
            except Exception:
                bid = ask = None
                bid_qty3 = ask_qty3 = 0
            try:
                candles = client.market_data.get_candles(
                    figi=f.figi,
                    from_=start,
                    to=now,
                    interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
                ).candles
            except Exception:
                candles = []
            closes = [quotation_to_float(c.close) for c in candles]
            vols = [int(c.volume) for c in candles if int(c.volume) > 0]
            changes = [abs(b - a) / tick for a, b in zip(closes, closes[1:])] if tick else []
            mid = ((bid + ask) / 2.0 if bid is not None and ask is not None else last) or 0.0
            notional = mid / tick * tick_rub if mid and tick else 0.0
            fee_side = notional * 0.00025 if notional else 0.0
            round_fee_ticks = fee_side * 2.0 / tick_rub if tick_rub else 999.0
            spread_ticks = (ask - bid) / tick if bid is not None and ask is not None else None
            row = {
                "ticker": f.ticker,
                "name": f.name,
                "figi": f.figi,
                "uid": f.uid,
                "expiration": f.expiration_date.date().isoformat() if f.expiration_date else "",
                "last": last,
                "tick": tick,
                "tick_rub": tick_rub,
                "notional_rub": round(notional, 2),
                "go_buy": quotation_to_float(f.initial_margin_on_buy),
                "go_sell": quotation_to_float(f.initial_margin_on_sell),
                "fee_side_1lot": round(fee_side, 2),
                "round_fee_ticks": round(round_fee_ticks, 2),
                "spread_ticks": round(spread_ticks, 2) if spread_ticks is not None else None,
                "bid_qty3": bid_qty3,
                "ask_qty3": ask_qty3,
                "vol_180m": sum(vols),
                "avg_1m_vol": round(sum(vols) / len(vols), 2) if vols else 0.0,
                "avg_abs_1m_ticks": round(sum(changes) / len(changes), 2) if changes else 0.0,
                "max_abs_1m_ticks": round(max(changes), 2) if changes else 0.0,
            }
            row["score"] = round(score_row(row), 2)
            rows.append(row)
            if idx % 50 == 0:
                print(f"scanned {idx}/{len(futures)} candidates={len(rows)}", flush=True)

    df = pd.DataFrame(rows).sort_values("score", ascending=False)
    REPORTS.mkdir(exist_ok=True)
    df.to_csv(REPORTS / "scalp_futures_candidates.csv", index=False)
    top = df.head(args.top)
    top.to_csv(args.out, index=False)
    print(top[["ticker", "name", "score", "spread_ticks", "round_fee_ticks", "vol_180m", "avg_abs_1m_ticks"]].to_string(index=False))


if __name__ == "__main__":
    main()
