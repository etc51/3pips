from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from t_tech.invest import CandleInterval, Client, InstrumentIdType
from t_tech.invest.utils import get_intervals

from ng_scalper_bot import find_tbank_token, quotation_to_float, tbank_find_future


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "raw" / "tbank_1m"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str) -> datetime:
    if value == "now":
        return utc_now()
    if len(value) == 10:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def candle_row(secid: str, candle: object) -> dict:
    return {
        "secid": secid,
        "time": candle.time,
        "open": quotation_to_float(candle.open),
        "high": quotation_to_float(candle.high),
        "low": quotation_to_float(candle.low),
        "close": quotation_to_float(candle.close),
        "volume": int(candle.volume),
        "is_complete": bool(candle.is_complete),
    }


def download_one(client: Client, secid: str, date_from: datetime, date_to: datetime, sleep_sec: float) -> pd.DataFrame:
    instrument = tbank_find_future(client, secid)
    info = client.instruments.future_by(
        id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
        id=instrument.uid,
    ).instrument
    start = max(date_from, info.first_1min_candle_date)
    finish = min(date_to, info.expiration_date)
    rows: list[dict] = []
    intervals = list(get_intervals(CandleInterval.CANDLE_INTERVAL_1_MIN, start, finish))
    for n, (left, right) in enumerate(intervals, start=1):
        try:
            resp = client.market_data.get_candles(
                figi=instrument.figi,
                from_=left,
                to=right,
                interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
            )
            rows.extend(candle_row(secid, candle) for candle in resp.candles)
        except Exception as exc:  # noqa: BLE001
            print(f"{secid} chunk_error {left.isoformat()} {right.isoformat()} {type(exc).__name__}: {exc}", flush=True)
        if n % 25 == 0 or n == len(intervals):
            print(f"{secid} progress {n}/{len(intervals)} rows={len(rows)}", flush=True)
        if sleep_sec > 0:
            time.sleep(sleep_sec)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.drop_duplicates(["secid", "time"]).sort_values(["secid", "time"]).reset_index(drop=True)
    return df


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--secids", nargs="+", default=["NGK6", "NGM6"])
    parser.add_argument("--from-date", default=None)
    parser.add_argument("--to-date", default="now")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--sleep-sec", type=float, default=0.05)
    args = parser.parse_args()

    date_to = parse_dt(args.to_date)
    date_from = parse_dt(args.from_date) if args.from_date else date_to - timedelta(days=args.days)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    token = find_tbank_token()
    all_frames: list[pd.DataFrame] = []
    with Client(token) as client:
        for secid in args.secids:
            print(f"{secid} download from={date_from.isoformat()} to={date_to.isoformat()}", flush=True)
            df = download_one(client, secid, date_from, date_to, args.sleep_sec)
            if df.empty:
                print(f"{secid} empty", flush=True)
                continue
            out_parquet = OUT_DIR / f"{secid}_1m.parquet"
            out_csv = OUT_DIR / f"{secid}_1m.csv"
            df.to_parquet(out_parquet, index=False)
            df.to_csv(out_csv, index=False)
            print(f"{secid} saved rows={len(df)} first={df['time'].min()} last={df['time'].max()}", flush=True)
            all_frames.append(df)
    if all_frames:
        panel = pd.concat(all_frames, ignore_index=True).sort_values(["secid", "time"])
        panel.to_parquet(OUT_DIR / "selected_1m.parquet", index=False)
        panel.to_csv(OUT_DIR / "selected_1m.csv", index=False)
        print(f"panel saved rows={len(panel)} secids={panel['secid'].nunique()}", flush=True)


if __name__ == "__main__":
    main()
