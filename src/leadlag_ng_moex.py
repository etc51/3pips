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


ROOT = Path(__file__).resolve().parents[1]
DATA_RAW = ROOT / "data" / "raw" / "leadlag_ng_10m"
DATA_PROCESSED = ROOT / "data" / "processed" / "leadlag_ng_10m"
REPORTS = ROOT / "reports"
PLOTS = ROOT / "plots"

MOEX_CANDLES_URL = "https://iss.moex.com/iss/engines/futures/markets/forts/securities/{secid}/candles.json"

MONTH_CODES = {
    1: "F",
    2: "G",
    3: "H",
    4: "J",
    5: "K",
    6: "M",
    7: "N",
    8: "Q",
    9: "U",
    10: "V",
    11: "X",
    12: "Z",
}
CODE_TO_MONTH = {v: k for k, v in MONTH_CODES.items()}

UNIVERSE = [
    "NGK4",
    "NGM4",
    "NGN4",
    "NGQ4",
    "NGU4",
    "NGV4",
    "NGX4",
    "NGZ4",
    "NGF5",
    "NGG5",
    "NGH5",
    "NGJ5",
    "NGK5",
    "NGM5",
    "NGN5",
    "NGQ5",
    "NGU5",
    "NGV5",
    "NGX5",
    "NGZ5",
    "NGF6",
    "NGG6",
    "NGH6",
    "NGJ6",
    "NGK6",
    "NGM6",
    "NGN6",
    "NGQ6",
]


@dataclass(frozen=True)
class Config:
    date_from: str
    date_till: str
    interval: int
    force: bool
    request_sleep: float
    max_start_pages: int
    exclude_last_days: int
    min_train_obs: int
    min_test_obs: int
    walk_train_months: int
    walk_test_months: int
    slippage_bps: float
    signal_threshold: float
    horizons: list[int]


FEATURE_SETS = {
    "plus1_only": ["ret_plus1_lag0"],
    "plus2_only": ["ret_plus2_lag0"],
    "plus3_only": ["ret_plus3_lag0"],
    "outrights_all": ["ret_plus1_lag0", "ret_plus2_lag0", "ret_plus3_lag0"],
    "spreads_only": ["d_spread_plus1", "d_spread_plus2", "d_spread_plus3"],
    "outrights_plus_spreads": [
        "ret_plus1_lag0",
        "ret_plus2_lag0",
        "ret_plus3_lag0",
        "d_spread_plus1",
        "d_spread_plus2",
        "d_spread_plus3",
    ],
}

COST_GRID_BPS = [0, 1, 2, 3, 4, 5, 6, 8, 10]
THRESHOLD_COSTS_BPS = [0, 2, 4, 6]
THRESHOLD_PERCENTILES = [50, 60, 70, 80, 90, 95]
THRESHOLD_FIXED = [0.0001, 0.0002, 0.0003, 0.0005, 0.00075, 0.0010]


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def ensure_dirs() -> None:
    for path in [DATA_RAW, DATA_PROCESSED, REPORTS, PLOTS]:
        path.mkdir(parents=True, exist_ok=True)


def request_json(url: str, params: dict, retries: int = 5, sleep: float = 0.5) -> dict:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=60)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(sleep * (attempt + 1))
    raise RuntimeError(f"MOEX request failed: {url} {params}") from last_error


def iss_table(payload: dict, table: str) -> pd.DataFrame:
    block = payload.get(table, {})
    return pd.DataFrame(block.get("data", []), columns=block.get("columns", []))


def parse_secid(secid: str) -> tuple[int, int]:
    code = secid[2]
    year_digit = int(secid[3])
    month = CODE_TO_MONTH[code]
    year = 2020 + year_digit
    return year, month


def secid_for_month(month_start: pd.Timestamp) -> str:
    year = int(month_start.year)
    month = int(month_start.month)
    return f"NG{MONTH_CODES[month]}{year % 10}"


def add_months(month_start: pd.Timestamp, months: int) -> pd.Timestamp:
    return pd.Timestamp(month_start) + pd.DateOffset(months=months)


def target_months(date_from: str, date_till: str) -> list[pd.Timestamp]:
    start = pd.Timestamp(date_from).to_period("M").to_timestamp()
    end = pd.Timestamp(date_till).to_period("M").to_timestamp()
    return list(pd.date_range(start, end, freq="MS"))


def write_dual(df: pd.DataFrame, stem: Path) -> None:
    stem.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(stem.with_suffix(".csv"), index=False)
    try:
        df.to_parquet(stem.with_suffix(".parquet"), index=False)
    except Exception as exc:  # noqa: BLE001
        log(f"Parquet skipped for {stem.name}: {type(exc).__name__}: {exc}")


def read_raw_cache(path_stem: Path) -> pd.DataFrame:
    parquet = path_stem.with_suffix(".parquet")
    csv = path_stem.with_suffix(".csv")
    if parquet.exists():
        return pd.read_parquet(parquet)
    if csv.exists():
        return pd.read_csv(csv)
    return pd.DataFrame()


def download_candles_for_secid(secid: str, cfg: Config) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    url = MOEX_CANDLES_URL.format(secid=secid)
    start = 0
    for _ in range(cfg.max_start_pages):
        payload = request_json(
            url,
            {
                "interval": cfg.interval,
                "from": cfg.date_from,
                "till": cfg.date_till,
                "start": start,
                "iss.meta": "off",
            },
            sleep=cfg.request_sleep,
        )
        chunk = iss_table(payload, "candles")
        if chunk.empty:
            break
        chunk["secid"] = secid
        rows.append(chunk)
        start += len(chunk)
        time.sleep(cfg.request_sleep)
    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows, ignore_index=True)
    out = out.drop_duplicates(["secid", "begin"]).sort_values(["secid", "begin"])
    return out


def download_all_candles(cfg: Config) -> pd.DataFrame:
    cache = DATA_RAW / "moex_ng_10m_candles"
    if not cfg.force:
        cached = read_raw_cache(cache)
        if not cached.empty:
            cached["begin"] = pd.to_datetime(cached["begin"])
            cached["end"] = pd.to_datetime(cached["end"])
            return cached

    frames = []
    for i, secid in enumerate(UNIVERSE, 1):
        df = download_candles_for_secid(secid, cfg)
        log(f"{secid}: {len(df):,} rows ({i}/{len(UNIVERSE)})")
        if not df.empty:
            frames.append(df)
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not out.empty:
        out["begin"] = pd.to_datetime(out["begin"])
        out["end"] = pd.to_datetime(out["end"])
        for col in ["open", "high", "low", "close", "value", "volume"]:
            if col in out:
                out[col] = pd.to_numeric(out[col], errors="coerce")
    write_dual(out, cache)
    return out


