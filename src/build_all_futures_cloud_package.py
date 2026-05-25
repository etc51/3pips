from __future__ import annotations

import argparse
import json
import re
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from t_tech.invest import CandleInterval, Client, InstrumentIdType, InstrumentStatus
from t_tech.invest.utils import get_intervals

from ng_scalper_bot import find_tbank_token, quotation_to_float


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "raw" / "tbank_1m"
REPORTS = ROOT / "reports"
PACKAGE_DIR = REPORTS / "cloud_all_futures_grid_package"


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


def q(value: object) -> float:
    return quotation_to_float(value)


def list_futures(client: Client, include_not_api_tradeable: bool) -> pd.DataFrame:
    now = utc_now()
    rows = []
    futures = client.instruments.futures(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE).instruments
    for f in futures:
        if f.expiration_date and f.expiration_date < now:
            continue
        tick = q(f.min_price_increment)
        tick_rub = q(f.min_price_increment_amount)
        if tick <= 0 or tick_rub <= 0:
            continue
        if not include_not_api_tradeable and not (f.api_trade_available_flag and f.buy_available_flag and f.sell_available_flag):
            continue
        rows.append(
            {
                "ticker": f.ticker,
                "name": f.name,
                "figi": f.figi,
                "uid": f.uid,
                "class_code": f.class_code,
                "expiration": f.expiration_date.isoformat() if f.expiration_date else "",
                "first_1min_candle_date": f.first_1min_candle_date.isoformat() if f.first_1min_candle_date else "",
                "lot": f.lot,
                "currency": f.currency,
                "tick": tick,
                "tick_rub": tick_rub,
                "go_buy": q(f.initial_margin_on_buy),
                "go_sell": q(f.initial_margin_on_sell),
                "api_trade_available": bool(f.api_trade_available_flag),
                "buy_available": bool(f.buy_available_flag),
                "sell_available": bool(f.sell_available_flag),
            }
        )
    return pd.DataFrame(rows).sort_values(["ticker"]).reset_index(drop=True)


def candle_row(ticker: str, candle: object) -> dict:
    return {
        "secid": ticker,
        "time": candle.time,
        "open": q(candle.open),
        "high": q(candle.high),
        "low": q(candle.low),
        "close": q(candle.close),
        "volume": int(candle.volume),
        "is_complete": bool(candle.is_complete),
    }


def read_existing(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    df = pd.read_csv(path)
    if not df.empty and "time" in df:
        df["time"] = pd.to_datetime(df["time"], utc=True)
    return df


def rate_limit_sleep_seconds(exc: Exception) -> float | None:
    text = str(exc)
    if "RESOURCE_EXHAUSTED" not in text and "resource exhausted" not in text.lower():
        return None
    match = re.search(r"ratelimit_reset=(\d+)", text)
    if match:
        return max(2.0, float(match.group(1)) + 1.0)
    return 10.0


def download_one(
    client: Client,
    row: pd.Series,
    date_from: datetime,
    date_to: datetime,
    sleep_sec: float,
    force: bool,
    retries: int,
) -> pd.DataFrame:
    ticker = str(row["ticker"])
    out_csv = DATA_DIR / f"{ticker}_1m.csv"
    existing = pd.DataFrame() if force else read_existing(out_csv)
    first = pd.to_datetime(row.get("first_1min_candle_date"), utc=True, errors="coerce")
    exp = pd.to_datetime(row.get("expiration"), utc=True, errors="coerce")
    start = date_from if pd.isna(first) else max(date_from, first.to_pydatetime())
    finish = date_to if pd.isna(exp) else min(date_to, exp.to_pydatetime())
    if not existing.empty:
        last = existing["time"].max().to_pydatetime()
        start = max(start, last + timedelta(minutes=1))
    rows: list[dict] = []
    if start < finish:
        intervals = list(get_intervals(CandleInterval.CANDLE_INTERVAL_1_MIN, start, finish))
        for n, (left, right) in enumerate(intervals, start=1):
            for attempt in range(retries + 1):
                try:
                    resp = client.market_data.get_candles(
                        figi=row["figi"],
                        from_=left,
                        to=right,
                        interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
                    )
                    rows.extend(candle_row(ticker, candle) for candle in resp.candles)
                    break
                except Exception as exc:  # noqa: BLE001
                    wait = rate_limit_sleep_seconds(exc)
                    if wait is not None and attempt < retries:
                        print(
                            f"{ticker} rate_limit_wait {wait:.1f}s attempt={attempt + 1}/{retries} "
                            f"{left.isoformat()} {right.isoformat()}",
                            flush=True,
                        )
                        time.sleep(wait)
                        continue
                    print(f"{ticker} chunk_error {left.isoformat()} {right.isoformat()} {type(exc).__name__}: {exc}", flush=True)
                    break
            if n % 20 == 0 or n == len(intervals):
                print(f"{ticker} progress {n}/{len(intervals)} new_rows={len(rows)}", flush=True)
            if sleep_sec > 0:
                time.sleep(sleep_sec)
    new_df = pd.DataFrame(rows)
    frames = [df for df in [existing, new_df] if not df.empty]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True, sort=False)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.drop_duplicates(["secid", "time"]).sort_values(["secid", "time"]).reset_index(drop=True)
    df.to_csv(out_csv, index=False)
    try:
        df.to_parquet(DATA_DIR / f"{ticker}_1m.parquet", index=False)
    except Exception:
        pass
    return df


