from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.openai_client import build_openai_client


RUNS_ROOT = ROOT / "reports" / "paper_runs"
OUTPUT_ROOT = ROOT / "reports" / "analysis" / "openai_trade_analyst"
EXCLUDED_PORTFOLIOS = {"stock_watch"}


def read_csv(path: Path, nrows: int | None = None) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path, nrows=nrows)
    except Exception:
        return pd.read_csv(path, nrows=nrows, engine="python", on_bad_lines="skip")


def read_json(path: Path) -> Any:
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def to_numeric_series(series: pd.Series | None) -> pd.Series:
    if series is None:
        return pd.Series(dtype=float)
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce")
    cleaned = (
        series.astype(str)
        .str.replace("\u00a0", "", regex=False)
        .str.replace(" ", "", regex=False)
        .str.replace('"', "", regex=False)
        .str.replace(",", ".", regex=False)
    )
    return pd.to_numeric(cleaned, errors="coerce")


def round_num(value: Any, digits: int = 2) -> float | None:
    try:
        value = float(value)
    except Exception:
        return None
    if pd.isna(value):
        return None
    return round(value, digits)


def bucket_series(series: pd.Series, bins: list[float], labels: list[str]) -> pd.Series:
    if series.empty:
        return pd.Series(dtype=object)
    return pd.cut(series, bins=bins, labels=labels, include_lowest=True, right=True).astype(str)


def contract_family(secid: str) -> str:
    secid = str(secid or "")
    if secid.endswith("perpA"):
        return secid
    head = secid.rstrip("0123456789")
    month_codes = set("FGHJKMNQUVXZ")
    if len(head) > 1 and head[-1].upper() in month_codes:
        head = head[:-1]
    return head or secid


def strategy_context() -> dict[str, Any]:
    passport_path = ROOT / "PROJECT_PASSPORT.md"
    passport_text = passport_path.read_text(encoding="utf-8", errors="ignore") if passport_path.exists() else ""
    return {
        "project_mode": "paper_trading",
        "source_passport_path": str(passport_path),
        "project_passport_md": passport_text,
        "strategy_core": [
            "ищем краткосрочный импульс цены",
            "входим только если цена, momentum, VWAP, локальный пробой, объем и стакан согласованы",
            "сразу ставим защитный стоп",
            "при движении в плюс включаем трейлинг",
            "результат считаем в рублях с учетом комиссии",
        ],
        "current_live_question": "не дать нескольким большим стопам уничтожать серию маленьких плюсов",
        "existing_logic_already_present": [
            "система paper, реальные ордера основной бот не отправляет",
            "разделение на портфели classic_core / gl_watch / neo / tail_research / stock_watch",
            "внутренние режимы strict и aggressive",
            "отдельные shadow-слои GPT full / relaxed / loose для сравнения",
            "счёт полного стопа в рублях",
            "ограничение полного стопа через median win * 4",
            "верхние потолки полного стопа 4000 / 2000 / 1000 ₽ по режимам риска",
            "risk governor с переходами normal / reduced / micro",
            "daily profit guard и выключение aggressive после достижения дневной защиты прибыли",
            "expiry / roll фильтры",
            "фильтры duplicate position и book 0/0",
            "stream_stoplimit и аварийный emergency_market_after_missed_limit",
            "автовосстановление supervisor и dashboard",
        ],
        "known_focus_for_review": [
            "повторяющиеся плохие входы",
            "убийцы дня",
            "качество слоёв strict/aggressive и GPT-shadow",
            "нужны ли дополнительные исследовательские слои",
        ],
        "layer_families": {
            "actual_entry_layers": ["strict", "aggressive"],
            "gpt_shadow_layers": ["gpt_full", "gpt_relaxed", "gpt_loose"],
            "four_layer_backtest_layers": ["current_actual", "gpt_full", "gpt_relaxed", "gpt_loose"],
        },
    }


def discover_run_dirs(runs_root: Path) -> list[Path]:
    dirs: list[Path] = []
    for run_dir in sorted(runs_root.iterdir()):
        if not run_dir.is_dir():
            continue
        if any(run_dir.glob("*_multi_futures_paper_trades.csv")):
            dirs.append(run_dir)
    return dirs


