from __future__ import annotations

import argparse
import json
import re
import shutil
import time
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from pathlib import Path

import pandas as pd
import requests
import urllib3
from t_tech.invest import CandleInterval, Client, InstrumentStatus
from t_tech.invest.utils import get_intervals

from ng_scalper_bot import find_tbank_token, quotation_to_float


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "raw" / "tbank_stocks_1m"
REPORTS = ROOT / "reports"
PACKAGE_DIR = REPORTS / "stock_1m_scalp_gpt_package"
STOCK_RESEARCH_ROOT = Path("D:/moex_tbank_stock_strategy/reports")
HISTORY_DATA_URL = "https://invest-public-api.tbank.ru/history-data"


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
    if value is None:
        return 0.0
    try:
        return float(quotation_to_float(value))
    except Exception:
        return 0.0


def rate_limit_sleep_seconds(exc: Exception) -> float | None:
    text = str(exc)
    if "RESOURCE_EXHAUSTED" not in text and "resource exhausted" not in text.lower():
        return None
    match = re.search(r"ratelimit_reset=(\d+)", text)
    if match:
        return max(2.0, float(match.group(1)) + 1.0)
    return 10.0


def list_tqbr_shares(client: Client) -> pd.DataFrame:
    rows: list[dict] = []
    shares = client.instruments.shares(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE).instruments
    for s in shares:
        tick = q(getattr(s, "min_price_increment", None))
        lot = int(getattr(s, "lot", 0) or 0)
        if getattr(s, "class_code", "") != "TQBR":
            continue
        if getattr(s, "currency", "") != "rub":
            continue
        if tick <= 0 or lot <= 0:
            continue
        if not (s.api_trade_available_flag and s.buy_available_flag and s.sell_available_flag):
            continue
        rows.append(
            {
                "ticker": s.ticker,
                "name": s.name,
                "figi": s.figi,
                "uid": s.uid,
                "class_code": s.class_code,
                "lot": lot,
                "currency": s.currency,
                "tick": tick,
                "tick_rub_per_lot": tick * lot,
                "api_trade_available": bool(s.api_trade_available_flag),
                "buy_available": bool(s.buy_available_flag),
                "sell_available": bool(s.sell_available_flag),
                "short_enabled": bool(getattr(s, "short_enabled_flag", False)),
                "for_iis": bool(getattr(s, "for_iis_flag", False)),
                "first_1min_candle_date": s.first_1min_candle_date.isoformat()
                if s.first_1min_candle_date
                else "",
            }
        )
    return pd.DataFrame(rows).sort_values("ticker").reset_index(drop=True)


def candle_row(ticker: str, candle: object, lot: int) -> dict:
    close = q(candle.close)
    volume_lots = int(candle.volume)
    return {
        "secid": ticker,
        "time": candle.time,
        "open": q(candle.open),
        "high": q(candle.high),
        "low": q(candle.low),
        "close": close,
        "volume_lots": volume_lots,
        "lot": int(lot),
        "turnover_rub": close * volume_lots * int(lot),
        "is_complete": bool(candle.is_complete),
    }


