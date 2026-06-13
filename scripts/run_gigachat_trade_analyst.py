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

from src.gigachat_client import build_gigachat_client


DEFAULT_RUN_DIR = ROOT / "reports" / "paper_runs" / "v7_live_20260525"
EXCLUDED_PORTFOLIOS = {"stock_watch"}


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.read_csv(path, engine="python", on_bad_lines="skip")


def read_json(path: Path) -> Any:
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def clean_float_series(df: pd.DataFrame, name: str) -> pd.Series:
    if name not in df.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(df[name], errors="coerce")


def bucket_series(series: pd.Series, bins: list[float], labels: list[str]) -> pd.Series:
    if series.empty:
        return pd.Series(dtype=object)
    return pd.cut(series, bins=bins, labels=labels, include_lowest=True, right=True).astype(str)


def load_trade_table(run_dir: Path) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for path in sorted(run_dir.glob("*_multi_futures_paper_trades.csv")):
        portfolio = path.name.replace("_multi_futures_paper_trades.csv", "")
        if portfolio in EXCLUDED_PORTFOLIOS:
            continue
        df = read_csv(path)
        if df.empty:
            continue
        df["portfolio"] = portfolio
        parts.append(df)
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True, sort=False)
    df["opened_at"] = pd.to_datetime(df.get("opened_at"), errors="coerce")
    df["closed_at"] = pd.to_datetime(df.get("closed_at"), errors="coerce")
    for col in ["qty", "entry_price", "exit_price", "ticks", "gross_rub", "fees_rub", "net_rub", "stop_overrun_ticks"]:
        df[col] = pd.to_numeric(df.get(col), errors="coerce")
    df["trade_date"] = df["closed_at"].dt.strftime("%Y-%m-%d")
    return df


def load_entry_audit_table(run_dir: Path) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for path in sorted(run_dir.glob("*_entry_audit.csv")):
        portfolio = path.name.replace("_entry_audit.csv", "")
        if portfolio in EXCLUDED_PORTFOLIOS:
            continue
        df = read_csv(path)
        if df.empty:
            continue
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
        df[col] = pd.to_numeric(df.get(col), errors="coerce")
    return df


