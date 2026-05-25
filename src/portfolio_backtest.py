from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

from run_research import ROOT


DATA_RAW = ROOT / "data" / "raw"
DATA_PROCESSED = ROOT / "data" / "processed"
RESULTS = ROOT / "results"
REPORTS = ROOT / "reports"

INITIAL_CAPITAL = 500_000.0
RISK_PER_TRADE = 0.005
MAX_MARGIN_USAGE = 0.20
MARGIN_RATE = 0.30
COMMISSION_BPS = 3.0
DEFAULT_FIXED_STOP_POINTS = 0.15

SPREAD_FORMULAS = {
    "front_next": "front - next",
    "front_winter": "front - winter",
    "summer_winter": "summer - winter",
}


@dataclass(frozen=True)
class StrategyDef:
    strategy_id: str
    description: str
    instrument_type: str
    spread: str | None
    series: str | None
    months: tuple[int, ...]
    side: int
    side_text: str


@dataclass(frozen=True)
class Variant:
    holding_days: int
    stop_mode: str
    stop_value: float
    take_profit_r: float | None
    slippage_bps: float


STRATEGIES = [
    StrategyDef(
        "strategy_A_aug_front_next_short",
        "August front_next short",
        "spread",
        "front_next",
        None,
        (8,),
        -1,
        "short spread = sell front + buy next",
    ),
    StrategyDef(
        "strategy_B_oct_nov_front_next_long",
        "October-November front_next long",
        "spread",
        "front_next",
        None,
        (10, 11),
        1,
        "long spread = buy front + sell next",
    ),
    StrategyDef(
        "strategy_C_nov_second_short",
        "November second-contract short",
        "outright",
        None,
        "second",
        (11,),
        -1,
        "short second-month NG/NGM",
    ),
]


def read_csv(path: Path, date_cols: list[str] | None = None) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, parse_dates=date_cols or [])


def lot_volume(family: str) -> float:
    return 100.0 if family == "NG" else 1.0


def normalize_history() -> pd.DataFrame:
    history = read_csv(DATA_RAW / "moex_history_daily.csv", ["TRADEDATE"])
    if history.empty:
        raise RuntimeError("data/raw/moex_history_daily.csv is empty. Run src/run_research.py first.")
    external = read_csv(DATA_RAW / "external_daily.csv", ["date"])
    h = history.rename(columns={"TRADEDATE": "date", "SECID": "secid"}).copy()
    for col in ["OPEN", "HIGH", "LOW", "CLOSE", "SETTLEPRICE", "VOLUME", "OPENPOSITION", "NUMTRADES"]:
        if col in h:
            h[col.lower()] = pd.to_numeric(h[col], errors="coerce")
    h["price"] = h["settleprice"].where(h["settleprice"].notna(), h["close"])
    h["family"] = h["family"].astype(str)
    keep = [
        "date",
        "secid",
        "family",
        "contract_month",
        "contract_year",
        "contract_ym",
        "open",
        "high",
        "low",
        "close",
        "settleprice",
        "price",
        "volume",
        "openposition",
        "numtrades",
    ]
    h = h[[c for c in keep if c in h]].dropna(subset=["date", "secid", "price"])
    if not external.empty:
        ext = external[["date", "usdrub_cbr"]].copy()
        ext["usdrub_cbr"] = pd.to_numeric(ext["usdrub_cbr"], errors="coerce").ffill()
        h = h.merge(ext, on="date", how="left")
    h["usdrub_cbr"] = pd.to_numeric(h.get("usdrub_cbr"), errors="coerce").ffill().bfill()
    h["lotvolume"] = h["family"].map(lot_volume)
    return h.sort_values(["family", "secid", "date"])


def leg_row_map(history: pd.DataFrame) -> pd.DataFrame:
    return history.set_index(["date", "secid"]).sort_index()


def build_spread_panel(spreads: pd.DataFrame, history: pd.DataFrame, spread_name: str) -> pd.DataFrame:
    if spreads.empty:
        return pd.DataFrame()
    leg_map = leg_row_map(history)
    rows = []
    s = spreads[spreads["spread"] == spread_name].copy()
    for _, row in s.iterrows():
        key1 = (row["date"], row["front_secid"])
        key2 = (row["date"], row["back_secid"])
        if key1 not in leg_map.index or key2 not in leg_map.index:
            continue
        leg1 = leg_map.loc[key1]
        leg2 = leg_map.loc[key2]
        if isinstance(leg1, pd.DataFrame):
            leg1 = leg1.iloc[-1]
        if isinstance(leg2, pd.DataFrame):
            leg2 = leg2.iloc[-1]
        rows.append(
            {
                "date": row["date"],
                "family": row["family"],
                "instrument_type": "spread",
                "spread": spread_name,
                "leg1_secid": row["front_secid"],
                "leg2_secid": row["back_secid"],
                "leg1_price": float(leg1["price"]),
                "leg2_price": float(leg2["price"]),
                "price": float(leg1["price"]) - float(leg2["price"]),
                "leg1_volume": float(leg1.get("volume", np.nan)),
                "leg2_volume": float(leg2.get("volume", np.nan)),
                "leg1_trades": float(leg1.get("numtrades", np.nan)),
                "leg2_trades": float(leg2.get("numtrades", np.nan)),
                "leg1_oi": float(leg1.get("openposition", np.nan)),
                "leg2_oi": float(leg2.get("openposition", np.nan)),
                "usdrub_cbr": float(leg1.get("usdrub_cbr", np.nan)),
                "lotvolume": lot_volume(str(row["family"])),
            }
        )
    out = pd.DataFrame(rows).sort_values(["family", "date"])
    if not out.empty:
        out["atr14"] = out.groupby("family")["price"].transform(lambda x: x.diff().abs().rolling(14, min_periods=5).mean())
        out["atr14_signal"] = out.groupby("family")["atr14"].shift(1)
    return out