def trade_uid_from_row(row: pd.Series) -> str:
    opened_at = row.get("opened_at")
    if pd.isna(opened_at):
        opened_at = row.get("closed_at")
    parts = [
        str(opened_at or ""),
        str(row.get("closed_at") or ""),
        str(row.get("portfolio") or ""),
        str(row.get("contour") or ""),
        str(row.get("secid") or ""),
        str(row.get("direction") or ""),
        str(int(float(row.get("qty") or 0))),
        f"{float(row.get('entry_price') or 0.0):.8f}",
        f"{float(row.get('exit_price') or 0.0):.8f}",
        f"{float(row.get('net_rub') or 0.0):.4f}",
    ]
    return "|".join(parts)


def entry_uid_from_row(row: pd.Series) -> str:
    timestamp = row.get("event_time")
    if pd.isna(timestamp):
        timestamp = row.get("opened_at")
    parts = [
        str(timestamp or ""),
        str(row.get("portfolio") or ""),
        str(row.get("contour") or ""),
        str(row.get("secid") or ""),
        str(row.get("direction") or ""),
        str(int(float(row.get("qty") or 0))),
        f"{float(row.get('entry_price') or 0.0):.8f}",
    ]
    return "|".join(parts)


def load_all_trades(run_dirs: list[Path]) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for run_dir in run_dirs:
        for path in sorted(run_dir.glob("*_multi_futures_paper_trades.csv")):
            portfolio = path.name.replace("_multi_futures_paper_trades.csv", "")
            if portfolio in EXCLUDED_PORTFOLIOS:
                continue
            df = read_csv(path)
            if df.empty:
                continue
            df["run_name"] = run_dir.name
            df["portfolio"] = portfolio
            parts.append(df)
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True, sort=False)
    df["opened_at"] = pd.to_datetime(df.get("opened_at"), errors="coerce")
    df["closed_at"] = pd.to_datetime(df.get("closed_at"), errors="coerce")
    for col in ["qty", "entry_price", "exit_price", "ticks", "gross_rub", "fees_rub", "net_rub", "stop_overrun_ticks"]:
        df[col] = to_numeric_series(df.get(col))
    df["opened_at"] = df["opened_at"].where(df["opened_at"].notna(), df["closed_at"])
    df = df.dropna(subset=["closed_at", "secid", "net_rub"]).copy()
    df["trade_uid"] = df.apply(trade_uid_from_row, axis=1)
    df = df.sort_values(["closed_at", "run_name"]).drop_duplicates(subset=["trade_uid"], keep="first").reset_index(drop=True)
    df["trade_date"] = df["closed_at"].dt.strftime("%Y-%m-%d")
    df["family"] = df["secid"].apply(contract_family)
    return df


def load_all_entry_audits(run_dirs: list[Path]) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for run_dir in run_dirs:
        for path in sorted(run_dir.glob("*_entry_audit.csv")):
            portfolio = path.name.replace("_entry_audit.csv", "")
            if portfolio in EXCLUDED_PORTFOLIOS:
                continue
            df = read_csv(path)
            if df.empty:
                continue
            df["run_name"] = run_dir.name
            df["portfolio"] = portfolio
            parts.append(df)
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True, sort=False)
    df["event_time"] = pd.to_datetime(df.get("event_time"), errors="coerce")
    numeric_cols = [
        "qty",
        "entry_price",
        "exit_price",
        "net_rub",
        "ticks",
        "last_price",
        "last_close",
        "vwap",
        "directional_vwap_ticks",
        "directional_vwap_to_stop",
        "momentum_ticks",
        "trend_ticks",
        "last_volume",
        "avg_volume",
        "volume_ratio",
        "bid_qty_top3",
        "ask_qty_top3",
        "book_signal_ratio",
        "spread_ticks",
        "spread_to_stop_ratio",
        "fee_ticks",
        "fee_to_stop_ratio",
        "recent_range_ticks",
        "breakout_margin_ticks",
        "stop_ticks",
        "trail_ticks",
        "trail_arm_ticks",
        "full_stop_risk_rub",
    ]
    for col in numeric_cols:
        df[col] = to_numeric_series(df.get(col))
    df = df.dropna(subset=["event_time", "secid"]).copy()
    df["entry_uid"] = df.apply(entry_uid_from_row, axis=1)
    df = df.sort_values(["event_time", "run_name"]).drop_duplicates(subset=["entry_uid", "event_type"], keep="first").reset_index(drop=True)
    return df