def read_existing(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    df = pd.read_csv(path)
    if not df.empty and "time" in df:
        df["time"] = pd.to_datetime(df["time"], utc=True)
    return df


def parse_history_archive(ticker: str, content: bytes, lot: int, date_from: datetime, date_to: datetime) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    with zipfile.ZipFile(BytesIO(content)) as zf:
        for name in zf.namelist():
            if not name.lower().endswith(".csv"):
                continue
            raw = zf.read(name).decode("utf-8", errors="replace")
            if not raw.strip():
                continue
            df = pd.read_csv(
                StringIO(raw),
                sep=";",
                header=None,
                names=["uid", "time", "open", "close", "high", "low", "volume_lots", "_empty"],
                usecols=[0, 1, 2, 3, 4, 5, 6],
            )
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    panel = pd.concat(frames, ignore_index=True, sort=False)
    panel["time"] = pd.to_datetime(panel["time"], utc=True, errors="coerce")
    panel = panel.dropna(subset=["time"])
    panel = panel[(panel["time"] >= pd.Timestamp(date_from)) & (panel["time"] <= pd.Timestamp(date_to))]
    if panel.empty:
        return pd.DataFrame()
    for col in ["open", "close", "high", "low"]:
        panel[col] = pd.to_numeric(panel[col], errors="coerce")
    panel["volume_lots"] = pd.to_numeric(panel["volume_lots"], errors="coerce").fillna(0).astype("int64")
    panel["secid"] = ticker
    panel["lot"] = int(lot)
    panel["turnover_rub"] = panel["close"] * panel["volume_lots"] * int(lot)
    panel["is_complete"] = True
    return panel[
        ["secid", "time", "open", "high", "low", "close", "volume_lots", "lot", "turnover_rub", "is_complete"]
    ].drop_duplicates(["secid", "time"]).sort_values(["secid", "time"]).reset_index(drop=True)


def download_history_archive_one(
    token: str,
    row: pd.Series,
    year: int,
    date_from: datetime,
    date_to: datetime,
    out_dir: Path,
    retries: int,
    verify_tls: bool,
    allow_insecure_tls_fallback: bool,
) -> tuple[pd.DataFrame, dict]:
    ticker = str(row["ticker"])
    archive_dir = out_dir / "_history_archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{ticker}_{year}_history.zip"
    meta = {
        "ticker": ticker,
        "year": int(year),
        "source": "history-data",
        "archive_path": str(archive_path),
        "tls_verified": bool(verify_tls),
        "used_insecure_tls_fallback": False,
        "http_status": None,
        "bytes": 0,
        "error": "",
    }
    content = archive_path.read_bytes() if archive_path.exists() and archive_path.stat().st_size > 0 else b""
    if not content:
        for attempt in range(retries + 1):
            try:
                response = requests.get(
                    HISTORY_DATA_URL,
                    params={"figi": str(row["figi"]), "year": int(year)},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=120,
                    verify=verify_tls,
                )
            except requests.exceptions.SSLError as exc:
                if allow_insecure_tls_fallback and verify_tls:
                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                    response = requests.get(
                        HISTORY_DATA_URL,
                        params={"figi": str(row["figi"]), "year": int(year)},
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=120,
                        verify=False,
                    )
                    meta["used_insecure_tls_fallback"] = True
                else:
                    meta["error"] = f"SSLError: {exc}"
                    if attempt >= retries:
                        return pd.DataFrame(), meta
                    time.sleep(2 + attempt)
                    continue
            except Exception as exc:  # noqa: BLE001
                meta["error"] = f"{type(exc).__name__}: {exc}"
                if attempt >= retries:
                    return pd.DataFrame(), meta
                time.sleep(2 + attempt)
                continue

            meta["http_status"] = int(response.status_code)
            if response.status_code == 200 and response.content[:2] == b"PK":
                content = response.content
                archive_path.write_bytes(content)
                break
            meta["error"] = response.text[:500]
            if response.status_code == 404:
                return pd.DataFrame(), meta
            if attempt < retries:
                time.sleep(2 + attempt)
        if not content:
            return pd.DataFrame(), meta

    meta["bytes"] = int(len(content))
    df = parse_history_archive(ticker, content, int(row["lot"]), date_from, date_to)
    out_csv = out_dir / f"{ticker}_1m.csv"
    if not df.empty:
        df.to_csv(out_csv, index=False)
        try:
            df.to_parquet(out_dir / f"{ticker}_1m.parquet", index=False)
        except Exception:
            pass
    return df, meta


def download_one(
    client: Client,
    row: pd.Series,
    date_from: datetime,
    date_to: datetime,
    out_dir: Path,
    sleep_sec: float,
    force: bool,
    retries: int,
) -> pd.DataFrame:
    ticker = str(row["ticker"])
    out_csv = out_dir / f"{ticker}_1m.csv"
    existing = pd.DataFrame() if force else read_existing(out_csv)
    first = pd.to_datetime(row.get("first_1min_candle_date"), utc=True, errors="coerce")
    start = date_from if pd.isna(first) else max(date_from, first.to_pydatetime())
    finish = date_to
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
                    rows.extend(candle_row(ticker, candle, int(row["lot"])) for candle in resp.candles)
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

    frames = [df for df in [existing, pd.DataFrame(rows)] if not df.empty]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True, sort=False)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.drop_duplicates(["secid", "time"]).sort_values(["secid", "time"]).reset_index(drop=True)
    df.to_csv(out_csv, index=False)
    try:
        df.to_parquet(out_dir / f"{ticker}_1m.parquet", index=False)
    except Exception:
        pass
    return df


