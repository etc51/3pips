from __future__ import annotations

import argparse
from pathlib import Path

from autonomy_common import now_str, safe_float, write_csv_rows, write_json, write_text
from research_strategy_registry import resolve_trade_date


THRESHOLD_VALUES = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5]

COUNTERFACTUAL_FIELDS = [
    "trade_date",
    "sample",
    "experiment_rank",
    "group",
    "portfolio_group",
    "contour",
    "family",
    "threshold_ratio",
    "days",
    "unique_entries",
    "kept_entries",
    "blocked_entries",
    "blocked_wins",
    "blocked_losses",
    "block_rate_pct",
    "base_net_rub",
    "kept_net_rub",
    "delta_net_rub",
    "base_expectancy_rub",
    "kept_expectancy_rub",
    "delta_expectancy_rub",
    "base_profit_factor",
    "kept_profit_factor",
    "delta_profit_factor",
    "base_top3_loss_rub",
    "kept_top3_loss_rub",
    "delta_top3_loss_rub",
    "confidence_weight",
    "rank_score",
    "candidate_status",
    "evaluation_state",
    "recommended_action",
    "note",
]


def maybe_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return round(float(value), 6)
    except Exception:
        return None


def trade_group(row: dict) -> str:
    portfolio_group = str(row.get("portfolio_group") or "").upper()
    contour = str(row.get("contour") or "").upper()
    family = str(row.get("family") or "").upper()
    if not portfolio_group or not contour or not family:
        return ""
    return f"{portfolio_group}/{contour}::{family}"


def gross_loss_abs(rows: list[dict]) -> float:
    return round(abs(sum(value for value in (safe_float(row.get("net_rub")) for row in rows) if value < 0)), 2)


def profit_factor(rows: list[dict]) -> float:
    gross_win = round(sum(value for value in (safe_float(row.get("net_rub")) for row in rows) if value > 0), 2)
    gross_loss = gross_loss_abs(rows)
    if gross_loss <= 0:
        return 999.0 if gross_win > 0 else 0.0
    return round(gross_win / gross_loss, 4)


def top3_loss_abs(rows: list[dict]) -> float:
    losses = sorted((abs(safe_float(row.get("net_rub"))) for row in rows if safe_float(row.get("net_rub")) < 0), reverse=True)
    return round(sum(losses[:3]), 2)


def filter_trade_date(rows: list[dict], trade_date: str) -> list[dict]:
    return [row for row in rows if str(row.get("closed_at") or "").startswith(trade_date)]


