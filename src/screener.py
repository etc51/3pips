from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from run_research import ROOT, add_features, signal_library


DATA_PROCESSED = ROOT / "data" / "processed"
RESULTS = ROOT / "results"
REPORTS = ROOT / "reports"


@dataclass
class ScreenConfig:
    date: str
    source: str
    top: int
    min_trades: int
    min_sharpe: float
    max_p_adj: float
    family: str | None
    out_csv: Path
    out_md: Path


def read_csv(path: Path, date_cols: list[str] | None = None) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, parse_dates=date_cols or [])


def clean_float(value: object, digits: int = 4) -> str:
    if value is None or pd.isna(value):
        return ""
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def action_label(signal_value: float) -> str:
    if signal_value > 0:
        return "LONG"
    if signal_value < 0:
        return "SHORT"
    return "FLAT"


def load_results(source: str) -> pd.DataFrame:
    path = RESULTS / ("top_robust_patterns.csv" if source == "robust" else "full_results.csv")
    df = read_csv(path)
    if df.empty:
        return df
    for col in ["series", "spread"]:
        if col not in df:
            df[col] = np.nan
    return df


def filter_results(df: pd.DataFrame, cfg: ScreenConfig) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out = out[out["n_trades"].fillna(0) >= cfg.min_trades]
    out = out[out["ann_sharpe"].fillna(-np.inf) >= cfg.min_sharpe]
    if "p_adj_bh" in out:
        out = out[out["p_adj_bh"].fillna(1.0) <= cfg.max_p_adj]
    if cfg.family:
        out = out[out["family"].astype(str).str.upper() == cfg.family.upper()]
    return out


def available_date(cont: pd.DataFrame, spreads: pd.DataFrame, requested: str) -> pd.Timestamp:
    frames = [df for df in [cont, spreads] if not df.empty and "date" in df]
    if not frames:
        raise RuntimeError("No processed data found. Run src/run_research.py first.")
    all_dates = pd.concat([df[["date"]] for df in frames], ignore_index=True)["date"].dropna()
    if requested == "latest":
        return pd.Timestamp(all_dates.max()).normalize()
    target = pd.Timestamp(requested).normalize()
    dates = all_dates[all_dates <= target]
    if dates.empty:
        raise RuntimeError(f"No data on or before {target.date()}.")
    return pd.Timestamp(dates.max()).normalize()


def metric_score(row: pd.Series) -> float:
    sharpe = float(row.get("ann_sharpe", 0) or 0)
    walk = float(row.get("walk_test_mean", 0) or 0)
    hit = float(row.get("hit_rate", 0) or 0)
    penalty = abs(float(row.get("max_drawdown", 0) or 0))
    return sharpe + 10 * walk + hit - 0.05 * penalty


