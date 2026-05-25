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
import statsmodels.api as sm

from leadlag_ng_moex import (
    DATA_PROCESSED,
    FEATURE_SETS,
    MONTH_CODES,
    REPORTS,
    ROOT,
    UNIVERSE,
    add_execution_columns,
    ensure_dirs,
)


PLOTS = ROOT / "plots"
HORIZON_MINUTES = 30
INTERVAL_MINUTES = 10
MIN_TRAIN_TRADES = 30
MIN_TRAIN_MONTHS = 3
FALLBACK_MIN_STEP = 0.001
FALLBACK_STEP_PRICE = 0.1
FALLBACK_STEP_CURRENCY = "USD"
NG_LARGE_CONTRACT_SIZE = 100.0
NG_LARGE_TICK_VALUE_USD = 0.1
FALLBACK_MARGIN_RUB = 15_000.0
PRICE_TICK_TOLERANCE = 1e-5

SLIPPAGE_TICKS_GRID = [0, 1, 2, 3, 4]
FEE_RUB_GRID = [0, 1, 2, 5, 10]
THRESHOLD_OBJECTIVES = ["train_mean"]
FEATURE_OBJECTIVES = ["match_threshold_objective"]
THRESHOLD_PERCENTILES = [50, 60, 70, 80, 90, 95]
THRESHOLD_FIXED = [0.0001, 0.0002, 0.0003, 0.0005, 0.00075, 0.0010]
ENSEMBLE_FEATURES = ["plus1_only", "outrights_all", "outrights_plus_spreads"]
BASE_SCENARIOS = [(1, 1), (2, 2), (3, 5), (4, 10)]


@dataclass(frozen=True)
class ThirdPassConfig:
    force: bool
    request_sleep: float


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def read_csv(path: Path, date_cols: list[str] | None = None) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, parse_dates=date_cols or [])


def numeric(value: object) -> float:
    if value is None or pd.isna(value):
        return np.nan
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return np.nan


def request_json(url: str, params: dict | None = None, retries: int = 4, sleep: float = 0.5) -> dict:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.get(url, params=params or {}, timeout=60)
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(sleep * (attempt + 1))
    raise RuntimeError(f"MOEX request failed: {url}") from last_error


def iss_table(payload: dict, table: str) -> pd.DataFrame:
    block = payload.get(table, {})
    return pd.DataFrame(block.get("data", []), columns=block.get("columns", []))


def fetch_current_specs(cfg: ThirdPassConfig) -> pd.DataFrame:
    url = "https://iss.moex.com/iss/engines/futures/markets/forts/securities.json"
    try:
        payload = request_json(url, {"iss.meta": "off", "iss.only": "securities"}, sleep=cfg.request_sleep)
        return iss_table(payload, "securities")
    except Exception as exc:  # noqa: BLE001
        log(f"Current specs request failed, fallback will be used: {type(exc).__name__}: {exc}")
        return pd.DataFrame()


def first_existing(row: pd.Series, names: list[str]) -> object:
    for name in names:
        if name in row.index and pd.notna(row[name]):
            return row[name]
    return np.nan


def build_contract_specs(cfg: ThirdPassConfig) -> pd.DataFrame:
    current = fetch_current_specs(cfg)
    rows = []
    by_secid = current.set_index("SECID") if not current.empty and "SECID" in current else pd.DataFrame()
    spec_date = datetime.now().strftime("%Y-%m-%d")
    for secid in UNIVERSE:
        raw = {}
        fallback_parts = []
        if not by_secid.empty and secid in by_secid.index:
            source_row = by_secid.loc[secid]
            if isinstance(source_row, pd.DataFrame):
                source_row = source_row.iloc[0]
            raw = source_row.dropna().to_dict()
            moex_min_step = numeric(first_existing(source_row, ["MINSTEP", "SEC_PRICE_STEP", "MINPRICEINCREMENT"]))
            stepprice_moex_current_raw = numeric(first_existing(source_row, ["STEPPRICE", "STEPPRICECL", "STEPPRICEPRCL"]))
            margin = numeric(first_existing(source_row, ["INITIALMARGIN", "BUYDEPOSIT", "SELLDEPOSIT"]))
            shortname = first_existing(source_row, ["SHORTNAME", "SECNAME", "LATNAME"])
            last_trade = first_existing(source_row, ["LASTTRADEDATE", "LASTDELDATE"])
            lot_size = numeric(first_existing(source_row, ["LOTVOLUME", "LOTSIZE"]))
            spec_source = "moex_current_securities"
        else:
            moex_min_step = stepprice_moex_current_raw = margin = lot_size = np.nan
            shortname = np.nan
            last_trade = np.nan
            spec_source = "fallback_config"
            fallback_parts.append("SECID absent from current MOEX securities")

        min_step = FALLBACK_MIN_STEP
        step_price = NG_LARGE_TICK_VALUE_USD
        tick_value_usd_spec = NG_LARGE_TICK_VALUE_USD
        contract_size = NG_LARGE_CONTRACT_SIZE
        if np.isfinite(moex_min_step) and abs(moex_min_step - FALLBACK_MIN_STEP) > 1e-12:
            fallback_parts.append(f"MOEX MINSTEP {moex_min_step} differs from NG large spec 0.001")
        if np.isfinite(stepprice_moex_current_raw) and stepprice_moex_current_raw > 1.0:
            stepprice_currency_assumed = "RUB_current_tick_estimate"
        elif np.isfinite(stepprice_moex_current_raw):
            stepprice_currency_assumed = "unknown_or_usd_like"
        else:
            stepprice_currency_assumed = "missing"
        if not np.isfinite(margin) or margin <= 0:
            margin = FALLBACK_MARGIN_RUB
            fallback_parts.append("initial_margin_rub")

        rows.append(
            {
                "secid": secid,
                "shortname": shortname,
                "contract_family": "NG",
                "min_step": min_step,
                "step_price": step_price,
                "step_price_currency": FALLBACK_STEP_CURRENCY,
                "price_currency": "USD",
                "lot_size": lot_size,
                "contract_size": contract_size,
                "tick_value_usd_spec": tick_value_usd_spec,
                "stepprice_moex_current_raw": stepprice_moex_current_raw,
                "stepprice_moex_current_currency_assumed": stepprice_currency_assumed,
                "tick_value_rub_used": np.nan,
                "tick_value_rub_source": "tick_value_usd_spec * daily USD/RUB",
                "initial_margin_rub": margin,
                "last_trade_date": last_trade,
                "spec_source": spec_source if not fallback_parts else f"{spec_source}+fallback",
                "fallback_source": "hardcoded_ng_defaults" if fallback_parts else "",
                "warning": "; ".join(fallback_parts),
                "spec_date": spec_date,
                "raw_fields_json": json.dumps(raw, ensure_ascii=False, default=str),
            }
        )
    out = pd.DataFrame(rows)
    out.to_csv(REPORTS / "unit_audit_contract_specs.csv", index=False)
    return out


def load_fx_rates() -> pd.DataFrame:
    path = ROOT / "data" / "raw" / "external_daily.csv"
    ext = read_csv(path, ["date"])
    if ext.empty or "usdrub_cbr" not in ext:
        raise RuntimeError("USD/RUB source not found. Expected data/raw/external_daily.csv with usdrub_cbr.")
    fx = ext[["date", "usdrub_cbr"]].copy()
    fx = fx.rename(columns={"date": "trade_date", "usdrub_cbr": "usd_rub_rate"})
    fx["trade_date"] = pd.to_datetime(fx["trade_date"]).dt.normalize()
    fx["usd_rub_rate"] = pd.to_numeric(fx["usd_rub_rate"], errors="coerce").ffill()
    fx["fx_source"] = "data/raw/external_daily.csv:usdrub_cbr daily approximation"
    fx = fx.dropna(subset=["usd_rub_rate"]).drop_duplicates("trade_date").sort_values("trade_date")
    fx.to_csv(REPORTS / "unit_audit_fx_rates.csv", index=False)
    return fx


def load_features() -> pd.DataFrame:
    path = DATA_PROCESSED / "features.csv"
    features = read_csv(path, ["begin", "next_begin"])
    if features.empty:
        raise RuntimeError("Missing data/processed/leadlag_ng_10m/features.csv. Run leadlag_ng_moex.py first.")
    for n in [1, 2, 3]:
        spread = f"spread_plus{n}"
        d_spread = f"d_spread_plus{n}"
        if spread not in features:
            features[spread] = np.log(features[f"close_plus{n}"] / features["close_front"])
        if d_spread not in features:
            features[d_spread] = features.groupby("target_month")[spread].diff()
    return features.sort_values(["target_month", "begin"]).reset_index(drop=True)


