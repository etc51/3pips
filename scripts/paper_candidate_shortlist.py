from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from autonomy_common import now_str, safe_float, safe_int, write_csv_rows, write_json, write_text
from research_strategy_registry import normalize_bool, resolve_trade_date, safe_read_rows


SHORTLIST_FIELDS = [
    "trade_date",
    "shortlist_rank",
    "registry_id",
    "candidate_label",
    "paper_route",
    "registry_status",
    "validation_state",
    "runtime_ready",
    "shortlist_status",
    "blocking_reason",
    "stability_score",
    "autopromote_ready",
    "priority",
    "registry_rank",
    "scenario_anchor",
    "recommended_use",
    "required_features",
    "stability_days",
    "beat_base_days",
    "beat_base_pct",
    "delta_total_rub",
    "latest_day_delta_rub",
    "worst_day_rub",
    "sample_trades",
    "sample_win_rate_pct",
    "sample_expectancy_rub",
    "sample_profit_factor",
    "latest_day_expectancy_rub",
    "latest_day_profit_factor",
    "selection_run_date",
    "selection_age_days",
    "selection_fresh",
    "target_contract",
    "plus1_contract",
    "selection_method",
    "selected_ok",
    "orderbook_source_effective",
    "warning",
    "run_id",
    "family",
    "instrument_type",
    "execution_params_json",
    "evidence_json",
]

OPERATIONAL_ROUTES = {"paper_autopolicy", "leadlag_orderbook_monitor", "candidate_runtime"}
RUNTIME_READY_STATUSES = {"paper_candidate"}
SELECTION_FRESH_MAX_AGE_DAYS = 3
AUTOPOLICY_MIN_SAMPLE_TRADES = 10
AUTOPOLICY_MIN_SAMPLE_PROFIT_FACTOR = 1.0


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def parse_trade_date(value: object) -> date | None:
    text = str(value or "").strip()
    if len(text) != 10:
        return None
    try:
        return date.fromisoformat(text)
    except Exception:
        return None


def maybe_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except Exception:
        return None


def maybe_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return round(float(value), 6)
    except Exception:
        return None


def bool_text(value: bool) -> str:
    return "True" if value else "False"