def collapse_entry_rows(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        entry_id = str(row.get("entry_id") or "").strip()
        group = trade_group(row)
        spread_ratio = maybe_float(row.get("spread_to_stop_ratio"))
        if not entry_id or not group or spread_ratio is None:
            continue
        closed_at = str(row.get("closed_at") or "").strip()
        if not closed_at:
            continue
        item = dict(row)
        item["group"] = group
        item["portfolio_group"] = str(row.get("portfolio_group") or "").upper()
        item["contour"] = str(row.get("contour") or "").upper()
        item["family"] = str(row.get("family") or "").upper()
        item["spread_to_stop_ratio"] = spread_ratio
        grouped.setdefault(entry_id, []).append(item)
    collapsed: list[dict] = []
    for entry_id, items in grouped.items():
        chosen = sorted(
            items,
            key=lambda item: (
                str(item.get("closed_at") or ""),
                str(item.get("opened_at") or ""),
                str(item.get("model") or ""),
            ),
        )[0]
        out = dict(chosen)
        out["entry_id"] = entry_id
        collapsed.append(out)
    collapsed.sort(key=lambda row: (str(row.get("closed_at") or ""), str(row.get("entry_id") or "")))
    return collapsed


def grouped_entries(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        group = str(row.get("group") or "").strip().upper()
        if group:
            grouped.setdefault(group, []).append(row)
    return grouped


def confidence_weight(unique_entries: int, days: int, blocked_losses: int) -> float:
    return round(min(1.0, unique_entries / 12.0) * min(1.0, days / 3.0) * min(1.0, blocked_losses / 4.0), 4)


def candidate_status(
    *,
    sample: str,
    unique_entries: int,
    days: int,
    blocked_entries: int,
    block_rate_pct: float,
    base_expectancy_rub: float,
    delta_expectancy_rub: float,
    kept_profit_factor: float,
    base_profit_factor: float,
    kept_top3_loss_rub: float,
    base_top3_loss_rub: float,
    blocked_losses: int,
    blocked_wins: int,
) -> str:
    if (
        sample == "all_sample"
        and unique_entries >= 8
        and days >= 2
        and blocked_entries >= 2
        and 5.0 <= block_rate_pct <= 40.0
        and base_expectancy_rub < 0
        and delta_expectancy_rub >= 75.0
        and kept_profit_factor >= base_profit_factor
        and kept_top3_loss_rub <= base_top3_loss_rub
        and blocked_losses >= blocked_wins + 2
    ):
        return "candidate"
    if unique_entries >= 4 and blocked_entries >= 1 and delta_expectancy_rub > 0 and blocked_losses > blocked_wins:
        return "monitor_only"
    return "exploratory"


def recommended_action(status: str) -> str:
    if status == "candidate":
        return "research_backtest_next"
    if status == "monitor_only":
        return "collect_more_sample"
    return "do_not_promote"


def rank_score(delta_expectancy_rub: float, weight: float) -> float:
    return round(delta_expectancy_rub * weight, 3)


def row_sort_key(row: dict) -> tuple:
    status_order = {"candidate": 0, "monitor_only": 1, "exploratory": 2}
    sample_order = {"all_sample": 0, "latest_day": 1}
    return (
        status_order.get(str(row.get("candidate_status") or ""), 99),
        sample_order.get(str(row.get("sample") or ""), 99),
        -safe_float(row.get("rank_score")),
        -safe_float(row.get("delta_top3_loss_rub")),
        -safe_float(row.get("delta_profit_factor")),
        safe_float(row.get("block_rate_pct")),
        safe_float(row.get("threshold_ratio")),
        str(row.get("group") or ""),
    )


def build_sample_rows(trade_date: str, sample: str, rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for group, entries in grouped_entries(rows).items():
        entries = sorted(entries, key=lambda row: (str(row.get("closed_at") or ""), str(row.get("entry_id") or "")))
        head = entries[0]
        unique_entries = len(entries)
        days = len({str(entry.get("closed_at") or "")[:10] for entry in entries if str(entry.get("closed_at") or "")})
        base_net_rub = round(sum(safe_float(entry.get("net_rub")) for entry in entries), 2)
        base_expectancy_rub = round(base_net_rub / unique_entries, 2) if unique_entries else 0.0
        base_profit = profit_factor(entries)
        base_top3_loss = top3_loss_abs(entries)
        for threshold in THRESHOLD_VALUES:
            blocked = [entry for entry in entries if safe_float(entry.get("spread_to_stop_ratio")) > threshold]
            kept = [entry for entry in entries if safe_float(entry.get("spread_to_stop_ratio")) <= threshold]
            blocked_entries = len(blocked)
            kept_entries = len(kept)
            blocked_wins = sum(1 for entry in blocked if safe_float(entry.get("net_rub")) > 0)
            blocked_losses = sum(1 for entry in blocked if safe_float(entry.get("net_rub")) < 0)
            kept_net_rub = round(sum(safe_float(entry.get("net_rub")) for entry in kept), 2)
            kept_expectancy_rub = round(kept_net_rub / kept_entries, 2) if kept_entries else 0.0
            kept_profit = profit_factor(kept)
            kept_top3_loss = top3_loss_abs(kept)
            block_rate_pct = round(blocked_entries / unique_entries * 100.0, 2) if unique_entries else 0.0
            delta_net_rub = round(kept_net_rub - base_net_rub, 2)
            delta_expectancy_rub = round(kept_expectancy_rub - base_expectancy_rub, 2)
            delta_profit = round(kept_profit - base_profit, 4)
            delta_top3_loss = round(base_top3_loss - kept_top3_loss, 2)
            weight = confidence_weight(unique_entries, days, blocked_losses)
            status = candidate_status(
                sample=sample,
                unique_entries=unique_entries,
                days=days,
                blocked_entries=blocked_entries,
                block_rate_pct=block_rate_pct,
                base_expectancy_rub=base_expectancy_rub,
                delta_expectancy_rub=delta_expectancy_rub,
                kept_profit_factor=kept_profit,
                base_profit_factor=base_profit,
                kept_top3_loss_rub=kept_top3_loss,
                base_top3_loss_rub=base_top3_loss,
                blocked_losses=blocked_losses,
                blocked_wins=blocked_wins,
            )
            out.append(
                {
                    "trade_date": trade_date,
                    "sample": sample,
                    "experiment_rank": 0,
                    "group": group,
                    "portfolio_group": str(head.get("portfolio_group") or "").upper(),
                    "contour": str(head.get("contour") or "").upper(),
                    "family": str(head.get("family") or "").upper(),
                    "threshold_ratio": threshold,
                    "days": days,
                    "unique_entries": unique_entries,
                    "kept_entries": kept_entries,
                    "blocked_entries": blocked_entries,
                    "blocked_wins": blocked_wins,
                    "blocked_losses": blocked_losses,
                    "block_rate_pct": block_rate_pct,
                    "base_net_rub": base_net_rub,
                    "kept_net_rub": kept_net_rub,
                    "delta_net_rub": delta_net_rub,
                    "base_expectancy_rub": base_expectancy_rub,
                    "kept_expectancy_rub": kept_expectancy_rub,
                    "delta_expectancy_rub": delta_expectancy_rub,
                    "base_profit_factor": base_profit,
                    "kept_profit_factor": kept_profit,
                    "delta_profit_factor": delta_profit,
                    "base_top3_loss_rub": base_top3_loss,
                    "kept_top3_loss_rub": kept_top3_loss,
                    "delta_top3_loss_rub": delta_top3_loss,
                    "confidence_weight": weight,
                    "rank_score": rank_score(delta_expectancy_rub, weight),
                    "candidate_status": status,
                    "evaluation_state": "trade_level_counterfactual",
                    "recommended_action": recommended_action(status),
                    "note": (
                        f"Threshold {threshold} blocks {blocked_entries}/{unique_entries} entries and changes expectancy "
                        f"from {base_expectancy_rub} to {kept_expectancy_rub} RUB."
                    ),
                }
            )
    out.sort(key=row_sort_key)
    for index, row in enumerate(out, start=1):
        row["experiment_rank"] = index
    return out


def build_microstructure_counterfactual(*, trade_date: str, latest_day_rows: list[dict], all_sample_rows: list[dict]) -> list[dict]:
    rows = build_sample_rows(trade_date, "latest_day", latest_day_rows) + build_sample_rows(trade_date, "all_sample", all_sample_rows)
    rows.sort(key=row_sort_key)
    for index, row in enumerate(rows, start=1):
        row["experiment_rank"] = index
    return rows


def top_candidate_row(rows: list[dict]) -> dict:
    return sorted(rows, key=row_sort_key)[0] if rows else {}


def collection_status(*, unique_entries: int, candidate_count: int, monitor_only: int) -> str:
    if unique_entries <= 0:
        return "awaiting_entry_shadow_rows"
    if candidate_count > 0:
        return "counterfactual_candidate_ready"
    if monitor_only > 0:
        return "counterfactual_monitor_only"
    return "counterfactual_exploratory"


def summarize_research(rows: list[dict], trade_date: str, unique_entries: int) -> dict:
    by_sample: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for row in rows:
        sample = str(row.get("sample") or "unknown")
        status = str(row.get("candidate_status") or "unknown")
        by_sample[sample] = by_sample.get(sample, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
    top = top_candidate_row(rows)
    candidate_rows = sum(1 for row in rows if str(row.get("candidate_status") or "") == "candidate")
    monitor_rows = sum(1 for row in rows if str(row.get("candidate_status") or "") == "monitor_only")
    return {
        "trade_date": trade_date,
        "generated_at": now_str(),
        "rows": len(rows),
        "unique_entries": unique_entries,
        "latest_day_rows": sum(1 for row in rows if str(row.get("sample") or "") == "latest_day"),
        "all_sample_rows": sum(1 for row in rows if str(row.get("sample") or "") == "all_sample"),
        "candidate_count": candidate_rows,
        "monitor_only": monitor_rows,
        "evaluation_state": "trade_level_counterfactual",
        "collection_status": collection_status(
            unique_entries=unique_entries,
            candidate_count=candidate_rows,
            monitor_only=monitor_rows,
        ),
        "top_candidate_group": str(top.get("group") or ""),
        "top_candidate_threshold": maybe_float(top.get("threshold_ratio")),
        "top_candidate_sample": str(top.get("sample") or ""),
        "top_candidate_status": str(top.get("candidate_status") or ""),
        "by_sample": by_sample,
        "by_status": by_status,
    }


def markdown_table(rows: list[dict], columns: list[str], limit: int = 20) -> str:
    subset = rows[:limit]
    if not subset:
        return "No counterfactual rows yet."
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    body: list[str] = []
    for row in subset:
        values = ["" if row.get(key) in (None, "") else str(row.get(key)) for key in columns]
        body.append("| " + " | ".join(values) + " |")
    return "\n".join([header, sep, *body])


def render_markdown(rows: list[dict], summary: dict) -> str:
    return "\n".join(
        [
            "# Microstructure Counterfactual",
            "",
            f"- trade_date: {summary['trade_date']}",
            f"- unique_entries: {summary['unique_entries']}",
            f"- rows: {summary['rows']}",
            f"- candidate_count: {summary['candidate_count']}",
            f"- monitor_only: {summary['monitor_only']}",
            f"- evaluation_state: {summary['evaluation_state']}",
            f"- collection_status: {summary['collection_status']}",
            f"- top_candidate_group: {summary['top_candidate_group']}",
            f"- top_candidate_threshold: {summary['top_candidate_threshold']}",
            f"- top_candidate_sample: {summary['top_candidate_sample']}",
            f"- top_candidate_status: {summary['top_candidate_status']}",
            "",
            markdown_table(
                rows,
                [
                    "sample",
                    "experiment_rank",
                    "group",
                    "threshold_ratio",
                    "candidate_status",
                    "rank_score",
                    "delta_expectancy_rub",
                    "delta_top3_loss_rub",
                    "block_rate_pct",
                    "unique_entries",
                ],
                limit=20,
            ),
        ]
    )


def persist_outputs(*, rows: list[dict], summary: dict, directories: list[Path]) -> None:
    for directory in directories:
        write_csv_rows(directory / "microstructure_counterfactual.csv", rows, fieldnames=COUNTERFACTUAL_FIELDS)
        write_json(directory / "microstructure_counterfactual_summary.json", summary)
        write_text(directory / "microstructure_counterfactual.md", render_markdown(rows, summary))


def build_and_persist_microstructure_counterfactual(
    *,
    project_root: Path,
    trade_date: str,
    all_entry_shadow_rows: list[dict],
    research_dir: Path | None = None,
    latest_dir: Path | None = None,
    bundle_dir: Path | None = None,
) -> tuple[list[dict], dict]:
    trade_date = resolve_trade_date(project_root, trade_date)
    research_dir = research_dir or project_root / "reports" / "autonomy" / "research" / trade_date
    latest_dir = latest_dir or project_root / "reports" / "autonomy" / "latest"
    all_entries = collapse_entry_rows(all_entry_shadow_rows)
    latest_day_entries = filter_trade_date(all_entries, trade_date)
    rows = build_microstructure_counterfactual(
        trade_date=trade_date,
        latest_day_rows=latest_day_entries,
        all_sample_rows=all_entries,
    )
    summary = summarize_research(rows, trade_date, len(all_entries))
    directories = [research_dir, latest_dir]
    if bundle_dir is not None:
        directories.append(bundle_dir)
    persist_outputs(rows=rows, summary=summary, directories=directories)
    return rows, summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build trade-level microstructure counterfactual research from entry-shadow rows.")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--run-name", default="v7_live_20260525")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_root = args.project_root.resolve()
    run_dir = project_root / "reports" / "paper_runs" / args.run_name
    from daily_autonomy_runner import load_entry_shadow_rows  # local import avoids extra startup coupling

    rows, summary = build_and_persist_microstructure_counterfactual(
        project_root=project_root,
        trade_date=str(args.trade_date or ""),
        all_entry_shadow_rows=load_entry_shadow_rows(run_dir),
    )
    print(
        f"[{now_str()}] microstructure_counterfactual trade_date={summary['trade_date']} "
        f"rows={summary['rows']} candidates={summary['candidate_count']}",
        flush=True,
    )
    if rows:
        print(
            f"top_candidate={summary['top_candidate_group']} threshold={summary['top_candidate_threshold']} sample={summary['top_candidate_sample']}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