def merge_entries_with_trades(audit_df: pd.DataFrame, trade_df: pd.DataFrame) -> pd.DataFrame:
    if audit_df.empty or trade_df.empty:
        return pd.DataFrame()
    entries = audit_df[audit_df["event_type"] == "entry"].copy()
    if entries.empty:
        return pd.DataFrame()
    drop_cols = ["event_type", "exit_price", "exit_source", "net_rub", "ticks"]
    existing_drop = [col for col in drop_cols if col in entries.columns]
    if existing_drop:
        entries = entries.drop(columns=existing_drop)
    trades = trade_df.copy()
    trades["entry_uid"] = trades.apply(entry_uid_from_row, axis=1)
    merged = entries.merge(
        trades[
            [
                "entry_uid",
                "run_name",
                "closed_at",
                "exit_price",
                "exit_source",
                "ticks",
                "gross_rub",
                "fees_rub",
                "net_rub",
                "stop_overrun_ticks",
                "trade_date",
            ]
        ],
        on="entry_uid",
        how="left",
        suffixes=("", "_trade"),
    )
    merged["result_class"] = merged["net_rub"].apply(
        lambda value: "win" if pd.notna(value) and value > 0 else ("loss" if pd.notna(value) and value < 0 else "flat")
    )
    merged["spread_bucket"] = bucket_series(
        merged["spread_to_stop_ratio"],
        bins=[-1.0, 0.15, 0.35, 0.60, 1.0, 999.0],
        labels=["tiny", "small", "medium", "heavy", "dominates"],
    )
    merged["volume_bucket"] = bucket_series(
        merged["volume_ratio"],
        bins=[-1.0, 0.8, 1.0, 1.2, 1.6, 99.0],
        labels=["weak", "subpar", "ok", "strong", "spike"],
    )
    merged["book_bucket"] = bucket_series(
        merged["book_signal_ratio"],
        bins=[-999.0, 0.8, 1.0, 1.5, 2.5, 999.0],
        labels=["against", "flat", "ok", "strong", "extreme"],
    )
    return merged


def summarize_patterns(merged: pd.DataFrame) -> dict[str, Any]:
    if merged.empty:
        return {}
    losing = merged[merged["result_class"] == "loss"].copy()
    winning = merged[merged["result_class"] == "win"].copy()

    def group_summary(df: pd.DataFrame, fields: list[str], top_n: int = 12) -> list[dict[str, Any]]:
        if df.empty:
            return []
        grouped = (
            df.groupby(fields, dropna=False)
            .agg(
                trades=("entry_uid", "count"),
                net_rub=("net_rub", "sum"),
                avg_net_rub=("net_rub", "mean"),
                avg_spread_to_stop=("spread_to_stop_ratio", "mean"),
                avg_volume_ratio=("volume_ratio", "mean"),
                avg_book_ratio=("book_signal_ratio", "mean"),
                avg_full_stop_rub=("full_stop_risk_rub", "mean"),
            )
            .reset_index()
            .sort_values(["trades", "net_rub"], ascending=[False, True])
            .head(top_n)
        )
        return json.loads(grouped.to_json(orient="records", force_ascii=False))

    metric_compare: dict[str, Any] = {}
    for metric in [
        "spread_to_stop_ratio",
        "volume_ratio",
        "book_signal_ratio",
        "fee_to_stop_ratio",
        "momentum_ticks",
        "trend_ticks",
        "full_stop_risk_rub",
        "stop_ticks",
        "breakout_margin_ticks",
    ]:
        metric_compare[metric] = {
            "wins_mean": round_num(winning.get(metric, pd.Series(dtype=float)).mean(), 4),
            "losses_mean": round_num(losing.get(metric, pd.Series(dtype=float)).mean(), 4),
        }

    top_killers = losing.sort_values("net_rub").head(24)
    return {
        "repeated_bad_reasons": group_summary(losing, ["family", "secid", "portfolio", "contour", "hard_entry_reason"], top_n=15),
        "spread_patterns": group_summary(losing, ["family", "portfolio", "spread_bucket", "hard_entry_allow"]),
        "volume_patterns": group_summary(losing, ["family", "portfolio", "volume_bucket", "soft_entry_allow"]),
        "book_patterns": group_summary(losing, ["family", "portfolio", "book_bucket", "hard_entry_allow"]),
        "metric_compare": metric_compare,
        "top_killer_trades": json.loads(
            top_killers[
                [
                    "trade_date",
                    "run_name",
                    "portfolio",
                    "contour",
                    "secid",
                    "family",
                    "direction",
                    "qty",
                    "net_rub",
                    "ticks",
                    "exit_source",
                    "spread_to_stop_ratio",
                    "volume_ratio",
                    "book_signal_ratio",
                    "fee_to_stop_ratio",
                    "full_stop_risk_rub",
                    "hard_entry_reason",
                    "soft_entry_reason",
                ]
            ].to_json(orient="records", force_ascii=False)
        ),
    }