def build_outright_panel(cont: pd.DataFrame, history: pd.DataFrame, series: str) -> pd.DataFrame:
    if cont.empty:
        return pd.DataFrame()
    leg_map = leg_row_map(history)
    rows = []
    c = cont[cont["series"] == series].copy()
    for _, row in c.iterrows():
        key = (row["date"], row["secid"])
        if key not in leg_map.index:
            continue
        leg = leg_map.loc[key]
        if isinstance(leg, pd.DataFrame):
            leg = leg.iloc[-1]
        rows.append(
            {
                "date": row["date"],
                "family": row["family"],
                "instrument_type": "outright",
                "series": series,
                "leg1_secid": row["secid"],
                "leg2_secid": "",
                "leg1_price": float(leg["price"]),
                "leg2_price": np.nan,
                "price": float(leg["price"]),
                "leg1_volume": float(leg.get("volume", np.nan)),
                "leg2_volume": np.inf,
                "leg1_trades": float(leg.get("numtrades", np.nan)),
                "leg2_trades": np.inf,
                "leg1_oi": float(leg.get("openposition", np.nan)),
                "leg2_oi": np.inf,
                "usdrub_cbr": float(leg.get("usdrub_cbr", np.nan)),
                "lotvolume": lot_volume(str(row["family"])),
            }
        )
    out = pd.DataFrame(rows).sort_values(["family", "date"])
    if not out.empty:
        out["atr14"] = out.groupby("family")["price"].transform(lambda x: x.diff().abs().rolling(14, min_periods=5).mean())
        out["atr14_signal"] = out.groupby("family")["atr14"].shift(1)
    return out


def is_liquid(row: pd.Series, min_volume: float, min_trades: float, min_oi: float) -> bool:
    checks = [
        row["leg1_volume"] >= min_volume,
        row["leg2_volume"] >= min_volume,
        row["leg1_trades"] >= min_trades,
        row["leg2_trades"] >= min_trades,
        row["leg1_oi"] >= min_oi,
        row["leg2_oi"] >= min_oi,
    ]
    return bool(all(checks))


def stop_distance(row: pd.Series, variant: Variant) -> float:
    if variant.stop_mode == "fixed":
        return float(variant.stop_value)
    value = float(row.get("atr14_signal", np.nan)) * float(variant.stop_value)
    return value if np.isfinite(value) and value > 0 else np.nan


def leg_notional(price: float, row: pd.Series) -> float:
    return abs(float(price) * float(row["lotvolume"]) * float(row["usdrub_cbr"]))


def leg_margin(price: float, row: pd.Series) -> float:
    return leg_notional(price, row) * MARGIN_RATE


def trading_cost(row: pd.Series, variant: Variant, qty: int) -> float:
    per_side_bps = (COMMISSION_BPS + variant.slippage_bps) / 10_000.0
    notional = leg_notional(row["leg1_price"], row)
    if row["instrument_type"] == "spread":
        notional += leg_notional(row["leg2_price"], row)
    return notional * per_side_bps * qty


def margin_required(row: pd.Series, qty: int) -> float:
    margin = leg_margin(row["leg1_price"], row)
    if row["instrument_type"] == "spread":
        margin += leg_margin(row["leg2_price"], row)
    return margin * qty


def daily_position_pnl(prev: pd.Series, cur: pd.Series, side: int, qty: int) -> float:
    lot = float(cur["lotvolume"])
    fx = float(cur["usdrub_cbr"])
    if cur["instrument_type"] == "spread":
        leg1_side = side
        leg2_side = -side
        return qty * lot * fx * (
            leg1_side * (float(cur["leg1_price"]) - float(prev["leg1_price"]))
            + leg2_side * (float(cur["leg2_price"]) - float(prev["leg2_price"]))
        )
    return qty * lot * fx * side * (float(cur["leg1_price"]) - float(prev["leg1_price"]))