def add_orderbook_snapshot(client: Client, specs: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict] = []
    for _, row in specs.iterrows():
        try:
            book = client.market_data.get_order_book(figi=row["figi"], depth=10)
            bid = q(book.bids[0].price) if book.bids else 0.0
            ask = q(book.asks[0].price) if book.asks else 0.0
            bid_qty = int(book.bids[0].quantity) if book.bids else 0
            ask_qty = int(book.asks[0].quantity) if book.asks else 0
            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
            spread = ask - bid if bid > 0 and ask > 0 else 0.0
            rows.append(
                {
                    "ticker": row["ticker"],
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "spread": spread,
                    "spread_ticks": spread / float(row["tick"]) if row["tick"] else 0.0,
                    "spread_bps": spread / mid * 10000 if mid else 0.0,
                    "bid_qty_lots_top1": bid_qty,
                    "ask_qty_lots_top1": ask_qty,
                    "bid_notional_top1_rub": bid * bid_qty * int(row["lot"]),
                    "ask_notional_top1_rub": ask * ask_qty * int(row["lot"]),
                }
            )
        except Exception as exc:  # noqa: BLE001
            rows.append({"ticker": row["ticker"], "error": f"{type(exc).__name__}: {exc}"})
        time.sleep(0.03)
    return pd.DataFrame(rows)


def rank_liquidity(client: Client, specs: pd.DataFrame, rank_days: int, sleep_sec: float, retries: int, force: bool) -> pd.DataFrame:
    rank_dir = DATA_DIR / "_rank_recent"
    rank_dir.mkdir(parents=True, exist_ok=True)
    date_to = utc_now()
    date_from = date_to - timedelta(days=rank_days)
    rows: list[dict] = []
    for idx, row in specs.iterrows():
        ticker = str(row["ticker"])
        print(f"rank {idx + 1}/{len(specs)} {ticker}", flush=True)
        df = download_one(client, row, date_from, date_to, rank_dir, sleep_sec, force, retries)
        if df.empty:
            rows.append({"ticker": ticker, "rank_rows": 0, "rank_turnover_rub": 0.0, "rank_volume_lots": 0})
            continue
        rows.append(
            {
                "ticker": ticker,
                "rank_rows": int(len(df)),
                "rank_nonzero_volume_rows": int((df["volume_lots"] > 0).sum()),
                "rank_turnover_rub": float(df["turnover_rub"].sum()),
                "rank_median_1m_turnover_rub": float(df["turnover_rub"].median()),
                "rank_volume_lots": int(df["volume_lots"].sum()),
                "rank_start": str(df["time"].min()),
                "rank_end": str(df["time"].max()),
            }
        )
    rank = pd.DataFrame(rows)
    return specs.merge(rank, on="ticker", how="left").sort_values(
        ["rank_turnover_rub", "rank_nonzero_volume_rows", "ticker"],
        ascending=[False, False, True],
    )


def copy_microstructure(out_dir: Path, selected_tickers: set[str]) -> dict:
    micro_out = out_dir / "microstructure"
    micro_out.mkdir(parents=True, exist_ok=True)
    copied: dict[str, dict] = {}
    candidates = [
        STOCK_RESEARCH_ROOT / "microstructure" / "orderbook_snapshots.csv",
        STOCK_RESEARCH_ROOT / "microstructure" / "last_trades_summary.csv",
        STOCK_RESEARCH_ROOT / "microstructure" / "last_trades.csv",
        STOCK_RESEARCH_ROOT / "microstructure_walkforward" / "microstructure_proxy_by_ticker.csv",
    ]
    for src in candidates:
        if not src.exists():
            continue
        try:
            df = pd.read_csv(src)
            tick_col = next((c for c in ["ticker", "secid", "symbol"] if c in df.columns), None)
            if tick_col:
                df = df[df[tick_col].astype(str).isin(selected_tickers)].copy()
            if src.name == "last_trades.csv" and len(df) > 500_000:
                summary_path = micro_out / "micro_last_trades_sample.csv"
                df.tail(500_000).to_csv(summary_path, index=False)
                dst = summary_path
                note = "tail_sample_500000_rows"
            else:
                dst = micro_out / src.name
                df.to_csv(dst, index=False)
                note = "filtered_selected_tickers"
            copied[src.name] = {
                "path": str(dst),
                "rows": int(len(df)),
                "size_bytes": int(dst.stat().st_size),
                "note": note,
            }
        except Exception as exc:  # noqa: BLE001
            copied[src.name] = {"error": f"{type(exc).__name__}: {exc}"}
    return copied