def summarize_days(trade_df: pd.DataFrame) -> dict[str, Any]:
    if trade_df.empty:
        return {}
    daily = (
        trade_df.groupby("trade_date", dropna=False)
        .agg(
            trades=("secid", "count"),
            net_rub=("net_rub", "sum"),
            gross_rub=("gross_rub", "sum"),
            fees_rub=("fees_rub", "sum"),
            avg_trade=("net_rub", "mean"),
            win_rate=("net_rub", lambda s: float((s > 0).mean()) if len(s) else 0.0),
        )
        .reset_index()
        .sort_values("trade_date")
    )
    daily["day_class"] = daily["net_rub"].apply(lambda x: "good" if x > 0 else ("flat" if x == 0 else "bad"))
    killer_days: list[dict[str, Any]] = []
    for day in daily[daily["net_rub"] < 0]["trade_date"].tolist():
        day_trades = trade_df[trade_df["trade_date"] == day].sort_values("net_rub")
        total = float(day_trades["net_rub"].sum())
        killer_days.append(
            {
                "trade_date": day,
                "day_net_rub": round(total, 2),
                "top_3_losers_share_pct": round(float(day_trades.head(3)["net_rub"].sum() / total * 100), 2) if total else None,
                "top_3_losers": json.loads(
                    day_trades[["secid", "portfolio", "contour", "net_rub", "ticks", "exit_source"]]
                    .head(3)
                    .to_json(orient="records", force_ascii=False)
                ),
            }
        )
    return {
        "daily_summary": json.loads(daily.to_json(orient="records", force_ascii=False)),
        "killer_days": killer_days,
    }


def summarize_filters(merged: pd.DataFrame) -> dict[str, Any]:
    if merged.empty:
        return {}
    candidates: list[dict[str, Any]] = []
    rules = [
        ("spread_to_stop_ratio > 0.35", merged["spread_to_stop_ratio"] > 0.35),
        ("spread_to_stop_ratio > 0.60", merged["spread_to_stop_ratio"] > 0.60),
        ("volume_ratio < 1.0", merged["volume_ratio"] < 1.0),
        ("book_signal_ratio < 1.0", merged["book_signal_ratio"] < 1.0),
        ("hard_entry_allow == false", merged["hard_entry_allow"] == False),  # noqa: E712
        ("soft_entry_allow == false", merged["soft_entry_allow"] == False),  # noqa: E712
        ("full_stop_risk_rub > 4000", merged["full_stop_risk_rub"] > 4000),
        ("fee_to_stop_ratio > 0.25", merged["fee_to_stop_ratio"] > 0.25),
    ]
    total = float(merged["net_rub"].sum()) if "net_rub" in merged else 0.0
    for label, mask in rules:
        subset = merged[mask.fillna(False)].copy()
        if subset.empty:
            continue
        candidates.append(
            {
                "rule": label,
                "trades": int(len(subset)),
                "net_rub": round(float(subset["net_rub"].sum()), 2),
                "avg_net_rub": round(float(subset["net_rub"].mean()), 2),
                "loss_share_pct": round(float((subset["net_rub"] < 0).mean() * 100), 2),
                "portfolio_share_pct": round(float(subset["net_rub"].sum() / total * 100), 2) if total else None,
            }
        )
    return {"filter_candidates": sorted(candidates, key=lambda row: row["avg_net_rub"])}