def screen_panel(
    panel: pd.DataFrame,
    results: pd.DataFrame,
    instrument_type: str,
    as_of: pd.Timestamp,
) -> pd.DataFrame:
    if panel.empty or results.empty:
        return pd.DataFrame()
    group_cols = ["family", "series"] if instrument_type == "outright" else ["family", "spread"]
    rows: list[dict] = []
    wanted = results[results["instrument_type"] == instrument_type].copy()
    if wanted.empty:
        return pd.DataFrame()

    for keys, group in panel.groupby(group_cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        label = dict(zip(group_cols, keys))
        g = group[group["date"] <= as_of].sort_values("date")
        if g.empty:
            continue
        g = add_features(g)
        signals = signal_library(g)
        latest = g.iloc[-1]
        matching = wanted.copy()
        for col, value in label.items():
            matching = matching[matching[col].astype(str) == str(value)]
        if matching.empty:
            continue

        for _, stat in matching.iterrows():
            pattern = stat["pattern"]
            signal = signals.get(pattern)
            if signal is None or len(signal) == 0:
                continue
            signal_value = float(pd.Series(signal).iloc[-1])
            if not np.isfinite(signal_value) or signal_value == 0:
                continue
            row = {
                "as_of": latest["date"],
                "family": latest.get("family"),
                "instrument_type": instrument_type,
                "series": latest.get("series", ""),
                "spread": latest.get("spread", ""),
                "pattern": pattern,
                "action": action_label(signal_value),
                "signal": signal_value,
                "holding_days": int(stat.get("holding_days", 0)),
                "score": metric_score(stat),
                "ann_sharpe": stat.get("ann_sharpe"),
                "hit_rate": stat.get("hit_rate"),
                "n_trades": stat.get("n_trades"),
                "p_adj_bh": stat.get("p_adj_bh"),
                "walk_test_mean": stat.get("walk_test_mean"),
                "max_drawdown": stat.get("max_drawdown"),
                "price": latest.get("price"),
                "volume": latest.get("volume"),
                "open_interest": latest.get("open_interest"),
                "dte": latest.get("dte"),
                "rsi_14": latest.get("rsi_14"),
                "atr_proxy_14": latest.get("atr_proxy_14"),
                "henry_hub_spot": latest.get("henry_hub_spot"),
                "brent_spot": latest.get("brent_spot"),
                "wti_spot": latest.get("wti_spot"),
                "storage_surplus_bcf": latest.get("storage_surplus_bcf"),
                "storage_change_vs_5y": latest.get("storage_change_vs_5y"),
            }
            if instrument_type == "outright":
                row["secid"] = latest.get("secid")
            else:
                row["front_secid"] = latest.get("front_secid")
                row["back_secid"] = latest.get("back_secid")
                row["front_price"] = latest.get("front_price")
                row["back_price"] = latest.get("back_price")
            rows.append(row)

    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    return out.sort_values(["score", "ann_sharpe", "n_trades"], ascending=[False, False, False])


def build_regime(cont: pd.DataFrame, spreads: pd.DataFrame, as_of: pd.Timestamp, family: str | None) -> pd.DataFrame:
    if cont.empty:
        return pd.DataFrame()
    front = cont[(cont["date"] <= as_of) & (cont["series"] == "front")].copy()
    if family:
        front = front[front["family"].astype(str).str.upper() == family.upper()]
    latest_front = front.sort_values("date").groupby("family", as_index=False).tail(1)
    if latest_front.empty:
        return pd.DataFrame()

    front_next = pd.DataFrame()
    if not spreads.empty:
        front_next = spreads[(spreads["date"] <= as_of) & (spreads["spread"] == "front_next")].copy()
        if family:
            front_next = front_next[front_next["family"].astype(str).str.upper() == family.upper()]
        front_next = front_next.sort_values("date").groupby("family", as_index=False).tail(1)
        front_next = front_next[["family", "price", "back_price"]].rename(
            columns={"price": "front_next_spread", "back_price": "second_price"}
        )

    regime = latest_front.merge(front_next, on="family", how="left")
    regime["basis_to_spot"] = regime["price"] / regime["henry_hub_spot"] - 1
    regime["curve_front_next_pct"] = regime["second_price"] / regime["price"] - 1
    regime["curve_state"] = np.where(
        regime["curve_front_next_pct"] > 0.01,
        "contango",
        np.where(regime["curve_front_next_pct"] < -0.01, "backwardation", "flat"),
    )
    keep = [
        "date",
        "family",
        "secid",
        "price",
        "second_price",
        "curve_state",
        "curve_front_next_pct",
        "basis_to_spot",
        "henry_hub_spot",
        "volume",
        "open_interest",
        "dte",
        "storage_surplus_bcf",
        "storage_change_vs_5y",
    ]
    return regime[[c for c in keep if c in regime]]


def markdown_table(df: pd.DataFrame, columns: list[str]) -> str:
    if df.empty:
        return "Нет строк."
    work = df[[c for c in columns if c in df]].copy()
    headers = list(work.columns)
    rows = []
    for _, row in work.iterrows():
        values = []
        for col in headers:
            value = row[col]
            if isinstance(value, float):
                values.append(clean_float(value))
            elif pd.isna(value):
                values.append("")
            elif isinstance(value, pd.Timestamp):
                values.append(str(value.date()))
            else:
                values.append(str(value))
        rows.append(values)
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    out.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(out)


def write_report(screen: pd.DataFrame, regime: pd.DataFrame, cfg: ScreenConfig, as_of: pd.Timestamp) -> None:
    cfg.out_md.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# NG/NGM screener",
        "",
        f"As of: {as_of.date()}",
        f"Source: {cfg.source}; filters: n_trades >= {cfg.min_trades}, Sharpe >= {cfg.min_sharpe}, q <= {cfg.max_p_adj}",
        "",
        "## Regime",
        markdown_table(
            regime,
            [
                "family",
                "secid",
                "price",
                "second_price",
                "curve_state",
                "curve_front_next_pct",
                "basis_to_spot",
                "henry_hub_spot",
                "volume",
                "open_interest",
                "dte",
            ],
        ),
        "",
        "## Active Signals",
        markdown_table(
            screen.head(cfg.top),
            [
                "family",
                "instrument_type",
                "series",
                "spread",
                "secid",
                "front_secid",
                "back_secid",
                "action",
                "pattern",
                "holding_days",
                "score",
                "ann_sharpe",
                "hit_rate",
                "n_trades",
                "p_adj_bh",
                "price",
                "dte",
            ],
        ),
        "",
        "Note: signals are generated after the latest close and match the backtest convention of entering on the next trading day.",
    ]
    cfg.out_md.write_text("\n".join(lines), encoding="utf-8")