def write_prompt(path: Path) -> None:
    text = """You are given one or more ZIP files with MOEX TQBR stock data from T-Bank.

Task: test whether the short-term futures idea can be adapted to liquid MOEX stocks.

Use all uploaded ZIP parts. First unzip, load every CSV/parquet, and print exact row counts by ticker.

Data:
- tbank_1m_top_stocks.csv: 1-minute candles for 2026.
- stock_specs.csv: ticker, lot, tick, tick_rub_per_lot, short_enabled.
- stock_liquidity_rank.csv: recent turnover/liquidity ranking.
- current_orderbook_snapshot.csv: current spread/top book snapshot.
- microstructure/*.csv: live-collected orderbook and tape context, only from 2026-05-21 onward.

Important microstructure rule:
Do not use live microstructure data from 2026-05-21 onward as if it existed before that date. Use it only for execution filters, spread/liquidity classes, and live-paper sizing policy.

Core strategy idea:
- Short-term directional entry.
- Immediate protective stop.
- Favorable move activates trailing stop.
- Exit by trailing stop, protective stop, or max hold.
- Compare two exit models:
  1. soft_gpt_model: trailing activation and candle-like pessimistic execution as in the futures GPT backtests.
  2. hard_tick_model: stop follows every favorable tick after activation; if stop-limit is not filled, emergency market exit is assumed with extra slippage.

Costs:
- side_commission_rate = 0.00025 unless another exact tariff is supplied.
- round_turn_commission = 2 * side_commission_rate * traded_notional_rub.
- traded_notional_rub = price * lot * quantity_lots.
- Evaluate slippage stress: 0, 1, 2, 3, 4, 5 ticks round trip.
- Include spread cost stress using bid/ask proxy and current/live microstructure where available.

Sizing:
- Stocks have no futures GO. Use capital and ruble stop risk.
- For per-profile statistics assume 1 lot.
- Also report portfolio simulations with max full stop risk per ticker: 500, 1000, 2000, 4000 RUB.
- Position quantity must be reduced by ruble stop risk, not by narrowing the stop.

Search:
Use deterministic staged search. Do not use random sampling.

Stage 0: audit
- loaded tickers, row counts, start/end dates
- short_enabled split
- tick/lot/spread/turnover stats

Stage 1: broad deterministic coarse grid for every ticker
Signal families:
- momentum_breakout
- vwap_impulse
- range_expansion
- trend_pullback
- pure_trailing_after_impulse
- opening_range_impulse
- liquidity_filtered_momentum

Directions:
- long
- short only if short_enabled=true
- both only if short_enabled=true

Parameters:
- momentum_pct: 0.0002,0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01
- momentum_ticks: 1,2,3,5,8,13,21,34,55,89
- breakout_lookback: 3,5,8,13,21,34,55,89,144
- trend_fast: 3,5,8,13,21
- trend_slow: 8,13,21,34,55,89,144, only trend_slow > trend_fast
- volume_multiplier: 0,0.5,0.8,1.0,1.2,1.5,2.0,2.5,3.0,4.0
- volume_window: 20,40,60,120
- vwap_mode: disabled, rolling20, rolling60, session
- vwap_buffer_pct: 0,0.0001,0.0002,0.0005,0.001,0.002
- stop_pct: 0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02
- trail_pct: 0.0002,0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01
- trail_activation_pct: 0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02
- direct_stop_ticks: 1,2,3,5,8,13,21,34,55,89,144,233
- direct_trail_ticks: 1,2,3,5,8,13,21,34,55,89,144
- direct_activation_ticks: 1,2,3,5,8,13,21,34,55,89,144
- cooldown_minutes: 0,1,3,5,10,20,40,60
- max_hold_minutes: 3,5,10,15,30,60,90,120
- session_filter: main_session, exclude_first_last_10min, morning_only, afternoon_only, evening_if_data_exists

Stage 2: fine expansion
For every survivor, expand all adjacent declared parameter values. Do not silently top-N prune. If needed, split survivors into multiple result groups by ticker liquidity rank and profile family.

Stage 3: stress validation
- train first 70%, test last 30%, full
- rolling windows: 60d/20d, 90d/30d, expanding/next month
- slippage 0..5 ticks
- remove best 1/3/5 trades
- drawdown, losing streaks
- time-of-day breakdown
- long/short breakdown
- microstructure spread/liquidity classes

Stage 4: neighborhood validation
For final candidates, run deterministic neighbors within +/-1 and +/-2 grid indices. Mark PARAMETER_SPIKE if neighborhood profitable rate at 2T < 50%.

Ranking:
Do not rank by single highest historical profit. Prefer:
- profitable on test at 2T after commission
- survives 3T stress or degrades mildly
- remove_best_3 still positive
- stable rolling windows
- stable neighborhood
- full stop rub not too large relative to median winning trade
- high enough trade count
- low spread/stop and commission/stop pressure

Required output: put all results into one ZIP.
Inside the ZIP include:
- stock_stage0_audit.csv
- stock_grid_results.csv
- stock_top_profiles_by_ticker.csv
- stock_stress_summary.csv
- stock_final_live_paper_profiles.json
- stock_rejected_tickers.csv
- stock_slippage_stress.csv
- stock_rolling_walkforward.csv
- stock_neighborhood_summary.csv
- stock_time_of_day_breakdown.csv
- stock_direction_breakdown.csv
- stock_ruble_stop_risk_summary.csv
- stock_microstructure_policy.csv
- stock_portfolio_simulation.csv
- stock_project_diagnostics.json
- README_RESULT.md

Final answer in Russian:
1. Say whether results are enough for live-paper.
2. Show LIVE_NOW candidates.
3. Show WATCHLIST candidates.
4. Show rejected candidates by reason.
5. Show expected trades/day and commission/day.
6. Show per-ticker recommended quantity for stop-risk 500/1000/2000/4000 RUB.
7. Explicitly compare soft_gpt_model vs hard_tick_model.
"""
    path.write_text(text, encoding="utf-8")