def summarize_layers(trade_df: pd.DataFrame) -> dict[str, Any]:
    actual_layers = []
    if not trade_df.empty:
        grouped = (
            trade_df.groupby("contour", dropna=False)
            .agg(
                trades=("secid", "count"),
                wins=("net_rub", lambda s: int((s > 0).sum())),
                losses=("net_rub", lambda s: int((s < 0).sum())),
                net_rub=("net_rub", "sum"),
                avg_trade=("net_rub", "mean"),
                median_trade=("net_rub", "median"),
            )
            .reset_index()
            .sort_values("net_rub", ascending=False)
        )
        actual_layers = json.loads(grouped.to_json(orient="records", force_ascii=False))

    layer_artifacts: dict[str, Any] = {}
    files = {
        "four_layer_backtest_summary": ROOT / "reports" / "paper_runs" / "v7_live_20260525" / "analysis" / "four_layer_backtest" / "four_layer_backtest_summary.csv",
        "four_layer_backtest_train_test": ROOT / "reports" / "paper_runs" / "v7_live_20260525" / "analysis" / "four_layer_backtest" / "four_layer_backtest_train_test.csv",
        "four_layer_walkforward_summary": ROOT / "reports" / "paper_runs" / "v7_live_20260525" / "analysis" / "four_layer_backtest" / "four_layer_walkforward_summary_2t.csv",
        "four_layer_backtest_by_ticker": ROOT / "reports" / "paper_runs" / "v7_live_20260525" / "analysis" / "four_layer_backtest" / "four_layer_backtest_by_ticker.csv",
        "gpt_shadow_layers_3d_summary": ROOT / "reports" / "paper_runs" / "v7_live_20260525" / "analysis" / "gpt_shadow_layers_3d_summary.csv",
        "v7_exact_gpt_summary_by_ticker": ROOT / "reports" / "paper_runs" / "v7_live_20260525" / "analysis" / "v7_exact_gpt" / "v7_gpt_exact_source_summary_by_ticker.csv",
        "time_filter_no_entries_after_1745": ROOT / "reports" / "paper_runs" / "v7_live_20260525" / "analysis" / "time_filters" / "no_entries_after_1745_daily_summary.csv",
    }
    for name, path in files.items():
        if not path.exists():
            continue
        df = read_csv(path)
        if df.empty:
            continue
        if name == "four_layer_backtest_by_ticker":
            filtered = (
                df[(df["slippage_ticks"] == 2)]
                .sort_values("net_rub", ascending=False)
                .groupby("layer", dropna=False)
                .head(8)
                .reset_index(drop=True)
            )
            layer_artifacts[name] = json.loads(filtered.to_json(orient="records", force_ascii=False))
        else:
            layer_artifacts[name] = json.loads(df.to_json(orient="records", force_ascii=False))
    return {
        "actual_entry_layers": actual_layers,
        "research_layers": layer_artifacts,
    }


def summarize_tickers_and_families(trade_df: pd.DataFrame) -> dict[str, Any]:
    if trade_df.empty:
        return {}
    ticker_df = (
        trade_df.groupby(["family", "secid"], dropna=False)
        .agg(
            trades=("secid", "count"),
            wins=("net_rub", lambda s: int((s > 0).sum())),
            losses=("net_rub", lambda s: int((s < 0).sum())),
            net_rub=("net_rub", "sum"),
            avg_trade=("net_rub", "mean"),
        )
        .reset_index()
        .sort_values("net_rub", ascending=False)
    )
    family_df = (
        trade_df.groupby("family", dropna=False)
        .agg(
            trades=("secid", "count"),
            wins=("net_rub", lambda s: int((s > 0).sum())),
            losses=("net_rub", lambda s: int((s < 0).sum())),
            net_rub=("net_rub", "sum"),
            avg_trade=("net_rub", "mean"),
        )
        .reset_index()
        .sort_values("net_rub", ascending=False)
    )
    return {
        "best_tickers": json.loads(ticker_df.head(15).to_json(orient="records", force_ascii=False)),
        "worst_tickers": json.loads(ticker_df.sort_values("net_rub").head(15).to_json(orient="records", force_ascii=False)),
        "best_families": json.loads(family_df.head(12).to_json(orient="records", force_ascii=False)),
        "worst_families": json.loads(family_df.sort_values("net_rub").head(12).to_json(orient="records", force_ascii=False)),
    }


