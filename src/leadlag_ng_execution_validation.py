from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

from leadlag_ng_moex import ROOT, REPORTS, ensure_dirs


DATA_RAW_1M = ROOT / "data" / "raw" / "leadlag_ng_1m_execution"
PLOTS = ROOT / "plots"
MOEX_CANDLES_URL = "https://iss.moex.com/iss/engines/futures/markets/forts/securities/{secid}/candles.json"

MIN_STEP = 0.001
TICK_VALUE_USD = 0.1
COST_SCENARIOS = [(1, 1), (2, 2), (3, 5), (4, 10)]
EXECUTION_MODES = [
    "old_open_next",
    "delayed_one_10m_bar",
    "next_1m_after_signal",
    "adverse_1m_fill",
    "high_low_touch_check",
]


@dataclass(frozen=True)
class Config:
    force: bool
    request_sleep: float


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def read_csv(path: Path, date_cols: list[str] | None = None) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, parse_dates=date_cols or [])


def request_json(url: str, params: dict, retries: int = 4, sleep: float = 0.5) -> dict:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.get(url, params=params, timeout=60)
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(sleep * (attempt + 1))
    raise RuntimeError(f"MOEX request failed: {url} {params}") from last_error


def iss_table(payload: dict, table: str) -> pd.DataFrame:
    block = payload.get(table, {})
    return pd.DataFrame(block.get("data", []), columns=block.get("columns", []))


def download_1m_for_secid(secid: str, cfg: Config) -> pd.DataFrame:
    DATA_RAW_1M.mkdir(parents=True, exist_ok=True)
    path = DATA_RAW_1M / f"{secid}_1m.csv"
    if path.exists() and not cfg.force:
        return read_csv(path, ["begin", "end"])
    rows = []
    start = 0
    url = MOEX_CANDLES_URL.format(secid=secid)
    while True:
        payload = request_json(
            url,
            {
                "interval": 1,
                "from": "2024-05-23",
                "till": "2026-05-23",
                "start": start,
                "iss.meta": "off",
            },
            sleep=cfg.request_sleep,
        )
        chunk = iss_table(payload, "candles")
        if chunk.empty:
            break
        chunk["target_contract"] = secid
        rows.append(chunk)
        start += len(chunk)
        time.sleep(cfg.request_sleep)
    out = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    if not out.empty:
        out["begin"] = pd.to_datetime(out["begin"])
        out["end"] = pd.to_datetime(out["end"])
        for col in ["open", "high", "low", "close", "volume"]:
            if col in out:
                out[col] = pd.to_numeric(out[col], errors="coerce")
        out = out.drop_duplicates(["target_contract", "begin"]).sort_values(["target_contract", "begin"])
    out.to_csv(path, index=False)
    log(f"{secid} 1m rows: {len(out):,}")
    return out


def load_1m_candles(contracts: list[str], cfg: Config) -> pd.DataFrame:
    frames = [download_1m_for_secid(secid, cfg) for secid in contracts]
    return pd.concat([x for x in frames if not x.empty], ignore_index=True) if frames else pd.DataFrame()


def load_10m_open_map() -> pd.DataFrame:
    features = read_csv(ROOT / "data" / "processed" / "leadlag_ng_10m" / "features.csv", ["begin"])
    if features.empty:
        raise RuntimeError("Missing features.csv; run leadlag pipeline first.")
    cols = ["target_contract", "begin", "open_10m"]
    out = features.rename(columns={"secid_front": "target_contract", "open_front": "open_10m"})[cols].copy()
    out = out.drop_duplicates(["target_contract", "begin"])
    return out


def selected_candidate_signals() -> pd.DataFrame:
    trades = read_csv(REPORTS / "third_pass_strategy_trades.csv", ["begin_signal", "entry_begin", "exit_begin"])
    if trades.empty:
        raise RuntimeError("Missing third_pass_strategy_trades.csv; run unit-corrected third pass first.")
    mask = (
        (trades["strategy_mode"] == "fixed_plus1_only")
        & (trades["portfolio_mode"] == "global_no_overlap")
        & (trades["threshold_objective"] == "train_mean")
        & (trades[["slippage_ticks_roundtrip", "fee_rub_per_contract_roundtrip"]].apply(tuple, axis=1).isin(COST_SCENARIOS))
    )
    cols = [
        "test_month",
        "sample_month",
        "begin_signal",
        "target_contract",
        "plus1_contract",
        "plus2_contract",
        "plus3_contract",
        "prediction",
        "abs_prediction",
        "signal_direction",
        "entry_begin",
        "exit_begin",
        "entry_open",
        "exit_open",
        "usd_rub_rate",
        "fx_source",
        "initial_margin_rub",
        "margin_source",
        "selected_threshold",
        "selected_threshold_type",
        "slippage_ticks_roundtrip",
        "fee_rub_per_contract_roundtrip",
    ]
    out = trades.loc[mask, [c for c in cols if c in trades]].copy()
    out = out.drop_duplicates(["begin_signal", "target_contract", "slippage_ticks_roundtrip", "fee_rub_per_contract_roundtrip"]).sort_values("begin_signal")
    if out.empty:
        raise RuntimeError("No fixed_plus1_only/global_no_overlap candidate signals found.")
    return out