def fixed_leg_path(history: pd.DataFrame, entry: pd.Series) -> pd.DataFrame:
    h = history.copy()
    leg1 = h[h["secid"] == entry["leg1_secid"]].copy()
    leg1 = leg1[leg1["date"] >= entry["date"]].sort_values("date")
    if entry["instrument_type"] == "spread":
        leg2 = h[h["secid"] == entry["leg2_secid"]].copy()
        leg2 = leg2[leg2["date"] >= entry["date"]].sort_values("date")
        merged = leg1.merge(leg2, on="date", suffixes=("_1", "_2"), how="inner")
        if merged.empty:
            return pd.DataFrame()
        out = pd.DataFrame(
            {
                "date": merged["date"],
                "family": entry["family"],
                "instrument_type": "spread",
                "spread": entry.get("spread", ""),
                "leg1_secid": entry["leg1_secid"],
                "leg2_secid": entry["leg2_secid"],
                "leg1_price": merged["price_1"].astype(float),
                "leg2_price": merged["price_2"].astype(float),
                "leg1_volume": merged["volume_1"].astype(float),
                "leg2_volume": merged["volume_2"].astype(float),
                "leg1_trades": merged["numtrades_1"].astype(float),
                "leg2_trades": merged["numtrades_2"].astype(float),
                "leg1_oi": merged["openposition_1"].astype(float),
                "leg2_oi": merged["openposition_2"].astype(float),
                "usdrub_cbr": merged["usdrub_cbr_1"].astype(float),
                "lotvolume": lot_volume(str(entry["family"])),
            }
        )
        out["price"] = out["leg1_price"] - out["leg2_price"]
        return out
    out = pd.DataFrame(
        {
            "date": leg1["date"],
            "family": entry["family"],
            "instrument_type": "outright",
            "series": entry.get("series", ""),
            "leg1_secid": entry["leg1_secid"],
            "leg2_secid": "",
            "leg1_price": leg1["price"].astype(float),
            "leg2_price": np.nan,
            "leg1_volume": leg1["volume"].astype(float),
            "leg2_volume": np.inf,
            "leg1_trades": leg1["numtrades"].astype(float),
            "leg2_trades": np.inf,
            "leg1_oi": leg1["openposition"].astype(float),
            "leg2_oi": np.inf,
            "usdrub_cbr": leg1["usdrub_cbr"].astype(float),
            "lotvolume": lot_volume(str(entry["family"])),
        }
    )
    out["price"] = out["leg1_price"]
    return out


def planned_signal_indices(panel: pd.DataFrame, months: tuple[int, ...]) -> list[int]:
    work = panel.reset_index(drop=True).copy()
    active = work["date"].dt.month.isin(months)
    starts = active & ~active.shift(1, fill_value=False)
    return work.index[starts].tolist()