def latest_health(run_dirs: list[Path]) -> list[dict[str, Any]]:
    latest_dir = max(run_dirs, key=lambda p: p.stat().st_mtime) if run_dirs else None
    if latest_dir is None:
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(latest_dir.glob("*_health.json")):
        payload = read_json(path)
        if not isinstance(payload, dict):
            continue
        rows.append(
            {
                "portfolio": path.name.replace("_health.json", ""),
                "status": payload.get("status"),
                "closed_trades": payload.get("closed_trades"),
                "closed_net": payload.get("closed_net"),
                "open_positions": payload.get("open_positions"),
                "reconnect_count": payload.get("reconnect_count"),
                "last_stream_age_sec": payload.get("last_stream_age_sec"),
                "timestamp": payload.get("timestamp"),
            }
        )
    return rows


def latest_open_positions(run_dirs: list[Path]) -> list[dict[str, Any]]:
    latest_dir = max(run_dirs, key=lambda p: p.stat().st_mtime) if run_dirs else None
    if latest_dir is None:
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(latest_dir.glob("*_paper_open_positions.json")):
        portfolio = path.name.replace("_paper_open_positions.json", "")
        if portfolio in EXCLUDED_PORTFOLIOS:
            continue
        payload = read_json(path)
        if payload is None:
            continue
        positions = payload if isinstance(payload, list) else payload.get("positions", [])
        for item in positions:
            if not isinstance(item, dict) or not item:
                continue
            secid = item.get("secid") or item.get("ticker")
            if not secid:
                continue
            rows.append(
                {
                    "portfolio": portfolio,
                    "secid": secid,
                    "direction": item.get("direction"),
                    "qty": item.get("qty"),
                    "entry_price": item.get("entry_price"),
                    "last_price": item.get("last_price"),
                    "mark_price": item.get("mark_price"),
                    "stop_price": item.get("stop_price"),
                    "opened_at": item.get("opened_at"),
                }
            )
    return rows