def write_prompt(path: Path) -> None:
    path.write_text(
        """You are given a ZIP with MOEX futures 1-minute candle data from T-Bank API.

Goal:
Run a broad first-pass search over ALL included futures without pre-filtering for liquidity. The objective is to find any futures where the short-term momentum + trailing-stop idea has a real candidate edge.

Files:
- tbank_1m_all_futures.csv: candles with secid,time,open,high,low,close,volume,is_complete.
- instrument_specs.csv: tick size, tick value in RUB, margin, expiration, API flags.
- row_counts.csv: rows per ticker.

Core idea to test:
- Enter long/short after short-term directional movement.
- Immediately place a stop.
- If price moves in favor, activate trailing stop.
- Exit by trailing stop or max hold.
- Everything must be after commission and slippage.
- Parameters may be expressed in percent of price and converted to ticks, or directly in ticks. Test both.

Important:
Do NOT discard a ticker only because volume is low. Low-liquidity futures may still be useful with 1 contract. But report liquidity/microstructure risk separately.

First-pass universe search:
Run a very large coarse grid across every ticker:
- signal families:
  - momentum_breakout
  - vwap_impulse
  - range_expansion
  - trend_pullback
  - pure_trailing_after_impulse
- direction: long, short, both
- momentum_pct: 0.0002,0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01
- momentum_ticks: 1,2,3,5,8,13,21,34,55,89
- breakout_lookback: 3,5,8,13,21,34,55,89,144
- trend_fast: 3,5,8,13,21
- trend_slow: 8,13,21,34,55,89
- volume_multiplier: 0,0.5,0.8,1.0,1.2,1.5,2.0,2.5,3.0,4.0
- volume_window: 20,40,60,120
- vwap_mode: disabled, rolling20, rolling60, session
- vwap_buffer_pct: 0,0.0001,0.0002,0.0005,0.001
- stop_pct: 0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02
- trail_pct: 0.0002,0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008
- trail_activation_pct: 0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015
- min_stop_ticks: 1,2,3,5,8,13,21,34,55,89
- min_trail_ticks: 1,2,3,5,8,13,21,34,55
- cooldown_minutes: 0,1,3,5,10,20
- max_hold_minutes: 5,10,15,30,60,90,120,180
- entry timing:
  - next_bar_open
  - signal_bar_close
  - adverse_1tick
- session filters:
  - all available data
  - main hours only
  - weekend separately if identifiable from timestamps
  - exclude first/last 10 minutes of day

Costs:
- Use tick_rub from instrument_specs.
- If exact broker commission is unknown, use conservative commission estimate:
  round_turn_fee_rub = max(2 * notional_rub * 0.00025, 1 tick_rub)
  where notional_rub = close / tick * tick_rub.
- Evaluate slippage 0,1,2,3,4,5 ticks round trip.
- Main robustness must survive at least 2T. 3T-5T are stress levels.

Split:
- train = first 70%
- test = last 30%
- full sample
- rolling walk-forward:
  - 60d train / 20d test
  - 90d train / 30d test
  - expanding train / next month test

Selection:
Do NOT optimize for highest historical PnL.
Primary target is robust live-paper candidates:
- test_trades >= 40, unless ticker has fewer than 5000 rows; then allow but label LOW_SAMPLE.
- test_net_2t > 0.
- test_profit_factor_2t >= 1.10.
- remove-best-1/3/5 trades must not destroy all profit.
- rolling active profitable windows at 2T >= 60% for KEEP.
- neighborhood stability:
  For top candidates, test at least 1000 nearby variants around the selected parameters.
  KEEP only if neighborhood_profitable_2t_pct >= 50%, unless explicitly labeled speculative.

Labels:
- KEEP_FOR_LIVE_PAPER
- WATCHLIST_SPECULATIVE
- REJECT_OVERFIT
- REJECT_FRAGILE
- LOW_SAMPLE
- NO_EDGE

Outputs to create:
- all_futures_grid_results.csv
- all_futures_top_profiles_by_ticker.csv
- all_futures_stress_summary.csv
- all_futures_final_live_paper_profiles.json
- all_futures_rolling_walkforward.csv
- all_futures_slippage_stress.csv
- all_futures_neighborhood_summary.csv
- all_futures_time_of_day_breakdown.csv
- all_futures_direction_breakdown.csv
- all_futures_rejected_tickers.csv

Final answer:
1. Count tickers and rows actually loaded.
2. Show all KEEP_FOR_LIVE_PAPER tickers.
3. Show WATCHLIST_SPECULATIVE tickers separately.
4. Explain rejected tickers by reason.
5. Recommend a live-paper portfolio split into:
   - strong contour
   - weak/speculative contour
6. Do not recommend rejected profiles as live candidates.
""",
        encoding="utf-8",
    )