def apply_global_no_overlap(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades
    kept = []
    active_until = pd.Timestamp.min
    for _, row in trades.sort_values(["entry_begin", "exit_begin", "begin_signal"]).iterrows():
        entry = pd.Timestamp(row["entry_begin"])
        if entry < active_until:
            continue
        kept.append(row)
        active_until = pd.Timestamp(row["exit_begin"])
    return pd.DataFrame(kept).reset_index(drop=True) if kept else trades.iloc[0:0].copy()


def add_pnl(trades: pd.DataFrame, slippage_ticks: int, fee_rub: float) -> pd.DataFrame:
    out = trades.copy()
    if out.empty:
        return out
    out["price_delta"] = out["exit_open"] - out["entry_open"]
    out["raw_ticks"] = out["price_delta"] / MIN_STEP
    out["signed_ticks"] = out["signal_direction"] * out["raw_ticks"]
    out["tick_value_usd"] = TICK_VALUE_USD
    out["tick_value_rub"] = TICK_VALUE_USD * out["usd_rub_rate"]
    out["gross_pnl_rub"] = out["signed_ticks"] * out["tick_value_rub"]
    out["slippage_ticks_roundtrip"] = slippage_ticks
    out["fee_rub_per_contract_roundtrip"] = fee_rub
    out["net_pnl_rub"] = out["gross_pnl_rub"] - slippage_ticks * out["tick_value_rub"] - fee_rub
    out["return_on_go"] = out["net_pnl_rub"] / out["initial_margin_rub"]
    out["horizon_minutes"] = 30
    return out


def build_old_open_next(signals: pd.DataFrame) -> pd.DataFrame:
    out = signals.copy()
    out["execution_mode"] = "old_open_next"
    out["skipped_reason"] = ""
    out["fill_verified"] = True
    out["fill_unverified"] = False
    return out


def build_delayed(signals: pd.DataFrame, open10: pd.DataFrame) -> pd.DataFrame:
    rows = []
    open_map = open10.set_index(["target_contract", "begin"])["open_10m"].to_dict()
    for _, row in signals.iterrows():
        entry = pd.Timestamp(row["begin_signal"]) + pd.Timedelta(minutes=20)
        exit_time = entry + pd.Timedelta(minutes=30)
        key_entry = (row["target_contract"], entry)
        key_exit = (row["target_contract"], exit_time)
        rec = row.to_dict()
        rec.update({"execution_mode": "delayed_one_10m_bar", "entry_begin": entry, "exit_begin": exit_time})
        if key_entry not in open_map or key_exit not in open_map:
            rec.update({"skipped_reason": "missing_10m_execution_candle", "entry_open": np.nan, "exit_open": np.nan})
        else:
            rec.update({"skipped_reason": "", "entry_open": open_map[key_entry], "exit_open": open_map[key_exit]})
        rec["fill_verified"] = rec["skipped_reason"] == ""
        rec["fill_unverified"] = False
        rows.append(rec)
    return pd.DataFrame(rows)


def one_minute_lookup(candles1m: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {secid: g.sort_values("begin").reset_index(drop=True) for secid, g in candles1m.groupby("target_contract")}


def first_1m_at_or_after(g: pd.DataFrame, timestamp: pd.Timestamp) -> pd.Series | None:
    idx = g["begin"].searchsorted(timestamp)
    if idx >= len(g):
        return None
    return g.iloc[int(idx)]


def build_1m_mode(signals: pd.DataFrame, candles1m: pd.DataFrame, mode: str) -> pd.DataFrame:
    by_contract = one_minute_lookup(candles1m)
    rows = []
    for _, row in signals.iterrows():
        rec = row.to_dict()
        rec["execution_mode"] = mode
        signal_time = pd.Timestamp(row["begin_signal"]) + pd.Timedelta(minutes=10)
        g = by_contract.get(row["target_contract"])
        if g is None or g.empty:
            rec.update({"skipped_reason": "missing_1m_contract", "entry_open": np.nan, "exit_open": np.nan, "fill_verified": False, "fill_unverified": True})
            rows.append(rec)
            continue
        entry_bar = first_1m_at_or_after(g, signal_time)
        if entry_bar is None:
            rec.update({"skipped_reason": "missing_1m_entry", "entry_open": np.nan, "exit_open": np.nan, "fill_verified": False, "fill_unverified": True})
            rows.append(rec)
            continue
        exit_target = pd.Timestamp(entry_bar["begin"]) + pd.Timedelta(minutes=30)
        exit_bar = first_1m_at_or_after(g, exit_target)
        if exit_bar is None:
            rec.update({"skipped_reason": "missing_1m_exit", "entry_open": entry_bar["open"], "exit_open": np.nan, "fill_verified": False, "fill_unverified": True})
            rows.append(rec)
            continue
        entry_price = float(entry_bar["open"])
        exit_price = float(exit_bar["open"])
        if mode == "adverse_1m_fill":
            if int(row["signal_direction"]) > 0:
                entry_price += MIN_STEP
                exit_price -= MIN_STEP
            else:
                entry_price -= MIN_STEP
                exit_price += MIN_STEP
        entry_touch = float(entry_bar["low"]) - 1e-12 <= entry_price <= float(entry_bar["high"]) + 1e-12
        exit_touch = float(exit_bar["low"]) - 1e-12 <= exit_price <= float(exit_bar["high"]) + 1e-12
        rec.update(
            {
                "entry_begin": entry_bar["begin"],
                "exit_begin": exit_bar["begin"],
                "entry_open": entry_price,
                "exit_open": exit_price,
                "entry_1m_low": entry_bar["low"],
                "entry_1m_high": entry_bar["high"],
                "exit_1m_low": exit_bar["low"],
                "exit_1m_high": exit_bar["high"],
                "skipped_reason": "",
                "fill_verified": bool(entry_touch and exit_touch),
                "fill_unverified": bool(not (entry_touch and exit_touch)),
            }
        )
        rows.append(rec)
    return pd.DataFrame(rows)


def build_execution_trades(signals: pd.DataFrame, open10: pd.DataFrame, candles1m: pd.DataFrame) -> pd.DataFrame:
    frames = [
        build_old_open_next(signals),
        build_delayed(signals, open10),
        build_1m_mode(signals, candles1m, "next_1m_after_signal"),
        build_1m_mode(signals, candles1m, "adverse_1m_fill"),
        build_1m_mode(signals, candles1m, "high_low_touch_check"),
    ]
    base = pd.concat(frames, ignore_index=True)
    out_frames = []
    for (mode, slippage, fee), g in base.groupby(["execution_mode", "slippage_ticks_roundtrip", "fee_rub_per_contract_roundtrip"]):
        valid = g[g["skipped_reason"].fillna("") == ""].copy()
        if mode == "old_open_next":
            selected = valid.copy()
        else:
            selected = apply_global_no_overlap(valid)
        skipped = g[g["skipped_reason"].fillna("") != ""].copy()
        selected["portfolio_mode"] = "global_no_overlap"
        skipped["portfolio_mode"] = "global_no_overlap"
        combined = pd.concat([selected, skipped], ignore_index=True)
        costed = add_pnl(combined, int(slippage), float(fee))
        out_frames.append(costed)
    out = pd.concat(out_frames, ignore_index=True) if out_frames else pd.DataFrame()
    out["strategy_mode"] = "fixed_plus1_only"
    return out.sort_values(["execution_mode", "slippage_ticks_roundtrip", "fee_rub_per_contract_roundtrip", "begin_signal"])


def equity_metrics(trades: pd.DataFrame) -> tuple[float, float]:
    if trades.empty:
        return np.nan, 0.0
    events = []
    for _, row in trades.iterrows():
        events.append({"time": row["entry_begin"], "order": 1, "pnl": 0.0})
        events.append({"time": row["exit_begin"], "order": 0, "pnl": row["net_pnl_rub"]})
    ev = pd.DataFrame(events).sort_values(["time", "order"])
    ev["equity"] = ev["pnl"].cumsum()
    dd = ev["equity"] - ev["equity"].cummax()
    return float(dd.min()), float(ev["equity"].iloc[-1])


def summarize(trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    valid = trades[trades["skipped_reason"].fillna("") == ""].copy()
    rows = []
    month_rows = []
    fill_rows = []
    for keys, g in valid.groupby(["execution_mode", "slippage_ticks_roundtrip", "fee_rub_per_contract_roundtrip"]):
        mode, slippage, fee = keys
        monthly = g.groupby("test_month")["net_pnl_rub"].sum()
        max_dd, total = equity_metrics(g)
        rows.append(
            {
                "execution_mode": mode,
                "slippage_ticks_roundtrip": slippage,
                "fee_rub_per_contract_roundtrip": fee,
                "n_trades": len(g),
                "skipped_trades": int(((trades["execution_mode"] == mode) & (trades["slippage_ticks_roundtrip"] == slippage) & (trades["fee_rub_per_contract_roundtrip"] == fee) & (trades["skipped_reason"].fillna("") != "")).sum()),
                "fill_unverified": int(g["fill_unverified"].fillna(False).sum()),
                "gross_pnl_rub_sum": float(g["gross_pnl_rub"].sum()),
                "net_pnl_rub_sum": float(g["net_pnl_rub"].sum()),
                "mean_net_pnl_rub": float(g["net_pnl_rub"].mean()),
                "net_hit_rate": float((g["net_pnl_rub"] > 0).mean()),
                "positive_months": int((monthly > 0).sum()),
                "total_months": int(monthly.size),
                "max_drawdown_rub": max_dd,
                "passes_costs_boolean": bool(total > 0 and (monthly > 0).mean() >= 0.55 and len(g) >= 100),
                "order_book_bid_ask_available": False,
            }
        )
        for month, mg in g.groupby("test_month"):
            month_rows.append(
                {
                    "execution_mode": mode,
                    "slippage_ticks_roundtrip": slippage,
                    "fee_rub_per_contract_roundtrip": fee,
                    "test_month": month,
                    "n_trades": len(mg),
                    "net_pnl_rub_sum": float(mg["net_pnl_rub"].sum()),
                    "mean_net_pnl_rub": float(mg["net_pnl_rub"].mean()),
                    "hit_rate": float((mg["net_pnl_rub"] > 0).mean()),
                    "positive_month": bool(mg["net_pnl_rub"].sum() > 0),
                }
            )
    for mode, g in trades.groupby("execution_mode"):
        fill_rows.append(
            {
                "execution_mode": mode,
                "rows": len(g),
                "skipped_rows": int((g["skipped_reason"].fillna("") != "").sum()),
                "fill_unverified_rows": int(g["fill_unverified"].fillna(False).sum()),
                "fill_verified_rows": int(g["fill_verified"].fillna(False).sum()),
                "order_book_bid_ask_available": False,
                "note": "Historical bid/ask/order book was not available in this pipeline.",
            }
        )
    return pd.DataFrame(rows), pd.DataFrame(month_rows), pd.DataFrame(fill_rows)


def write_report(summary: pd.DataFrame, fill: pd.DataFrame) -> None:
    def line(mode: str, slippage: int, fee: int) -> str:
        row = summary[
            (summary["execution_mode"] == mode)
            & (summary["slippage_ticks_roundtrip"] == slippage)
            & (summary["fee_rub_per_contract_roundtrip"] == fee)
        ]
        if row.empty:
            return "нет строк"
        r = row.iloc[0]
        return (
            f"net={r['net_pnl_rub_sum']:.2f} RUB, trades={int(r['n_trades'])}, "
            f"positive_months={int(r['positive_months'])}/{int(r['total_months'])}, "
            f"maxDD={r['max_drawdown_rub']:.2f} RUB, skipped={int(r['skipped_trades'])}, "
            f"fill_unverified={int(r['fill_unverified'])}"
        )

    old_2 = summary[(summary["execution_mode"] == "old_open_next") & (summary["slippage_ticks_roundtrip"] == 2) & (summary["fee_rub_per_contract_roundtrip"] == 2)]
    delayed_2 = summary[(summary["execution_mode"] == "delayed_one_10m_bar") & (summary["slippage_ticks_roundtrip"] == 2) & (summary["fee_rub_per_contract_roundtrip"] == 2)]
    adverse_2 = summary[(summary["execution_mode"] == "adverse_1m_fill") & (summary["slippage_ticks_roundtrip"] == 2) & (summary["fee_rub_per_contract_roundtrip"] == 2)]
    pass_2 = bool(not adverse_2.empty and adverse_2.iloc[0]["passes_costs_boolean"])
    pass_4 = bool(
        not summary[
            (summary["execution_mode"] == "adverse_1m_fill")
            & (summary["slippage_ticks_roundtrip"] == 4)
            & (summary["fee_rub_per_contract_roundtrip"] == 10)
        ].empty
        and summary[
            (summary["execution_mode"] == "adverse_1m_fill")
            & (summary["slippage_ticks_roundtrip"] == 4)
            & (summary["fee_rub_per_contract_roundtrip"] == 10)
        ].iloc[0]["passes_costs_boolean"]
    )
    edge_fragile = (
        (not old_2.empty and old_2.iloc[0]["net_pnl_rub_sum"] > 0)
        and ((not delayed_2.empty and delayed_2.iloc[0]["net_pnl_rub_sum"] <= 0) or (not adverse_2.empty and adverse_2.iloc[0]["net_pnl_rub_sum"] <= 0))
    )
    lines = [
        "# Execution validation MOEX NG lead-lag",
        "",
        "Проверяется только уже выбранный кандидат: `fixed_plus1_only`, horizon `30m`, portfolio `global_no_overlap`. Новые фичи, feature selection и оптимизация стратегии не добавлялись.",
        "",
        "Unit logic: `tick_value_usd=0.1`, `tick_value_rub=0.1*USD/RUB`. Historical bid/ask/order book в этом pipeline недоступен, поэтому bid/ask execution не проверен.",
        "",
        "## 2 ticks + 2 RUB fee",
        f"- old_open_next: {line('old_open_next', 2, 2)}",
        f"- next_1m_after_signal: {line('next_1m_after_signal', 2, 2)}",
        f"- delayed_one_10m_bar: {line('delayed_one_10m_bar', 2, 2)}",
        f"- adverse_1m_fill: {line('adverse_1m_fill', 2, 2)}",
        f"- high_low_touch_check: {line('high_low_touch_check', 2, 2)}",
        "",
        "## Required answers",
        f"- Сколько PnL было в old_open_next: {line('old_open_next', 2, 2)}",
        f"- Сколько осталось в next_1m_after_signal: {line('next_1m_after_signal', 2, 2)}",
        f"- Сколько осталось в delayed_one_10m_bar: {line('delayed_one_10m_bar', 2, 2)}",
        f"- Сколько осталось в adverse_1m_fill: {line('adverse_1m_fill', 2, 2)}",
        f"- Проходит после 2 ticks + 2 RUB fee на adverse_1m_fill: `{pass_2}`.",
        f"- Проходит после 4 ticks + 10 RUB fee на adverse_1m_fill: `{pass_4}`.",
        f"- Fill/order book: bid/ask не проверен; см. `reports/execution_validation_fill_quality.csv`.",
        "",
        "## Decision",
    ]
    if edge_fragile:
        lines.append("Edge исчезает или становится неустойчивым при delayed/adverse исполнении. 10m результат зависит от агрессивного/оптимистичного исполнения; переходить к live trading нельзя.")
    elif pass_2:
        lines.append("Edge сохраняется в проверенных execution assumptions на базовом cost. Следующий шаг - paper order book monitor, а не live trading.")
    else:
        lines.append("Edge не проходит реалистичную execution validation на базовом adverse/fill сценарии. Следующий шаг - только paper/order-book исследование.")
    lines.extend(
        [
            "",
            "## Files",
            "- `reports/execution_validation_trades.csv`",
            "- `reports/execution_validation_summary.csv`",
            "- `reports/execution_validation_by_month.csv`",
            "- `reports/execution_validation_fill_quality.csv`",
        ]
    )
    (REPORTS / "execution_validation_summary_ru.md").write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv: Iterable[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(description="Execution validation for MOEX NG lead-lag.")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--request-sleep", type=float, default=0.08)
    args = parser.parse_args(list(argv) if argv is not None else None)
    return Config(**vars(args))


def run(cfg: Config) -> dict:
    ensure_dirs()
    DATA_RAW_1M.mkdir(parents=True, exist_ok=True)
    signals = selected_candidate_signals()
    open10 = load_10m_open_map()
    contracts = sorted(signals["target_contract"].dropna().unique())
    candles1m = load_1m_candles(contracts, cfg)
    trades = build_execution_trades(signals, open10, candles1m)
    trades.to_csv(REPORTS / "execution_validation_trades.csv", index=False)
    summary, by_month, fill = summarize(trades)
    summary.to_csv(REPORTS / "execution_validation_summary.csv", index=False)
    by_month.to_csv(REPORTS / "execution_validation_by_month.csv", index=False)
    fill.to_csv(REPORTS / "execution_validation_fill_quality.csv", index=False)
    write_report(summary, fill)
    return {
        "signals": len(signals),
        "contracts_1m": len(contracts),
        "candles_1m": len(candles1m),
        "execution_trade_rows": len(trades),
    }


def main(argv: Iterable[str] | None = None) -> int:
    cfg = parse_args(argv)
    result = run(cfg)
    log("Done")
    log(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