def build_context(run_dirs: list[Path], out_dir: Path) -> dict[str, Any]:
    trade_df = load_all_trades(run_dirs)
    audit_df = load_all_entry_audits(run_dirs)
    merged = merge_entries_with_trades(audit_df, trade_df)

    out_dir.mkdir(parents=True, exist_ok=True)
    if not trade_df.empty:
        trade_df.sort_values(["trade_date", "closed_at"]).to_csv(out_dir / "openai_all_trade_rows.csv", index=False)
    if not merged.empty:
        merged.sort_values(["trade_date", "event_time"]).to_csv(out_dir / "openai_all_entry_trade_join.csv", index=False)

    daily_summary = summarize_days(trade_df)
    if daily_summary:
        pd.DataFrame(daily_summary["daily_summary"]).to_csv(out_dir / "openai_trade_days_summary.csv", index=False)

    layer_summary = summarize_layers(trade_df)
    if layer_summary.get("actual_entry_layers"):
        pd.DataFrame(layer_summary["actual_entry_layers"]).to_csv(out_dir / "openai_actual_layers_summary.csv", index=False)

    ticker_family_summary = summarize_tickers_and_families(trade_df)
    if ticker_family_summary.get("best_tickers"):
        pd.DataFrame(ticker_family_summary["best_tickers"]).to_csv(out_dir / "openai_best_tickers.csv", index=False)
    if ticker_family_summary.get("worst_tickers"):
        pd.DataFrame(ticker_family_summary["worst_tickers"]).to_csv(out_dir / "openai_worst_tickers.csv", index=False)

    return {
        "strategy_context": strategy_context(),
        "run_dirs": [str(path) for path in run_dirs],
        "run_level_trade_rows": [
            {
                "run_name": run_name,
                "portfolio": portfolio,
                "trades": int(len(group)),
                "net_rub": round(float(group["net_rub"].sum()), 2),
                "trade_days": sorted(group["trade_date"].dropna().unique().tolist()),
            }
            for (run_name, portfolio), group in trade_df.groupby(["run_name", "portfolio"], dropna=False)
        ]
        if not trade_df.empty
        else [],
        "trade_days": [row["trade_date"] for row in daily_summary.get("daily_summary", [])],
        "total_runs_with_trades": len(run_dirs),
        "total_unique_trades": int(len(trade_df)),
        "total_net_rub": round(float(trade_df["net_rub"].sum()), 2) if not trade_df.empty else 0.0,
        "latest_health": latest_health(run_dirs),
        "latest_open_positions": latest_open_positions(run_dirs),
        "ticker_family_summary": ticker_family_summary,
        "bad_entry_patterns": summarize_patterns(merged),
        "day_classification": daily_summary,
        "filter_ideas": summarize_filters(merged),
        "layer_review": layer_summary,
        "valid_filter_fields": [
            "portfolio",
            "contour",
            "secid",
            "family",
            "direction",
            "spread_to_stop_ratio",
            "volume_ratio",
            "book_signal_ratio",
            "fee_to_stop_ratio",
            "momentum_ticks",
            "trend_ticks",
            "full_stop_risk_rub",
            "stop_ticks",
            "trail_ticks",
            "trail_arm_ticks",
            "hard_entry_allow",
            "soft_entry_allow",
            "hard_entry_reason",
            "soft_entry_reason",
            "risk_mode",
            "exit_source",
        ],
    }