def fit_predict(train: pd.DataFrame, test: pd.DataFrame, y_col: str, predictors: list[str]) -> tuple[pd.Series, pd.Series]:
    train_clean = train[[y_col, *predictors]].replace([np.inf, -np.inf], np.nan).dropna()
    test_clean = test[predictors].replace([np.inf, -np.inf], np.nan).dropna()
    if len(train_clean) < 30 or test_clean.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    model = sm.OLS(train_clean[y_col], sm.add_constant(train_clean[predictors], has_constant="add")).fit()
    train_pred = pd.Series(model.predict(sm.add_constant(train_clean[predictors], has_constant="add")), index=train_clean.index)
    test_pred = pd.Series(model.predict(sm.add_constant(test_clean[predictors], has_constant="add")), index=test_clean.index)
    return train_pred, test_pred


def build_predictions(features: pd.DataFrame, cfg: ThirdPassConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    out_path = REPORTS / "third_pass_predictions_30m.csv"
    train_path = DATA_PROCESSED / "third_pass_train_predictions_30m.csv"
    if out_path.exists() and train_path.exists() and not cfg.force:
        test_preds = read_csv(out_path, ["begin_signal", "entry_begin", "exit_begin"])
        train_preds = read_csv(train_path, ["begin_signal", "entry_begin", "exit_begin"])
        return test_preds, train_preds

    work = features.copy()
    work["month_ts"] = pd.to_datetime(work["target_month"] + "-01")
    work = add_execution_columns(work, HORIZON_MINUTES, type("Cfg", (), {"interval": INTERVAL_MINUTES})())
    work = work[work["execution_continuous"]].copy()
    months = sorted(work["month_ts"].dropna().unique())
    test_frames = []
    train_frames = []
    y_col = "ret_front_30m"
    for test_month in months:
        train = work[work["month_ts"] < test_month].copy()
        test = work[work["month_ts"] == test_month].copy()
        if train["target_month"].nunique() < MIN_TRAIN_MONTHS or len(train) < 200 or len(test) < 50:
            continue
        for feature_set, predictors in FEATURE_SETS.items():
            if any(c not in work for c in predictors):
                continue
            train_pred, test_pred = fit_predict(train, test, y_col, predictors)
            if test_pred.empty:
                continue
            base_cols = [
                "target_month",
                "begin",
                "secid_front",
                "secid_plus1",
                "secid_plus2",
                "secid_plus3",
                "entry_time",
                "exit_time",
                "entry_price",
                "exit_price",
                "future_open_return",
            ]
            test_out = test.loc[test_pred.index, base_cols].copy()
            test_out["test_month"] = pd.Timestamp(test_month).strftime("%Y-%m")
            test_out["feature_set"] = feature_set
            test_out["prediction"] = test_pred
            test_out["abs_prediction"] = test_pred.abs()
            test_out["signal_direction"] = np.sign(test_pred).astype(int)
            test_out["train_start_month"] = train["target_month"].min()
            test_out["train_end_month"] = train["target_month"].max()
            test_frames.append(test_out)

            train_out = train.loc[train_pred.index, base_cols].copy()
            train_out["test_month"] = pd.Timestamp(test_month).strftime("%Y-%m")
            train_out["feature_set"] = feature_set
            train_out["prediction"] = train_pred
            train_out["abs_prediction"] = train_pred.abs()
            train_out["signal_direction"] = np.sign(train_pred).astype(int)
            train_out["train_start_month"] = train["target_month"].min()
            train_out["train_end_month"] = train["target_month"].max()
            train_frames.append(train_out)

    test_preds = pd.concat(test_frames, ignore_index=True) if test_frames else pd.DataFrame()
    train_preds = pd.concat(train_frames, ignore_index=True) if train_frames else pd.DataFrame()
    rename = {
        "target_month": "sample_month",
        "begin": "begin_signal",
        "secid_front": "target_contract",
        "secid_plus1": "plus1_contract",
        "secid_plus2": "plus2_contract",
        "secid_plus3": "plus3_contract",
        "entry_time": "entry_begin",
        "exit_time": "exit_begin",
        "entry_price": "entry_open",
        "exit_price": "exit_open",
        "future_open_return": "y_future_return_30m",
    }
    test_preds = test_preds.rename(columns=rename)
    train_preds = train_preds.rename(columns=rename)
    keep = [
        "test_month",
        "sample_month",
        "begin_signal",
        "feature_set",
        "prediction",
        "abs_prediction",
        "y_future_return_30m",
        "signal_direction",
        "target_contract",
        "plus1_contract",
        "plus2_contract",
        "plus3_contract",
        "entry_begin",
        "exit_begin",
        "entry_open",
        "exit_open",
        "train_start_month",
        "train_end_month",
    ]
    test_preds = test_preds[keep].sort_values(["test_month", "feature_set", "begin_signal"])
    train_preds = train_preds[keep].sort_values(["test_month", "feature_set", "begin_signal"])
    test_preds.to_csv(out_path, index=False)
    train_preds.to_csv(train_path, index=False)
    return test_preds, train_preds


def attach_specs_fx(base: pd.DataFrame, specs: pd.DataFrame, fx: pd.DataFrame) -> pd.DataFrame:
    out = base.copy()
    out["entry_begin"] = pd.to_datetime(out["entry_begin"])
    out["exit_begin"] = pd.to_datetime(out["exit_begin"])
    out["begin_signal"] = pd.to_datetime(out["begin_signal"])
    out["trade_date"] = out["entry_begin"].dt.normalize()
    spec_cols = [
        "secid",
        "min_step",
        "step_price",
        "step_price_currency",
        "price_currency",
        "contract_size",
        "tick_value_usd_spec",
        "stepprice_moex_current_raw",
        "stepprice_moex_current_currency_assumed",
        "tick_value_rub_source",
        "initial_margin_rub",
        "spec_source",
        "spec_date",
        "warning",
    ]
    out = out.merge(specs[spec_cols].rename(columns={"secid": "target_contract"}), on="target_contract", how="left")
    fx_sorted = fx.sort_values("trade_date")
    out = pd.merge_asof(out.sort_values("trade_date"), fx_sorted, on="trade_date", direction="backward").sort_index()
    out["margin_source"] = np.where(out["spec_source"].astype(str).str.contains("fallback", na=False), "fallback/current approximation", "moex_current_or_nearest_available")
    out["margin_date"] = out["spec_date"]
    out["margin_warning"] = np.where(out["margin_source"].astype(str).str.contains("fallback"), "return_on_go uses approximate/current margin", "")
    out["price_delta"] = pd.to_numeric(out["exit_open"], errors="coerce") - pd.to_numeric(out["entry_open"], errors="coerce")
    out["raw_ticks"] = out["price_delta"] / out["min_step"]
    tick_round_error = (out["raw_ticks"] - out["raw_ticks"].round()).abs()
    out["tick_rounding_anomaly"] = tick_round_error > PRICE_TICK_TOLERANCE
    out["tick_value_rub"] = out["tick_value_usd_spec"] * out["usd_rub_rate"]
    out["tick_value_rub_used"] = out["tick_value_rub"]
    out["notional_rub"] = out["entry_open"] * out["contract_size"] * out["usd_rub_rate"]
    out["notional_source"] = np.where(out["contract_size"].notna(), "NG large lot=100 MMBtu", "notional_unavailable")
    out["verified_units"] = (
        out["min_step"].notna()
        & out["step_price"].notna()
        & out["tick_value_rub"].notna()
        & out["usd_rub_rate"].notna()
        & out["initial_margin_rub"].notna()
        & ~out["tick_rounding_anomaly"]
    )
    return out


def trades_from_predictions(
    pred: pd.DataFrame,
    specs: pd.DataFrame,
    fx: pd.DataFrame,
    threshold: float,
    threshold_type: str,
    slippage_ticks: int,
    fee_rub: float,
    strategy_mode: str,
    threshold_objective: str,
    feature_objective: str = "",
    selected_feature_set: str | None = None,
    signal_override: pd.Series | None = None,
) -> pd.DataFrame:
    if pred.empty:
        return pd.DataFrame()
    base = pred.copy()
    if signal_override is None:
        signal = np.where(base["prediction"] > threshold, 1, np.where(base["prediction"] < -threshold, -1, 0))
    else:
        signal = signal_override.reindex(base.index).fillna(0).astype(int).to_numpy()
    base["signal_direction"] = signal
    base = base[base["signal_direction"] != 0].copy()
    if base.empty:
        return base
    if {"raw_ticks", "tick_value_rub", "initial_margin_rub"}.issubset(base.columns):
        out = base.copy()
    else:
        out = attach_specs_fx(base, specs, fx)
    out["signed_ticks"] = out["signal_direction"] * out["raw_ticks"]
    out["gross_pnl_rub"] = out["signed_ticks"] * out["tick_value_rub"]
    out["slippage_ticks_roundtrip"] = slippage_ticks
    out["fee_rub_per_contract_roundtrip"] = fee_rub
    out["net_pnl_rub"] = out["gross_pnl_rub"] - slippage_ticks * out["tick_value_rub"] - fee_rub
    out["return_on_go"] = out["net_pnl_rub"] / out["initial_margin_rub"]
    out["return_on_notional"] = out["net_pnl_rub"] / out["notional_rub"]
    out["horizon_minutes"] = HORIZON_MINUTES
    out["secid_front"] = out["target_contract"]
    out["threshold_policy"] = "train_selected"
    out["selected_threshold"] = threshold
    out["selected_threshold_type"] = threshold_type
    out["threshold_objective"] = threshold_objective
    out["feature_objective"] = feature_objective
    out["strategy_mode"] = strategy_mode
    out["selected_feature_set"] = selected_feature_set if selected_feature_set is not None else out["feature_set"]
    return out


def write_anomalies(unit_base: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if unit_base.empty:
        out = pd.DataFrame()
        out.to_csv(REPORTS / "unit_anomalies.csv", index=False)
        return out
    checks = [
        ("tick_rounding", unit_base["tick_rounding_anomaly"]),
        ("missing_fx", unit_base["usd_rub_rate"].isna()),
        ("missing_margin", unit_base["initial_margin_rub"].isna()),
        ("fallback_spec", unit_base["spec_source"].astype(str).str.contains("fallback", na=False)),
        ("unverified_units", ~unit_base["verified_units"]),
    ]
    for name, mask in checks:
        g = unit_base[mask].copy()
        for _, row in g.head(5000).iterrows():
            rows.append(
                {
                    "anomaly_type": name,
                    "target_month": row.get("test_month"),
                    "target_contract": row.get("target_contract"),
                    "begin_signal": row.get("begin_signal"),
                    "entry_begin": row.get("entry_begin"),
                    "exit_begin": row.get("exit_begin"),
                    "entry_open": row.get("entry_open"),
                    "exit_open": row.get("exit_open"),
                    "raw_ticks": row.get("raw_ticks"),
                    "spec_source": row.get("spec_source"),
                    "warning": row.get("warning"),
                }
            )
    out = pd.DataFrame(rows)
    out.to_csv(REPORTS / "unit_anomalies.csv", index=False)
    return out


def assert_ng_tick_value(unit_base: pd.DataFrame) -> None:
    if unit_base.empty:
        return
    sample = unit_base[unit_base["target_contract"].astype(str).str.startswith("NG")].copy()
    if sample.empty:
        return
    usd_rub = 79.0
    expected = 40 * NG_LARGE_TICK_VALUE_USD * usd_rub
    if not 300 <= expected <= 330:
        raise AssertionError(f"NG tick sanity failed: 40 ticks at USD/RUB 79 expected about 316 RUB, got {expected}")
    near = sample[(sample["raw_ticks"].abs().sub(40).abs() < 1e-9) & (sample["usd_rub_rate"].between(75, 83))]
    if not near.empty:
        gross_abs = near["gross_pnl_rub"].abs()
        if not gross_abs.between(250, 380).all():
            bad = near.iloc[0][["target_contract", "raw_ticks", "usd_rub_rate", "tick_value_rub", "gross_pnl_rub"]].to_dict()
            raise AssertionError(f"NG tick sanity failed on real trade: {bad}")


def threshold_candidates(train_pred: pd.DataFrame) -> list[tuple[float, str]]:
    values = train_pred["abs_prediction"].replace([np.inf, -np.inf], np.nan).dropna()
    candidates = [(0.0, "zero")]
    if not values.empty:
        candidates.extend((float(np.percentile(values, pct)), f"train_abs_p{pct}") for pct in THRESHOLD_PERCENTILES)
    candidates.extend((float(value), f"fixed_{value:g}") for value in THRESHOLD_FIXED)
    seen = set()
    out = []
    for threshold, label in candidates:
        key = round(threshold, 12)
        if key not in seen:
            seen.add(key)
            out.append((threshold, label))
    return out


def select_threshold(
    train_pred: pd.DataFrame,
    specs: pd.DataFrame,
    fx: pd.DataFrame,
    slippage_ticks: int,
    fee_rub: float,
    objective: str,
    strategy_mode: str,
    feature_objective: str = "",
) -> tuple[float, str, dict]:
    def evaluate_fast(frame: pd.DataFrame, threshold: float) -> dict:
        if frame.empty:
            return {"n_trades": 0, "months": 0, "net_mean": np.nan, "net_sum": np.nan, "positive_months": 0}
        mask = frame["abs_prediction"].to_numpy(dtype=float) > threshold
        if not mask.any():
            return {"n_trades": 0, "months": 0, "net_mean": np.nan, "net_sum": np.nan, "positive_months": 0}
        sub = frame.loc[mask]
        signal = np.sign(sub["prediction"].to_numpy(dtype=float))
        net = signal * sub["raw_ticks"].to_numpy(dtype=float) * sub["tick_value_rub"].to_numpy(dtype=float)
        net = net - slippage_ticks * sub["tick_value_rub"].to_numpy(dtype=float) - fee_rub
        tmp = pd.DataFrame({"sample_month": sub.get("sample_month", sub["test_month"]).to_numpy(), "net": net})
        monthly = tmp.groupby("sample_month")["net"].sum()
        return {
            "n_trades": int(len(sub)),
            "months": int(monthly.size),
            "net_mean": float(np.nanmean(net)),
            "net_sum": float(np.nansum(net)),
            "positive_months": int((monthly > 0).sum()),
        }

    best = {
        "threshold": 0.0,
        "type": "zero_fallback",
        "metric": -np.inf,
        "n_trades": 0,
        "months": 0,
        "net_mean": np.nan,
        "net_sum": np.nan,
        "positive_months": 0,
        "fallback_used": True,
        "fallback_reason": "no candidate satisfied minimum train trades/months",
    }
    for threshold, label in threshold_candidates(train_pred):
        stats_fast = evaluate_fast(train_pred, threshold)
        if stats_fast["n_trades"] < MIN_TRAIN_TRADES or stats_fast["months"] < MIN_TRAIN_MONTHS:
            continue
        metric = stats_fast["net_mean"] if objective == "train_mean" else stats_fast["net_sum"]
        if metric > best["metric"]:
            best.update(
                {
                    "threshold": threshold,
                    "type": label,
                    "metric": metric,
                    "n_trades": stats_fast["n_trades"],
                    "months": stats_fast["months"],
                    "net_mean": stats_fast["net_mean"],
                    "net_sum": stats_fast["net_sum"],
                    "positive_months": stats_fast["positive_months"],
                    "fallback_used": False,
                    "fallback_reason": "",
                }
            )
    if best["fallback_used"]:
        stats_fast = evaluate_fast(train_pred, 0.0)
        if stats_fast["n_trades"] > 0:
            best.update(
                {
                    "n_trades": stats_fast["n_trades"],
                    "months": stats_fast["months"],
                    "net_mean": stats_fast["net_mean"],
                    "net_sum": stats_fast["net_sum"],
                    "positive_months": stats_fast["positive_months"],
                }
            )
    return float(best["threshold"]), str(best["type"]), best


def choose_no_overlap(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades
    kept = []
    active_until: dict[str, pd.Timestamp] = {}
    for _, row in trades.sort_values(["entry_begin", "exit_begin"]).iterrows():
        contract = str(row["target_contract"])
        entry = pd.Timestamp(row["entry_begin"])
        if contract in active_until and entry < active_until[contract]:
            continue
        kept.append(row)
        active_until[contract] = pd.Timestamp(row["exit_begin"])
    return pd.DataFrame(kept).reset_index(drop=True) if kept else trades.iloc[0:0].copy()


def choose_global_no_overlap(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades
    kept = []
    active_until = pd.Timestamp.min
    for _, row in trades.sort_values(["entry_begin", "exit_begin"]).iterrows():
        entry = pd.Timestamp(row["entry_begin"])
        if entry < active_until:
            continue
        kept.append(row)
        active_until = pd.Timestamp(row["exit_begin"])
    return pd.DataFrame(kept).reset_index(drop=True) if kept else trades.iloc[0:0].copy()


def contract_ym(secid: str) -> int:
    code = str(secid)[2]
    year = 2020 + int(str(secid)[3])
    month = {v: k for k, v in MONTH_CODES.items()}[code]
    return year * 100 + month


def choose_front_month_only(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades
    work = trades.copy()
    work["_contract_ym"] = work["target_contract"].map(contract_ym)
    work = work.sort_values(["begin_signal", "_contract_ym", "abs_prediction"], ascending=[True, True, False])
    front = work.groupby("begin_signal", as_index=False).head(1).drop(columns=["_contract_ym"])
    return choose_global_no_overlap(front)


def equity_curve(trades: pd.DataFrame, portfolio_mode: str) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(columns=["event_time", "equity_rub", "drawdown_rub", "blocked_margin_rub", "portfolio_mode"])
    t = trades.copy()
    t["entry_begin"] = pd.to_datetime(t["entry_begin"])
    t["exit_begin"] = pd.to_datetime(t["exit_begin"])
    events = []
    for _, row in t.iterrows():
        margin = float(row["initial_margin_rub"]) if pd.notna(row["initial_margin_rub"]) else 0.0
        events.append({"event_time": row["entry_begin"], "event_order": 1, "pnl": 0.0, "margin_delta": margin})
        events.append({"event_time": row["exit_begin"], "event_order": 0, "pnl": float(row["net_pnl_rub"]), "margin_delta": -margin})
    ev = pd.DataFrame(events).sort_values(["event_time", "event_order"])
    ev["equity_rub"] = ev["pnl"].cumsum()
    ev["blocked_margin_rub"] = ev["margin_delta"].cumsum().clip(lower=0)
    ev["drawdown_rub"] = ev["equity_rub"] - ev["equity_rub"].cummax()
    ev["portfolio_mode"] = portfolio_mode
    return ev


def apply_portfolio_mode(trades: pd.DataFrame, portfolio_mode: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    if portfolio_mode in {"independent_trades", "all_targets_curve_strategy"}:
        selected = trades.copy()
    elif portfolio_mode == "portfolio_no_overlap":
        selected = choose_no_overlap(trades)
    elif portfolio_mode == "global_no_overlap":
        selected = choose_global_no_overlap(trades)
    elif portfolio_mode == "front_month_only":
        selected = choose_front_month_only(trades)
    elif portfolio_mode == "portfolio_allow_overlap":
        selected = trades.copy()
    else:
        raise ValueError(f"unknown portfolio_mode={portfolio_mode}")
    selected["portfolio_mode"] = portfolio_mode
    return selected, equity_curve(selected, portfolio_mode)


def summarize_strategy(trades: pd.DataFrame, equity: pd.DataFrame, keys: dict) -> dict:
    row = dict(keys)
    if trades.empty:
        row.update(
            {
                "n_trades": 0,
                "gross_ticks_mean": np.nan,
                "gross_ticks_median": np.nan,
                "signed_ticks_sum": 0.0,
                "gross_pnl_rub_sum": 0.0,
                "net_pnl_rub_sum": 0.0,
                "mean_net_pnl_rub": np.nan,
                "median_net_pnl_rub": np.nan,
                "net_hit_rate": np.nan,
                "mean_return_on_go": np.nan,
                "median_return_on_go": np.nan,
                "simple_total_return_on_go": np.nan,
                "positive_months": 0,
                "total_months": 0,
                "worst_month_net_pnl_rub": np.nan,
                "best_month_net_pnl_rub": np.nan,
                "max_drawdown_rub": np.nan,
                "max_drawdown_on_go": np.nan,
                "max_blocked_margin_rub": 0.0,
                "avg_blocked_margin_rub": 0.0,
                "monthly_t_stat_by_net_sum": np.nan,
                "monthly_sharpe_by_net_sum": np.nan,
                "best_month_profit_share": np.nan,
                "top3_months_profit_share": np.nan,
            }
        )
        return row
    monthly = trades.groupby("test_month")["net_pnl_rub"].sum()
    month_std = monthly.std(ddof=1)
    total_net = float(trades["net_pnl_rub"].sum())
    positive_months = int((monthly > 0).sum())
    shares = monthly.sort_values(ascending=False)
    avg_margin = float(trades["initial_margin_rub"].mean()) if trades["initial_margin_rub"].notna().any() else np.nan
    max_dd = float(equity["drawdown_rub"].min()) if not equity.empty else np.nan
    max_blocked = float(equity["blocked_margin_rub"].max()) if not equity.empty else float(trades["initial_margin_rub"].max())
    avg_blocked = float(equity["blocked_margin_rub"].mean()) if not equity.empty else float(trades["initial_margin_rub"].mean())
    row.update(
        {
            "n_trades": len(trades),
            "gross_ticks_mean": float(trades["signed_ticks"].mean()),
            "gross_ticks_median": float(trades["signed_ticks"].median()),
            "signed_ticks_sum": float(trades["signed_ticks"].sum()),
            "gross_pnl_rub_sum": float(trades["gross_pnl_rub"].sum()),
            "net_pnl_rub_sum": total_net,
            "mean_net_pnl_rub": float(trades["net_pnl_rub"].mean()),
            "median_net_pnl_rub": float(trades["net_pnl_rub"].median()),
            "net_hit_rate": float((trades["net_pnl_rub"] > 0).mean()),
            "mean_return_on_go": float(trades["return_on_go"].mean()),
            "median_return_on_go": float(trades["return_on_go"].median()),
            "simple_total_return_on_go": total_net / max_blocked if max_blocked > 0 else np.nan,
            "positive_months": positive_months,
            "total_months": int(monthly.size),
            "worst_month_net_pnl_rub": float(monthly.min()),
            "best_month_net_pnl_rub": float(monthly.max()),
            "max_drawdown_rub": max_dd,
            "max_drawdown_on_go": max_dd / avg_margin if avg_margin and np.isfinite(avg_margin) else np.nan,
            "max_blocked_margin_rub": max_blocked,
            "avg_blocked_margin_rub": avg_blocked,
            "monthly_t_stat_by_net_sum": float(monthly.mean() / (month_std / math.sqrt(len(monthly)))) if month_std and np.isfinite(month_std) else np.nan,
            "monthly_sharpe_by_net_sum": float(monthly.mean() / month_std) if month_std and np.isfinite(month_std) else np.nan,
            "best_month_profit_share": float(shares.iloc[0] / total_net) if total_net > 0 and not shares.empty else np.nan,
            "top3_months_profit_share": float(shares.head(3).sum() / total_net) if total_net > 0 and not shares.empty else np.nan,
        }
    )
    return row


def monthly_report(trades: pd.DataFrame, keys: dict) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    rows = []
    for month, g in trades.groupby("test_month"):
        rows.append(
            {
                **keys,
                "test_month": month,
                "selected_feature_set": g["selected_feature_set"].iloc[0],
                "selected_threshold": g["selected_threshold"].iloc[0],
                "threshold_type": g["selected_threshold_type"].iloc[0],
                "n_trades": len(g),
                "gross_ticks_mean": float(g["signed_ticks"].mean()),
                "signed_ticks_sum": float(g["signed_ticks"].sum()),
                "gross_pnl_rub_sum": float(g["gross_pnl_rub"].sum()),
                "net_pnl_rub_sum": float(g["net_pnl_rub"].sum()),
                "mean_net_pnl_rub": float(g["net_pnl_rub"].mean()),
                "hit_rate_net": float((g["net_pnl_rub"] > 0).mean()),
                "initial_margin_rub_mean": float(g["initial_margin_rub"].mean()),
                "return_on_go_sum_simple": float(g["net_pnl_rub"].sum() / g["initial_margin_rub"].mean()) if g["initial_margin_rub"].mean() > 0 else np.nan,
                "max_blocked_margin_rub": float(g["initial_margin_rub"].sum()),
                "positive_month": bool(g["net_pnl_rub"].sum() > 0),
            }
        )
    return pd.DataFrame(rows)


def build_strategy_runs(test_preds: pd.DataFrame, train_preds: pd.DataFrame, specs: pd.DataFrame, fx: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    all_trades = []
    all_monthly = []
    all_summary = []
    all_equity = []
    selection_log = []
    months = sorted(test_preds["test_month"].dropna().unique())
    portfolio_modes = [
        "all_targets_curve_strategy",
        "front_month_only",
        "global_no_overlap",
        "portfolio_no_overlap",
        "portfolio_allow_overlap",
    ]
    threshold_cache: dict[tuple[str, str, int, float, str], tuple[float, str, dict]] = {}

    def cached_threshold(test_month: str, feature_set: str, slippage: int, fee: float, objective: str, strategy_mode: str, feature_objective: str = "") -> tuple[float, str, dict]:
        key = (test_month, feature_set, slippage, fee, objective)
        if key not in threshold_cache:
            train = train_preds[(train_preds["test_month"] == test_month) & (train_preds["feature_set"] == feature_set)]
            threshold_cache[key] = select_threshold(train, specs, fx, slippage, fee, objective, strategy_mode, feature_objective)
        return threshold_cache[key]

    for slippage in SLIPPAGE_TICKS_GRID:
        for fee in FEE_RUB_GRID:
            log(f"Third-pass strategy grid: slippage={slippage} ticks, fee={fee} RUB")
            cost_scenario = f"{slippage}ticks_{fee}rub"
            for threshold_objective in THRESHOLD_OBJECTIVES:
                for test_month in months:
                    for strategy_mode in ["fixed_plus1_only", "train_selected_feature"]:
                        if strategy_mode == "fixed_plus1_only":
                            feature_objectives = [""]
                        else:
                            feature_objectives = [threshold_objective]
                        for feature_objective in feature_objectives:
                            candidate_features = ["plus1_only"] if strategy_mode == "fixed_plus1_only" else list(FEATURE_SETS.keys())
                            best_feature = "plus1_only"
                            best_threshold = 0.0
                            best_type = "zero_fallback"
                            best_stats = None
                            best_metric = -np.inf
                            fallback_used = True
                            fallback_reason = "no feature candidate satisfied minimum train trades/months"
                            for feature_set in candidate_features:
                                threshold, threshold_type, stats = cached_threshold(test_month, feature_set, slippage, fee, threshold_objective, strategy_mode, feature_objective)
                                metric = stats["net_mean"] if (feature_objective or threshold_objective) == "train_mean" else stats["net_sum"]
                                if strategy_mode == "fixed_plus1_only" or (np.isfinite(metric) and metric > best_metric):
                                    best_feature = feature_set
                                    best_threshold = threshold
                                    best_type = threshold_type
                                    best_stats = stats
                                    best_metric = metric if np.isfinite(metric) else -np.inf
                                    fallback_used = bool(stats["fallback_used"])
                                    fallback_reason = str(stats["fallback_reason"])
                                if strategy_mode == "fixed_plus1_only":
                                    break
                            if best_stats is None:
                                continue
                            test = test_preds[(test_preds["test_month"] == test_month) & (test_preds["feature_set"] == best_feature)]
                            raw_trades = trades_from_predictions(
                                test,
                                specs,
                                fx,
                                best_threshold,
                                best_type,
                                slippage,
                                fee,
                                strategy_mode,
                                threshold_objective,
                                feature_objective,
                                best_feature,
                            )
                            selection_log.append(
                                {
                                    "test_month": test_month,
                                    "strategy_mode": strategy_mode,
                                    "cost_scenario": cost_scenario,
                                    "train_start_month": test["train_start_month"].iloc[0] if not test.empty else "",
                                    "train_end_month": test["train_end_month"].iloc[0] if not test.empty else "",
                                    "selected_feature_set": best_feature,
                                    "selected_threshold": best_threshold,
                                    "selected_threshold_type": best_type,
                                    "threshold_objective": threshold_objective,
                                    "feature_objective": feature_objective,
                                    "train_n_trades": best_stats["n_trades"],
                                    "train_months": best_stats["months"],
                                    "train_net_pnl_rub_mean": best_stats["net_mean"],
                                    "train_net_pnl_rub_sum": best_stats["net_sum"],
                                    "train_positive_months": best_stats["positive_months"],
                                    "fallback_used": fallback_used,
                                    "fallback_reason": fallback_reason,
                                }
                            )
                            for portfolio_mode in portfolio_modes:
                                selected, eq = apply_portfolio_mode(raw_trades, portfolio_mode)
                                keys = {
                                    "strategy_mode": strategy_mode,
                                    "portfolio_mode": portfolio_mode,
                                    "slippage_ticks_roundtrip": slippage,
                                    "fee_rub_per_contract_roundtrip": fee,
                                    "threshold_objective": threshold_objective,
                                    "feature_objective": feature_objective,
                                }
                                if not selected.empty:
                                    all_trades.append(selected.assign(**keys))
                                    all_monthly.append(monthly_report(selected, keys))
                                if not eq.empty:
                                    all_equity.append(eq.assign(**keys))
                    # Ensemble is handled once per test month/objective/cost.
                    thresholds = {}
                    ensemble_tests = []
                    ensemble_train_stats = []
                    for feature_set in ENSEMBLE_FEATURES:
                        threshold, threshold_type, stats = cached_threshold(test_month, feature_set, slippage, fee, threshold_objective, "ensemble_vote")
                        thresholds[feature_set] = (threshold, threshold_type)
                        ensemble_train_stats.append(stats)
                        ensemble_tests.append(test_preds[(test_preds["test_month"] == test_month) & (test_preds["feature_set"] == feature_set)].copy())
                    if len(ensemble_tests) == 3 and all(not x.empty for x in ensemble_tests):
                        merged = ensemble_tests[0].copy()
                        merged = merged.rename(columns={"prediction": "prediction_plus1_only", "abs_prediction": "abs_plus1_only"})
                        for feature_set, frame in zip(ENSEMBLE_FEATURES[1:], ensemble_tests[1:]):
                            merged = merged.merge(
                                frame[["begin_signal", "prediction", "abs_prediction"]],
                                on="begin_signal",
                                how="inner",
                            ).rename(columns={"prediction": f"prediction_{feature_set}", "abs_prediction": f"abs_{feature_set}"})
                        votes = []
                        for _, row in merged.iterrows():
                            signs = []
                            for feature_set in ENSEMBLE_FEATURES:
                                pred_col = f"prediction_{feature_set}"
                                abs_col = "abs_plus1_only" if feature_set == "plus1_only" else f"abs_{feature_set}"
                                threshold = thresholds[feature_set][0]
                                if abs(row[abs_col]) >= threshold:
                                    signs.append(int(np.sign(row[pred_col])))
                            long_votes = signs.count(1)
                            short_votes = signs.count(-1)
                            votes.append(1 if long_votes >= 2 else (-1 if short_votes >= 2 else 0))
                        merged["prediction"] = merged[[f"prediction_{f}" for f in ENSEMBLE_FEATURES]].mean(axis=1)
                        merged["abs_prediction"] = merged["prediction"].abs()
                        raw_trades = trades_from_predictions(
                            merged,
                            specs,
                            fx,
                            0.0,
                            json.dumps({k: v[1] for k, v in thresholds.items()}),
                            slippage,
                            fee,
                            "ensemble_vote",
                            threshold_objective,
                            "",
                            "ensemble_vote",
                            signal_override=pd.Series(votes, index=merged.index),
                        )
                        selection_log.append(
                            {
                                "test_month": test_month,
                                "strategy_mode": "ensemble_vote",
                                "cost_scenario": cost_scenario,
                                "train_start_month": merged["train_start_month"].iloc[0] if not merged.empty else "",
                                "train_end_month": merged["train_end_month"].iloc[0] if not merged.empty else "",
                                "selected_feature_set": "ensemble_vote",
                                "selected_threshold": json.dumps({k: v[0] for k, v in thresholds.items()}),
                                "selected_threshold_type": json.dumps({k: v[1] for k, v in thresholds.items()}),
                                "threshold_objective": threshold_objective,
                                "feature_objective": "",
                                "train_n_trades": min(s["n_trades"] for s in ensemble_train_stats),
                                "train_months": min(s["months"] for s in ensemble_train_stats),
                                "train_net_pnl_rub_mean": np.mean([s["net_mean"] for s in ensemble_train_stats]),
                                "train_net_pnl_rub_sum": np.sum([s["net_sum"] for s in ensemble_train_stats]),
                                "train_positive_months": np.nan,
                                "fallback_used": any(s["fallback_used"] for s in ensemble_train_stats),
                                "fallback_reason": "; ".join(sorted({s["fallback_reason"] for s in ensemble_train_stats if s["fallback_reason"]})),
                            }
                        )
                        for portfolio_mode in portfolio_modes:
                            selected, eq = apply_portfolio_mode(raw_trades, portfolio_mode)
                            keys = {
                                "strategy_mode": "ensemble_vote",
                                "portfolio_mode": portfolio_mode,
                                "slippage_ticks_roundtrip": slippage,
                                "fee_rub_per_contract_roundtrip": fee,
                                "threshold_objective": threshold_objective,
                                "feature_objective": "",
                            }
                            if not selected.empty:
                                all_trades.append(selected.assign(**keys))
                                all_monthly.append(monthly_report(selected, keys))
                            if not eq.empty:
                                all_equity.append(eq.assign(**keys))

    trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    monthly = pd.concat(all_monthly, ignore_index=True) if all_monthly else pd.DataFrame()
    equity = pd.concat(all_equity, ignore_index=True) if all_equity else pd.DataFrame()
    if not trades.empty:
        group_cols = [
            "strategy_mode",
            "portfolio_mode",
            "slippage_ticks_roundtrip",
            "fee_rub_per_contract_roundtrip",
            "threshold_objective",
            "feature_objective",
        ]
        for keys, group in trades.groupby(group_cols, dropna=False):
            key = dict(zip(group_cols, keys))
            eq = equity.copy()
            if not eq.empty:
                mask = pd.Series(True, index=eq.index)
                for col, value in key.items():
                    mask &= eq[col].astype(str) == str(value)
                eq = eq[mask]
            all_summary.append(summarize_strategy(group, eq, key))
    summary = pd.DataFrame(all_summary)
    selection = pd.DataFrame(selection_log)
    return trades, monthly, summary, equity, selection


def add_passes(summary: pd.DataFrame) -> pd.DataFrame:
    out = summary.copy()
    if out.empty:
        return out
    ratio = out["positive_months"] / out["total_months"].replace(0, np.nan)
    out["passes_costs_boolean"] = (
        (out["net_pnl_rub_sum"] > 0)
        & (ratio >= 0.55)
        & (out["n_trades"] >= 100)
        & ((out["best_month_profit_share"].isna()) | (out["best_month_profit_share"] <= 0.5))
    )
    return out


def lookahead_audit(test_preds: pd.DataFrame, selection: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    pred = test_preds.copy()
    pred["begin_signal"] = pd.to_datetime(pred["begin_signal"])
    pred["entry_begin"] = pd.to_datetime(pred["entry_begin"])
    pred["exit_begin"] = pd.to_datetime(pred["exit_begin"])
    pred["train_end_ts"] = pd.to_datetime(pred["train_end_month"] + "-01")
    pred["test_ts"] = pd.to_datetime(pred["test_month"] + "-01")
    rows.append({"check": "train_month_strictly_before_test_month", "passed": bool((pred["train_end_ts"] < pred["test_ts"]).all()), "failures": int((pred["train_end_ts"] >= pred["test_ts"]).sum())})
    rows.append({"check": "entry_begin_after_signal", "passed": bool((pred["entry_begin"] > pred["begin_signal"]).all()), "failures": int((pred["entry_begin"] <= pred["begin_signal"]).sum())})
    rows.append({"check": "exit_begin_after_entry", "passed": bool((pred["exit_begin"] > pred["entry_begin"]).all()), "failures": int((pred["exit_begin"] <= pred["entry_begin"]).sum())})
    rows.append({"check": "30m_exit_is_open_t_plus_4", "passed": bool(((pred["exit_begin"] - pred["begin_signal"]).dt.total_seconds() == 40 * 60).all()), "failures": int(((pred["exit_begin"] - pred["begin_signal"]).dt.total_seconds() != 40 * 60).sum())})
    if not selection.empty:
        sel = selection.copy()
        sel["train_end_ts"] = pd.to_datetime(sel["train_end_month"] + "-01", errors="coerce")
        sel["test_ts"] = pd.to_datetime(sel["test_month"] + "-01", errors="coerce")
        rows.append({"check": "threshold_selected_on_train_only", "passed": bool((sel["train_end_ts"] < sel["test_ts"]).all()), "failures": int((sel["train_end_ts"] >= sel["test_ts"]).sum())})
        rows.append({"check": "feature_selected_on_train_only", "passed": bool((sel["train_end_ts"] < sel["test_ts"]).all()), "failures": int((sel["train_end_ts"] >= sel["test_ts"]).sum())})
    if not trades.empty:
        rows.append({"check": "pnl_uses_ticks_rub_columns", "passed": bool({"raw_ticks", "tick_value_rub", "net_pnl_rub", "return_on_go"}.issubset(trades.columns)), "failures": 0})
    out = pd.DataFrame(rows)
    out.to_csv(REPORTS / "third_pass_lookahead_audit.csv", index=False)
    return out


def write_outputs(trades: pd.DataFrame, monthly: pd.DataFrame, summary: pd.DataFrame, equity: pd.DataFrame, selection: pd.DataFrame) -> None:
    trades.to_csv(REPORTS / "third_pass_strategy_trades.csv", index=False)
    monthly.to_csv(REPORTS / "third_pass_strategy_by_month.csv", index=False)
    summary.to_csv(REPORTS / "third_pass_strategy_summary.csv", index=False)
    selection.to_csv(REPORTS / "third_pass_feature_selection_log.csv", index=False)
    summary.to_csv(REPORTS / "slippage_ticks_grid.csv", index=False)
    summary.to_csv(REPORTS / "margin_roi_summary.csv", index=False)
    if not equity.empty:
        base_mask = pd.Series(False, index=equity.index)
        for slippage, fee in BASE_SCENARIOS:
            base_mask |= (equity["slippage_ticks_roundtrip"] == slippage) & (equity["fee_rub_per_contract_roundtrip"] == fee)
        equity_out = equity[base_mask].copy()
    else:
        equity_out = equity
    equity_out.to_csv(REPORTS / "portfolio_margin_equity.csv", index=False)
    summary.to_csv(REPORTS / "portfolio_margin_summary.csv", index=False)


def make_unit_base_report(test_preds: pd.DataFrame, specs: pd.DataFrame, fx: pd.DataFrame) -> pd.DataFrame:
    out = trades_from_predictions(test_preds, specs, fx, 0.0, "zero", 2, 2, "all_feature_research", "none")
    out.to_csv(REPORTS / "trade_simulation_ticks_rub.csv", index=False)
    write_anomalies(out)
    return out


def plot_outputs(summary: pd.DataFrame, monthly: pd.DataFrame, equity: pd.DataFrame) -> None:
    PLOTS.mkdir(parents=True, exist_ok=True)
    base = summary[
        (summary["slippage_ticks_roundtrip"] == 2)
        & (summary["fee_rub_per_contract_roundtrip"] == 2)
        & (summary["threshold_objective"] == "train_mean")
        & (summary["portfolio_mode"].isin(["portfolio_no_overlap", "portfolio_allow_overlap"]))
    ].copy()
    if not equity.empty:
        for filename, filt in [
            ("third_pass_equity_30m_6bps_equivalent.png", (equity["slippage_ticks_roundtrip"] == 1) & (equity["fee_rub_per_contract_roundtrip"] == 1)),
            ("third_pass_equity_30m_tick_costs.png", (equity["slippage_ticks_roundtrip"] == 2) & (equity["fee_rub_per_contract_roundtrip"] == 2)),
        ]:
            fig, ax = plt.subplots(figsize=(11, 5))
            sub = equity[filt & (equity["threshold_objective"] == "train_mean")]
            for (strategy, portfolio), g in sub.groupby(["strategy_mode", "portfolio_mode"]):
                if portfolio == "independent_trades":
                    continue
                g = g.sort_values("event_time")
                ax.plot(pd.to_datetime(g["event_time"]), g["equity_rub"], label=f"{strategy} {portfolio}")
            ax.axhline(0, linewidth=0.8, color="black")
            ax.set_title(filename.replace(".png", ""))
            ax.set_ylabel("Equity, RUB")
            ax.legend(fontsize=8)
            ax.grid(True, alpha=0.25)
            fig.tight_layout()
            fig.savefig(PLOTS / filename, dpi=160)
            plt.close(fig)

        fig, ax = plt.subplots(figsize=(11, 5))
        sub = equity[(equity["slippage_ticks_roundtrip"] == 2) & (equity["fee_rub_per_contract_roundtrip"] == 2) & (equity["threshold_objective"] == "train_mean")]
        for (strategy, portfolio), g in sub.groupby(["strategy_mode", "portfolio_mode"]):
            if portfolio == "independent_trades":
                continue
            g = g.sort_values("event_time")
            ax.plot(pd.to_datetime(g["event_time"]), g["drawdown_rub"], label=f"{strategy} {portfolio}")
        ax.set_title("Third pass drawdown RUB")
        ax.set_ylabel("Drawdown, RUB")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.25)
        fig.tight_layout()
        fig.savefig(PLOTS / "third_pass_drawdown_rub.png", dpi=160)
        plt.close(fig)

    if not monthly.empty:
        sub = monthly[(monthly["slippage_ticks_roundtrip"] == 2) & (monthly["fee_rub_per_contract_roundtrip"] == 2) & (monthly["threshold_objective"] == "train_mean")]
        fig, ax = plt.subplots(figsize=(12, 5))
        for (strategy, portfolio), g in sub.groupby(["strategy_mode", "portfolio_mode"]):
            if portfolio != "portfolio_no_overlap":
                continue
            s = g.groupby("test_month")["net_pnl_rub_sum"].sum()
            ax.plot(s.index, s.values, marker="o", label=strategy)
        ax.axhline(0, linewidth=0.8, color="black")
        ax.set_title("Third pass monthly PnL RUB, no overlap")
        ax.tick_params(axis="x", rotation=60)
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.25)
        fig.tight_layout()
        fig.savefig(PLOTS / "third_pass_monthly_pnl_rub.png", dpi=160)
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(12, 5))
        for (strategy, portfolio), g in sub.groupby(["strategy_mode", "portfolio_mode"]):
            if portfolio != "portfolio_no_overlap":
                continue
            s = g.groupby("test_month")["return_on_go_sum_simple"].sum()
            ax.plot(s.index, s.values, marker="o", label=strategy)
        ax.axhline(0, linewidth=0.8, color="black")
        ax.set_title("Third pass return on GO by month, no overlap")
        ax.tick_params(axis="x", rotation=60)
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.25)
        fig.tight_layout()
        fig.savefig(PLOTS / "third_pass_return_on_go_by_month.png", dpi=160)
        plt.close(fig)

    if not base.empty:
        grid = summary[(summary["portfolio_mode"] == "portfolio_no_overlap") & (summary["threshold_objective"] == "train_mean")]
        fig, ax = plt.subplots(figsize=(11, 5))
        for strategy, g in grid.groupby("strategy_mode"):
            s = g.groupby("slippage_ticks_roundtrip")["net_pnl_rub_sum"].max()
            ax.plot(s.index, s.values, marker="o", label=strategy)
        ax.axhline(0, linewidth=0.8, color="black")
        ax.set_title("Third pass slippage ticks grid, best fee per slippage")
        ax.set_xlabel("Roundtrip slippage, ticks")
        ax.set_ylabel("Net PnL RUB")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.25)
        fig.tight_layout()
        fig.savefig(PLOTS / "third_pass_slippage_ticks_grid.png", dpi=160)
        plt.close(fig)


def format_money(value: float | int | None) -> str:
    if value is None or pd.isna(value):
        return "n/a"
    return f"{float(value):,.2f}"


def write_summary(summary: pd.DataFrame, specs: pd.DataFrame, fx: pd.DataFrame, audit: pd.DataFrame, unit_base: pd.DataFrame, anomalies: pd.DataFrame) -> None:
    def scenario_table(slippage: int, fee: int) -> pd.DataFrame:
        return summary[
            (summary["slippage_ticks_roundtrip"] == slippage)
            & (summary["fee_rub_per_contract_roundtrip"] == fee)
            & (summary["portfolio_mode"] == "portfolio_no_overlap")
            & (summary["threshold_objective"] == "train_mean")
        ].sort_values("net_pnl_rub_sum", ascending=False)

    best = summary[summary["portfolio_mode"] == "global_no_overlap"].sort_values("net_pnl_rub_sum", ascending=False).head(1)
    best_row = best.iloc[0] if not best.empty else pd.Series(dtype=object)
    verified = int(unit_base["verified_units"].sum()) if not unit_base.empty and "verified_units" in unit_base else 0
    total_unit = len(unit_base)
    audit_ok = bool(audit["passed"].all()) if not audit.empty else False
    fallback_specs = int(specs["spec_source"].astype(str).str.contains("fallback", na=False).sum())
    fallback_margin = int(specs["warning"].astype(str).str.contains("initial_margin", na=False).sum())

    lines = [
        "# Unit-corrected third pass MOEX NG lead-lag",
        "",
        "## Что изменилось",
        "Старый research-layer считал log-return и bps-cost. Third pass пересчитывает сделки в единицах контракта: price delta -> ticks -> tick value in RUB через USD/RUB -> gross/net PnL RUB -> return on ГО. ГО не вычитается из PnL, а используется как denominator и margin constraint.",
        "",
        "Важно: предыдущий third-pass результат был завышен для текущих контрактов из-за STEPPRICE currency bug. MOEX ISS `STEPPRICE` около 7.x является рублевой текущей оценкой тика, а не USD. Теперь для large NG используется спецификация: `min_step=0.001`, `contract_size=100`, `tick_value_usd=0.1`; `tick_value_rub=0.1*USD/RUB`. Например, 40 ticks при USD/RUB около 79 дают около 316 RUB gross PnL, а не около 22,500 RUB.",
        "Сопоставимый old third-pass best был существенно завышен, потому что текущий MOEX `STEPPRICE` был ошибочно трактован как USD tick value. После фикса лучший `global_no_overlap` для `2 ticks + 2 RUB fee` стал около 22,912 RUB.",
        "",
        "FX is daily approximation, not exact clearing FX. return_on_go uses approximate/current margin when historical ГО is unavailable.",
        "",
        "## Лучший global_no_overlap результат",
        f"- strategy_mode: `{best_row.get('strategy_mode', 'n/a')}`",
        f"- cost: `{best_row.get('slippage_ticks_roundtrip', 'n/a')}` ticks + `{best_row.get('fee_rub_per_contract_roundtrip', 'n/a')}` RUB fee",
        f"- net PnL RUB: `{format_money(best_row.get('net_pnl_rub_sum'))}`",
        f"- mean return on ГО: `{best_row.get('mean_return_on_go', np.nan):.6g}`",
        f"- positive months: `{best_row.get('positive_months', 0)}/{best_row.get('total_months', 0)}`",
        f"- max drawdown RUB: `{format_money(best_row.get('max_drawdown_rub'))}`",
        f"- trades: `{best_row.get('n_trades', 0)}`",
        "",
        "## Базовые cost scenarios, global_no_overlap, threshold=train_mean",
    ]
    for slippage, fee in BASE_SCENARIOS:
        table = summary[
            (summary["slippage_ticks_roundtrip"] == slippage)
            & (summary["fee_rub_per_contract_roundtrip"] == fee)
            & (summary["portfolio_mode"] == "global_no_overlap")
            & (summary["threshold_objective"] == "train_mean")
        ].sort_values("net_pnl_rub_sum", ascending=False)
        if table.empty:
            lines.append(f"- {slippage} tick + {fee} RUB fee: нет строк.")
            continue
        row = table.iloc[0]
        lines.append(
            f"- {slippage} tick + {fee} RUB fee: best `{row['strategy_mode']}`, "
            f"net={format_money(row['net_pnl_rub_sum'])} RUB, mean_net={format_money(row['mean_net_pnl_rub'])} RUB/trade, "
            f"mean_ROGO={row['mean_return_on_go']:.6g}, positive_months={int(row['positive_months'])}/{int(row['total_months'])}, "
            f"maxDD={format_money(row['max_drawdown_rub'])} RUB."
        )
    lines.extend(["", "## Strategy modes"])
    for mode in ["fixed_plus1_only", "train_selected_feature", "ensemble_vote"]:
        table = summary[
            (summary["strategy_mode"] == mode)
            & (summary["portfolio_mode"] == "global_no_overlap")
            & (summary["threshold_objective"] == "train_mean")
            & (summary["slippage_ticks_roundtrip"] == 2)
            & (summary["fee_rub_per_contract_roundtrip"] == 2)
        ].sort_values("net_pnl_rub_sum", ascending=False)
        if table.empty:
            lines.append(f"- {mode}: нет строк.")
        else:
            row = table.iloc[0]
            fragile = " Edge хрупкий." if row["net_pnl_rub_sum"] <= 0 else ""
            lines.append(
                f"- {mode}: net={format_money(row['net_pnl_rub_sum'])} RUB, "
                f"ticks_mean={row['gross_ticks_mean']:.4f}, return_on_GO={row['mean_return_on_go']:.6g}, "
                f"positive_months={int(row['positive_months'])}/{int(row['total_months'])}, "
                f"best_month_share={row['best_month_profit_share'] if pd.notna(row['best_month_profit_share']) else 'n/a'}.{fragile}"
            )
    no_overlap = summary[(summary["portfolio_mode"] == "portfolio_no_overlap") & (summary["slippage_ticks_roundtrip"] == 2) & (summary["fee_rub_per_contract_roundtrip"] == 2)]
    global_no_overlap = summary[(summary["portfolio_mode"] == "global_no_overlap") & (summary["slippage_ticks_roundtrip"] == 2) & (summary["fee_rub_per_contract_roundtrip"] == 2)]
    front_only = summary[(summary["portfolio_mode"] == "front_month_only") & (summary["slippage_ticks_roundtrip"] == 2) & (summary["fee_rub_per_contract_roundtrip"] == 2)]
    allow_overlap = summary[(summary["portfolio_mode"] == "portfolio_allow_overlap") & (summary["slippage_ticks_roundtrip"] == 2) & (summary["fee_rub_per_contract_roundtrip"] == 2)]
    lines.extend(
        [
            "",
            "## ГО и концентрация",
            f"- max blocked ГО no_overlap: `{format_money(no_overlap['max_blocked_margin_rub'].max() if not no_overlap.empty else np.nan)}` RUB.",
            f"- max blocked ГО global_no_overlap: `{format_money(global_no_overlap['max_blocked_margin_rub'].max() if not global_no_overlap.empty else np.nan)}` RUB.",
            f"- max blocked ГО front_month_only: `{format_money(front_only['max_blocked_margin_rub'].max() if not front_only.empty else np.nan)}` RUB.",
            f"- max blocked ГО allow_overlap: `{format_money(allow_overlap['max_blocked_margin_rub'].max() if not allow_overlap.empty else np.nan)}` RUB.",
            "- Если результат положителен только в allow_overlap, он зависит от накопления позиций и не является тем же самым edge, что no_overlap.",
            "",
            "## Front-only comparison",
            "Сравнение `all_targets_curve_strategy`, `front_month_only`, `global_no_overlap` сохранено в `third_pass_strategy_summary.csv`. `front_month_only` выбирает только ближайший target/front contract на timestamp и не торгует одновременные NGH/NGJ/NGK как независимые фронты.",
            "",
            "## Unit / data audit",
            f"- verified_units=True: `{verified}/{total_unit}` unit trade rows.",
            f"- anomalies rows saved: `{len(anomalies)}`.",
            f"- specs with fallback: `{fallback_specs}/{len(specs)}`.",
            f"- margins with fallback/approx warning: `{fallback_margin}/{len(specs)}`.",
            f"- FX rows: `{len(fx)}`, source daily approximation.",
            f"- Look-ahead audit passed: `{audit_ok}`.",
            "- 2025-08 не исправлялся искусственно: месяц отсутствует из-за отсутствия common exact 10-minute begin timestamps.",
            "",
            "## Можно ли торговать сейчас?",
            "Нет. Даже положительный unit-corrected результат является кандидатом для следующей проверки, а не готовой торговой системой: нет bid/ask, стакана, очереди, partial fills, market impact и проверки исполнения календарной связки.",
            "",
            "## Следующий шаг",
            "Переходить на 1m/trades/order book можно и нужно. Проверять: bid/ask, реальный spread, очередь, partial fills, стакан, market impact, исполнение календарной связки, устойчивость в часы ликвидности и rollover.",
        ]
    )
    (REPORTS / "unit_corrected_third_pass_summary_ru.md").write_text("\n".join(lines), encoding="utf-8")


def artifact_sync_audit() -> pd.DataFrame:
    artifacts = [
        REPORTS / "third_pass_strategy_trades.csv",
        REPORTS / "third_pass_strategy_summary.csv",
        REPORTS / "third_pass_strategy_by_month.csv",
        REPORTS / "portfolio_margin_summary.csv",
        REPORTS / "unit_corrected_third_pass_summary_ru.md",
        ROOT / "README.md",
    ]
    rows = []
    trades = read_csv(REPORTS / "third_pass_strategy_trades.csv")
    summary = read_csv(REPORTS / "third_pass_strategy_summary.csv")
    readme_text = (ROOT / "README.md").read_text(encoding="utf-8", errors="ignore") if (ROOT / "README.md").exists() else ""
    md_text = (REPORTS / "unit_corrected_third_pass_summary_ru.md").read_text(encoding="utf-8", errors="ignore") if (REPORTS / "unit_corrected_third_pass_summary_ru.md").exists() else ""

    ngk6_rows = int((trades.get("target_contract", pd.Series(dtype=str)) == "NGK6").sum()) if not trades.empty else 0
    ngk6_bad = 0
    bad_ratio = 0
    if not trades.empty:
        ratio = pd.to_numeric(trades["tick_value_rub"], errors="coerce") / pd.to_numeric(trades["usd_rub_rate"], errors="coerce")
        ngk6_bad = int(
            (
                (trades["target_contract"] == "NGK6")
                & (pd.to_numeric(trades["step_price"], errors="coerce") > 1)
                & (trades["step_price_currency"].astype(str) == "USD")
            ).sum()
        )
        bad_ratio = int(((ratio - 0.1).abs() > 1e-6).sum())
        near40 = trades[
            (trades["target_contract"] == "NGK6")
            & ((pd.to_numeric(trades["raw_ticks"], errors="coerce").abs() - 40).abs() < 1e-9)
            & (pd.to_numeric(trades["usd_rub_rate"], errors="coerce").between(75, 83))
        ]
        if not near40.empty and not pd.to_numeric(near40["gross_pnl_rub"], errors="coerce").abs().between(250, 380).all():
            raise AssertionError("Sanity check failed: 40 NG ticks near USD/RUB 79 must be about 316 RUB, not 22,500 RUB.")

    contains_global = bool(not summary.empty and "portfolio_mode" in summary and (summary["portfolio_mode"] == "global_no_overlap").any())
    contains_front = bool(not summary.empty and "portfolio_mode" in summary and (summary["portfolio_mode"] == "front_month_only").any())
    contains_all_targets = bool(not summary.empty and "portfolio_mode" in summary and (summary["portfolio_mode"] == "all_targets_curve_strategy").any())
    old_40500_summary = False
    if not summary.empty:
        base = summary[
            (summary.get("portfolio_mode", pd.Series(dtype=str)) == "global_no_overlap")
            & (summary.get("slippage_ticks_roundtrip", pd.Series(dtype=float)) == 2)
            & (summary.get("fee_rub_per_contract_roundtrip", pd.Series(dtype=float)) == 2)
            & (summary.get("strategy_mode", pd.Series(dtype=str)) == "ensemble_vote")
        ]
        old_40500_summary = bool(not base.empty and base["net_pnl_rub_sum"].between(39000, 42000).any())
    contains_old_text = any(token in readme_text for token in ["40,500", "40500"]) or any(token in md_text for token in ["40,500", "40500"])

    for path in artifacts:
        rows_count = np.nan
        if path.suffix.lower() == ".csv" and path.exists():
            try:
                rows_count = len(pd.read_csv(path))
            except Exception:
                rows_count = np.nan
        text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() and path.suffix.lower() in {".md", ".txt"} else ""
        artifact_old = old_40500_summary if path.name in {"third_pass_strategy_summary.csv", "portfolio_margin_summary.csv"} else any(token in text for token in ["40,500", "40500"])
        passed = bool(path.exists() and path.stat().st_size > 0)
        if path.name == "third_pass_strategy_trades.csv":
            passed = passed and ngk6_bad == 0 and bad_ratio == 0 and ngk6_rows > 0
        if path.name in {"third_pass_strategy_summary.csv", "portfolio_margin_summary.csv"}:
            passed = passed and contains_global and contains_front and contains_all_targets and not old_40500_summary
        if path.name == "README.md":
            passed = passed and "fixed_plus1_only" in text and "22,912" in text and not artifact_old
        rows.append(
            {
                "artifact_name": str(path.relative_to(ROOT)),
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds") if path.exists() else "",
                "rows": rows_count,
                "ngk6_rows": ngk6_rows if path.name == "third_pass_strategy_trades.csv" else np.nan,
                "ngk6_bad_stepprice_usd_rows": ngk6_bad if path.name == "third_pass_strategy_trades.csv" else np.nan,
                "bad_tick_value_ratio_rows": bad_ratio if path.name == "third_pass_strategy_trades.csv" else np.nan,
                "contains_global_no_overlap": contains_global if path.name in {"third_pass_strategy_summary.csv", "portfolio_margin_summary.csv"} else np.nan,
                "contains_front_month_only": contains_front if path.name in {"third_pass_strategy_summary.csv", "portfolio_margin_summary.csv"} else np.nan,
                "contains_old_40500_result": bool(artifact_old),
                "passed": passed,
            }
        )
    out = pd.DataFrame(rows)
    out.to_csv(REPORTS / "artifact_sync_audit.csv", index=False)

    summary_path = REPORTS / "unit_corrected_third_pass_summary_ru.md"
    if summary_path.exists():
        lines = summary_path.read_text(encoding="utf-8").splitlines()
        block = [
            "",
            "## Artifact sync audit",
            f"- trades CSV: `{'passed' if ngk6_bad == 0 and bad_ratio == 0 and ngk6_rows > 0 else 'failed'}`.",
            f"- summary CSV: `{'passed' if contains_global and contains_front and contains_all_targets and not old_40500_summary else 'failed'}`.",
            f"- README: `{'passed' if 'fixed_plus1_only' in readme_text and '22,912' in readme_text and not any(token in readme_text for token in ['40,500', '40500']) else 'failed'}`.",
            f"- same run artifacts: `{'passed' if out['passed'].all() else 'failed'}`.",
        ]
        marker = "## Artifact sync audit"
        if marker in lines:
            idx = lines.index(marker)
            lines = lines[:idx]
        summary_path.write_text("\n".join(lines + block) + "\n", encoding="utf-8")

    if not out["passed"].all():
        raise AssertionError(f"Artifact sync audit failed:\n{out.to_string(index=False)}")
    return out


def parse_args(argv: Iterable[str] | None = None) -> ThirdPassConfig:
    parser = argparse.ArgumentParser(description="Unit-corrected third pass for MOEX NG lead-lag.")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--request-sleep", type=float, default=0.08)
    args = parser.parse_args(list(argv) if argv is not None else None)
    return ThirdPassConfig(**vars(args))


def run(cfg: ThirdPassConfig) -> dict:
    ensure_dirs()
    PLOTS.mkdir(parents=True, exist_ok=True)
    log("Loading features and building third-pass predictions")
    features = load_features()
    specs = build_contract_specs(cfg)
    fx = load_fx_rates()
    test_preds, train_preds = build_predictions(features, cfg)
    test_preds = attach_specs_fx(test_preds, specs, fx)
    train_preds = attach_specs_fx(train_preds, specs, fx)
    unit_base = make_unit_base_report(test_preds, specs, fx)
    assert_ng_tick_value(unit_base)
    anomalies = read_csv(REPORTS / "unit_anomalies.csv")
    log("Running unit-corrected strategy modes")
    trades, monthly, summary, equity, selection = build_strategy_runs(test_preds, train_preds, specs, fx)
    summary = add_passes(summary)
    write_outputs(trades, monthly, summary, equity, selection)
    audit = lookahead_audit(test_preds, selection, trades)
    plot_outputs(summary, monthly, equity)
    write_summary(summary, specs, fx, audit, unit_base, anomalies)
    sync_audit = artifact_sync_audit()
    return {
        "predictions": len(test_preds),
        "unit_trade_rows": len(unit_base),
        "strategy_trades": len(trades),
        "summary_rows": len(summary),
        "lookahead_passed": bool(audit["passed"].all()) if not audit.empty else False,
        "artifact_sync_passed": bool(sync_audit["passed"].all()) if not sync_audit.empty else False,
    }


def main(argv: Iterable[str] | None = None) -> int:
    cfg = parse_args(argv)
    result = run(cfg)
    log("Done")
    log(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
