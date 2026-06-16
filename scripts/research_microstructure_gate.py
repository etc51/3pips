from __future__ import annotations

import argparse
import json
from pathlib import Path

from autonomy_common import now_str, safe_float, safe_int, write_csv_rows, write_json, write_text
from research_strategy_registry import resolve_trade_date


THRESHOLD_VALUES = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5]

EXPERIMENT_FIELDS = [
    "trade_date",
    "sample",
    "experiment_rank",
    "group",
    "portfolio_group",
    "contour",
    "family",
    "threshold_ratio",
    "review_events",
    "dominates_events",
    "heavy_events",
    "watch_events",
    "blocked_events",
    "blocked_pct",
    "allowed_events",
    "allowed_review_pct",
    "blocked_dominates_capture_pct",
    "blocked_heavy_capture_pct",
    "toxic_capture_pct",
    "watch_preserve_pct",
    "trades",
    "net_rub",
    "expectancy_rub",
    "experiment_score",
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


def maybe_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except Exception:
        return None


def filter_snapshot_date(rows: list[dict], trade_date: str) -> list[dict]:
    return [row for row in rows if str(row.get("snapshot_time") or "").startswith(trade_date)]


def metrics_map(rows: list[dict], key_fn) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        key = str(key_fn(row) or "")
        if key:
            grouped.setdefault(key, []).append(row)
    out: dict[str, dict] = {}
    for key, items in grouped.items():
        trades = len(items)
        net_rub = round(sum(safe_float(item.get("net_rub")) for item in items), 2)
        out[key] = {
            "group": key,
            "trades": trades,
            "net_rub": net_rub,
            "expectancy_rub": round(net_rub / trades, 2) if trades else 0.0,
        }
    return out


def markdown_table(rows: list[dict], columns: list[str], limit: int = 20) -> str:
    subset = rows[:limit]
    if not subset:
        return "No microstructure gate experiments."
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    body: list[str] = []
    for row in subset:
        values: list[str] = []
        for key in columns:
            value = row.get(key)
            values.append("" if value in (None, "") else str(value))
        body.append("| " + " | ".join(values) + " |")
    return "\n".join([header, sep, *body])


def threshold_score(
    *,
    toxic_capture_pct: float,
    watch_preserve_pct: float,
    allowed_review_pct: float,
    negative_slice: bool,
) -> float:
    score = toxic_capture_pct * 0.7 + watch_preserve_pct * 0.3
    score -= abs(allowed_review_pct - 35.0) * 0.4
    if negative_slice:
        score += 10.0
    return round(score, 3)


def candidate_status(*, review_events: int, negative_slice: bool, toxic_capture_pct: float, watch_preserve_pct: float) -> str:
    if review_events < 200:
        return "insufficient_sample"
    if negative_slice and toxic_capture_pct >= 60.0 and watch_preserve_pct >= 50.0:
        return "backtest_candidate"
    if toxic_capture_pct >= 50.0 and watch_preserve_pct >= 35.0:
        return "monitor_only"
    return "insufficient_signal"


def recommended_action(status: str) -> str:
    if status == "backtest_candidate":
        return "research_backtest_next"
    if status == "monitor_only":
        return "collect_more_sample"
    if status == "insufficient_sample":
        return "wait_for_more_review_events"
    return "do_not_promote"


def rows_to_grouped(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        group = str(row.get("group") or "").strip().upper()
        if not group:
            continue
        if maybe_float(row.get("spread_to_stop_ratio")) is None:
            continue
        grouped.setdefault(group, []).append(dict(row, group=group))
    return grouped


def build_sample_rows(
    *,
    trade_date: str,
    sample: str,
    rows: list[dict],
    trade_metrics_by_group: dict[str, dict],
) -> list[dict]:
    grouped = rows_to_grouped(rows)
    out: list[dict] = []
    for group, items in grouped.items():
        sample_row = items[0]
        dominates_total = sum(1 for item in items if str(item.get("spread_class") or "") == "SPREAD_DOMINATES")
        heavy_total = sum(1 for item in items if str(item.get("spread_class") or "") == "SPREAD_HEAVY")
        watch_total = sum(1 for item in items if str(item.get("spread_class") or "") == "SPREAD_WATCH")
        toxic_total = dominates_total + heavy_total
        review_events = len(items)
        metrics = trade_metrics_by_group.get(group, {})
        net_rub = safe_float(metrics.get("net_rub"))
        expectancy_rub = safe_float(metrics.get("expectancy_rub"))
        negative_slice = net_rub < 0 or expectancy_rub < 0
        for threshold in THRESHOLD_VALUES:
            blocked = [item for item in items if safe_float(item.get("spread_to_stop_ratio")) > threshold]
            allowed = [item for item in items if safe_float(item.get("spread_to_stop_ratio")) <= threshold]
            blocked_dominates = sum(1 for item in blocked if str(item.get("spread_class") or "") == "SPREAD_DOMINATES")
            blocked_heavy = sum(1 for item in blocked if str(item.get("spread_class") or "") == "SPREAD_HEAVY")
            allowed_watch = sum(1 for item in allowed if str(item.get("spread_class") or "") == "SPREAD_WATCH")
            blocked_events = len(blocked)
            allowed_events = len(allowed)
            blocked_pct = round(blocked_events / review_events * 100.0, 2) if review_events else 0.0
            allowed_review_pct = round(allowed_events / review_events * 100.0, 2) if review_events else 0.0
            blocked_dominates_capture_pct = round(blocked_dominates / dominates_total * 100.0, 2) if dominates_total else 0.0
            blocked_heavy_capture_pct = round(blocked_heavy / heavy_total * 100.0, 2) if heavy_total else 0.0
            toxic_capture_pct = round((blocked_dominates + blocked_heavy) / toxic_total * 100.0, 2) if toxic_total else 0.0
            watch_preserve_pct = round(allowed_watch / watch_total * 100.0, 2) if watch_total else 0.0
            status = candidate_status(
                review_events=review_events,
                negative_slice=negative_slice,
                toxic_capture_pct=toxic_capture_pct,
                watch_preserve_pct=watch_preserve_pct,
            )
            score = threshold_score(
                toxic_capture_pct=toxic_capture_pct,
                watch_preserve_pct=watch_preserve_pct,
                allowed_review_pct=allowed_review_pct,
                negative_slice=negative_slice,
            )
            out.append(
                {
                    "trade_date": trade_date,
                    "sample": sample,
                    "experiment_rank": 0,
                    "group": group,
                    "portfolio_group": str(sample_row.get("portfolio_group") or "").upper(),
                    "contour": str(sample_row.get("contour") or "").lower(),
                    "family": str(sample_row.get("family") or "").upper(),
                    "threshold_ratio": threshold,
                    "review_events": review_events,
                    "dominates_events": dominates_total,
                    "heavy_events": heavy_total,
                    "watch_events": watch_total,
                    "blocked_events": blocked_events,
                    "blocked_pct": blocked_pct,
                    "allowed_events": allowed_events,
                    "allowed_review_pct": allowed_review_pct,
                    "blocked_dominates_capture_pct": blocked_dominates_capture_pct,
                    "blocked_heavy_capture_pct": blocked_heavy_capture_pct,
                    "toxic_capture_pct": toxic_capture_pct,
                    "watch_preserve_pct": watch_preserve_pct,
                    "trades": safe_int(metrics.get("trades")),
                    "net_rub": net_rub,
                    "expectancy_rub": expectancy_rub,
                    "experiment_score": score,
                    "candidate_status": status,
                    "evaluation_state": "review_event_proxy",
                    "recommended_action": recommended_action(status),
                    "note": (
                        f"Blocks {toxic_capture_pct}% of toxic review events while preserving {watch_preserve_pct}% "
                        f"of watch events on a {'negative' if negative_slice else 'non-negative'} slice."
                    ),
                }
            )
    out.sort(
        key=lambda row: (
            0 if str(row.get("candidate_status") or "") == "backtest_candidate" else 1 if str(row.get("candidate_status") or "") == "monitor_only" else 2,
            -safe_float(row.get("experiment_score")),
            safe_float(row.get("threshold_ratio")),
            str(row.get("group") or ""),
        )
    )
    for index, row in enumerate(out, start=1):
        row["experiment_rank"] = index
    return out


def build_microstructure_gate_research(
    *,
    trade_date: str,
    latest_day_rows: list[dict],
    all_sample_rows: list[dict],
    latest_day_trade_metrics_by_group: dict[str, dict],
    all_sample_trade_metrics_by_group: dict[str, dict],
) -> list[dict]:
    rows: list[dict] = []
    rows.extend(
        build_sample_rows(
            trade_date=trade_date,
            sample="latest_day",
            rows=latest_day_rows,
            trade_metrics_by_group=latest_day_trade_metrics_by_group,
        )
    )
    rows.extend(
        build_sample_rows(
            trade_date=trade_date,
            sample="all_sample",
            rows=all_sample_rows,
            trade_metrics_by_group=all_sample_trade_metrics_by_group,
        )
    )
    rows.sort(
        key=lambda row: (
            0 if str(row.get("sample") or "") == "latest_day" else 1,
            safe_int(row.get("experiment_rank"), 10**6),
            str(row.get("group") or ""),
        )
    )
    return rows


def top_candidate_row(rows: list[dict]) -> dict:
    if not rows:
        return {}
    status_order = {
        "backtest_candidate": 0,
        "monitor_only": 1,
        "insufficient_signal": 2,
        "insufficient_sample": 3,
    }
    sample_order = {
        "all_sample": 0,
        "latest_day": 1,
    }
    ranked = sorted(
        rows,
        key=lambda row: (
            status_order.get(str(row.get("candidate_status") or ""), 99),
            -safe_float(row.get("experiment_score")),
            sample_order.get(str(row.get("sample") or ""), 99),
            safe_float(row.get("threshold_ratio")),
            str(row.get("group") or ""),
        ),
    )
    return ranked[0]


def collection_status(*, rows_count: int, backtest_candidates: int, monitor_only: int) -> str:
    if rows_count <= 0:
        return "awaiting_review_rows"
    if backtest_candidates > 0:
        return "proxy_backtest_candidate_ready"
    if monitor_only > 0:
        return "proxy_monitor_only"
    return "proxy_exploratory"


def summarize_research(rows: list[dict], trade_date: str) -> dict:
    by_sample: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for row in rows:
        sample = str(row.get("sample") or "unknown")
        status = str(row.get("candidate_status") or "unknown")
        by_sample[sample] = by_sample.get(sample, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
    top = top_candidate_row(rows)
    backtest_candidates = sum(1 for row in rows if str(row.get("candidate_status") or "") == "backtest_candidate")
    monitor_rows = sum(1 for row in rows if str(row.get("candidate_status") or "") == "monitor_only")
    return {
        "trade_date": trade_date,
        "generated_at": now_str(),
        "rows": len(rows),
        "latest_day_rows": sum(1 for row in rows if str(row.get("sample") or "") == "latest_day"),
        "all_sample_rows": sum(1 for row in rows if str(row.get("sample") or "") == "all_sample"),
        "backtest_candidates": backtest_candidates,
        "monitor_only": monitor_rows,
        "evaluation_state": "review_event_proxy",
        "collection_status": collection_status(
            rows_count=len(rows),
            backtest_candidates=backtest_candidates,
            monitor_only=monitor_rows,
        ),
        "top_candidate_group": str(top.get("group") or ""),
        "top_candidate_threshold": maybe_float(top.get("threshold_ratio")),
        "top_candidate_sample": str(top.get("sample") or ""),
        "top_candidate_status": str(top.get("candidate_status") or ""),
        "by_sample": by_sample,
        "by_status": by_status,
    }


def render_markdown(rows: list[dict], summary: dict) -> str:
    return "\n".join(
        [
            "# Microstructure Gate Research",
            "",
            f"- trade_date: {summary['trade_date']}",
            f"- rows: {summary['rows']}",
            f"- latest_day_rows: {summary['latest_day_rows']}",
            f"- all_sample_rows: {summary['all_sample_rows']}",
            f"- backtest_candidates: {summary['backtest_candidates']}",
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
                    "experiment_score",
                    "toxic_capture_pct",
                    "watch_preserve_pct",
                    "allowed_review_pct",
                    "net_rub",
                    "expectancy_rub",
                ],
                limit=20,
            ),
        ]
    )


def persist_outputs(*, rows: list[dict], summary: dict, directories: list[Path]) -> None:
    for directory in directories:
        write_csv_rows(directory / "microstructure_gate_research.csv", rows, fieldnames=EXPERIMENT_FIELDS)
        write_json(directory / "microstructure_gate_research_summary.json", summary)
        write_text(directory / "microstructure_gate_research.md", render_markdown(rows, summary))


def build_and_persist_microstructure_gate_research(
    *,
    project_root: Path,
    trade_date: str,
    all_wide_spread_rows: list[dict],
    all_trade_rows: list[dict],
    research_dir: Path | None = None,
    latest_dir: Path | None = None,
    bundle_dir: Path | None = None,
) -> tuple[list[dict], dict]:
    trade_date = resolve_trade_date(project_root, trade_date)
    research_dir = research_dir or project_root / "reports" / "autonomy" / "research" / trade_date
    latest_dir = latest_dir or project_root / "reports" / "autonomy" / "latest"
    latest_day_rows = filter_snapshot_date(all_wide_spread_rows, trade_date)
    group_key_fn = lambda row: f"{str(row.get('portfolio_group') or '').upper()}/{str(row.get('contour') or '').upper()}::{str(row.get('family') or '').upper()}"
    latest_day_trade_metrics_by_group = metrics_map(
        [row for row in all_trade_rows if str(row.get("closed_at") or row.get("trade_date") or "").startswith(trade_date)],
        group_key_fn,
    )
    all_sample_trade_metrics_by_group = metrics_map(all_trade_rows, group_key_fn)
    rows = build_microstructure_gate_research(
        trade_date=trade_date,
        latest_day_rows=latest_day_rows,
        all_sample_rows=all_wide_spread_rows,
        latest_day_trade_metrics_by_group=latest_day_trade_metrics_by_group,
        all_sample_trade_metrics_by_group=all_sample_trade_metrics_by_group,
    )
    summary = summarize_research(rows, trade_date)
    directories = [research_dir, latest_dir]
    if bundle_dir is not None:
        directories.append(bundle_dir)
    persist_outputs(rows=rows, summary=summary, directories=directories)
    return rows, summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build research-only microstructure gate experiments from wide-spread review snapshots.")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--run-name", default="v7_live_20260525")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_root = args.project_root.resolve()
    run_dir = project_root / "reports" / "paper_runs" / args.run_name
    from daily_autonomy_runner import load_primary_trades, load_wide_spread_reviews  # local import avoids extra startup coupling

    all_trade_rows = load_primary_trades(run_dir)
    all_wide_spread_rows = load_wide_spread_reviews(run_dir)
    rows, summary = build_and_persist_microstructure_gate_research(
        project_root=project_root,
        trade_date=str(args.trade_date or ""),
        all_wide_spread_rows=all_wide_spread_rows,
        all_trade_rows=all_trade_rows,
    )
    print(
        f"[{now_str()}] microstructure_gate_research trade_date={summary['trade_date']} "
        f"rows={summary['rows']} backtest_candidates={summary['backtest_candidates']}",
        flush=True,
    )
    if summary.get("top_candidate_group"):
        print(
            f"top_candidate={summary.get('top_candidate_group')} "
            f"threshold={summary.get('top_candidate_threshold')} "
            f"sample={summary.get('top_candidate_sample')}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