def json_text(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def load_registry_rows(project_root: Path) -> list[dict]:
    return (
        safe_read_rows(project_root / "data" / "processed" / "research_strategy_registry.csv")
        or safe_read_rows(project_root / "reports" / "autonomy" / "latest" / "research_strategy_registry.csv")
    )


def load_contract_selection(project_root: Path) -> dict:
    rows = safe_read_rows(project_root / "reports" / "paper_contract_selection.csv")
    if not rows:
        return {}
    ranked: list[tuple[date, int, dict]] = []
    for index, row in enumerate(rows):
        run_date = parse_trade_date(row.get("run_date")) or date.min
        ranked.append((run_date, index, row))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return dict(ranked[0][2])


def compute_stability_score(row: dict) -> float:
    beat_base_pct = safe_float(row.get("beat_base_pct"))
    stability_days = max(safe_int(row.get("stability_days")), 0)
    positive_days = max(safe_int(row.get("positive_days")), 0)
    delta_total_rub = safe_float(row.get("delta_total_rub"))
    median_daily_net_rub = safe_float(row.get("median_daily_net_rub"))
    latest_day_delta_rub = safe_float(row.get("latest_day_delta_rub"))
    test_profit_factor = safe_float(row.get("test_profit_factor"))
    worst_day_rub = safe_float(row.get("worst_day_rub"))
    test_max_drawdown = safe_float(row.get("test_max_drawdown"))
    sample_trades = max(safe_int(row.get("sample_trades")), 0)
    sample_expectancy_rub = safe_float(row.get("sample_expectancy_rub"))
    sample_profit_factor = safe_float(row.get("sample_profit_factor"))
    latest_day_expectancy_rub = safe_float(row.get("latest_day_expectancy_rub"))
    latest_day_profit_factor = safe_float(row.get("latest_day_profit_factor"))
    priority = max(safe_int(row.get("priority")), 0)

    score = 0.0
    score += 25.0 * clamp(beat_base_pct / 100.0, 0.0, 1.0)
    score += 15.0 * clamp(stability_days / 5.0, 0.0, 1.0)
    score += 10.0 * clamp(positive_days / max(stability_days, 1), 0.0, 1.0)
    score += 15.0 * clamp(delta_total_rub / 5000.0, -1.0, 1.0)
    score += 10.0 * clamp(median_daily_net_rub / 3000.0, -1.0, 1.0)
    score += 10.0 * clamp(latest_day_delta_rub / 3000.0, -1.0, 1.0)
    score += 10.0 * clamp(test_profit_factor / 2.0, 0.0, 1.0)
    score += 4.0 * clamp(sample_trades / 30.0, 0.0, 1.0)
    score += 8.0 * clamp(sample_expectancy_rub / 400.0, -1.0, 1.0)
    score += 6.0 * clamp((sample_profit_factor - 1.0) / 1.5, -1.0, 1.0)
    score += 3.0 * clamp(latest_day_expectancy_rub / 250.0, -1.0, 1.0)
    score += 2.0 * clamp(latest_day_profit_factor - 1.0, -1.0, 1.0)
    score -= 15.0 * clamp(max(0.0, -worst_day_rub) / 3000.0, 0.0, 1.0)
    score -= 10.0 * clamp(abs(min(test_max_drawdown, 0.0)) / 0.15, 0.0, 1.0)
    score += 5.0 if normalize_bool(row.get("autopromote_ready")) else 0.0
    score += 5.0 if str(row.get("status") or "") == "paper_candidate" else 0.0
    score += 3.0 if str(row.get("paper_route") or "") == "paper_autopolicy" else 0.0
    score += min(priority, 100) * 0.1
    return round(clamp(score, 0.0, 100.0), 3)


def build_contract_selection_context(selection: dict, trade_date: str) -> dict:
    run_date = parse_trade_date(selection.get("run_date"))
    trade_day = parse_trade_date(trade_date)
    age_days = (trade_day - run_date).days if trade_day is not None and run_date is not None else None
    selected_ok = normalize_bool(selection.get("selected_ok"))
    selection_fresh = bool(selected_ok and age_days is not None and 0 <= age_days <= SELECTION_FRESH_MAX_AGE_DAYS)
    return {
        "selection_run_date": str(selection.get("run_date") or ""),
        "selection_age_days": age_days,
        "selection_fresh": selection_fresh,
        "target_contract": str(selection.get("target_contract") or ""),
        "plus1_contract": str(selection.get("plus1_contract") or ""),
        "selection_method": str(selection.get("selection_method") or ""),
        "selected_ok": selected_ok,
        "orderbook_source_effective": str(selection.get("orderbook_source_effective") or ""),
        "warning": str(selection.get("warning") or ""),
        "run_id": str(selection.get("run_id") or ""),
    }


def paper_autopolicy_quality_gate_reason(row: dict) -> str:
    sample_trades = maybe_int(row.get("sample_trades"))
    sample_expectancy_rub = maybe_float(row.get("sample_expectancy_rub"))
    sample_profit_factor = maybe_float(row.get("sample_profit_factor"))
    if sample_trades is None or sample_expectancy_rub is None or sample_profit_factor is None:
        return "missing_sample_quality_metrics"
    if sample_trades < AUTOPOLICY_MIN_SAMPLE_TRADES:
        return "insufficient_sample_trades"
    if sample_expectancy_rub <= 0:
        return "non_positive_sample_expectancy"
    if sample_profit_factor < AUTOPOLICY_MIN_SAMPLE_PROFIT_FACTOR:
        return "sample_profit_factor_below_1"
    return ""


def shortlist_decision(row: dict, contract_ctx: dict) -> tuple[bool, str, str]:
    route = str(row.get("paper_route") or "")
    status = str(row.get("status") or "")
    validation_state = str(row.get("validation_state") or "")

    if route == "paper_autopolicy":
        if status not in RUNTIME_READY_STATUSES:
            return False, "review_only", "status_not_paper_candidate"
        quality_gate_reason = paper_autopolicy_quality_gate_reason(row)
        if quality_gate_reason:
            return False, "review_only", quality_gate_reason
        return True, "ready_now", ""

    if route == "leadlag_orderbook_monitor":
        if status not in RUNTIME_READY_STATUSES:
            return False, "review_only", "status_not_paper_candidate"
        if validation_state not in {"unit_corrected_positive", "consensus_backed"}:
            return False, "review_only", "validation_not_strong_enough"
        if not contract_ctx.get("selection_run_date"):
            return False, "waiting_contract_selection", "missing_contract_selection"
        if not contract_ctx.get("selected_ok"):
            return False, "waiting_contract_selection", "contract_selection_not_ok"
        if not contract_ctx.get("selection_fresh"):
            return False, "waiting_contract_selection", "stale_contract_selection"
        if not contract_ctx.get("target_contract") or not contract_ctx.get("plus1_contract"):
            return False, "waiting_contract_selection", "contract_pair_missing"
        return True, "ready_now", ""

    if route == "candidate_runtime":
        return False, "review_only", "candidate_runtime_needs_manual_release"

    return False, "review_only", "unsupported_route"


def registry_rows_to_shortlist(rows: list[dict], trade_date: str, contract_selection: dict) -> list[dict]:
    contract_ctx = build_contract_selection_context(contract_selection, trade_date)
    out: list[dict] = []
    for row in rows:
        route = str(row.get("paper_route") or "")
        status = str(row.get("status") or "")
        if route not in OPERATIONAL_ROUTES:
            continue
        if status not in {"paper_candidate", "validated"}:
            continue
        runtime_ready, shortlist_status, blocking_reason = shortlist_decision(row, contract_ctx)
        row_contract_ctx = contract_ctx if route == "leadlag_orderbook_monitor" else {
            "selection_run_date": "",
            "selection_age_days": None,
            "selection_fresh": False,
            "target_contract": "",
            "plus1_contract": "",
            "selection_method": "",
            "selected_ok": False,
            "orderbook_source_effective": "",
            "warning": "",
            "run_id": "",
        }
        out.append(
            {
                "trade_date": trade_date,
                "shortlist_rank": 0,
                "registry_id": str(row.get("registry_id") or ""),
                "candidate_label": str(row.get("candidate_label") or ""),
                "paper_route": route,
                "registry_status": status,
                "validation_state": str(row.get("validation_state") or ""),
                "runtime_ready": runtime_ready,
                "shortlist_status": shortlist_status,
                "blocking_reason": blocking_reason,
                "stability_score": compute_stability_score(row),
                "autopromote_ready": normalize_bool(row.get("autopromote_ready")),
                "priority": maybe_int(row.get("priority")),
                "registry_rank": maybe_int(row.get("rank")),
                "scenario_anchor": str(row.get("scenario_anchor") or ""),
                "recommended_use": str(row.get("recommended_use") or ""),
                "required_features": str(row.get("required_features") or ""),
                "stability_days": maybe_int(row.get("stability_days")),
                "beat_base_days": maybe_int(row.get("beat_base_days")),
                "beat_base_pct": maybe_float(row.get("beat_base_pct")),
                "delta_total_rub": maybe_float(row.get("delta_total_rub")),
                "latest_day_delta_rub": maybe_float(row.get("latest_day_delta_rub")),
                "worst_day_rub": maybe_float(row.get("worst_day_rub")),
                "sample_trades": maybe_int(row.get("sample_trades")),
                "sample_win_rate_pct": maybe_float(row.get("sample_win_rate_pct")),
                "sample_expectancy_rub": maybe_float(row.get("sample_expectancy_rub")),
                "sample_profit_factor": maybe_float(row.get("sample_profit_factor")),
                "latest_day_expectancy_rub": maybe_float(row.get("latest_day_expectancy_rub")),
                "latest_day_profit_factor": maybe_float(row.get("latest_day_profit_factor")),
                "selection_run_date": row_contract_ctx["selection_run_date"],
                "selection_age_days": row_contract_ctx["selection_age_days"],
                "selection_fresh": row_contract_ctx["selection_fresh"],
                "target_contract": row_contract_ctx["target_contract"],
                "plus1_contract": row_contract_ctx["plus1_contract"],
                "selection_method": row_contract_ctx["selection_method"],
                "selected_ok": row_contract_ctx["selected_ok"],
                "orderbook_source_effective": row_contract_ctx["orderbook_source_effective"],
                "warning": row_contract_ctx["warning"],
                "run_id": row_contract_ctx["run_id"],
                "family": str(row.get("family") or ""),
                "instrument_type": str(row.get("instrument_type") or ""),
                "execution_params_json": str(row.get("execution_params_json") or ""),
                "evidence_json": str(row.get("evidence_json") or ""),
            }
        )
    out.sort(
        key=lambda row: (
            0 if bool(row.get("runtime_ready")) else 1,
            -safe_float(row.get("stability_score")),
            0 if bool(row.get("autopromote_ready")) else 1,
            -safe_int(row.get("priority")),
            safe_int(row.get("registry_rank"), 10**6),
            str(row.get("candidate_label") or ""),
        )
    )
    for index, row in enumerate(out, start=1):
        row["shortlist_rank"] = index
        row["runtime_ready"] = bool_text(bool(row.get("runtime_ready")))
        row["autopromote_ready"] = bool_text(bool(row.get("autopromote_ready")))
        row["selection_fresh"] = bool_text(bool(row.get("selection_fresh")))
        row["selected_ok"] = bool_text(bool(row.get("selected_ok")))
    return out


def summarize_shortlist(rows: list[dict], trade_date: str) -> dict:
    by_route: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for row in rows:
        by_route[str(row.get("paper_route") or "unknown")] = by_route.get(str(row.get("paper_route") or "unknown"), 0) + 1
        by_status[str(row.get("shortlist_status") or "unknown")] = by_status.get(str(row.get("shortlist_status") or "unknown"), 0) + 1
    return {
        "trade_date": trade_date,
        "generated_at": now_str(),
        "rows": len(rows),
        "runtime_ready": sum(1 for row in rows if normalize_bool(row.get("runtime_ready"))),
        "paper_autopolicy_ready": sum(
            1 for row in rows if str(row.get("paper_route") or "") == "paper_autopolicy" and normalize_bool(row.get("runtime_ready"))
        ),
        "leadlag_ready": sum(
            1 for row in rows if str(row.get("paper_route") or "") == "leadlag_orderbook_monitor" and normalize_bool(row.get("runtime_ready"))
        ),
        "waiting_contract_selection": by_status.get("waiting_contract_selection", 0),
        "review_only": by_status.get("review_only", 0),
        "by_route": by_route,
        "by_status": by_status,
    }


def markdown_table(rows: list[dict], columns: list[str], limit: int = 20) -> str:
    subset = rows[:limit]
    if not subset:
        return "No shortlist rows."
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    body = []
    for row in subset:
        values = []
        for key in columns:
            value = row.get(key)
            values.append("" if value in (None, "") else str(value))
        body.append("| " + " | ".join(values) + " |")
    return "\n".join([header, sep, *body])


def render_shortlist_markdown(rows: list[dict], summary: dict) -> str:
    return "\n".join(
        [
            "# Paper Candidate Shortlist",
            "",
            f"- trade_date: {summary['trade_date']}",
            f"- rows: {summary['rows']}",
            f"- runtime_ready: {summary['runtime_ready']}",
            f"- paper_autopolicy_ready: {summary['paper_autopolicy_ready']}",
            f"- leadlag_ready: {summary['leadlag_ready']}",
            f"- waiting_contract_selection: {summary['waiting_contract_selection']}",
            f"- review_only: {summary['review_only']}",
            "",
            markdown_table(
                rows,
                [
                    "shortlist_rank",
                    "candidate_label",
                    "paper_route",
                    "runtime_ready",
                    "shortlist_status",
                    "blocking_reason",
                    "stability_score",
                    "sample_expectancy_rub",
                    "sample_profit_factor",
                    "target_contract",
                    "plus1_contract",
                ],
            ),
        ]
    )


def persist_shortlist_outputs(*, rows: list[dict], summary: dict, directories: list[Path]) -> None:
    for directory in directories:
        write_csv_rows(directory / "paper_candidate_shortlist.csv", rows, fieldnames=SHORTLIST_FIELDS)
        write_json(directory / "paper_candidate_shortlist_summary.json", summary)
        write_text(directory / "paper_candidate_shortlist.md", render_shortlist_markdown(rows, summary))


def build_and_persist_paper_candidate_shortlist(
    *,
    project_root: Path,
    trade_date: str,
    registry_rows: list[dict] | None = None,
    research_dir: Path | None = None,
    latest_dir: Path | None = None,
    bundle_dir: Path | None = None,
) -> tuple[list[dict], dict]:
    trade_date = resolve_trade_date(project_root, trade_date)
    research_dir = research_dir or project_root / "reports" / "autonomy" / "research" / trade_date
    latest_dir = latest_dir or project_root / "reports" / "autonomy" / "latest"
    registry_rows = list(registry_rows) if registry_rows is not None else load_registry_rows(project_root)
    contract_selection = load_contract_selection(project_root)
    rows = registry_rows_to_shortlist(registry_rows, trade_date, contract_selection)
    summary = summarize_shortlist(rows, trade_date)
    directories = [research_dir, latest_dir]
    if bundle_dir is not None:
        directories.append(bundle_dir)
    persist_shortlist_outputs(rows=rows, summary=summary, directories=directories)
    return rows, summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an operational paper candidate shortlist from the research strategy registry.")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--trade-date", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows, summary = build_and_persist_paper_candidate_shortlist(
        project_root=args.project_root,
        trade_date=str(args.trade_date or ""),
    )
    print(
        f"[{now_str()}] paper_candidate_shortlist trade_date={summary['trade_date']} "
        f"rows={summary['rows']} runtime_ready={summary['runtime_ready']}",
        flush=True,
    )
    if rows:
        print(f"top_candidate={rows[0].get('candidate_label')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