def build_prompts(context: dict[str, Any]) -> dict[str, str]:
    compact = json.dumps(context, ensure_ascii=False, indent=2)
    common = (
        "Ниже контекст по paper-боту MOEX/T-Bank. "
        "Нужно отвечать только по данным из контекста и учитывать, что часть защит уже реализована.\n\n"
        f"{compact}\n\n"
    )
    return {
        "full_strategy_report": common
        + (
            "Сделай полный русский отчет для разработчика стратегии.\n"
            "Нужно обязательно дать разделы:\n"
            "1. Как ты понял стратегию.\n"
            "2. Все торговые дни и что по ним произошло.\n"
            "3. Что уже изменено и выглядит правильным, не требует дополнительных настроек логики.\n"
            "4. Что еще обязательно изменить.\n"
            "5. Какие тикеры, семейства и контуры полезны, а какие ломают день.\n"
            "6. Что видно по исследовательским слоям strict/aggressive/GPT/full/relaxed/loose.\n"
            "7. Нужно ли добавить еще исследовательские слои, и если да, какие именно.\n"
            "8. Приоритетный план действий на ближайшие шаги.\n"
            "Не проси дополнительных данных, если их уже хватает для первого вывода.\n"
        ),
        "bad_entry_patterns": common
        + (
            "Задача: найти повторяющиеся паттерны плохих входов.\n"
            "Нужно:\n"
            "1. Назвать 7 самых повторяемых свойств убыточных входов.\n"
            "2. Отделить структурные проблемы от случайных.\n"
            "3. Отдельно отметить семейства/тикеры, где плохой паттерн повторяется.\n"
            "4. В конце дать 7 коротких проверяемых гипотез.\n"
        ),
        "day_classification": common
        + (
            "Задача: классифицировать хороший день / плохой день / убийцы дня.\n"
            "Нужно:\n"
            "1. Коротко оценить каждый торговый день.\n"
            "2. Для плохих дней назвать главных убийц дня.\n"
            "3. Сказать, дни ломают несколько крупных минусов или фон из мелких ошибок.\n"
            "4. В конце дать вывод по дневной защите прибыли.\n"
        ),
        "layer_review": common
        + (
            "Задача: пройтись по всем слоям, которые есть в исследовании.\n"
            "Нужно:\n"
            "1. Сравнить actual strict/aggressive и исследовательские слои current_actual / gpt_full / gpt_relaxed / gpt_loose.\n"
            "2. Отдельно учесть four_layer_backtest и walkforward.\n"
            "3. Сказать, какой слой полезен, какой деградирует, какой требует отдельного наблюдения.\n"
            "4. Предложить до 5 новых исследовательских слоев, если они действительно нужны.\n"
        ),
        "engineering_tasks": common
        + (
            "Задача: назвать инженерные доработки и отчеты.\n"
            "Нужно:\n"
            "1. Назвать 7 самых полезных новых CSV/JSON/MD отчетов.\n"
            "2. Для каждого указать поля.\n"
            "3. Назвать 5 health-check проверок, которые стоит автоматизировать.\n"
            "4. Дать это как короткий список задач разработчику.\n"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-root", type=Path, default=RUNS_ROOT)
    parser.add_argument("--out-dir", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument(
        "--task",
        choices=["all", "full_strategy_report", "bad_entry_patterns", "day_classification", "layer_review", "engineering_tasks"],
        default="all",
    )
    args = parser.parse_args()

    run_dirs = discover_run_dirs(args.runs_root.resolve())
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    context = build_context(run_dirs, out_dir)
    context_path = out_dir / "openai_trade_context.json"
    context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")

    prompts = build_prompts(context)
    prompt_path = out_dir / "openai_task_prompts.json"
    prompt_path.write_text(json.dumps(prompts, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.skip_llm:
        print(f"Wrote {context_path}")
        print(f"Wrote {prompt_path}")
        return

    client = build_openai_client()
    system_prompt = (
        "Ты сильный аналитик торговых систем. "
        "Ты не управляешь ботом и не делаешь вид, что у тебя есть данные вне контекста. "
        "Твоя задача - понять стратегию, отличить уже реализованные защиты от недоделок, "
        "разобрать результаты paper-торговли и дать практичные выводы."
    )

    outputs: dict[str, str] = {}
    selected = prompts.items() if args.task == "all" else [(args.task, prompts[args.task])]
    for task_name, user_prompt in selected:
        try:
            answer = client.ask(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.1,
                max_tokens=3200,
            )
        except Exception as exc:  # noqa: BLE001
            message = (
                "# OpenAI runtime error\n\n"
                f"task: `{task_name}`\n\n"
                f"error: `{type(exc).__name__}: {exc}`\n\n"
                "Что уже подготовлено:\n"
                "- `openai_trade_context.json`\n"
                "- `openai_task_prompts.json`\n\n"
                "Что делать дальше:\n"
                "1. Проверить, что сервер может ходить в OpenAI API.\n"
                "2. Если прямой `api.openai.com` недоступен, задать `OPENAI_BASE_URL` на рабочий OpenAI-compatible gateway.\n"
                "3. Повторно запустить `python scripts/run_openai_trade_analyst.py`.\n"
            )
            (out_dir / "openai_runtime_error.md").write_text(message, encoding="utf-8")
            print(f"Wrote {out_dir / 'openai_runtime_error.md'}")
            return
        outputs[task_name] = answer.strip()
        (out_dir / f"{task_name}.md").write_text(answer.strip() + "\n", encoding="utf-8")

    if args.task == "all":
        master_lines = [
            "# OpenAI Trade Analyst",
            "",
            f"Runs root: `{args.runs_root.resolve()}`",
            "",
        ]
        for task_name in ["full_strategy_report", "bad_entry_patterns", "day_classification", "layer_review", "engineering_tasks"]:
            master_lines.append(f"## {task_name}")
            master_lines.append("")
            master_lines.append(outputs.get(task_name, ""))
            master_lines.append("")
        (out_dir / "openai_master_review.md").write_text("\n".join(master_lines), encoding="utf-8")

    print(f"Wrote {context_path}")
    print(f"Wrote {prompt_path}")
    for task_name in outputs:
        print(f"Wrote {out_dir / f'{task_name}.md'}")
    if args.task == "all":
        print(f"Wrote {out_dir / 'openai_master_review.md'}")


if __name__ == "__main__":
    main()