def backtest_variant(
    strategy: StrategyDef,
    family: str,
    panel: pd.DataFrame,
    history: pd.DataFrame,
    variant: Variant,
    min_volume: float,
    min_trades: float,
    min_oi: float,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    g = panel[panel["family"] == family].sort_values("date").reset_index(drop=True).copy()
    if g.empty:
        return pd.DataFrame(), pd.DataFrame(), {"liquidity_rejections": 0, "size_rejections": 0}
    g["date"] = pd.to_datetime(g["date"])
    signal_indices = planned_signal_indices(g, strategy.months)
    daily_pnl = pd.Series(0.0, index=g.index)
    daily_margin = pd.Series(0.0, index=g.index)
    date_to_idx = {pd.Timestamp(d): i for i, d in enumerate(g["date"])}
    trades = []
    liquidity_rejections = 0
    size_rejections = 0
    blocked_until = -1

    for signal_idx in signal_indices:
        entry_idx = signal_idx + 1
        if entry_idx >= len(g) or entry_idx <= blocked_until:
            continue
        entry = g.iloc[entry_idx]
        if not is_liquid(entry, min_volume, min_trades, min_oi):
            liquidity_rejections += 1
            continue
        path = fixed_leg_path(history, entry)
        if path.empty or pd.Timestamp(entry["date"]) not in set(pd.to_datetime(path["date"])):
            liquidity_rejections += 1
            continue
        path = path[pd.to_datetime(path["date"]) >= pd.Timestamp(entry["date"])]
        path = path.reset_index(drop=True)
        path_entry = path.iloc[0]
        dist = stop_distance(entry, variant)
        if not np.isfinite(dist) or dist <= 0:
            size_rejections += 1
            continue
        risk_budget = INITIAL_CAPITAL * RISK_PER_TRADE
        stop_rub_one = dist * float(entry["lotvolume"]) * float(entry["usdrub_cbr"])
        max_qty_by_risk = math.floor(risk_budget / stop_rub_one) if stop_rub_one > 0 else 0
        margin_one = margin_required(path_entry, 1)
        max_qty_by_margin = math.floor(INITIAL_CAPITAL * MAX_MARGIN_USAGE / margin_one) if margin_one > 0 else 0
        qty = int(max(0, min(max_qty_by_risk, max_qty_by_margin)))
        if qty < 1:
            size_rejections += 1
            continue

        entry_cost = trading_cost(path_entry, variant, qty)
        daily_pnl.iloc[entry_idx] -= entry_cost
        daily_margin.iloc[entry_idx] = margin_required(path_entry, qty)

        path_exit_idx = min(variant.holding_days, len(path) - 1)
        exit_reason = "time_stop"
        realized_points = 0.0
        tp_distance = None if variant.take_profit_r is None else dist * variant.take_profit_r

        if len(path) - 1 < variant.holding_days:
            exit_reason = "contract_last_available"

        for i in range(1, min(variant.holding_days, len(path) - 1) + 1):
            cur_points = strategy.side * (float(path.iloc[i]["price"]) - float(path_entry["price"]))
            if cur_points <= -dist:
                path_exit_idx = i
                exit_reason = f"{variant.stop_mode}_stop"
                realized_points = cur_points
                break
            if tp_distance is not None and cur_points >= tp_distance:
                path_exit_idx = i
                exit_reason = f"take_profit_{variant.take_profit_r:g}R"
                realized_points = cur_points
                break
            realized_points = cur_points

        for i in range(1, path_exit_idx + 1):
            pnl = daily_position_pnl(path.iloc[i - 1], path.iloc[i], strategy.side, qty)
            idx = date_to_idx.get(pd.Timestamp(path.iloc[i]["date"]))
            if idx is not None:
                daily_pnl.iloc[idx] += pnl
                daily_margin.iloc[idx] = margin_required(path.iloc[i], qty)

        exit_row = path.iloc[path_exit_idx]
        exit_idx = date_to_idx.get(pd.Timestamp(exit_row["date"]), entry_idx)
        exit_cost = trading_cost(exit_row, variant, qty)
        daily_pnl.iloc[exit_idx] -= exit_cost
        trade_pnl = float(daily_pnl.iloc[entry_idx : exit_idx + 1].sum())
        trades.append(
            {
                "strategy_id": strategy.strategy_id,
                "family": family,
                "instrument_type": strategy.instrument_type,
                "spread": strategy.spread or "",
                "series": strategy.series or "",
                "side": strategy.side,
                "position": strategy.side_text,
                "holding_days_param": variant.holding_days,
                "stop_mode": variant.stop_mode,
                "stop_value": variant.stop_value,
                "take_profit_r": "none" if variant.take_profit_r is None else variant.take_profit_r,
                "slippage_bps": variant.slippage_bps,
                "entry_date": entry["date"],
                "exit_date": exit_row["date"],
                "exit_reason": exit_reason,
                "entry_price": path_entry["price"],
                "exit_price": exit_row["price"],
                "realized_points": realized_points,
                "qty": qty,
                "entry_margin": margin_required(path_entry, qty),
                "entry_margin_usage": margin_required(path_entry, qty) / INITIAL_CAPITAL,
                "entry_cost": entry_cost,
                "exit_cost": exit_cost,
                "pnl_rub": trade_pnl,
                "pnl_pct_initial": trade_pnl / INITIAL_CAPITAL,
                "leg1_entry": entry["leg1_secid"],
                "leg2_entry": entry["leg2_secid"],
                "leg1_exit": exit_row["leg1_secid"],
                "leg2_exit": exit_row["leg2_secid"],
                "leg1_entry_price": path_entry["leg1_price"],
                "leg2_entry_price": path_entry["leg2_price"],
                "leg1_exit_price": exit_row["leg1_price"],
                "leg2_exit_price": exit_row["leg2_price"],
                "stop_distance_points": dist,
            }
        )
        blocked_until = exit_idx

    equity = pd.DataFrame(
        {
            "date": g["date"],
            "strategy_id": strategy.strategy_id,
            "family": family,
            "holding_days_param": variant.holding_days,
            "stop_mode": variant.stop_mode,
            "stop_value": variant.stop_value,
            "take_profit_r": "none" if variant.take_profit_r is None else variant.take_profit_r,
            "slippage_bps": variant.slippage_bps,
            "daily_pnl": daily_pnl,
            "margin": daily_margin,
        }
    )
    equity["equity"] = INITIAL_CAPITAL + equity["daily_pnl"].cumsum()
    equity["daily_return"] = equity["equity"].pct_change().fillna(0.0)
    return pd.DataFrame(trades), equity, {"liquidity_rejections": liquidity_rejections, "size_rejections": size_rejections}


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return np.nan
    return float((equity / equity.cummax() - 1).min())


def max_consecutive_losses(pnls: pd.Series) -> int:
    longest = current = 0
    for value in pnls:
        if value < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def summarize(trades: pd.DataFrame, equity: pd.DataFrame, meta: dict, period_name: str, start: str | None = None, end: str | None = None) -> dict:
    e = equity.copy()
    t = trades.copy()
    if start:
        start_ts = pd.Timestamp(start)
        e = e[e["date"] >= start_ts]
        t = t[t["entry_date"] >= start_ts]
    if end:
        end_ts = pd.Timestamp(end)
        e = e[e["date"] <= end_ts]
        t = t[t["entry_date"] <= end_ts]
    if e.empty:
        final_equity = INITIAL_CAPITAL
        total_return = 0.0
        cagr = 0.0
        mdd = np.nan
        sharpe = np.nan
        avg_margin_usage = 0.0
    else:
        eq = INITIAL_CAPITAL + e["daily_pnl"].cumsum()
        final_equity = float(eq.iloc[-1])
        total_return = final_equity / INITIAL_CAPITAL - 1
        years = max((e["date"].max() - e["date"].min()).days / 365.25, 1 / 365.25)
        cagr = (final_equity / INITIAL_CAPITAL) ** (1 / years) - 1 if final_equity > 0 else -1.0
        mdd = max_drawdown(eq)
        daily_ret = eq.pct_change().fillna(0.0)
        sharpe = float(np.sqrt(252) * daily_ret.mean() / daily_ret.std(ddof=1)) if daily_ret.std(ddof=1) > 0 else np.nan
        avg_margin_usage = float((e["margin"] / INITIAL_CAPITAL).mean())
    calmar = float(cagr / abs(mdd)) if mdd and np.isfinite(mdd) and mdd < 0 else np.nan
    pnls = t["pnl_rub"] if not t.empty else pd.Series(dtype=float)
    gross_profit = float(pnls[pnls > 0].sum()) if not pnls.empty else 0.0
    gross_loss = float(-pnls[pnls < 0].sum()) if not pnls.empty else 0.0
    return {
        "period": period_name,
        "total_return": total_return,
        "CAGR": cagr,
        "max_drawdown": mdd,
        "Sharpe": sharpe,
        "Calmar": calmar,
        "number_of_trades": int(len(t)),
        "win_rate": float((pnls > 0).mean()) if len(t) else np.nan,
        "average_trade_rub": float(pnls.mean()) if len(t) else np.nan,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else np.inf if gross_profit > 0 else np.nan,
        "max_consecutive_losses": max_consecutive_losses(pnls) if len(t) else 0,
        "worst_trade_rub": float(pnls.min()) if len(t) else np.nan,
        "average_margin_usage": avg_margin_usage,
        "liquidity_rejection_count": int(meta.get("liquidity_rejections", 0)),
        "size_rejection_count": int(meta.get("size_rejections", 0)),
    }


def variant_key(strategy: StrategyDef, family: str, variant: Variant) -> dict:
    return {
        "strategy_id": strategy.strategy_id,
        "family": family,
        "holding_days_param": variant.holding_days,
        "stop_mode": variant.stop_mode,
        "stop_value": variant.stop_value,
        "take_profit_r": "none" if variant.take_profit_r is None else variant.take_profit_r,
        "slippage_bps": variant.slippage_bps,
    }


def run_all(args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    RESULTS.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    history = normalize_history()
    cont = read_csv(DATA_PROCESSED / "continuous_daily.csv", ["date"])
    spreads = read_csv(DATA_PROCESSED / "calendar_spreads.csv", ["date"])

    panels: dict[tuple[str, str], pd.DataFrame] = {}
    panels[("spread", "front_next")] = build_spread_panel(spreads, history, "front_next")
    panels[("outright", "second")] = build_outright_panel(cont, history, "second")

    variants = [
        Variant(h, "atr", stop, tp, slip)
        for h in [5, 10, 15, 20]
        for stop in [1.0, 1.5, 2.0]
        for slip in [5.0, 10.0, 20.0]
        for tp in [None, 1.0, 2.0]
    ]
    base_fixed = Variant(20, "fixed", DEFAULT_FIXED_STOP_POINTS, None, 10.0)

    all_trades = []
    all_equity = []
    all_summary = []
    all_sensitivity = []
    families = ["NG", "NGM"]
    for strategy in STRATEGIES:
        panel_key = ("spread", strategy.spread) if strategy.instrument_type == "spread" else ("outright", strategy.series)
        panel = panels.get(panel_key, pd.DataFrame())
        for family in families:
            if panel.empty or family not in set(panel["family"].dropna()):
                continue
            for variant in variants + [base_fixed]:
                trades, equity, meta = backtest_variant(
                    strategy,
                    family,
                    panel,
                    history,
                    variant,
                    args.min_volume,
                    args.min_trades,
                    args.min_oi,
                )
                key = variant_key(strategy, family, variant)
                summaries = [
                    {**key, **summarize(trades, equity, meta, "all")},
                    {**key, **summarize(trades, equity, meta, "train_2020_2023", None, "2023-12-31")},
                    {**key, **summarize(trades, equity, meta, "test_2024_2026", "2024-01-01", None)},
                    {**key, **summarize(trades, equity, meta, "recent_2024_2026", "2024-01-01", None)},
                ]
                all_summary.extend(summaries)
                test = summaries[2]
                all_sensitivity.append(
                    {
                        **key,
                        "test_total_return": test["total_return"],
                        "test_max_drawdown": test["max_drawdown"],
                        "test_trades": test["number_of_trades"],
                        "test_win_rate": test["win_rate"],
                        "test_profit_factor": test["profit_factor"],
                        "passed_test": bool(
                            test["number_of_trades"] > 0
                            and test["total_return"] > 0
                            and (pd.isna(test["max_drawdown"]) or test["max_drawdown"] >= -args.max_acceptable_drawdown)
                        ),
                    }
                )
                if variant == base_fixed or (variant.holding_days == 20 and variant.stop_mode == "atr" and variant.stop_value == 1.5 and variant.take_profit_r is None and variant.slippage_bps == 10.0):
                    if not trades.empty:
                        all_trades.append(trades)
                    if not equity.empty:
                        all_equity.append(equity)

    trades_df = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    equity_df = pd.concat(all_equity, ignore_index=True) if all_equity else pd.DataFrame()
    summary_df = pd.DataFrame(all_summary)
    sensitivity_df = pd.DataFrame(all_sensitivity)

    trades_df.to_csv(RESULTS / "portfolio_strategy_trades.csv", index=False)
    equity_df.to_csv(RESULTS / "portfolio_strategy_equity.csv", index=False)
    summary_df.to_csv(RESULTS / "portfolio_strategy_summary.csv", index=False)
    sensitivity_df.to_csv(RESULTS / "portfolio_strategy_sensitivity.csv", index=False)
    write_report(summary_df, sensitivity_df, trades_df, args)
    return trades_df, equity_df, summary_df, sensitivity_df


def pct(value: object) -> str:
    if pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def num(value: object, digits: int = 2) -> str:
    if pd.isna(value):
        return ""
    if value == np.inf:
        return "inf"
    return f"{float(value):.{digits}f}"


def md_table(df: pd.DataFrame, columns: list[str], percent_cols: set[str] | None = None) -> str:
    if df.empty:
        return "Нет строк."
    percent_cols = percent_cols or set()
    work = df[[c for c in columns if c in df]].copy()
    rows = []
    for _, row in work.iterrows():
        values = []
        for col in work.columns:
            value = row[col]
            if col in percent_cols:
                values.append(pct(value))
            elif isinstance(value, float):
                values.append(num(value, 4))
            else:
                values.append("" if pd.isna(value) else str(value))
        rows.append(values)
    out = ["| " + " | ".join(work.columns) + " |", "| " + " | ".join(["---"] * len(work.columns)) + " |"]
    out.extend("| " + " | ".join(values) + " |" for values in rows)
    return "\n".join(out)


def write_report(summary: pd.DataFrame, sensitivity: pd.DataFrame, trades: pd.DataFrame, args: argparse.Namespace) -> None:
    base = summary[
        (summary["holding_days_param"] == 20)
        & (summary["stop_mode"] == "atr")
        & (summary["stop_value"] == 1.5)
        & (summary["take_profit_r"].astype(str) == "none")
        & (summary["slippage_bps"] == 10.0)
    ].copy()
    test = base[base["period"] == "test_2024_2026"].copy()
    passed = test[
        (test["number_of_trades"] > 0)
        & (test["total_return"] > 0)
        & (test["max_drawdown"].fillna(0) >= -args.max_acceptable_drawdown)
    ].sort_values(["total_return", "Sharpe"], ascending=[False, False])
    rejected = test.merge(
        passed[["strategy_id", "family"]],
        on=["strategy_id", "family"],
        how="left",
        indicator=True,
    )
    rejected = rejected[rejected["_merge"] == "left_only"].drop(columns=["_merge"])
    best_sens = sensitivity[sensitivity["passed_test"]].sort_values(["test_total_return", "test_max_drawdown"], ascending=[False, False]).head(30)

    lines = [
        "# Реалистичный portfolio-level backtest MOEX NG/NGM",
        "",
        "## Исправление методологии",
        "Старые equity curves из `results/equity_curves.csv` не используются для выводов по этим стратегиям: они были построены как средняя доходность сигналов и могли компаундировать перекрывающиеся 20-дневные сигналы. Новый тест ведет портфель по дням, разрешает не более одной открытой позиции на стратегию, входит только на следующей дневной свече после появления сигнала и считает PnL по ногам.",
        "",
        "## Формулы спредов",
        f"- `front_next = {SPREAD_FORMULAS['front_next']}`. Long spread: buy front + sell next. Short spread: sell front + buy next.",
        f"- `summer_winter = {SPREAD_FORMULAS['summer_winter']}`. Long spread: buy summer + sell winter. Short spread: sell summer + buy winter.",
        f"- `front_winter = {SPREAD_FORMULAS['front_winter']}`. Long spread: buy front + sell winter. Short spread: sell front + buy winter.",
        "",
        "Эти формулы проверены по коду построения `data/processed/calendar_spreads.csv`: поле `price` считается как первая указанная нога минус вторая указанная нога.",
        "",
        "## Допущения исполнения",
        f"- Initial capital: {INITIAL_CAPITAL:,.0f} RUB; risk per trade: {RISK_PER_TRADE:.2%}; max margin usage: {MAX_MARGIN_USAGE:.0%}.",
        "- PnL считается по отдельным ногам и дневному `USD/RUB`: `daily_pnl = side * delta_price * lotvolume * USD/RUB * contracts`.",
        "- `lotvolume`: NG = 100, NR/NGM = 1. Историческое ГО оценивается консервативно как 30% notional по каждой ноге без межмесячного offset.",
        f"- Комиссия: {COMMISSION_BPS:g} bps per side; slippage в sensitivity: 5/10/20 bps per side.",
        f"- Liquidity filter по каждой ноге: volume >= {args.min_volume}, trades >= {args.min_trades}, open interest >= {args.min_oi}. Bid/ask в ISS history отсутствует, поэтому slippage bps используется как proxy.",
        "- Stop-loss проверяется по дневному settlement спреда/контракта. Intraday пересечение по стакану не моделируется, потому что в текущем архиве нет синхронного bid/ask/last по обеим ногам.",
        "",
        "## Точные ноги стратегий",
        "- A `strategy_A_aug_front_next_short`: `front_next = front - next`, short spread = sell front + buy next, вход на следующей дневной свече после первого августовского сигнала.",
        "- B `strategy_B_oct_nov_front_next_long`: `front_next = front - next`, long spread = buy front + sell next, вход на следующей дневной свече после старта окна Oct-Nov.",
        "- C `strategy_C_nov_second_short`: outright short второго месячного контракта, вход на следующей дневной свече после первого ноябрьского сигнала.",
        "",
        "## Base case",
        "Base case: holding_days=20, ATR stop=1.5 ATR, take-profit отсутствует, slippage=10 bps.",
        md_table(
            base.sort_values(["strategy_id", "family", "period"]),
            [
                "strategy_id",
                "family",
                "period",
                "total_return",
                "CAGR",
                "max_drawdown",
                "Sharpe",
                "Calmar",
                "number_of_trades",
                "win_rate",
                "average_trade_rub",
                "profit_factor",
                "max_consecutive_losses",
                "worst_trade_rub",
                "average_margin_usage",
                "liquidity_rejection_count",
            ],
            {"total_return", "CAGR", "max_drawdown", "win_rate", "average_margin_usage"},
        ),
        "",
        "## Прошло out-of-sample",
        "Критерий: test 2024-2026 положительный, есть сделки, max drawdown не хуже заданного лимита.",
        md_table(
            passed,
            [
                "strategy_id",
                "family",
                "total_return",
                "max_drawdown",
                "Sharpe",
                "number_of_trades",
                "win_rate",
                "average_trade_rub",
                "profit_factor",
                "liquidity_rejection_count",
            ],
            {"total_return", "max_drawdown", "win_rate"},
        ),
        "",
        "## Отклонено в base case",
        md_table(
            rejected.sort_values(["strategy_id", "family"]),
            [
                "strategy_id",
                "family",
                "total_return",
                "max_drawdown",
                "Sharpe",
                "number_of_trades",
                "win_rate",
                "average_trade_rub",
                "profit_factor",
                "liquidity_rejection_count",
            ],
            {"total_return", "max_drawdown", "win_rate"},
        ),
        "",
        "## Sensitivity: только варианты, прошедшие test-period",
        md_table(
            best_sens,
            [
                "strategy_id",
                "family",
                "holding_days_param",
                "stop_mode",
                "stop_value",
                "take_profit_r",
                "slippage_bps",
                "test_total_return",
                "test_max_drawdown",
                "test_trades",
                "test_win_rate",
                "test_profit_factor",
            ],
            {"test_total_return", "test_max_drawdown", "test_win_rate"},
        ),
        "",
        "## Файлы",
        "- `results/portfolio_strategy_trades.csv`",
        "- `results/portfolio_strategy_equity.csv`",
        "- `results/portfolio_strategy_summary.csv`",
        "- `results/portfolio_strategy_sensitivity.csv`",
        "- `reports/strategy_A_aug_front_next_short_ru.md`",
        "- `reports/strategy_B_oct_nov_front_next_long_ru.md`",
        "- `reports/strategy_C_nov_second_short_ru.md`",
        "",
    ]

    if not trades.empty:
        base_trades = trades[
            (trades["holding_days_param"] == 20)
            & (trades["stop_mode"] == "atr")
            & (trades["stop_value"] == 1.5)
            & (trades["take_profit_r"].astype(str) == "none")
            & (trades["slippage_bps"] == 10.0)
        ]
        lines.extend(
            [
                "## Сделки base case",
                md_table(
                    base_trades.sort_values(["strategy_id", "family", "entry_date"]),
                    [
                        "strategy_id",
                        "family",
                        "entry_date",
                        "exit_date",
                        "exit_reason",
                        "qty",
                        "pnl_rub",
                        "entry_margin_usage",
                        "leg1_entry",
                        "leg2_entry",
                        "leg1_exit",
                        "leg2_exit",
                    ],
                    {"entry_margin_usage"},
                ),
                "",
            ]
        )
    lines.extend(
        [
            "## Вывод",
            "Стратегии, прошедшие OOS, можно рассматривать только как кандидаты для наблюдения и дальнейшей проверки на intraday bid/ask и точном архиве ГО. Для реального счета текущий слой еще недостаточен: нет исторического стакана, точного межмесячного margin offset и фактической исполнимости стопов внутри дня.",
        ]
    )
    (REPORTS / "strategy_realistic_backtest_ru.md").write_text("\n".join(lines), encoding="utf-8")
    write_strategy_reports(summary, sensitivity, trades, args)


def write_strategy_reports(summary: pd.DataFrame, sensitivity: pd.DataFrame, trades: pd.DataFrame, args: argparse.Namespace) -> None:
    by_id = {s.strategy_id: s for s in STRATEGIES}
    for strategy_id, strategy in by_id.items():
        s_summary = summary[summary["strategy_id"] == strategy_id].copy()
        s_sens = sensitivity[sensitivity["strategy_id"] == strategy_id].copy()
        s_trades = trades[trades["strategy_id"] == strategy_id].copy() if not trades.empty else pd.DataFrame()
        base = s_summary[
            (s_summary["holding_days_param"] == 20)
            & (s_summary["stop_mode"] == "atr")
            & (s_summary["stop_value"] == 1.5)
            & (s_summary["take_profit_r"].astype(str) == "none")
            & (s_summary["slippage_bps"] == 10.0)
        ].copy()
        passed = s_sens[s_sens["passed_test"]].sort_values(["test_total_return", "test_max_drawdown"], ascending=[False, False])
        base_trades = s_trades[
            (s_trades["holding_days_param"] == 20)
            & (s_trades["stop_mode"] == "atr")
            & (s_trades["stop_value"] == 1.5)
            & (s_trades["take_profit_r"].astype(str) == "none")
            & (s_trades["slippage_bps"] == 10.0)
        ] if not s_trades.empty else pd.DataFrame()
        lines = [
            f"# {strategy.strategy_id}",
            "",
            f"Описание: {strategy.description}.",
            f"Позиция: {strategy.side_text}.",
            f"Инструмент: {strategy.instrument_type}; spread={strategy.spread or ''}; series={strategy.series or ''}.",
            "",
            "## Формула и ноги",
        ]
        if strategy.instrument_type == "spread":
            formula = SPREAD_FORMULAS[strategy.spread or ""]
            if strategy.side > 0:
                legs = "buy first leg + sell second leg"
            else:
                legs = "sell first leg + buy second leg"
            lines.extend(
                [
                    f"`{strategy.spread} = {formula}`.",
                    f"Для этой стратегии: {legs}.",
                ]
            )
        else:
            lines.append("Outright second-month: short фиксированного второго контракта, выбранного на дату входа.")
        lines.extend(
            [
                "",
                "## Base Case Metrics",
                md_table(
                    base.sort_values(["family", "period"]),
                    [
                        "family",
                        "period",
                        "total_return",
                        "CAGR",
                        "max_drawdown",
                        "Sharpe",
                        "Calmar",
                        "number_of_trades",
                        "win_rate",
                        "average_trade_rub",
                        "profit_factor",
                        "max_consecutive_losses",
                        "worst_trade_rub",
                        "average_margin_usage",
                        "liquidity_rejection_count",
                    ],
                    {"total_return", "CAGR", "max_drawdown", "win_rate", "average_margin_usage"},
                ),
                "",
                "## Passed Sensitivity Variants",
                md_table(
                    passed.head(20),
                    [
                        "family",
                        "holding_days_param",
                        "stop_mode",
                        "stop_value",
                        "take_profit_r",
                        "slippage_bps",
                        "test_total_return",
                        "test_max_drawdown",
                        "test_trades",
                        "test_win_rate",
                        "test_profit_factor",
                    ],
                    {"test_total_return", "test_max_drawdown", "test_win_rate"},
                ),
                "",
                "## Base Case Trades",
                md_table(
                    base_trades.sort_values(["family", "entry_date"]),
                    [
                        "family",
                        "entry_date",
                        "exit_date",
                        "exit_reason",
                        "qty",
                        "pnl_rub",
                        "entry_margin_usage",
                        "leg1_entry",
                        "leg2_entry",
                        "leg1_exit",
                        "leg2_exit",
                    ],
                    {"entry_margin_usage"},
                ),
                "",
                "## Decision",
            ]
        )
        base_test = base[base["period"] == "test_2024_2026"]
        if base_test.empty or not ((base_test["total_return"] > 0) & (base_test["max_drawdown"].fillna(0) >= -args.max_acceptable_drawdown)).any():
            lines.append("Base case отклонен для реальной торговли: test 2024-2026 не положительный или сделок слишком мало.")
        else:
            lines.append("Base case прошел формальный OOS-фильтр, но требует проверки на intraday bid/ask и точном архиве ГО.")
        if passed.empty:
            lines.append("В sensitivity нет вариантов, прошедших test-period.")
        else:
            lines.append("В sensitivity есть положительные OOS-варианты, но они остаются кандидатами для наблюдения, а не готовой системой.")
        (REPORTS / f"{strategy_id}_ru.md").write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Portfolio-level backtest for selected MOEX NG/NGM strategies.")
    parser.add_argument("--min-volume", type=float, default=50.0)
    parser.add_argument("--min-trades", type=float, default=5.0)
    parser.add_argument("--min-oi", type=float, default=100.0)
    parser.add_argument("--max-acceptable-drawdown", type=float, default=0.15)
    return parser.parse_args()


def main() -> int:
    trades, equity, summary, sensitivity = run_all(parse_args())
    print(f"Wrote {RESULTS / 'portfolio_strategy_trades.csv'} ({len(trades)} rows)")
    print(f"Wrote {RESULTS / 'portfolio_strategy_equity.csv'} ({len(equity)} rows)")
    print(f"Wrote {RESULTS / 'portfolio_strategy_summary.csv'} ({len(summary)} rows)")
    print(f"Wrote {RESULTS / 'portfolio_strategy_sensitivity.csv'} ({len(sensitivity)} rows)")
    print(f"Wrote {REPORTS / 'strategy_realistic_backtest_ru.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