def load_health_table(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob("*_health.json")):
        portfolio = path.name.replace("_health.json", "")
        payload = read_json(path)
        if not isinstance(payload, dict):
            continue
        rows.append(
            {
                "portfolio": portfolio,
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


def load_open_positions(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob("*_paper_open_positions.json")):
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


def merge_entry_and_trade_data(audit_df: pd.DataFrame, trade_df: pd.DataFrame) -> pd.DataFrame:
    if audit_df.empty or trade_df.empty:
        return pd.DataFrame()
    entries = audit_df[audit_df["event_type"] == "entry"].copy()
    if entries.empty:
        return pd.DataFrame()

    entries["opened_at"] = pd.to_datetime(entries["event_time"], errors="coerce")
    entries["entry_join_key"] = (
        entries["portfolio"].astype(str)
        + "|"
        + entries["contour"].astype(str)
        + "|"
        + entries["secid"].astype(str)
        + "|"
        + entries["direction"].astype(str)
        + "|"
        + entries["qty"].fillna(0).astype(int).astype(str)
        + "|"
        + entries["entry_price"].round(8).astype(str)
        + "|"
        + entries["opened_at"].dt.strftime("%Y-%m-%d %H:%M:%S")
    )
    drop_cols = [
        "event_type",
        "exit_price",
        "exit_source",
        "net_rub",
        "ticks",
    ]
    existing_drop = [col for col in drop_cols if col in entries.columns]
    if existing_drop:
        entries = entries.drop(columns=existing_drop)

    trade_df = trade_df.copy()
    trade_df["trade_join_key"] = (
        trade_df["portfolio"].astype(str)
        + "|"
        + trade_df["contour"].astype(str)
        + "|"
        + trade_df["secid"].astype(str)
        + "|"
        + trade_df["direction"].astype(str)
        + "|"
        + trade_df["qty"].fillna(0).astype(int).astype(str)
        + "|"
        + trade_df["entry_price"].round(8).astype(str)
        + "|"
        + trade_df["opened_at"].dt.strftime("%Y-%m-%d %H:%M:%S")
    )
    merged = entries.merge(
        trade_df[
            [
                "trade_join_key",
                "closed_at",
                "exit_price",
                "exit_source",
                "trigger_price",
                "trigger_source",
                "stop_limit_qty",
                "stop_overrun_ticks",
                "ticks",
                "gross_rub",
                "fees_rub",
                "net_rub",
                "closed_net_rub",
            ]
        ],
        left_on="entry_join_key",
        right_on="trade_join_key",
        how="left",
    )
    merged["result_class"] = merged["net_rub"].apply(
        lambda value: "win" if pd.notna(value) and value > 0 else ("loss" if pd.notna(value) and value < 0 else "flat")
    )
    merged["trade_date"] = pd.to_datetime(merged["closed_at"], errors="coerce").dt.strftime("%Y-%m-%d")
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

    def group_summary(df: pd.DataFrame, fields: list[str], top_n: int = 10) -> list[dict[str, Any]]:
        if df.empty:
            return []
        grouped = (
            df.groupby(fields, dropna=False)
            .agg(
                trades=("entry_join_key", "count"),
                net_rub=("net_rub", "sum"),
                avg_net_rub=("net_rub", "mean"),
                avg_spread_to_stop=("spread_to_stop_ratio", "mean"),
                avg_volume_ratio=("volume_ratio", "mean"),
                avg_book_ratio=("book_signal_ratio", "mean"),
                avg_full_stop_rub=("full_stop_risk_rub", "mean"),
            )
            .reset_index()
        )
        grouped = grouped.sort_values(["trades", "net_rub"], ascending=[False, True]).head(top_n)
        return json.loads(grouped.to_json(orient="records", force_ascii=False))

    metric_compare = {}
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
        win_mean = float(winning[metric].mean()) if metric in winning else None
        loss_mean = float(losing[metric].mean()) if metric in losing else None
        metric_compare[metric] = {
            "wins_mean": round(win_mean, 4) if win_mean == win_mean else None,
            "losses_mean": round(loss_mean, 4) if loss_mean == loss_mean else None,
        }

    repeated_reasons = group_summary(losing, ["portfolio", "secid", "family", "contour", "hard_entry_reason"], top_n=15)
    spread_patterns = group_summary(losing, ["portfolio", "family", "spread_bucket", "hard_entry_allow"], top_n=12)
    volume_patterns = group_summary(losing, ["portfolio", "family", "volume_bucket", "soft_entry_allow"], top_n=12)
    book_patterns = group_summary(losing, ["portfolio", "family", "book_bucket", "hard_entry_allow"], top_n=12)

    top_killers = (
        losing.sort_values("net_rub")
        [
            [
                "trade_date",
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
                "hard_entry_allow",
                "hard_entry_reason",
                "soft_entry_allow",
                "soft_entry_reason",
                "raw_signal_reason",
            ]
        ]
        .head(20)
    )

    return {
        "repeated_bad_reasons": repeated_reasons,
        "spread_patterns": spread_patterns,
        "volume_patterns": volume_patterns,
        "book_patterns": book_patterns,
        "metric_compare": metric_compare,
        "top_killer_trades": json.loads(top_killers.to_json(orient="records", force_ascii=False)),
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
    bad_days = daily[daily["net_rub"] < 0]["trade_date"].tolist()
    killer_rows = []
    for day in bad_days:
        day_trades = trade_df[trade_df["trade_date"] == day].sort_values("net_rub")
        killer_rows.append(
            {
                "trade_date": day,
                "day_net_rub": round(float(day_trades["net_rub"].sum()), 2),
                "top_3_losers_share_pct": round(float(day_trades.head(3)["net_rub"].sum() / day_trades["net_rub"].sum() * 100), 2)
                if float(day_trades["net_rub"].sum()) != 0
                else None,
                "top_3_losers": json.loads(
                    day_trades[["secid", "portfolio", "contour", "net_rub", "ticks", "exit_source"]]
                    .head(3)
                    .to_json(orient="records", force_ascii=False)
                ),
            }
        )
    return {
        "daily_summary": json.loads(daily.to_json(orient="records", force_ascii=False)),
        "killer_days": killer_rows,
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
    total = merged["net_rub"].sum()
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


def build_context(run_dir: Path) -> dict[str, Any]:
    trade_df = load_trade_table(run_dir)
    audit_df = load_entry_audit_table(run_dir)
    merged = merge_entry_and_trade_data(audit_df, trade_df)

    contour_summary: dict[str, dict[str, Any]] = defaultdict(lambda: {"trades": 0, "net_rub": 0.0})
    ticker_summary: dict[str, dict[str, Any]] = defaultdict(lambda: {"trades": 0, "net_rub": 0.0})
    if not trade_df.empty:
        for _, row in trade_df.iterrows():
            contour_summary[str(row["portfolio"])]["trades"] += 1
            contour_summary[str(row["portfolio"])]["net_rub"] += float(row.get("net_rub") or 0.0)
            ticker_summary[str(row["secid"])]["trades"] += 1
            ticker_summary[str(row["secid"])]["net_rub"] += float(row.get("net_rub") or 0.0)

    contours = sorted(
        (
            {"contour": contour, "trades": values["trades"], "net_rub": round(values["net_rub"], 2)}
            for contour, values in contour_summary.items()
        ),
        key=lambda row: row["net_rub"],
        reverse=True,
    )
    best_tickers = sorted(
        (
            {"secid": secid, "trades": values["trades"], "net_rub": round(values["net_rub"], 2)}
            for secid, values in ticker_summary.items()
        ),
        key=lambda row: row["net_rub"],
        reverse=True,
    )[:12]
    worst_tickers = sorted(
        (
            {"secid": secid, "trades": values["trades"], "net_rub": round(values["net_rub"], 2)}
            for secid, values in ticker_summary.items()
        ),
        key=lambda row: row["net_rub"],
    )[:12]

    return {
        "run_dir": str(run_dir),
        "total_trades": int(len(trade_df)),
        "total_net_rub": round(float(trade_df["net_rub"].sum()), 2) if not trade_df.empty else 0.0,
        "contours": contours,
        "best_tickers": best_tickers,
        "worst_tickers": worst_tickers,
        "health": load_health_table(run_dir),
        "open_positions": load_open_positions(run_dir),
        "bad_entry_patterns": summarize_patterns(merged),
        "day_classification": summarize_days(trade_df),
        "filter_ideas": summarize_filters(merged),
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
        "Ниже сжатый контекст по paper-боту MOEX/T-Bank. "
        "Отвечай только по данным из контекста. Никакой воды, никакой торговли в live, никаких просьб дать еще токены.\n\n"
        f"{compact}\n\n"
    )
    return {
        "bad_entry_patterns": common
        + (
            "Задача: найти повторяющиеся паттерны плохих входов.\n"
            "Нужно:\n"
            "1. Назвать 5 самых повторяемых свойств убыточных входов.\n"
            "2. Отделить структурные проблемы от случайных.\n"
            "3. Отдельно отметить семейства/тикеры, где плохой паттерн повторяется.\n"
            "4. В конце дать 5 коротких проверяемых гипотез.\n"
        ),
        "day_classification": common
        + (
            "Задача: классифицировать хороший день / плохой день / убийцы дня.\n"
            "Нужно:\n"
            "1. Коротко оценить каждый день из daily_summary.\n"
            "2. Для плохих дней назвать главных убийц дня.\n"
            "3. Сказать, дни ломают несколько крупных минусов или фон из мелких ошибок.\n"
            "4. В конце дать практический вывод по дневной защите прибыли.\n"
        ),
        "filter_ideas": common
        + (
            "Задача: придумать новые фильтры и идеи для проверки.\n"
            "Нужно:\n"
            "1. Дать до 7 новых фильтров или ограничителей.\n"
            "2. Каждый фильтр формулировать как проверяемое правило.\n"
            "3. Для каждого коротко пояснить, какой тип убытка он режет.\n"
            "4. Использовать только поля из valid_filter_fields.\n"
            "5. Не придумывать несуществующие поля.\n"
            "6. Не писать бессмысленные правила вроде отрицательного stop_ticks.\n"
            "7. Не предлагать полную переделку стратегии.\n"
        ),
        "engineering_tasks": common
        + (
            "Задача: помочь с диагностическими скриптами и отчетами.\n"
            "Нужно:\n"
            "1. Назвать 5 самых полезных новых CSV/JSON/MD отчетов.\n"
            "2. Для каждого указать поля, которые туда должны попасть.\n"
            "3. Назвать 3 проверки здоровья бота, которые стоит автоматизировать.\n"
            "4. Сформулировать это языком задач для разработчика, коротко и по делу.\n"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument(
        "--task",
        choices=["all", "bad_entry_patterns", "day_classification", "filter_ideas", "engineering_tasks"],
        default="all",
    )
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    out_dir = (args.out_dir or (run_dir / "analysis" / "gigachat")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    context = build_context(run_dir)
    context_path = out_dir / "gigachat_trade_context.json"
    context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")

    prompts = build_prompts(context)
    prompt_path = out_dir / "gigachat_task_prompts.json"
    prompt_path.write_text(json.dumps(prompts, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.skip_llm:
        print(f"Wrote {context_path}")
        print(f"Wrote {prompt_path}")
        return

    client = build_gigachat_client()
    system_prompt = (
        "Ты аналитик торгового робота. "
        "Ты не управляешь ботом и не меняешь конфиги сам. "
        "Твоя роль - сжато и полезно разбирать paper-результаты, входы, убытки, фильтры и инженерную диагностику."
    )
    outputs: dict[str, str] = {}
    selected = prompts.items() if args.task == "all" else [(args.task, prompts[args.task])]
    for task_name, user_prompt in selected:
        answer = client.ask(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.1,
            max_tokens=2200,
        )
        outputs[task_name] = answer.strip()
        (out_dir / f"{task_name}.md").write_text(answer.strip() + "\n", encoding="utf-8")

    master_lines = [
        "# GigaChat Trade Analyst",
        "",
        f"Run dir: `{run_dir}`",
        "",
    ]
    for task_name in ["bad_entry_patterns", "day_classification", "filter_ideas", "engineering_tasks"]:
        master_lines.append(f"## {task_name}")
        master_lines.append("")
        master_lines.append(outputs.get(task_name, ""))
        master_lines.append("")
    if args.task == "all":
        (out_dir / "gigachat_master_review.md").write_text("\n".join(master_lines), encoding="utf-8")

    print(f"Wrote {context_path}")
    print(f"Wrote {prompt_path}")
    for task_name in outputs:
        print(f"Wrote {out_dir / f'{task_name}.md'}")
    if args.task == "all":
        print(f"Wrote {out_dir / 'gigachat_master_review.md'}")


if __name__ == "__main__":
    main()