def run(cfg: ScreenConfig) -> pd.DataFrame:
    cont = read_csv(DATA_PROCESSED / "continuous_daily.csv", ["date"])
    spreads = read_csv(DATA_PROCESSED / "calendar_spreads.csv", ["date"])
    as_of = available_date(cont, spreads, cfg.date)
    results = filter_results(load_results(cfg.source), cfg)
    if results.empty and cfg.source == "robust":
        results = filter_results(load_results("full"), cfg)
    if results.empty:
        raise RuntimeError("No historical pattern results matched the screener filters.")

    screen = pd.concat(
        [
            screen_panel(cont, results, "outright", as_of),
            screen_panel(spreads, results, "spread", as_of),
        ],
        ignore_index=True,
    )
    if not screen.empty and cfg.family:
        screen = screen[screen["family"].astype(str).str.upper() == cfg.family.upper()]
    screen = screen.sort_values(["score", "ann_sharpe", "n_trades"], ascending=[False, False, False]).head(cfg.top)

    cfg.out_csv.parent.mkdir(parents=True, exist_ok=True)
    screen.to_csv(cfg.out_csv, index=False)
    regime = build_regime(cont, spreads, as_of, cfg.family)
    write_report(screen, regime, cfg, as_of)
    return screen


def parse_args() -> ScreenConfig:
    parser = argparse.ArgumentParser(description="Daily screener for MOEX NG/NGM pattern signals.")
    parser.add_argument("--date", default="latest", help="YYYY-MM-DD or latest.")
    parser.add_argument("--source", choices=["robust", "full"], default="robust")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--min-trades", type=int, default=20)
    parser.add_argument("--min-sharpe", type=float, default=0.0)
    parser.add_argument("--max-p-adj", type=float, default=0.10)
    parser.add_argument("--family", choices=["NG", "NGM"], default=None)
    parser.add_argument("--out-csv", type=Path, default=RESULTS / "screener_latest.csv")
    parser.add_argument("--out-md", type=Path, default=REPORTS / "screener_latest.md")
    args = parser.parse_args()
    return ScreenConfig(**vars(args))


def main() -> int:
    cfg = parse_args()
    screen = run(cfg)
    print(f"Wrote {cfg.out_csv}")
    print(f"Wrote {cfg.out_md}")
    if screen.empty:
        print("No active signals matched the filters.")
    else:
        cols = ["family", "instrument_type", "series", "spread", "action", "pattern", "holding_days", "score"]
        print(screen[[c for c in cols if c in screen]].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