def write_split_packages(specs: pd.DataFrame, counts: pd.DataFrame, panel: pd.DataFrame, max_part_mb: int) -> list[Path]:
    base_name = "cloud_all_futures_grid_package"
    prompt = PACKAGE_DIR / "PROMPT_FOR_CLOUD_GPT_ALL_FUTURES.md"
    metadata = PACKAGE_DIR / "metadata.json"
    specs_path = PACKAGE_DIR / "instrument_specs.csv"
    counts_path = PACKAGE_DIR / "row_counts.csv"
    manifest_rows = []
    zip_paths: list[Path] = []

    meta_zip = REPORTS / f"{base_name}_00_prompt_specs.zip"
    with zipfile.ZipFile(meta_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in [prompt, metadata, specs_path, counts_path]:
            if path.exists():
                zf.write(path, path.name)
    zip_paths.append(meta_zip)

    if panel.empty:
        return zip_paths

    tickers = [str(t) for t in counts[counts["rows"] > 0]["ticker"].tolist()]
    max_bytes = max_part_mb * 1024 * 1024
    part = 1
    current: list[str] = []
    current_bytes = 0

    def flush_part(tickers_part: list[str], part_no: int) -> Path | None:
        if not tickers_part:
            return None
        part_df = panel[panel["secid"].isin(tickers_part)].copy()
        part_counts = counts[counts["ticker"].isin(tickers_part)].copy()
        csv_path = PACKAGE_DIR / f"tbank_1m_all_futures_part_{part_no:02d}.csv"
        counts_part_path = PACKAGE_DIR / f"row_counts_part_{part_no:02d}.csv"
        part_df.to_csv(csv_path, index=False)
        part_counts.to_csv(counts_part_path, index=False)
        zip_path = REPORTS / f"{base_name}_part_{part_no:02d}.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in [csv_path, counts_part_path, specs_path, prompt, metadata]:
                if path.exists():
                    zf.write(path, path.name)
        manifest_rows.append(
            {
                "part": part_no,
                "zip": zip_path.name,
                "tickers": ",".join(tickers_part),
                "ticker_count": len(tickers_part),
                "rows": int(len(part_df)),
                "csv_bytes": int(csv_path.stat().st_size),
                "zip_bytes": int(zip_path.stat().st_size),
            }
        )
        return zip_path

    for ticker in tickers:
        path = DATA_DIR / f"{ticker}_1m.csv"
        est = path.stat().st_size if path.exists() else int(counts.loc[counts["ticker"] == ticker, "rows"].iloc[0]) * 80
        if current and current_bytes + est > max_bytes:
            zp = flush_part(current, part)
            if zp:
                zip_paths.append(zp)
            part += 1
            current = []
            current_bytes = 0
        current.append(ticker)
        current_bytes += est
    zp = flush_part(current, part)
    if zp:
        zip_paths.append(zp)

    manifest = pd.DataFrame(manifest_rows)
    manifest_path = PACKAGE_DIR / "split_manifest.csv"
    manifest.to_csv(manifest_path, index=False)
    with zipfile.ZipFile(meta_zip, "a", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.write(manifest_path, manifest_path.name)
    return zip_paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-date", default=None)
    parser.add_argument("--to-date", default="now")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--sleep-sec", type=float, default=0.03)
    parser.add_argument("--retries", type=int, default=20)
    parser.add_argument("--min-rows", type=int, default=1)
    parser.add_argument("--split-max-mb", type=int, default=180)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--include-not-api-tradeable", action="store_true")
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()

    date_to = parse_dt(args.to_date)
    date_from = parse_dt(args.from_date) if args.from_date else date_to - timedelta(days=args.days)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(exist_ok=True)

    token = find_tbank_token()
    with Client(token) as client:
        specs = list_futures(client, include_not_api_tradeable=args.include_not_api_tradeable)
        specs.to_csv(PACKAGE_DIR / "instrument_specs.csv", index=False)
        specs.to_csv(REPORTS / "all_futures_instrument_specs.csv", index=False)
        print(f"universe futures={len(specs)}", flush=True)
        if args.download:
            for idx, row in specs.iterrows():
                ticker = row["ticker"]
                print(f"{idx+1}/{len(specs)} {ticker} download", flush=True)
                df = download_one(client, row, date_from, date_to, args.sleep_sec, args.force, args.retries)
                print(f"{ticker} rows={len(df)}", flush=True)

    frames = []
    row_counts = []
    for _, row in specs.iterrows():
        ticker = row["ticker"]
        path = DATA_DIR / f"{ticker}_1m.csv"
        df = read_existing(path)
        rows = len(df)
        row_counts.append({"ticker": ticker, "rows": rows})
        if rows >= args.min_rows:
            frames.append(df)
    counts = pd.DataFrame(row_counts).sort_values(["rows", "ticker"], ascending=[False, True])
    counts.to_csv(PACKAGE_DIR / "row_counts.csv", index=False)
    if frames:
        panel = pd.concat(frames, ignore_index=True, sort=False)
        panel = panel.drop_duplicates(["secid", "time"]).sort_values(["secid", "time"]).reset_index(drop=True)
        panel.to_csv(PACKAGE_DIR / "tbank_1m_all_futures.csv", index=False)
        try:
            panel.to_parquet(PACKAGE_DIR / "tbank_1m_all_futures.parquet", index=False)
        except Exception:
            pass
    else:
        panel = pd.DataFrame()
    metadata = {
        "created_at_utc": utc_now().isoformat(),
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "universe_tickers": int(len(specs)),
        "tickers_with_rows": int((counts["rows"] > 0).sum()) if not counts.empty else 0,
        "total_rows": int(counts["rows"].sum()) if not counts.empty else 0,
        "min_rows_in_package": args.min_rows,
        "source": "T-Bank Invest API via t_tech.invest",
    }
    (PACKAGE_DIR / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    write_prompt(PACKAGE_DIR / "PROMPT_FOR_CLOUD_GPT_ALL_FUTURES.md")

    zip_path = REPORTS / "cloud_all_futures_grid_package.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in [
            PACKAGE_DIR / "instrument_specs.csv",
            PACKAGE_DIR / "row_counts.csv",
            PACKAGE_DIR / "metadata.json",
            PACKAGE_DIR / "PROMPT_FOR_CLOUD_GPT_ALL_FUTURES.md",
            PACKAGE_DIR / "tbank_1m_all_futures.csv",
        ]:
            if path.exists():
                zf.write(path, path.name)
    print(json.dumps(metadata, ensure_ascii=False, indent=2), flush=True)
    print(f"zip={zip_path}", flush=True)
    split_paths = write_split_packages(specs, counts, panel, args.split_max_mb)
    for path in split_paths:
        print(f"split_zip={path} bytes={path.stat().st_size}", flush=True)


if __name__ == "__main__":
    main()
