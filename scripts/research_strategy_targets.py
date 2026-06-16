from __future__ import annotations

import argparse
import json
from pathlib import Path

from autonomy_common import now_str, safe_float, safe_int, write_csv_rows, write_json, write_text
from paper_candidate_shortlist import load_contract_selection, parse_trade_date
from research_strategy_registry import normalize_bool, resolve_trade_date, safe_read_rows


TARGET_FIELDS = [
    "trade_date",
    "target_rank",
    "target_id",
    "shortlist_id",
    "registry_id",
    "candidate_label",
    "paper_route",
    "target_kind",
    "target_status",
    "launch_ready",
    "blocking_reason",
    "stability_score",
    "priority",
    "shortlist_rank",
    "registry_status",
    "validation_state",
    "scenario_anchor",
    "recommended_use",
    "required_features",
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
    "strategy_mode",
    "portfolio_mode",
    "selected_feature_set",
    "selected_threshold",
    "horizon",
    "execution_params_json",
    "binding_payload_json",
    "evidence_json",
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


def bool_text(value: bool) -> str:
    return "True" if value else "False"


def json_text(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def load_shortlist_rows(project_root: Path) -> list[dict]:
    return (
        safe_read_rows(project_root / "reports" / "autonomy" / "latest" / "paper_candidate_shortlist.csv")
        or safe_read_rows(project_root / "reports" / "autonomy" / "research" / "latest" / "paper_candidate_shortlist.csv")
    )


def decode_json_dict(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def fallback_contract_context(project_root: Path, trade_date: str) -> dict:
    selection = load_contract_selection(project_root)
    trade_day = parse_trade_date(trade_date)
    run_day = parse_trade_date(selection.get("run_date"))
    age_days = (trade_day - run_day).days if trade_day and run_day else None
    selected_ok = normalize_bool(selection.get("selected_ok"))
    selection_fresh = bool(selected_ok and age_days is not None and 0 <= age_days <= 3)
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


def shortlist_row_contract_context(row: dict, fallback: dict) -> dict:
    if str(row.get("target_contract") or "").strip() or str(row.get("selection_run_date") or "").strip():
        return {
            "selection_run_date": str(row.get("selection_run_date") or ""),
            "selection_age_days": maybe_int(row.get("selection_age_days")),
            "selection_fresh": normalize_bool(row.get("selection_fresh")),
            "target_contract": str(row.get("target_contract") or ""),
            "plus1_contract": str(row.get("plus1_contract") or ""),
            "selection_method": str(row.get("selection_method") or ""),
            "selected_ok": normalize_bool(row.get("selected_ok")),
            "orderbook_source_effective": str(row.get("orderbook_source_effective") or ""),
            "warning": str(row.get("warning") or ""),
            "run_id": str(row.get("run_id") or ""),
        }
    return dict(fallback)


def build_binding_payload(route: str, row: dict, execution_params: dict, contract_ctx: dict) -> dict:
    payload = {
        "registry_id": str(row.get("registry_id") or ""),
        "candidate_label": str(row.get("candidate_label") or ""),
        "paper_route": route,
        "scenario_anchor": str(row.get("scenario_anchor") or ""),
        "required_features": str(row.get("required_features") or ""),
    }
    payload.update(execution_params)
    if route == "leadlag_orderbook_monitor":
        payload.update(
            {
                "target_contract": contract_ctx.get("target_contract"),
                "plus1_contract": contract_ctx.get("plus1_contract"),
                "selection_method": contract_ctx.get("selection_method"),
                "orderbook_source_effective": contract_ctx.get("orderbook_source_effective"),
                "selection_run_date": contract_ctx.get("selection_run_date"),
                "selected_ok": contract_ctx.get("selected_ok"),
            }
        )
    return payload


def target_resolution(route: str, row: dict, contract_ctx: dict) -> tuple[str, bool, str, str]:
    runtime_ready = normalize_bool(row.get("runtime_ready"))
    shortlist_status = str(row.get("shortlist_status") or "")
    blocking_reason = str(row.get("blocking_reason") or "")
    if route == "paper_autopolicy":
        if runtime_ready:
            return "policy_overlay", True, "launch_ready", ""
        return "policy_overlay", False, shortlist_status or "blocked", blocking_reason or "not_runtime_ready"
    if route == "leadlag_orderbook_monitor":
        if runtime_ready and contract_ctx.get("target_contract") and contract_ctx.get("plus1_contract"):
            return "contract_pair", True, "launch_ready", ""
        reason = blocking_reason or "missing_contract_binding"
        return "contract_pair", False, shortlist_status or "blocked", reason
    if route == "candidate_runtime":
        return "manual_runtime_release", False, shortlist_status or "blocked", blocking_reason or "candidate_runtime_needs_manual_release"
    return "unknown", False, "blocked", blocking_reason or "unsupported_route"


def shortlist_rows_to_targets(rows: list[dict], trade_date: str, project_root: Path) -> list[dict]:
    fallback_contract = fallback_contract_context(project_root, trade_date)
    out: list[dict] = []
    for row in rows:
        route = str(row.get("paper_route") or "")
        execution_params = decode_json_dict(row.get("execution_params_json"))
        contract_ctx = shortlist_row_contract_context(row, fallback_contract) if route == "leadlag_orderbook_monitor" else {
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
        target_kind, launch_ready, target_status, blocking_reason = target_resolution(route, row, contract_ctx)
        shortlist_rank = maybe_int(row.get("shortlist_rank")) or 0
        shortlist_id = f"{trade_date}:{shortlist_rank}:{row.get('registry_id')}"
        strategy_mode = str(execution_params.get("strategy_mode") or "")
        portfolio_mode = str(execution_params.get("portfolio_mode") or "")
        selected_feature_set = str(execution_params.get("selected_feature_set") or "")
        selected_threshold = execution_params.get("selected_threshold")
        target_contract = str(contract_ctx.get("target_contract") or "")
        plus1_contract = str(contract_ctx.get("plus1_contract") or "")
        target_id = "|".join(
            [
                trade_date,
                str(row.get("registry_id") or ""),
                route or "route",
                target_contract or "no_target",
                plus1_contract or "no_plus1",
                str(contract_ctx.get("selection_run_date") or "no_selection"),
            ]
        )
        out.append(
            {
                "trade_date": trade_date,
                "target_rank": 0,
                "target_id": target_id,
                "shortlist_id": shortlist_id,
                "registry_id": str(row.get("registry_id") or ""),
                "candidate_label": str(row.get("candidate_label") or ""),
                "paper_route": route,
                "target_kind": target_kind,
                "target_status": target_status,
                "launch_ready": launch_ready,
                "blocking_reason": blocking_reason,
                "stability_score": maybe_float(row.get("stability_score")),
                "priority": maybe_int(row.get("priority")),
                "shortlist_rank": shortlist_rank,
                "registry_status": str(row.get("registry_status") or ""),
                "validation_state": str(row.get("validation_state") or ""),
                "scenario_anchor": str(row.get("scenario_anchor") or ""),
                "recommended_use": str(row.get("recommended_use") or ""),
                "required_features": str(row.get("required_features") or ""),
                "selection_run_date": contract_ctx.get("selection_run_date"),
                "selection_age_days": contract_ctx.get("selection_age_days"),
                "selection_fresh": contract_ctx.get("selection_fresh"),
                "target_contract": target_contract,
                "plus1_contract": plus1_contract,
                "selection_method": contract_ctx.get("selection_method"),
                "selected_ok": contract_ctx.get("selected_ok"),
                "orderbook_source_effective": contract_ctx.get("orderbook_source_effective"),
                "warning": contract_ctx.get("warning"),
                "run_id": contract_ctx.get("run_id"),
                "family": str(row.get("family") or ""),
                "instrument_type": str(row.get("instrument_type") or ""),
                "strategy_mode": strategy_mode,
                "portfolio_mode": portfolio_mode,
                "selected_feature_set": selected_feature_set,
                "selected_threshold": selected_threshold if selected_threshold not in ("", None) else "",
                "horizon": str(execution_params.get("horizon") or row.get("horizon") or ""),
                "execution_params_json": str(row.get("execution_params_json") or ""),
                "binding_payload_json": json_text(build_binding_payload(route, row, execution_params, contract_ctx)),
                "evidence_json": str(row.get("evidence_json") or ""),
            }
        )
    out.sort(
        key=lambda row: (
            0 if bool(row.get("launch_ready")) else 1,
            -safe_float(row.get("stability_score")),
            safe_int(row.get("shortlist_rank"), 10**6),
            str(row.get("candidate_label") or ""),
        )
    )
    for index, row in enumerate(out, start=1):
        row["target_rank"] = index
        row["launch_ready"] = bool_text(bool(row.get("launch_ready")))
        row["selection_fresh"] = bool_text(bool(row.get("selection_fresh")))
        row["selected_ok"] = bool_text(bool(row.get("selected_ok")))
    return out


def summarize_targets(rows: list[dict], trade_date: str) -> dict:
    by_route: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for row in rows:
        by_route[str(row.get("paper_route") or "unknown")] = by_route.get(str(row.get("paper_route") or "unknown"), 0) + 1
        by_status[str(row.get("target_status") or "unknown")] = by_status.get(str(row.get("target_status") or "unknown"), 0) + 1
        by_kind[str(row.get("target_kind") or "unknown")] = by_kind.get(str(row.get("target_kind") or "unknown"), 0) + 1
    return {
        "trade_date": trade_date,
        "generated_at": now_str(),
        "rows": len(rows),
        "launch_ready": sum(1 for row in rows if normalize_bool(row.get("launch_ready"))),
        "policy_overlay_ready": sum(
            1 for row in rows if str(row.get("target_kind") or "") == "policy_overlay" and normalize_bool(row.get("launch_ready"))
        ),
        "contract_pair_ready": sum(
            1 for row in rows if str(row.get("target_kind") or "") == "contract_pair" and normalize_bool(row.get("launch_ready"))
        ),
        "blocked": sum(1 for row in rows if str(row.get("target_status") or "") != "launch_ready"),
        "by_route": by_route,
        "by_status": by_status,
        "by_kind": by_kind,
    }


def markdown_table(rows: list[dict], columns: list[str], limit: int = 20) -> str:
    subset = rows[:limit]
    if not subset:
        return "No target rows."
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


def render_targets_markdown(rows: list[dict], summary: dict) -> str:
    return "\n".join(
        [
            "# Research Strategy Targets",
            "",
            f"- trade_date: {summary['trade_date']}",
            f"- rows: {summary['rows']}",
            f"- launch_ready: {summary['launch_ready']}",
            f"- policy_overlay_ready: {summary['policy_overlay_ready']}",
            f"- contract_pair_ready: {summary['contract_pair_ready']}",
            f"- blocked: {summary['blocked']}",
            "",
            markdown_table(
                rows,
                [
                    "target_rank",
                    "candidate_label",
                    "paper_route",
                    "target_kind",
                    "launch_ready",
                    "target_status",
                    "blocking_reason",
                    "target_contract",
                    "plus1_contract",
                ],
            ),
        ]
    )


def persist_target_outputs(*, rows: list[dict], summary: dict, directories: list[Path]) -> None:
    for directory in directories:
        write_csv_rows(directory / "research_strategy_targets.csv", rows, fieldnames=TARGET_FIELDS)
        write_json(directory / "research_strategy_targets_summary.json", summary)
        write_text(directory / "research_strategy_targets.md", render_targets_markdown(rows, summary))


def build_and_persist_research_strategy_targets(
    *,
    project_root: Path,
    trade_date: str,
    shortlist_rows: list[dict] | None = None,
    research_dir: Path | None = None,
    latest_dir: Path | None = None,
    bundle_dir: Path | None = None,
) -> tuple[list[dict], dict]:
    trade_date = resolve_trade_date(project_root, trade_date)
    research_dir = research_dir or project_root / "reports" / "autonomy" / "research" / trade_date
    latest_dir = latest_dir or project_root / "reports" / "autonomy" / "latest"
    shortlist_rows = list(shortlist_rows) if shortlist_rows is not None else load_shortlist_rows(project_root)
    rows = shortlist_rows_to_targets(shortlist_rows, trade_date, project_root)
    summary = summarize_targets(rows, trade_date)
    directories = [research_dir, latest_dir]
    if bundle_dir is not None:
        directories.append(bundle_dir)
    persist_target_outputs(rows=rows, summary=summary, directories=directories)
    return rows, summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build executable research strategy targets from the paper candidate shortlist.")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--trade-date", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows, summary = build_and_persist_research_strategy_targets(
        project_root=args.project_root,
        trade_date=str(args.trade_date or ""),
    )
    print(
        f"[{now_str()}] research_strategy_targets trade_date={summary['trade_date']} "
        f"rows={summary['rows']} launch_ready={summary['launch_ready']}",
        flush=True,
    )
    if rows:
        print(f"top_target={rows[0].get('candidate_label')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