def write_split_packages(
    specs: pd.DataFrame,
    counts: pd.DataFrame,
    panel: pd.DataFrame,
    max_part_mb: int,
    common_files: list[Path],
) -> list[Path]:
    base_name = "stock_1m_scalp_gpt_package"
    zip_paths: list[Path] = []
    max_bytes = max_part_mb * 1024 * 1024
    tickers = [str(t) for t in counts[counts["rows"] > 0]["ticker"].tolist()]
    part = 1
    current: list[str] = []
    current_bytes = 0
    manifest_rows: list[dict] = []

    def flush(tickers_part: list[str], part_no: int) -> Path | None:
        if not tickers_part:
            return None
        part_df = panel[panel["secid"].astype(str).isin(tickers_part)].copy()
        part_counts = counts[counts["ticker"].astype(str).isin(tickers_part)].copy()
        csv_path = PACKAGE_DIR / f"tbank_1m_top_stocks_part_{part_no:02d}.csv"
        counts_path = PACKAGE_DIR / f"row_counts_part_{part_no:02d}.csv"
        part_df.to_csv(csv_path, index=False)
        part_counts.to_csv(counts_path, index=False)
        zip_path = REPORTS / f"{base_name}_part_{part_no:02d}.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.write(csv_path, csv_path.name)
            zf.write(counts_path, counts_path.name)
            for f in common_files:
                if f.exists():
                    zf.write(f, f.relative_to(PACKAGE_DIR).as_posix() if f.is_relative_to(PACKAGE_DIR) else f.name)
            micro_dir = PACKAGE_DIR / "microstructure"
            if micro_dir.exists():
                for micro_file in micro_dir.glob("*.csv"):
                    zf.write(micro_file, f"microstructure/{micro_file.name}")
        manifest_rows.append(
            {
                "part": part_no,
                "zip": zip_path.name,
                "tickers": ",".join(tickers_part),
                "ticker_count": len(tickers_part),
                "rows": int(len(part_df)),
                "zip_mb": round(zip_path.stat().st_size / 1024 / 1024, 3),
            }
        )
        return zip_path

    for ticker in tickers:
        path = DATA_DIR / f"{ticker}_1m.csv"
        est = path.stat().st_size if path.exists() else int(counts.loc[counts["ticker"] == ticker, "rows"].iloc[0]) * 90
        if current and current_bytes + est > max_bytes:
            zp = flush(current, part)
            if zp:
                zip_paths.append(zp)
            part += 1
            current = []
            current_bytes = 0
        current.append(ticker)
        current_bytes += est
    zp = flush(current, part)
    if zp:
        zip_paths.append(zp)
    pd.DataFrame(manifest_rows).to_csv(PACKAGE_DIR / "split_manifest.csv", index=False)
    return zip_paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-date", default="2026-01-01")
    parser.add_argument("--to-date", default="now")
    parser.add_argument("--top-n", type=int, default=50)
    parser.add_argument("--rank-days", type=int, default=10)
    parser.add_argument("--sleep-sec", type=float, default=0.03)
    parser.add_argument("--retries", type=int, default=20)
    parser.add_argument("--split-max-mb", type=int, default=80)
    parser.add_argument("--force-rank", action="store_true")
    parser.add_argument("--force-candles", action="store_true")
    parser.add_argument("--history-archive", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--history-verify-tls", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--allow-insecure-history-tls-fallback", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--tail-after-archive", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()

    date_from = parse_dt(args.from_date)
    date_to = parse_dt(args.to_date)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)

    token = find_tbank_token()
    with Client(token) as client:
        specs = list_tqbr_shares(client)
        specs.to_csv(PACKAGE_DIR / "stock_specs_all_tqbr.csv", index=False)
        print(f"tqbr_api_tradeable={len(specs)}", flush=True)
        rank_cache = PACKAGE_DIR / "stock_liquidity_rank_all_tqbr.csv"
        if rank_cache.exists() and not args.force_rank:
            liquidity = pd.read_csv(rank_cache)
            print(f"rank cache reused rows={len(liquidity)}", flush=True)
        else:
            liquidity = rank_liquidity(client, specs, args.rank_days, args.sleep_sec, args.retries, args.force_rank)
        liquidity.to_csv(PACKAGE_DIR / "stock_liquidity_rank_all_tqbr.csv", index=False)
        selected = liquidity.head(args.top_n).copy().reset_index(drop=True)
        selected.insert(0, "liquidity_rank", range(1, len(selected) + 1))
        selected.to_csv(PACKAGE_DIR / "stock_specs.csv", index=False)
        selected.to_csv(PACKAGE_DIR / "stock_liquidity_rank.csv", index=False)
        orderbook = add_orderbook_snapshot(client, selected)
        orderbook.to_csv(PACKAGE_DIR / "current_orderbook_snapshot.csv", index=False)

        if args.download:
            history_meta: list[dict] = []
            for idx, row in selected.iterrows():
                print(f"download {idx + 1}/{len(selected)} {row['ticker']}", flush=True)
                if args.history_archive:
                    df, meta = download_history_archive_one(
                        token,
                        row,
                        date_from.year,
                        date_from,
                        date_to,
                        DATA_DIR,
                        args.retries,
                        args.history_verify_tls,
                        args.allow_insecure_history_tls_fallback,
                    )
                    history_meta.append(meta)
                    print(
                        f"{row['ticker']} archive rows={len(df)} bytes={meta.get('bytes', 0)} "
                        f"insecure_tls={meta.get('used_insecure_tls_fallback', False)} "
                        f"error={meta.get('error', '')[:80]}",
                        flush=True,
                    )
                    if args.tail_after_archive:
                        df = download_one(
                            client,
                            row,
                            date_from,
                            date_to,
                            DATA_DIR,
                            args.sleep_sec,
                            False,
                            args.retries,
                        )
                else:
                    df = download_one(
                        client,
                        row,
                        date_from,
                        date_to,
                        DATA_DIR,
                        args.sleep_sec,
                        args.force_candles,
                        args.retries,
                    )
                print(f"{row['ticker']} rows={len(df)}", flush=True)
            pd.DataFrame(history_meta).to_csv(PACKAGE_DIR / "history_archive_download_log.csv", index=False)

    frames: list[pd.DataFrame] = []
    counts: list[dict] = []
    for _, row in selected.iterrows():
        ticker = str(row["ticker"])
        df = read_existing(DATA_DIR / f"{ticker}_1m.csv")
        counts.append(
            {
                "ticker": ticker,
                "rows": int(len(df)),
                "start": str(df["time"].min()) if not df.empty else "",
                "end": str(df["time"].max()) if not df.empty else "",
            }
        )
        if not df.empty:
            frames.append(df)
    counts_df = pd.DataFrame(counts)
    counts_df.to_csv(PACKAGE_DIR / "row_counts.csv", index=False)
    panel = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
    if not panel.empty:
        panel = panel.drop_duplicates(["secid", "time"]).sort_values(["secid", "time"]).reset_index(drop=True)
        panel.to_csv(PACKAGE_DIR / "tbank_1m_top_stocks.csv", index=False)
        try:
            panel.to_parquet(PACKAGE_DIR / "tbank_1m_top_stocks.parquet", index=False)
        except Exception:
            pass

    micro = copy_microstructure(PACKAGE_DIR, set(selected["ticker"].astype(str)))
    write_prompt(PACKAGE_DIR / "STOCKS_SCALP_PROMPT_FOR_GPT.md")
    metadata = {
        "created_at_utc": utc_now().isoformat(),
        "source": "T-Bank Invest API",
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "top_n": int(args.top_n),
        "rank_days": int(args.rank_days),
        "tqbr_api_tradeable": int(len(specs)),
        "selected_tickers": selected["ticker"].astype(str).tolist(),
        "total_rows": int(counts_df["rows"].sum()) if not counts_df.empty else 0,
        "tickers_with_rows": int((counts_df["rows"] > 0).sum()) if not counts_df.empty else 0,
        "microstructure": micro,
    }
    (PACKAGE_DIR / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    common_files = [
        PACKAGE_DIR / "stock_specs.csv",
        PACKAGE_DIR / "stock_specs_all_tqbr.csv",
        PACKAGE_DIR / "stock_liquidity_rank.csv",
        PACKAGE_DIR / "stock_liquidity_rank_all_tqbr.csv",
        PACKAGE_DIR / "current_orderbook_snapshot.csv",
        PACKAGE_DIR / "history_archive_download_log.csv",
        PACKAGE_DIR / "row_counts.csv",
        PACKAGE_DIR / "metadata.json",
        PACKAGE_DIR / "STOCKS_SCALP_PROMPT_FOR_GPT.md",
    ]

    zip_path = REPORTS / "stock_1m_scalp_gpt_package.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        if not panel.empty:
            zf.write(PACKAGE_DIR / "tbank_1m_top_stocks.csv", "tbank_1m_top_stocks.csv")
        for f in common_files:
            if f.exists():
                zf.write(f, f.name)
        micro_dir = PACKAGE_DIR / "microstructure"
        if micro_dir.exists():
            for micro_file in micro_dir.glob("*.csv"):
                zf.write(micro_file, f"microstructure/{micro_file.name}")

    split_paths = write_split_packages(selected, counts_df, panel, args.split_max_mb, common_files)
    shutil.copy2(PACKAGE_DIR / "STOCKS_SCALP_PROMPT_FOR_GPT.md", REPORTS / "STOCKS_SCALP_PROMPT_FOR_GPT.md")
    print(json.dumps(metadata, ensure_ascii=False, indent=2), flush=True)
    print(f"zip={zip_path} bytes={zip_path.stat().st_size}", flush=True)
    for path in split_paths:
        print(f"split_zip={path} bytes={path.stat().st_size}", flush=True)


if __name__ == "__main__":
    main()