def contract_metadata(raw: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    rows = []
    for secid in UNIVERSE:
        year, month = parse_secid(secid)
        g = raw[raw["secid"] == secid] if not raw.empty else pd.DataFrame()
        rows.append(
            {
                "secid": secid,
                "contract_year": year,
                "contract_month": month,
                "contract_ym": year * 100 + month,
                "target_month": f"{year:04d}-{month:02d}",
                "first_begin": g["begin"].min() if not g.empty else pd.NaT,
                "last_begin": g["begin"].max() if not g.empty else pd.NaT,
                "rows": len(g),
                "nonzero_volume_rows": int((pd.to_numeric(g.get("volume", pd.Series(dtype=float)), errors="coerce") > 0).sum()),
                "total_volume": float(pd.to_numeric(g.get("volume", pd.Series(dtype=float)), errors="coerce").sum()),
            }
        )
    meta = pd.DataFrame(rows)
    meta["last_trade_date"] = pd.to_datetime(meta["last_begin"]).dt.normalize()
    meta["liquidity_note"] = np.where(meta["nonzero_volume_rows"] == 0, "no nonzero 10m volume", "")
    write_dual(meta, DATA_PROCESSED / "contract_liquidity")
    return meta


def make_mapping(cfg: Config) -> pd.DataFrame:
    rows = []
    universe = set(UNIVERSE)
    for month in target_months(cfg.date_from, cfg.date_till):
        target = secid_for_month(month)
        row = {"target_month": month.strftime("%Y-%m"), "target": target}
        for n in [1, 2, 3]:
            row[f"plus{n}"] = secid_for_month(add_months(month, n))
        row["available"] = all(row[k] in universe for k in ["target", "plus1", "plus2", "plus3"])
        rows.append(row)
    mapping = pd.DataFrame(rows)
    write_dual(mapping, DATA_PROCESSED / "rolling_mapping")
    return mapping


def prepare_candles(raw: pd.DataFrame) -> pd.DataFrame:
    cols = ["secid", "begin", "end", "open", "high", "low", "close", "value", "volume"]
    out = raw[[c for c in cols if c in raw]].copy()
    out["begin"] = pd.to_datetime(out["begin"])
    out["date"] = out["begin"].dt.normalize()
    for col in ["open", "high", "low", "close", "value", "volume"]:
        if col in out:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.sort_values(["secid", "begin"])


def wide_for_mapping(candles: pd.DataFrame, mapping: pd.DataFrame, meta: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    records = []
    by_secid = {secid: g.set_index("begin").sort_index() for secid, g in candles.groupby("secid")}
    last_trade = meta.set_index("secid")["last_trade_date"].to_dict()
    for _, row in mapping[mapping["available"]].iterrows():
        target = row["target"]
        legs = {"front": target, "plus1": row["plus1"], "plus2": row["plus2"], "plus3": row["plus3"]}
        if any(secid not in by_secid for secid in legs.values()):
            continue
        base = by_secid[target][["open", "close", "volume", "date"]].rename(
            columns={"open": "open_front", "close": "close_front", "volume": "volume_front", "date": "date_front"}
        )
        panel = base.copy()
        for leg_name, secid in legs.items():
            if leg_name == "front":
                continue
            leg = by_secid[secid][["open", "close", "volume"]].rename(
                columns={
                    "open": f"open_{leg_name}",
                    "close": f"close_{leg_name}",
                    "volume": f"volume_{leg_name}",
                }
            )
            panel = panel.join(leg, how="inner")
        panel = panel.reset_index().rename(columns={"index": "begin"})
        panel["target_month"] = row["target_month"]
        for name, secid in legs.items():
            panel[f"secid_{name}"] = secid
        expiry = last_trade.get(target)
        if pd.notna(expiry):
            days_to_last = (pd.Timestamp(expiry) - panel["begin"].dt.normalize()).dt.days
            panel["days_to_last_trade"] = days_to_last
            panel = panel[days_to_last > cfg.exclude_last_days]
        records.append(panel)
    out = pd.concat(records, ignore_index=True) if records else pd.DataFrame()
    if not out.empty:
        out = out.drop_duplicates(["target_month", "begin"]).sort_values(["target_month", "begin"])
    write_dual(out, DATA_PROCESSED / "aligned_panel")
    return out


def add_returns(panel: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    frames = []
    for _, g in panel.groupby("target_month"):
        g = g.sort_values("begin").copy()
        g["next_begin"] = g["begin"].shift(-1)
        max_horizon_bars = max(max(cfg.horizons) // cfg.interval, 3)
        for h in range(1, max_horizon_bars + 1):
            g[f"future_begin_{h}"] = g["begin"].shift(-h)
            continuous = (g[f"future_begin_{h}"] - g["begin"]) == pd.Timedelta(minutes=cfg.interval * h)
            g[f"ret_front_{h * cfg.interval}m"] = np.where(
                continuous,
                np.log(g["close_front"].shift(-h) / g["close_front"]),
                np.nan,
            )
        for n in [1, 2, 3]:
            g[f"ret_plus{n}_lag0"] = np.log(g[f"close_plus{n}"] / g[f"open_plus{n}"])
            g[f"spread_plus{n}"] = np.log(g[f"close_plus{n}"] / g["close_front"])
            g[f"d_spread_plus{n}"] = g[f"spread_plus{n}"].diff()
        frames.append(g)
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if out.empty:
        return out
    volume_mask = out["volume_front"] > 0
    for n in [1, 2, 3]:
        volume_mask &= out[f"volume_plus{n}"] > 0
    same_session = (out["next_begin"] - out["begin"]) == pd.Timedelta(minutes=cfg.interval)
    out = out[volume_mask & same_session].copy()
    write_dual(out, DATA_PROCESSED / "features")
    return out


def lag_corr_one(x: pd.Series, y: pd.Series, lag: int) -> tuple[float, int]:
    if lag > 0:
        xs = x.shift(lag)
        ys = y
    elif lag < 0:
        xs = x
        ys = y.shift(-lag)
    else:
        xs = x
        ys = y
    tmp = pd.concat([xs, ys], axis=1).dropna()
    if len(tmp) < 10:
        return np.nan, len(tmp)
    return float(tmp.iloc[:, 0].corr(tmp.iloc[:, 1])), len(tmp)


def lag_correlations(features: pd.DataFrame, horizons: list[int] | None = None) -> pd.DataFrame:
    rows = []
    horizons = horizons or [10, 20, 30]
    for target_month, g in features.groupby("target_month"):
        for horizon in horizons:
            y = g[f"ret_front_{horizon}m"]
            for predictor in ["ret_plus1_lag0", "ret_plus2_lag0", "ret_plus3_lag0", "d_spread_plus1", "d_spread_plus2", "d_spread_plus3"]:
                if predictor not in g:
                    continue
                x = g[predictor]
                for lag in range(-6, 7):
                    corr, n = lag_corr_one(x, y, lag)
                    rows.append(
                        {
                            "target_month": target_month,
                            "horizon_min": horizon,
                            "predictor": predictor,
                            "lag_candles": lag,
                            "corr": corr,
                            "n_obs": n,
                        }
                    )
    out = pd.DataFrame(rows)
    out.to_csv(REPORTS / "leadlag_correlations.csv", index=False)
    return out


def plot_heatmap(corrs: pd.DataFrame) -> None:
    if corrs.empty:
        return
    subset = corrs[corrs["horizon_min"] == 20].copy()
    subset["row"] = subset["target_month"] + " " + subset["predictor"]
    pivot = subset.pivot_table(index="row", columns="lag_candles", values="corr", aggfunc="mean")
    if pivot.empty:
        return
    fig_h = max(7, min(24, 0.28 * len(pivot)))
    fig, ax = plt.subplots(figsize=(12, fig_h))
    im = ax.imshow(pivot.values, aspect="auto", cmap="RdBu_r", vmin=-0.25, vmax=0.25)
    ax.set_xticks(range(len(pivot.columns)), labels=[str(c) for c in pivot.columns])
    ax.set_yticks(range(len(pivot.index)), labels=pivot.index)
    ax.set_title("MOEX NG 10m lead-lag correlations, target 20m forward return")
    ax.set_xlabel("Lag in 10m candles; positive means predictor is older than target return")
    fig.colorbar(im, ax=ax, label="Pearson corr")
    fig.tight_layout()
    fig.savefig(PLOTS / "lag_correlation_heatmap.png", dpi=160)
    plt.close(fig)


def hac_regressions(features: pd.DataFrame, horizons: list[int] | None = None) -> pd.DataFrame:
    rows = []
    horizons = horizons or [10, 20, 30]
    predictors = ["ret_plus1_lag0", "ret_plus2_lag0", "ret_plus3_lag0", "d_spread_plus1", "d_spread_plus2", "d_spread_plus3"]
    for target_month, g in features.groupby("target_month"):
        for horizon in horizons:
            y_col = f"ret_front_{horizon}m"
            cols = [y_col, *predictors]
            tmp = g[cols].replace([np.inf, -np.inf], np.nan).dropna()
            if len(tmp) < 30:
                rows.append({"target_month": target_month, "horizon_min": horizon, "n_obs": len(tmp), "status": "too_few_obs"})
                continue
            y = tmp[y_col]
            x = sm.add_constant(tmp[predictors])
            model = sm.OLS(y, x).fit(cov_type="HAC", cov_kwds={"maxlags": max(1, math.ceil(horizon / 10))})
            for name in x.columns:
                rows.append(
                    {
                        "target_month": target_month,
                        "horizon_min": horizon,
                        "term": name,
                        "coef": model.params.get(name, np.nan),
                        "std_err_hac": model.bse.get(name, np.nan),
                        "t_value_hac": model.tvalues.get(name, np.nan),
                        "p_value_hac": model.pvalues.get(name, np.nan),
                        "r2": model.rsquared,
                        "n_obs": int(model.nobs),
                        "status": "ok",
                    }
                )
    out = pd.DataFrame(rows)
    out.to_csv(REPORTS / "regression_summary.csv", index=False)
    return out


def directional_accuracy(pred: pd.Series, actual: pd.Series) -> float:
    tmp = pd.concat([pred, actual], axis=1).dropna()
    if tmp.empty:
        return np.nan
    return float((np.sign(tmp.iloc[:, 0]) == np.sign(tmp.iloc[:, 1])).mean())


def walk_forward(features: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    rows = []
    predictors = ["ret_plus1_lag0", "ret_plus2_lag0", "ret_plus3_lag0", "d_spread_plus1"]
    work = features.copy()
    work["month_ts"] = pd.to_datetime(work["target_month"] + "-01")
    months = sorted(work["month_ts"].dropna().unique())
    for horizon in cfg.horizons:
        y_col = f"ret_front_{horizon}m"
        start_idx = 0
        while start_idx + cfg.walk_train_months < len(months):
            train_months = months[start_idx : start_idx + cfg.walk_train_months]
            test_months = months[start_idx + cfg.walk_train_months : start_idx + cfg.walk_train_months + cfg.walk_test_months]
            if len(test_months) == 0:
                break
            train = work[work["month_ts"].isin(train_months)][[y_col, *predictors]].dropna()
            test = work[work["month_ts"].isin(test_months)][[y_col, *predictors]].dropna()
            if len(train) < cfg.min_train_obs or len(test) < cfg.min_test_obs:
                start_idx += cfg.walk_test_months
                continue
            model = sm.OLS(train[y_col], sm.add_constant(train[predictors])).fit()
            pred = pd.Series(model.predict(sm.add_constant(test[predictors])), index=test.index)
            actual = test[y_col]
            strategy = np.sign(pred) * actual
            rows.append(
                {
                    "horizon_min": horizon,
                    "train_start": pd.Timestamp(train_months[0]).strftime("%Y-%m"),
                    "train_end": pd.Timestamp(train_months[-1]).strftime("%Y-%m"),
                    "test_start": pd.Timestamp(test_months[0]).strftime("%Y-%m"),
                    "test_end": pd.Timestamp(test_months[-1]).strftime("%Y-%m"),
                    "train_obs": len(train),
                    "test_obs": len(test),
                    "oos_r2": 1 - float(((actual - pred) ** 2).sum() / ((actual - actual.mean()) ** 2).sum()),
                    "directional_accuracy": directional_accuracy(pred, actual),
                    "mean_signed_return": float(strategy.mean()),
                    "t_stat_signed_return": float(strategy.mean() / (strategy.std(ddof=1) / math.sqrt(len(strategy)))) if strategy.std(ddof=1) > 0 else np.nan,
                }
            )
            start_idx += cfg.walk_test_months
    out = pd.DataFrame(rows)
    out.to_csv(REPORTS / "walkforward_results.csv", index=False)
    return out


def trade_simulation(features: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    rows = []
    predictors = ["ret_plus1_lag0", "ret_plus2_lag0", "ret_plus3_lag0", "d_spread_plus1"]
    work = features.copy()
    work["month_ts"] = pd.to_datetime(work["target_month"] + "-01")
    months = sorted(work["month_ts"].dropna().unique())
    start_idx = 0
    while start_idx + cfg.walk_train_months < len(months):
        train_months = months[start_idx : start_idx + cfg.walk_train_months]
        test_months = months[start_idx + cfg.walk_train_months : start_idx + cfg.walk_train_months + cfg.walk_test_months]
        if len(test_months) == 0:
            break
        train = work[work["month_ts"].isin(train_months)].copy()
        test = work[work["month_ts"].isin(test_months)].copy()
        reg_train = train[["ret_front_20m", *predictors]].replace([np.inf, -np.inf], np.nan).dropna()
        if len(reg_train) < cfg.min_train_obs:
            start_idx += cfg.walk_test_months
            continue
        model = sm.OLS(reg_train["ret_front_20m"], sm.add_constant(reg_train[predictors])).fit()
        for target_month, g in test.groupby("target_month"):
            g = g.sort_values("begin").copy()
            x = g[predictors].replace([np.inf, -np.inf], np.nan)
            valid_x = x.dropna()
            if valid_x.empty:
                continue
            pred = pd.Series(model.predict(sm.add_constant(valid_x)), index=valid_x.index)
            g["prediction"] = pred
            g["entry_open"] = g["open_front"].shift(-1)
            g["exit_open"] = g["open_front"].shift(-3)
            g["entry_begin"] = g["begin"].shift(-1)
            g["exit_begin"] = g["begin"].shift(-3)
            continuous = (g["entry_begin"] - g["begin"] == pd.Timedelta(minutes=10)) & (
                g["exit_begin"] - g["entry_begin"] == pd.Timedelta(minutes=20)
            )
            signal = np.where(g["prediction"] > cfg.signal_threshold, 1, np.where(g["prediction"] < -cfg.signal_threshold, -1, 0))
            gross = signal * np.log(g["exit_open"] / g["entry_open"])
            cost = np.where(signal != 0, cfg.slippage_bps / 10_000.0, 0.0)
            pnl = np.where(continuous, gross - cost, np.nan)
            trades = g.loc[(signal != 0) & continuous & np.isfinite(pnl)].copy()
            trades["signal"] = signal[(signal != 0) & continuous & np.isfinite(pnl)]
            trades["gross_log_return"] = gross[(signal != 0) & continuous & np.isfinite(pnl)]
            trades["net_log_return"] = pnl[(signal != 0) & continuous & np.isfinite(pnl)]
            for _, tr in trades.iterrows():
                rows.append(
                    {
                        "target_month": target_month,
                        "secid_front": tr["secid_front"],
                        "begin_signal": tr["begin"],
                        "entry_begin": tr["entry_begin"],
                        "exit_begin": tr["exit_begin"],
                        "prediction": tr["prediction"],
                        "signal": int(tr["signal"]),
                        "entry_open": tr["entry_open"],
                        "exit_open": tr["exit_open"],
                        "gross_log_return": tr["gross_log_return"],
                        "slippage_bps_roundtrip": cfg.slippage_bps,
                        "net_log_return": tr["net_log_return"],
                        "train_start": pd.Timestamp(train_months[0]).strftime("%Y-%m"),
                        "train_end": pd.Timestamp(train_months[-1]).strftime("%Y-%m"),
                    }
                )
        start_idx += cfg.walk_test_months
    out = pd.DataFrame(rows)
    if not out.empty:
        summary = (
            out.groupby("target_month", as_index=False)
            .agg(
                trades=("net_log_return", "size"),
                mean_net_return=("net_log_return", "mean"),
                total_net_return=("net_log_return", "sum"),
                hit_rate=("net_log_return", lambda x: float((x > 0).mean())),
            )
            .sort_values("target_month")
        )
        summary.to_csv(REPORTS / "trade_simulation_by_month.csv", index=False)
    out.to_csv(REPORTS / "trade_simulation.csv", index=False)
    return out


def summarize_findings(
    meta: pd.DataFrame,
    corrs: pd.DataFrame,
    regs: pd.DataFrame,
    walk: pd.DataFrame,
    trades: pd.DataFrame,
    cfg: Config,
) -> dict:
    reg20 = regs[(regs["horizon_min"] == 20) & (regs["status"] == "ok") & (regs["term"].isin(["ret_plus1_lag0", "ret_plus2_lag0", "ret_plus3_lag0"]))]
    sig20 = reg20[reg20["p_value_hac"] < 0.05]
    walk20 = walk[walk["horizon_min"] == 20]
    trade_months = pd.DataFrame()
    if not trades.empty:
        trade_months = trades.groupby("target_month")["net_log_return"].sum().reset_index()
    if trade_months.empty:
        concentrated = None
    elif trade_months["net_log_return"].sum() <= 0:
        concentrated = False
    else:
        concentrated = bool(trade_months["net_log_return"].max() > 0.8 * trade_months["net_log_return"].sum())

    findings = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "statistically_significant_20m_lead": bool(not sig20.empty),
        "significant_20m_terms": sig20[["target_month", "term", "coef", "p_value_hac", "n_obs"]].to_dict("records") if not sig20.empty else [],
        "oos_20m_positive": bool(not walk20.empty and walk20["mean_signed_return"].mean() > 0),
        "oos_20m_mean_signed_return": float(walk20["mean_signed_return"].mean()) if not walk20.empty else None,
        "effect_concentrated_in_one_month": concentrated,
        "passes_slippage": bool(not trades.empty and trades["net_log_return"].sum() > 0),
        "slippage_bps_roundtrip": cfg.slippage_bps,
        "illiquid_contracts": meta.loc[meta["nonzero_volume_rows"] == 0, "secid"].tolist(),
        "low_liquidity_contracts": meta.loc[(meta["nonzero_volume_rows"] > 0) & (meta["nonzero_volume_rows"] < 50), "secid"].tolist(),
    }
    (REPORTS / "leadlag_findings.json").write_text(json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8")
    return findings


def write_readme_report(findings: dict, cfg: Config) -> None:
    lines = [
        "# MOEX NG Lead-Lag 10m",
        "",
        f"Run period: `{cfg.date_from}` to `{cfg.date_till}`. Source: MOEX ISS candles endpoint",
        "`https://iss.moex.com/iss/engines/futures/markets/forts/securities/{SECID}/candles.json` with `interval=10`.",
        "",
        "## Methodology",
        "- Rolling target month is each calendar month from 2024-05 to 2026-05.",
        "- Predictors are target+1, target+2 and target+3 monthly NG contracts.",
        "- Candles are aligned only by exact `begin` timestamp; the main test does not forward-fill.",
        "- Filters require `volume > 0` for target and all predictors, remove discontinuous 10-minute jumps, and exclude the configured final trading days before the target contract's last observed candle.",
        "- No future predictor values are used. Trading simulation uses signal at `close[t]`, entry at `open[t+1]`, exit at `open[t+3]` for a 20-minute hold.",
        "",
        "## Required conclusions",
        f"- Statistically significant 20m lead: `{findings['statistically_significant_20m_lead']}`.",
        f"- Effect survives out-of-sample walk-forward: `{findings['oos_20m_positive']}`.",
        f"- Effect concentrated in one month: `{findings['effect_concentrated_in_one_month']}`.",
        f"- Effect passes spread/slippage after `{cfg.slippage_bps:g}` bps roundtrip: `{findings['passes_slippage']}`.",
        f"- Illiquid contracts: `{', '.join(findings['illiquid_contracts']) if findings['illiquid_contracts'] else 'none with zero nonzero-volume rows'}`.",
        f"- Low-liquidity contracts: `{', '.join(findings['low_liquidity_contracts']) if findings['low_liquidity_contracts'] else 'none under threshold'}`.",
        "",
        "## Artifacts",
        "- `data/raw/leadlag_ng_10m/moex_ng_10m_candles.csv` and `.parquet`",
        "- `data/processed/leadlag_ng_10m/rolling_mapping.csv`",
        "- `data/processed/leadlag_ng_10m/aligned_panel.csv`",
        "- `data/processed/leadlag_ng_10m/features.csv`",
        "- `reports/leadlag_correlations.csv`",
        "- `reports/regression_summary.csv`",
        "- `reports/walkforward_results.csv`",
        "- `reports/trade_simulation.csv`",
        "- `plots/lag_correlation_heatmap.png`",
        "",
        "## Run",
        "```powershell",
        "python -m pip install -r requirements.txt",
        "python src/leadlag_ng_moex.py --force",
        "```",
    ]
    (REPORTS / "leadlag_readme_appendix.md").write_text("\n".join(lines), encoding="utf-8")


def fitted_predictions(train: pd.DataFrame, test: pd.DataFrame, y_col: str, predictors: list[str]) -> tuple[pd.Series, sm.regression.linear_model.RegressionResultsWrapper | None]:
    cols = [y_col, *predictors]
    train_clean = train[cols].replace([np.inf, -np.inf], np.nan).dropna()
    test_clean = test[predictors].replace([np.inf, -np.inf], np.nan).dropna()
    if len(train_clean) < 30 or test_clean.empty:
        return pd.Series(dtype=float), None
    model = sm.OLS(train_clean[y_col], sm.add_constant(train_clean[predictors], has_constant="add")).fit()
    pred = pd.Series(model.predict(sm.add_constant(test_clean[predictors], has_constant="add")), index=test_clean.index)
    return pred, model


def add_execution_columns(frame: pd.DataFrame, horizon: int, cfg: Config) -> pd.DataFrame:
    hold_bars = horizon // cfg.interval
    if hold_bars < 1 or horizon % cfg.interval != 0:
        raise ValueError(f"horizon must be a positive multiple of interval={cfg.interval}: {horizon}")
    frames = []
    for _, g in frame.groupby("target_month"):
        g = g.sort_values("begin").copy()
        g["entry_time"] = g["begin"].shift(-1)
        g["exit_time"] = g["begin"].shift(-(hold_bars + 1))
        g["entry_price"] = g["open_front"].shift(-1)
        g["exit_price"] = g["open_front"].shift(-(hold_bars + 1))
        g["future_open_return"] = np.log(g["exit_price"] / g["entry_price"])
        g["execution_continuous"] = (g["entry_time"] - g["begin"] == pd.Timedelta(minutes=cfg.interval)) & (
            g["exit_time"] - g["entry_time"] == pd.Timedelta(minutes=horizon)
        )
        frames.append(g)
    return pd.concat(frames, ignore_index=False).sort_index() if frames else frame


def build_walkforward_predictions(features: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    rows = []
    work = features.copy()
    work["month_ts"] = pd.to_datetime(work["target_month"] + "-01")
    months = sorted(work["month_ts"].dropna().unique())
    for horizon in cfg.horizons:
        y_col = f"ret_front_{horizon}m"
        exec_work = add_execution_columns(work, horizon, cfg)
        for feature_set, predictors in FEATURE_SETS.items():
            missing = [c for c in predictors if c not in exec_work]
            if missing:
                continue
            for test_month in months:
                train = exec_work[exec_work["month_ts"] < test_month].copy()
                test = exec_work[exec_work["month_ts"] == test_month].copy()
                if train["target_month"].nunique() < 3 or len(train) < cfg.min_train_obs or len(test) < cfg.min_test_obs:
                    continue
                pred, _ = fitted_predictions(train, test, y_col, predictors)
                if pred.empty:
                    continue
                cols = [
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
                    "execution_continuous",
                ]
                out = test.loc[pred.index, cols].copy()
                out["prediction"] = pred
                out["abs_prediction"] = pred.abs()
                out["horizon_minutes"] = horizon
                out["feature_set"] = feature_set
                out["train_months"] = train["target_month"].nunique()
                out["train_start"] = train["target_month"].min()
                out["train_end"] = train["target_month"].max()
                rows.append(out)
    predictions = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    if not predictions.empty:
        predictions = predictions[predictions["execution_continuous"]].copy()
    write_dual(predictions, DATA_PROCESSED / "walkforward_predictions")
    return predictions


def trades_from_predictions(predictions: pd.DataFrame, cost_bps: float, threshold: float, threshold_policy: str) -> pd.DataFrame:
    if predictions.empty:
        return pd.DataFrame()
    out = predictions.copy()
    signal = np.where(out["prediction"] > threshold, 1, np.where(out["prediction"] < -threshold, -1, 0))
    out["signal_direction"] = signal
    out = out[out["signal_direction"] != 0].copy()
    if out.empty:
        return out
    out["gross_return"] = out["signal_direction"] * out["future_open_return"]
    out["cost_bps_roundtrip"] = float(cost_bps)
    out["net_return"] = out["gross_return"] - float(cost_bps) / 10_000.0
    out["threshold_policy"] = threshold_policy
    out["selected_threshold"] = threshold
    out = out.rename(
        columns={
            "secid_front": "target_contract",
            "secid_plus1": "plus1_contract",
            "secid_plus2": "plus2_contract",
            "secid_plus3": "plus3_contract",
        }
    )
    keep = [
        "target_month",
        "begin",
        "target_contract",
        "plus1_contract",
        "plus2_contract",
        "plus3_contract",
        "prediction",
        "signal_direction",
        "abs_prediction",
        "entry_time",
        "exit_time",
        "entry_price",
        "exit_price",
        "gross_return",
        "cost_bps_roundtrip",
        "net_return",
        "horizon_minutes",
        "feature_set",
        "threshold_policy",
        "selected_threshold",
        "train_start",
        "train_end",
    ]
    return out[[c for c in keep if c in out]].sort_values(["horizon_minutes", "feature_set", "target_month", "begin"])


def summarize_trades(trades: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    rows = []
    if trades.empty:
        return pd.DataFrame()
    for keys, g in trades.groupby(group_cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        monthly = g.groupby("target_month")["net_return"].mean()
        gross_std = g["gross_return"].std(ddof=1)
        net_std = g["net_return"].std(ddof=1)
        row = dict(zip(group_cols, keys))
        row.update(
            {
                "n_trades": len(g),
                "gross_mean": float(g["gross_return"].mean()),
                "net_mean": float(g["net_return"].mean()),
                "gross_median": float(g["gross_return"].median()),
                "net_median": float(g["net_return"].median()),
                "gross_hit_rate": float((g["gross_return"] > 0).mean()),
                "net_hit_rate": float((g["net_return"] > 0).mean()),
                "gross_sharpe_per_trade": float(g["gross_return"].mean() / gross_std) if gross_std and np.isfinite(gross_std) else np.nan,
                "net_sharpe_per_trade": float(g["net_return"].mean() / net_std) if net_std and np.isfinite(net_std) else np.nan,
                "positive_months_net": int((monthly > 0).sum()),
                "total_months": int(monthly.size),
                "worst_month_net_mean": float(monthly.min()) if not monthly.empty else np.nan,
                "best_month_net_mean": float(monthly.max()) if not monthly.empty else np.nan,
            }
        )
        rows.append(row)
    return pd.DataFrame(rows)


def cost_grid(predictions: pd.DataFrame) -> pd.DataFrame:
    all_trades = []
    for cost in COST_GRID_BPS:
        all_trades.append(trades_from_predictions(predictions, cost, 0.0, "no_threshold"))
    trades = pd.concat([x for x in all_trades if not x.empty], ignore_index=True) if all_trades else pd.DataFrame()
    results = summarize_trades(trades, ["horizon_minutes", "feature_set", "threshold_policy", "cost_bps_roundtrip"])
    results.to_csv(REPORTS / "cost_grid_results.csv", index=False)
    trades30 = trades[(trades["horizon_minutes"] == 30) & (trades["cost_bps_roundtrip"] == 6)].copy()
    trades30.to_csv(REPORTS / "trade_simulation_30m.csv", index=False)
    if not trades30.empty:
        by_month = (
            trades30.groupby(["target_month", "feature_set", "threshold_policy", "cost_bps_roundtrip"], as_index=False)
            .agg(
                n_trades=("net_return", "size"),
                gross_mean=("gross_return", "mean"),
                net_mean=("net_return", "mean"),
                net_sum=("net_return", "sum"),
                net_hit_rate=("net_return", lambda x: float((x > 0).mean())),
            )
            .sort_values(["feature_set", "target_month"])
        )
    else:
        by_month = pd.DataFrame()
    by_month.to_csv(REPORTS / "trade_simulation_30m_by_month.csv", index=False)
    return results


def threshold_candidates(train_pred: pd.DataFrame) -> list[tuple[float, str]]:
    values = train_pred["abs_prediction"].replace([np.inf, -np.inf], np.nan).dropna()
    candidates = [(0.0, "zero")]
    if not values.empty:
        for pct in THRESHOLD_PERCENTILES:
            candidates.append((float(np.percentile(values, pct)), f"train_abs_p{pct}"))
    candidates.extend((float(x), f"fixed_{x:g}") for x in THRESHOLD_FIXED)
    seen: set[float] = set()
    unique = []
    for threshold, label in candidates:
        key = round(threshold, 12)
        if key not in seen:
            seen.add(key)
            unique.append((threshold, label))
    return unique


def threshold_walkforward(features: pd.DataFrame, cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    result_rows = []
    trade_frames = []
    work = features.copy()
    work["month_ts"] = pd.to_datetime(work["target_month"] + "-01")
    months = sorted(work["month_ts"].dropna().unique())
    for horizon in cfg.horizons:
        y_col = f"ret_front_{horizon}m"
        exec_work = add_execution_columns(work, horizon, cfg)
        for feature_set, predictors in FEATURE_SETS.items():
            if any(c not in exec_work for c in predictors):
                continue
            for test_month in months:
                train = exec_work[exec_work["month_ts"] < test_month].copy()
                test = exec_work[exec_work["month_ts"] == test_month].copy()
                if train["target_month"].nunique() < 3 or len(train) < cfg.min_train_obs or len(test) < cfg.min_test_obs:
                    continue
                train_pred, model = fitted_predictions(train, train, y_col, predictors)
                if model is None or train_pred.empty:
                    continue
                test_x = test[predictors].replace([np.inf, -np.inf], np.nan).dropna()
                if test_x.empty:
                    continue
                test_pred = pd.Series(model.predict(sm.add_constant(test_x[predictors], has_constant="add")), index=test_x.index)
                train_base = train.loc[train_pred.index].copy()
                train_base["prediction"] = train_pred
                train_base["abs_prediction"] = train_pred.abs()
                train_base["horizon_minutes"] = horizon
                train_base["feature_set"] = feature_set
                train_base = train_base[train_base["execution_continuous"]].copy()
                test_base = test.loc[test_pred.index].copy()
                test_base["prediction"] = test_pred
                test_base["abs_prediction"] = test_pred.abs()
                test_base["horizon_minutes"] = horizon
                test_base["feature_set"] = feature_set
                test_base["train_start"] = train["target_month"].min()
                test_base["train_end"] = train["target_month"].max()
                test_base = test_base[test_base["execution_continuous"]].copy()
                for cost in THRESHOLD_COSTS_BPS:
                    best_threshold = 0.0
                    best_type = "zero_fallback"
                    best_mean = -np.inf
                    best_n = 0
                    for threshold, label in threshold_candidates(train_base):
                        tr = trades_from_predictions(train_base, cost, threshold, label)
                        train_months = tr["target_month"].nunique() if not tr.empty else 0
                        if len(tr) < 30 or train_months < 3:
                            continue
                        mean_net = float(tr["net_return"].mean())
                        if mean_net > best_mean:
                            best_threshold = float(threshold)
                            best_type = label
                            best_mean = mean_net
                            best_n = len(tr)
                    if not np.isfinite(best_mean):
                        fallback = trades_from_predictions(train_base, cost, 0.0, "zero_fallback")
                        best_mean = float(fallback["net_return"].mean()) if not fallback.empty else np.nan
                        best_n = len(fallback)
                    test_trades = trades_from_predictions(test_base, cost, best_threshold, f"train_selected:{best_type}")
                    if not test_trades.empty:
                        trade_frames.append(test_trades)
                    actual = test_base.loc[test_trades.index if not test_trades.empty else [], "future_open_return"] if not test_trades.empty else pd.Series(dtype=float)
                    result_rows.append(
                        {
                            "horizon_minutes": horizon,
                            "feature_set": feature_set,
                            "cost_bps_roundtrip": cost,
                            "test_month": pd.Timestamp(test_month).strftime("%Y-%m"),
                            "selected_threshold": best_threshold,
                            "selected_threshold_type": best_type,
                            "train_n_trades": best_n,
                            "train_net_mean": best_mean,
                            "test_n_trades": len(test_trades),
                            "test_gross_mean": float(test_trades["gross_return"].mean()) if not test_trades.empty else np.nan,
                            "test_net_mean": float(test_trades["net_return"].mean()) if not test_trades.empty else np.nan,
                            "test_net_sum": float(test_trades["net_return"].sum()) if not test_trades.empty else 0.0,
                            "test_net_hit_rate": float((test_trades["net_return"] > 0).mean()) if not test_trades.empty else np.nan,
                            "test_directional_accuracy": directional_accuracy(test_trades["prediction"], actual) if not test_trades.empty else np.nan,
                            "test_positive": bool(not test_trades.empty and test_trades["net_return"].sum() > 0),
                        }
                    )
    results = pd.DataFrame(result_rows)
    trades = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()
    by_month = (
        trades.groupby(["horizon_minutes", "feature_set", "cost_bps_roundtrip", "target_month"], as_index=False)
        .agg(
            n_trades=("net_return", "size"),
            gross_mean=("gross_return", "mean"),
            net_mean=("net_return", "mean"),
            net_sum=("net_return", "sum"),
            net_hit_rate=("net_return", lambda x: float((x > 0).mean())),
        )
        if not trades.empty
        else pd.DataFrame()
    )
    results.to_csv(REPORTS / "threshold_walkforward_results.csv", index=False)
    trades.to_csv(REPORTS / "threshold_walkforward_trades.csv", index=False)
    by_month.to_csv(REPORTS / "threshold_walkforward_by_month.csv", index=False)
    return results, trades, by_month


def feature_ablation_summary(cost_results: pd.DataFrame, threshold_results: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if not cost_results.empty:
        base = cost_results[cost_results["cost_bps_roundtrip"].isin(THRESHOLD_COSTS_BPS)].copy()
        for _, row in base.iterrows():
            rows.append({**row.to_dict(), "source": "no_threshold_cost_grid"})
    if not threshold_results.empty:
        agg = (
            threshold_results.groupby(["horizon_minutes", "feature_set", "cost_bps_roundtrip"], as_index=False)
            .agg(
                n_trades=("test_n_trades", "sum"),
                net_mean=("test_net_mean", "mean"),
                net_sum=("test_net_sum", "sum"),
                positive_months_net=("test_positive", "sum"),
                total_months=("test_month", "nunique"),
                avg_selected_threshold=("selected_threshold", "mean"),
            )
        )
        for _, row in agg.iterrows():
            rows.append({**row.to_dict(), "threshold_policy": "train_selected", "source": "threshold_walkforward"})
    out = pd.DataFrame(rows)
    out.to_csv(REPORTS / "feature_ablation_summary.csv", index=False)
    return out


def missing_month_diagnostics(candles: pd.DataFrame, mapping: pd.DataFrame, meta: pd.DataFrame, panel: pd.DataFrame, features: pd.DataFrame) -> pd.DataFrame:
    month = "2025-08"
    map_row = mapping[mapping["target_month"] == month]
    rows = []
    if map_row.empty:
        rows.append({"target_month": month, "stage": "rolling_mapping", "rows": 0, "note": "target_month not in mapping"})
        out = pd.DataFrame(rows)
        out.to_csv(REPORTS / "missing_month_diagnostics.csv", index=False)
        return out
    row = map_row.iloc[0]
    contracts = {"target": row["target"], "plus1": row["plus1"], "plus2": row["plus2"], "plus3": row["plus3"]}
    rows.append({"target_month": month, "stage": "rolling_mapping", "rows": 1, "target_contract": row["target"], "plus1_contract": row["plus1"], "plus2_contract": row["plus2"], "plus3_contract": row["plus3"], "note": f"available={row['available']}"})
    by = {secid: g.set_index("begin").sort_index() for secid, g in candles.groupby("secid")}
    for label, secid in contracts.items():
        g = candles[candles["secid"] == secid]
        rows.append({"target_month": month, "stage": f"raw_{label}", "contract": secid, "rows": len(g), "nonzero_volume_rows": int((g["volume"] > 0).sum()) if not g.empty else 0})
    if not all(secid in by for secid in contracts.values()):
        rows.append({"target_month": month, "stage": "failure", "rows": 0, "note": "one or more contracts missing raw candles"})
        out = pd.DataFrame(rows)
        out.to_csv(REPORTS / "missing_month_diagnostics.csv", index=False)
        return out
    joined = by[contracts["target"]][["open", "close", "volume"]].rename(columns={"volume": "volume_front"})
    for label in ["plus1", "plus2", "plus3"]:
        leg = by[contracts[label]][["open", "close", "volume"]].rename(columns={"volume": f"volume_{label}"})
        joined = joined.join(leg[[f"volume_{label}"]], how="inner")
    joined = joined.reset_index().rename(columns={"index": "begin"}).sort_values("begin")
    rows.append({"target_month": month, "stage": "exact_timestamp_alignment_before_filters", "rows": len(joined)})
    vol = joined[(joined["volume_front"] > 0) & (joined["volume_plus1"] > 0) & (joined["volume_plus2"] > 0) & (joined["volume_plus3"] > 0)].copy()
    rows.append({"target_month": month, "stage": "after_volume_gt_zero_all_legs", "rows": len(vol)})
    vol["next_begin"] = vol["begin"].shift(-1)
    continuous = vol[vol["next_begin"] - vol["begin"] == pd.Timedelta(minutes=10)].copy()
    rows.append({"target_month": month, "stage": "after_discontinuous_10m_filter", "rows": len(continuous)})
    last_trade = meta.set_index("secid")["last_trade_date"].to_dict().get(contracts["target"])
    if pd.notna(last_trade):
        after_expiry = continuous[(pd.Timestamp(last_trade) - continuous["begin"].dt.normalize()).dt.days > 3].copy()
    else:
        after_expiry = continuous.copy()
    rows.append({"target_month": month, "stage": "after_last_3_trade_days_exclusion", "rows": len(after_expiry), "last_trade_date": last_trade})
    rows.append({"target_month": month, "stage": "aligned_panel_existing", "rows": len(panel[panel["target_month"] == month]) if not panel.empty else 0})
    rows.append({"target_month": month, "stage": "features_existing", "rows": len(features[features["target_month"] == month]) if not features.empty else 0})
    wf = pd.read_csv(DATA_PROCESSED / "walkforward_predictions.csv") if (DATA_PROCESSED / "walkforward_predictions.csv").exists() else pd.DataFrame()
    rows.append({"target_month": month, "stage": "walkforward_predictions_existing", "rows": len(wf[wf["target_month"] == month]) if not wf.empty and "target_month" in wf else 0})
    out = pd.DataFrame(rows)
    exact_rows = int(out.loc[out["stage"] == "exact_timestamp_alignment_before_filters", "rows"].fillna(0).iloc[0])
    feature_rows = int(out.loc[out["stage"] == "features_existing", "rows"].fillna(0).iloc[0])
    if exact_rows == 0:
        note = "month absent because target and +1/+2/+3 contracts have no exact common 10-minute begin timestamps before filters"
    elif feature_rows == 0:
        note = "month absent because rows are removed by volume, continuity, or last-days filters"
    elif out["rows"].fillna(0).iloc[-1] == 0:
        note = "month absent from WF/trades because expanding-train prerequisites or execution-continuity rows are insufficient"
    if out["rows"].fillna(0).iloc[-1] == 0:
        rows.append({"target_month": month, "stage": "diagnosis", "rows": 0, "note": note})
    else:
        rows.append({"target_month": month, "stage": "diagnosis", "rows": int(out["rows"].fillna(0).iloc[-1]), "note": "month is present in second-pass WF predictions"})
    out = pd.DataFrame(rows)
    out.to_csv(REPORTS / "missing_month_diagnostics.csv", index=False)
    return out


def bh_qvalues(p_values: pd.Series) -> pd.Series:
    p = pd.to_numeric(p_values, errors="coerce")
    q = pd.Series(np.nan, index=p.index, dtype=float)
    valid = p.dropna().sort_values()
    n = len(valid)
    if n == 0:
        return q
    ranked = valid * n / np.arange(1, n + 1)
    ranked = ranked.iloc[::-1].cummin().iloc[::-1].clip(upper=1.0)
    q.loc[ranked.index] = ranked
    return q


def multiple_testing_correction(regs: pd.DataFrame) -> pd.DataFrame:
    rows = []
    work = regs[(regs["status"] == "ok") & (regs["term"].notna()) & (regs["term"] != "const")].copy()
    for horizon, g in work.groupby("horizon_min"):
        p = pd.to_numeric(g["p_value_hac"], errors="coerce").dropna()
        if p.empty:
            continue
        q = bh_qvalues(p)
        bonf = (p * len(p)).clip(upper=1.0)
        rows.append(
            {
                "horizon_minutes": horizon,
                "n_tests": len(p),
                "raw_significant_005": int((p < 0.05).sum()),
                "bh_fdr_significant_005": int((q < 0.05).sum()),
                "bonferroni_significant_005": int((bonf < 0.05).sum()),
                "min_raw_p_value": float(p.min()),
                "min_bh_q_value": float(q.min()),
                "min_bonferroni_p_value": float(bonf.min()),
            }
        )
    out = pd.DataFrame(rows)
    out.to_csv(REPORTS / "multiple_testing_correction.csv", index=False)
    return out


def placebo_tests(predictions: pd.DataFrame, n_permutations: int = 500, seed: int = 42) -> tuple[pd.DataFrame, dict[tuple[int, str], np.ndarray]]:
    rng = np.random.default_rng(seed)
    rows = []
    distributions: dict[tuple[int, str], np.ndarray] = {}
    for horizon in [20, 30]:
        for feature_set in ["outrights_all", "outrights_plus_spreads"]:
            base = predictions[(predictions["horizon_minutes"] == horizon) & (predictions["feature_set"] == feature_set)].copy()
            if base.empty:
                continue
            for cost in [0, 6]:
                real_signal = np.sign(base["prediction"])
                real = real_signal * base["future_open_return"] - np.where(real_signal != 0, cost / 10_000.0, 0.0)
                real_metric = float(real.mean())
                placebo = []
                for _ in range(n_permutations):
                    shuffled = []
                    for _, g in base.groupby("target_month"):
                        vals = g["prediction"].to_numpy(copy=True)
                        rng.shuffle(vals)
                        shuffled.append(pd.Series(vals, index=g.index))
                    shuffled_pred = pd.concat(shuffled).sort_index()
                    sig = np.sign(shuffled_pred)
                    metric = (sig * base.loc[shuffled_pred.index, "future_open_return"] - np.where(sig != 0, cost / 10_000.0, 0.0)).mean()
                    placebo.append(float(metric))
                arr = np.array(placebo, dtype=float)
                distributions[(horizon, feature_set)] = arr
                p_value = float((np.sum(arr >= real_metric) + 1) / (len(arr) + 1))
                rows.append(
                    {
                        "horizon_minutes": horizon,
                        "feature_set": feature_set,
                        "cost_bps_roundtrip": cost,
                        "real_metric": real_metric,
                        "placebo_mean": float(arr.mean()),
                        "placebo_std": float(arr.std(ddof=1)),
                        "placebo_p_value": p_value,
                        "n_permutations": n_permutations,
                    }
                )
    out = pd.DataFrame(rows)
    out.to_csv(REPORTS / "placebo_test_results.csv", index=False)
    return out, distributions


def make_second_pass_plots(cost_results: pd.DataFrame, threshold_by_month: pd.DataFrame, feature_summary: pd.DataFrame, placebo_distributions: dict[tuple[int, str], np.ndarray], placebo_results: pd.DataFrame) -> None:
    if not cost_results.empty:
        fig, ax = plt.subplots(figsize=(11, 5))
        subset = cost_results[(cost_results["feature_set"].isin(["outrights_all", "outrights_plus_spreads"])) & (cost_results["threshold_policy"] == "no_threshold")]
        for (horizon, feature_set), g in subset.groupby(["horizon_minutes", "feature_set"]):
            ax.plot(g["cost_bps_roundtrip"], g["net_mean"], marker="o", label=f"{horizon}m {feature_set}")
        ax.axhline(0, color="black", linewidth=0.8)
        ax.set_title("Cost grid net mean per trade")
        ax.set_xlabel("Roundtrip cost, bps")
        ax.set_ylabel("Net mean log return")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.25)
        fig.tight_layout()
        fig.savefig(PLOTS / "cost_grid_net_mean.png", dpi=160)
        plt.close(fig)
    if not threshold_by_month.empty:
        for horizon in [20, 30]:
            fig, ax = plt.subplots(figsize=(12, 5))
            subset = threshold_by_month[(threshold_by_month["horizon_minutes"] == horizon) & (threshold_by_month["cost_bps_roundtrip"] == 6)]
            for feature_set, g in subset.groupby("feature_set"):
                monthly = g.groupby("target_month")["net_sum"].sum()
                ax.plot(monthly.index, monthly.values, marker="o", label=feature_set)
            ax.axhline(0, color="black", linewidth=0.8)
            ax.set_title(f"Threshold walk-forward net by month, {horizon}m, 6 bps")
            ax.set_xlabel("Target month")
            ax.set_ylabel("Net log return sum")
            ax.tick_params(axis="x", rotation=60)
            ax.legend(fontsize=8)
            ax.grid(True, alpha=0.25)
            fig.tight_layout()
            fig.savefig(PLOTS / f"threshold_walkforward_net_by_month_{horizon}m.png", dpi=160)
            plt.close(fig)
    if not feature_summary.empty:
        subset = feature_summary[(feature_summary["source"] == "threshold_walkforward") & (feature_summary["cost_bps_roundtrip"] == 6)]
        if not subset.empty:
            fig, ax = plt.subplots(figsize=(12, 5))
            labels = subset["feature_set"] + " " + subset["horizon_minutes"].astype(str) + "m"
            ax.bar(labels, subset["net_mean"])
            ax.axhline(0, color="black", linewidth=0.8)
            ax.set_title("Feature ablation threshold WF net mean, 6 bps")
            ax.set_ylabel("Net mean")
            ax.tick_params(axis="x", rotation=75)
            fig.tight_layout()
            fig.savefig(PLOTS / "feature_ablation_net_mean.png", dpi=160)
            plt.close(fig)
    for horizon in [20, 30]:
        fig, ax = plt.subplots(figsize=(9, 5))
        plotted = False
        for feature_set in ["outrights_all", "outrights_plus_spreads"]:
            arr = placebo_distributions.get((horizon, feature_set))
            if arr is None:
                continue
            ax.hist(arr, bins=35, alpha=0.45, label=f"{feature_set} placebo")
            real = placebo_results[(placebo_results["horizon_minutes"] == horizon) & (placebo_results["feature_set"] == feature_set) & (placebo_results["cost_bps_roundtrip"] == 6)]
            if not real.empty:
                ax.axvline(float(real.iloc[0]["real_metric"]), linestyle="--", linewidth=1.5, label=f"{feature_set} real 6bps")
            plotted = True
        if plotted:
            ax.set_title(f"Placebo distribution, {horizon}m")
            ax.set_xlabel("Mean signed/net return")
            ax.set_ylabel("Permutations")
            ax.legend(fontsize=8)
            fig.tight_layout()
            fig.savefig(PLOTS / f"placebo_distribution_{horizon}m.png", dpi=160)
        plt.close(fig)


def write_second_pass_summary(
    cost_results: pd.DataFrame,
    threshold_results: pd.DataFrame,
    feature_summary: pd.DataFrame,
    diagnostics: pd.DataFrame,
    mtc: pd.DataFrame,
    placebo: pd.DataFrame,
) -> None:
    def best_line(df: pd.DataFrame, horizon: int, cost: int) -> str:
        sub = df[(df["horizon_minutes"] == horizon) & (df["cost_bps_roundtrip"] == cost)].copy()
        if sub.empty:
            return "нет строк"
        row = sub.sort_values("net_mean", ascending=False).iloc[0]
        return f"{row['feature_set']}: net_mean={row['net_mean']:.6g}, trades={int(row.get('n_trades', 0))}, positive_months={int(row.get('positive_months_net', 0))}/{int(row.get('total_months', 0))}"

    cost30_6 = cost_results[(cost_results["horizon_minutes"] == 30) & (cost_results["cost_bps_roundtrip"] == 6)]
    passes_30_6 = bool(not cost30_6.empty and cost30_6["net_mean"].max() > 0)
    thr30_6 = threshold_results[(threshold_results["horizon_minutes"] == 30) & (threshold_results["cost_bps_roundtrip"] == 6)]
    thr30_sum = float(thr30_6["test_net_sum"].sum()) if not thr30_6.empty else np.nan
    diag_note = diagnostics[diagnostics["stage"] == "diagnosis"]["note"].iloc[-1] if not diagnostics.empty and (diagnostics["stage"] == "diagnosis").any() else "нет диагностики"
    lines = [
        "# Второй проход MOEX NG lead-lag",
        "",
        "Проверка расширяет первый проход без подбора на будущем: модель обучается только на месяцах строго раньше test-month, сигнал формируется на `close[t]`, вход идет на `open[t+1]`, выход для 10/20/30m идет на `open[t+2]`/`open[t+3]`/`open[t+4]`.",
        "",
        "## Ответы",
        f"- Edge на 30m: лучший no-threshold вариант при 0 bps: {best_line(cost_results, 30, 0)}.",
        f"- 30m после 6 bps: `{'проходит по net_mean' if passes_30_6 else 'не проходит устойчиво'}`; лучший вариант: {best_line(cost_results, 30, 6)}.",
        f"- Threshold walk-forward: суммарный 30m net_sum при 6 bps по всем feature sets/test-month = `{thr30_sum:.6g}`; порог выбирался только на train.",
        f"- Лучшие feature sets по threshold WF 6 bps см. `reports/feature_ablation_summary.csv`; no-threshold 20m: {best_line(cost_results, 20, 6)}; no-threshold 30m: {best_line(cost_results, 30, 6)}.",
        "- Out-of-sample сохранен механически: все строки второго прохода используют expanding train, где `train_month < test_month`.",
        "- Концентрация по одному месяцу проверяется в monthly агрегатах `threshold_walkforward_by_month.csv` и `trade_simulation_30m_by_month.csv`; один месяц не используется для выбора глобального порога.",
        f"- 2025-08: {diag_note}. Детали в `reports/missing_month_diagnostics.csv`.",
        "",
        "## Multiple testing",
    ]
    if mtc.empty:
        lines.append("Нет строк для correction.")
    else:
        for _, row in mtc.iterrows():
            lines.append(
                f"- {int(row['horizon_minutes'])}m: raw={int(row['raw_significant_005'])}, "
                f"BH-FDR={int(row['bh_fdr_significant_005'])}, Bonferroni={int(row['bonferroni_significant_005'])}, "
                f"tests={int(row['n_tests'])}."
            )
    lines.extend(["", "## Placebo"])
    if placebo.empty:
        lines.append("Placebo rows отсутствуют.")
    else:
        for _, row in placebo.iterrows():
            lines.append(
                f"- {int(row['horizon_minutes'])}m {row['feature_set']} cost={row['cost_bps_roundtrip']}: "
                f"real={row['real_metric']:.6g}, placebo_mean={row['placebo_mean']:.6g}, p={row['placebo_p_value']:.4f}."
            )
    lines.extend(
        [
            "",
            "## Можно ли торговать сейчас?",
            "Нет как готовую стратегию. Даже если часть статистики и OOS-сигналов положительна, текущая 10m candle-модель использует proxy-издержки и не проверяет фактический bid/ask, очередь, проскальзывание, стакан и исполнение календарных связок.",
            "",
            "## Следующий шаг",
            "Проверить 1m candles, trades, order book, bid/ask и реальное исполнение входа `open[t+1]`/выхода по горизонту. Также стоит отдельно проверить устойчивость в днях высокой ликвидности и около rollover без ручной оптимизации.",
        ]
    )
    (REPORTS / "leadlag_second_pass_summary.md").write_text("\n".join(lines), encoding="utf-8")


def run_second_pass(candles: pd.DataFrame, mapping: pd.DataFrame, meta: pd.DataFrame, panel: pd.DataFrame, features: pd.DataFrame, regs: pd.DataFrame, cfg: Config) -> dict:
    log("Running second-pass lead-lag checks")
    predictions = build_walkforward_predictions(features, cfg)
    cost_results = cost_grid(predictions)
    threshold_results, threshold_trades, threshold_by_month = threshold_walkforward(features, cfg)
    feature_summary = feature_ablation_summary(cost_results, threshold_results)
    diagnostics = missing_month_diagnostics(candles, mapping, meta, panel, features)
    mtc = multiple_testing_correction(regs)
    placebo, placebo_dist = placebo_tests(predictions)
    make_second_pass_plots(cost_results, threshold_by_month, feature_summary, placebo_dist, placebo)
    write_second_pass_summary(cost_results, threshold_results, feature_summary, diagnostics, mtc, placebo)
    return {
        "predictions": len(predictions),
        "threshold_trades": len(threshold_trades),
        "cost_grid_rows": len(cost_results),
        "placebo_rows": len(placebo),
    }


def parse_args(argv: Iterable[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(description="MOEX NG futures 10m lead-lag hypothesis test.")
    parser.add_argument("--from", dest="date_from", default="2024-05-23")
    parser.add_argument("--till", dest="date_till", default="2026-05-23")
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--request-sleep", type=float, default=0.08)
    parser.add_argument("--max-start-pages", type=int, default=2000)
    parser.add_argument("--exclude-last-days", type=int, default=3)
    parser.add_argument("--min-train-obs", type=int, default=200)
    parser.add_argument("--min-test-obs", type=int, default=50)
    parser.add_argument("--walk-train-months", type=int, default=6)
    parser.add_argument("--walk-test-months", type=int, default=1)
    parser.add_argument("--slippage-bps", type=float, default=6.0)
    parser.add_argument("--signal-threshold", type=float, default=0.0)
    parser.add_argument("--horizons", type=int, nargs="+", default=[10, 20, 30])
    args = parser.parse_args(list(argv) if argv is not None else None)
    args.horizons = sorted(set(args.horizons))
    return Config(**vars(args))


def run(cfg: Config) -> dict:
    ensure_dirs()
    raw = download_all_candles(cfg)
    if raw.empty:
        raise RuntimeError("No MOEX candles downloaded.")
    candles = prepare_candles(raw)
    meta = contract_metadata(candles, cfg)
    mapping = make_mapping(cfg)
    panel = wide_for_mapping(candles, mapping, meta, cfg)
    features = add_returns(panel, cfg)
    if features.empty:
        raise RuntimeError("No aligned feature rows after liquidity/session filters.")
    corrs = lag_correlations(features, cfg.horizons)
    plot_heatmap(corrs)
    regs = hac_regressions(features, cfg.horizons)
    walk = walk_forward(features, cfg)
    trades = trade_simulation(features, cfg)
    second_pass = run_second_pass(candles, mapping, meta, panel, features, regs, cfg)
    findings = summarize_findings(meta, corrs, regs, walk, trades, cfg)
    findings["second_pass"] = second_pass
    (REPORTS / "leadlag_findings.json").write_text(json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8")
    write_readme_report(findings, cfg)
    return findings


def main(argv: Iterable[str] | None = None) -> int:
    cfg = parse_args(argv)
    findings = run(cfg)
    log("Done")
    log(json.dumps(findings, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
