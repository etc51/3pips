from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from autonomy_common import now_str, read_csv_rows, safe_float, safe_int, write_csv_rows, write_json, write_text


REGISTRY_FIELDS = [
    "registry_id",
    "as_of_date",
    "registry_source",
    "source_stack",
    "source_artifact",
    "status",
    "paper_route",
    "rank",
    "priority",
    "candidate_label",
    "category",
    "scope",
    "action_type",
    "safe_mode",
    "autopromote_ready",
    "scenario_anchor",
    "candidate_type",
    "recommended_use",
    "family",
    "instrument_type",
    "leg1_secid",
    "leg2_secid",
    "direction",
    "horizon",
    "stability_days",
    "positive_days",
    "negative_days",
    "beat_base_days",
    "beat_base_pct",
    "delta_total_rub",
    "latest_day_delta_rub",
    "latest_day_rub",
    "median_daily_net_rub",
    "worst_day_rub",
    "best_day_rub",
    "test_total_return",
    "test_max_drawdown",
    "test_profit_factor",
    "evidence",
    "recommended_next_step",
    "required_features",
    "validation_state",
    "evidence_json",
    "execution_params_json",
]


def normalize_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y"}


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


def slug_token(value: object) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "").strip()).strip("_").lower()
    return text or "candidate"


def relpath_text(project_root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(project_root)).replace("\\", "/")
    except Exception:
        return str(path).replace("\\", "/")


def json_text(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def infer_strategy_lab_route(action_type: str, safe_mode: str) -> str:
    if safe_mode == "paper_autopolicy" or action_type == "runtime_policy":
        return "paper_autopolicy"
    if action_type == "research_then_runtime":
        return "candidate_runtime"
    return "research_only"


def infer_strategy_lab_status(action_type: str, safe_mode: str, autopromote_ready: bool) -> str:
    if safe_mode == "paper_autopolicy" and autopromote_ready:
        return "paper_candidate"
    if action_type in {"runtime_policy", "research_then_runtime"}:
        return "validated"
    return "research_only"


def strategy_lab_rows_to_registry(
    *,
    project_root: Path,
    trade_date: str,
    strategy_lab_rows: list[dict],
    manifest_payload: dict,
    source_artifact: Path,
) -> list[dict]:
    optimizer_by_scenario = {
        str(row.get("scenario") or ""): row
        for row in (manifest_payload.get("optimizer_top") or [])
        if isinstance(row, dict) and str(row.get("scenario") or "")
    }
    consensus_by_scenario = {
        str(row.get("scenario") or ""): row
        for row in (manifest_payload.get("research_consensus_top") or [])
        if isinstance(row, dict) and str(row.get("scenario") or "")
    }
    overall = manifest_payload.get("overall") if isinstance(manifest_payload.get("overall"), dict) else {}
    out: list[dict] = []
    for index, row in enumerate(strategy_lab_rows, start=1):
        action_type = str(row.get("action_type") or "")
        safe_mode = str(row.get("safe_mode") or "")
        autopromote_ready = normalize_bool(row.get("autopromote_ready"))
        anchor = str(row.get("scenario_anchor") or "")
        optimizer = optimizer_by_scenario.get(anchor, {})
        consensus = consensus_by_scenario.get(anchor, {})
        out.append(
            {
                "registry_id": f"strategy_lab:{slug_token(row.get('hypothesis_id') or row.get('candidate'))}",
                "as_of_date": trade_date,
                "registry_source": "strategy_lab_candidates",
                "source_stack": "autonomy_strategy_lab",
                "source_artifact": relpath_text(project_root, source_artifact),
                "status": infer_strategy_lab_status(action_type, safe_mode, autopromote_ready),
                "paper_route": infer_strategy_lab_route(action_type, safe_mode),
                "rank": maybe_int(row.get("rank")) or index,
                "priority": maybe_int(row.get("priority")),
                "candidate_label": str(row.get("candidate") or ""),
                "category": str(row.get("category") or ""),
                "scope": str(row.get("scope") or ""),
                "action_type": action_type,
                "safe_mode": safe_mode,
                "autopromote_ready": autopromote_ready,
                "scenario_anchor": anchor,
                "candidate_type": str(optimizer.get("candidate_type") or ""),
                "recommended_use": str(optimizer.get("recommended_use") or ""),
                "family": "",
                "instrument_type": "",
                "leg1_secid": "",
                "leg2_secid": "",
                "direction": "",
                "horizon": "",
                "stability_days": maybe_int(consensus.get("days")),
                "positive_days": maybe_int(consensus.get("positive_days")),
                "negative_days": maybe_int(consensus.get("negative_days")),
                "beat_base_days": maybe_int(consensus.get("beat_base_days")),
                "beat_base_pct": maybe_float(consensus.get("beat_base_pct")),
                "delta_total_rub": maybe_float(consensus.get("delta_total_rub")),
                "latest_day_delta_rub": maybe_float(consensus.get("latest_day_delta_rub")),
                "latest_day_rub": maybe_float(consensus.get("latest_day_rub")),
                "median_daily_net_rub": maybe_float(consensus.get("median_daily_net_rub")),
                "worst_day_rub": maybe_float(consensus.get("worst_day_rub")),
                "best_day_rub": maybe_float(consensus.get("best_day_rub")),
                "test_total_return": None,
                "test_max_drawdown": None,
                "test_profit_factor": None,
                "evidence": str(row.get("evidence") or ""),
                "recommended_next_step": str(row.get("recommended_next_step") or ""),
                "required_features": str(row.get("required_features") or ""),
                "validation_state": "consensus_backed" if consensus else "strategy_lab_only",
                "evidence_json": json_text({"overall": overall, "consensus": consensus, "optimizer": optimizer}),
                "execution_params_json": json_text(
                    {
                        "scenario_anchor": anchor,
                        "candidate_type": optimizer.get("candidate_type"),
                        "recommended_use": optimizer.get("recommended_use"),
                        "safe_mode": safe_mode,
                        "action_type": action_type,
                    }
                ),
            }
        )
    return out


def screener_rows_to_registry(*, project_root: Path, trade_date: str, rows: list[dict], source_artifact: Path) -> list[dict]:
    out: list[dict] = []
    for index, row in enumerate(rows, start=1):
        family = str(row.get("family") or "")
        instrument_type = str(row.get("instrument_type") or "")
        action = str(row.get("action") or "")
        pattern = str(row.get("pattern") or "")
        holding_days = maybe_int(row.get("holding_days"))
        secid = str(row.get("secid") or row.get("front_secid") or "")
        back = str(row.get("back_secid") or "")
        out.append(
            {
                "registry_id": f"screener:{slug_token(family)}:{slug_token(instrument_type)}:{slug_token(pattern)}:{slug_token(action)}:{holding_days or 0}",
                "as_of_date": trade_date,
                "registry_source": "screener_latest",
                "source_stack": "seasonal_screener",
                "source_artifact": relpath_text(project_root, source_artifact),
                "status": "validated",
                "paper_route": "observe_only",
                "rank": index,
                "priority": maybe_int(row.get("score")),
                "candidate_label": f"{family} {pattern} {action}".strip(),
                "category": "seasonal_pattern",
                "scope": str(row.get("series") or row.get("spread") or ""),
                "action_type": "research_then_runtime",
                "safe_mode": "research_only",
                "autopromote_ready": False,
                "scenario_anchor": "",
                "candidate_type": "",
                "recommended_use": "observe_only",
                "family": family,
                "instrument_type": instrument_type,
                "leg1_secid": secid,
                "leg2_secid": back,
                "direction": action,
                "horizon": f"{holding_days}d" if holding_days else "",
                "stability_days": None,
                "positive_days": None,
                "negative_days": None,
                "beat_base_days": None,
                "beat_base_pct": None,
                "delta_total_rub": None,
                "latest_day_delta_rub": None,
                "latest_day_rub": None,
                "median_daily_net_rub": None,
                "worst_day_rub": None,
                "best_day_rub": None,
                "test_total_return": None,
                "test_max_drawdown": None,
                "test_profit_factor": None,
                "evidence": f"score={row.get('score')} ann_sharpe={row.get('ann_sharpe')} n_trades={row.get('n_trades')}",
                "recommended_next_step": "confirm with portfolio backtest or shadow executor before runtime promotion",
                "required_features": "continuous daily, calendar spreads, screener results",
                "validation_state": "screener_snapshot",
                "evidence_json": json_text(row),
                "execution_params_json": json_text({"holding_days": holding_days, "action": action, "pattern": pattern}),
            }
        )
    return out


def third_pass_rows_to_registry(
    *,
    project_root: Path,
    trade_date: str,
    summary_rows: list[dict],
    selection_rows: list[dict],
    source_artifact: Path,
) -> list[dict]:
    selection_by_key = {
        (
            str(row.get("strategy_mode") or ""),
            str(row.get("threshold_objective") or ""),
            str(row.get("cost_scenario") or ""),
        ): row
        for row in selection_rows
    }
    filtered = [
        row
        for row in summary_rows
        if str(row.get("portfolio_mode") or "") in {"global_no_overlap", "front_month_only", "portfolio_no_overlap"}
        and str(row.get("threshold_objective") or "") == "train_mean"
    ]
    filtered.sort(key=lambda row: safe_float(row.get("net_pnl_rub_sum")), reverse=True)
    out: list[dict] = []
    for index, row in enumerate(filtered[:10], start=1):
        cost_key = f"{safe_int(row.get('slippage_ticks_roundtrip'))}ticks_{safe_int(row.get('fee_rub_per_contract_roundtrip'))}rub"
        selection = selection_by_key.get(
            (
                str(row.get("strategy_mode") or ""),
                str(row.get("threshold_objective") or ""),
                cost_key,
            ),
            {},
        )
        net = maybe_float(row.get("net_pnl_rub_sum"))
        max_dd = maybe_float(row.get("max_drawdown_rub"))
        status = "paper_candidate" if (net or 0.0) > 0 and str(row.get("portfolio_mode") or "") == "global_no_overlap" else "validated"
        out.append(
            {
                "registry_id": f"third_pass:{slug_token(row.get('strategy_mode'))}:{slug_token(row.get('portfolio_mode'))}:{cost_key}",
                "as_of_date": trade_date,
                "registry_source": "third_pass_strategy_summary",
                "source_stack": "leadlag_third_pass",
                "source_artifact": relpath_text(project_root, source_artifact),
                "status": status,
                "paper_route": "leadlag_orderbook_monitor",
                "rank": index,
                "priority": None,
                "candidate_label": f"{row.get('strategy_mode')} {row.get('portfolio_mode')}",
                "category": "leadlag_strategy",
                "scope": str(row.get("portfolio_mode") or ""),
                "action_type": "research_then_runtime",
                "safe_mode": "research_only",
                "autopromote_ready": False,
                "scenario_anchor": "",
                "candidate_type": "",
                "recommended_use": "leadlag_paper_candidate",
                "family": "NG",
                "instrument_type": "leadlag_pair",
                "leg1_secid": "",
                "leg2_secid": "",
                "direction": "BOTH",
                "horizon": "30m",
                "stability_days": maybe_int(row.get("total_months")),
                "positive_days": maybe_int(row.get("positive_months")),
                "negative_days": None,
                "beat_base_days": None,
                "beat_base_pct": None,
                "delta_total_rub": net,
                "latest_day_delta_rub": None,
                "latest_day_rub": None,
                "median_daily_net_rub": None,
                "worst_day_rub": maybe_float(row.get("worst_month_net_pnl_rub")),
                "best_day_rub": maybe_float(row.get("best_month_net_pnl_rub")),
                "test_total_return": maybe_float(row.get("simple_total_return_on_go")),
                "test_max_drawdown": max_dd,
                "test_profit_factor": None,
                "evidence": f"net_pnl_rub_sum={row.get('net_pnl_rub_sum')} max_dd={row.get('max_drawdown_rub')}",
                "recommended_next_step": "bridge into leadlag paper monitor shortlist",
                "required_features": "third-pass trades, selection log, orderbook-derived features",
                "validation_state": "unit_corrected_positive" if (net or 0.0) > 0 else "unit_corrected_review",
                "evidence_json": json_text(row),
                "execution_params_json": json_text(
                    {
                        "strategy_mode": row.get("strategy_mode"),
                        "portfolio_mode": row.get("portfolio_mode"),
                        "slippage_ticks_roundtrip": row.get("slippage_ticks_roundtrip"),
                        "fee_rub_per_contract_roundtrip": row.get("fee_rub_per_contract_roundtrip"),
                        "threshold_objective": row.get("threshold_objective"),
                        "selected_feature_set": selection.get("selected_feature_set"),
                        "selected_threshold": selection.get("selected_threshold"),
                    }
                ),
            }
        )
    return out


def portfolio_rows_to_registry(
    *,
    project_root: Path,
    trade_date: str,
    summary_rows: list[dict],
    sensitivity_rows: list[dict],
    source_artifact: Path,
) -> list[dict]:
    passed_keys = {
        (
            str(row.get("strategy_id") or ""),
            str(row.get("family") or ""),
            str(row.get("holding_days_param") or ""),
            str(row.get("stop_mode") or ""),
            str(row.get("stop_value") or ""),
            str(row.get("take_profit_r") or ""),
            str(row.get("slippage_bps") or ""),
        ): row
        for row in sensitivity_rows
    }
    filtered = [row for row in summary_rows if str(row.get("period") or "") == "test_2024_2026"]
    filtered.sort(key=lambda row: safe_float(row.get("total_return")), reverse=True)
    out: list[dict] = []
    for index, row in enumerate(filtered[:10], start=1):
        key = (
            str(row.get("strategy_id") or ""),
            str(row.get("family") or ""),
            str(row.get("holding_days_param") or ""),
            str(row.get("stop_mode") or ""),
            str(row.get("stop_value") or ""),
            str(row.get("take_profit_r") or ""),
            str(row.get("slippage_bps") or ""),
        )
        sensitivity = passed_keys.get(key, {})
        passed_test = normalize_bool(sensitivity.get("passed_test"))
        out.append(
            {
                "registry_id": f"portfolio:{slug_token(row.get('strategy_id'))}:{slug_token(row.get('family'))}:{slug_token(row.get('holding_days_param'))}:{slug_token(row.get('stop_mode'))}",
                "as_of_date": trade_date,
                "registry_source": "portfolio_strategy_summary",
                "source_stack": "portfolio_backtest",
                "source_artifact": relpath_text(project_root, source_artifact),
                "status": "validated" if passed_test else "research_only",
                "paper_route": "observe_only",
                "rank": index,
                "priority": None,
                "candidate_label": f"{row.get('strategy_id')} {row.get('family')}",
                "category": "portfolio_strategy",
                "scope": str(row.get("family") or ""),
                "action_type": "research_then_runtime",
                "safe_mode": "research_only",
                "autopromote_ready": False,
                "scenario_anchor": "",
                "candidate_type": "",
                "recommended_use": "portfolio_validation",
                "family": str(row.get("family") or ""),
                "instrument_type": "",
                "leg1_secid": "",
                "leg2_secid": "",
                "direction": "",
                "horizon": f"{maybe_int(row.get('holding_days_param')) or ''}d".strip(),
                "stability_days": None,
                "positive_days": None,
                "negative_days": None,
                "beat_base_days": None,
                "beat_base_pct": None,
                "delta_total_rub": None,
                "latest_day_delta_rub": None,
                "latest_day_rub": None,
                "median_daily_net_rub": None,
                "worst_day_rub": maybe_float(row.get("worst_trade_rub")),
                "best_day_rub": None,
                "test_total_return": maybe_float(sensitivity.get("test_total_return")),
                "test_max_drawdown": maybe_float(sensitivity.get("test_max_drawdown")),
                "test_profit_factor": maybe_float(sensitivity.get("test_profit_factor")),
                "evidence": f"test_total_return={sensitivity.get('test_total_return')} passed_test={passed_test}",
                "recommended_next_step": "keep as research-only until a dedicated paper executor exists",
                "required_features": "portfolio backtest summary and sensitivity grid",
                "validation_state": "portfolio_oos_pass" if passed_test else "portfolio_oos_review",
                "evidence_json": json_text({"summary": row, "sensitivity": sensitivity}),
                "execution_params_json": json_text(
                    {
                        "holding_days_param": row.get("holding_days_param"),
                        "stop_mode": row.get("stop_mode"),
                        "stop_value": row.get("stop_value"),
                        "take_profit_r": row.get("take_profit_r"),
                        "slippage_bps": row.get("slippage_bps"),
                    }
                ),
            }
        )
    return out


def build_registry_rows(
    *,
    project_root: Path,
    trade_date: str,
    manifest_payload: dict,
    strategy_lab_rows: list[dict],
    screener_rows: list[dict],
    third_pass_summary_rows: list[dict],
    third_pass_selection_rows: list[dict],
    portfolio_summary_rows: list[dict],
    portfolio_sensitivity_rows: list[dict],
) -> list[dict]:
    research_dir = project_root / "reports" / "autonomy" / "research" / trade_date
    rows: list[dict] = []
    if strategy_lab_rows:
        rows.extend(
            strategy_lab_rows_to_registry(
                project_root=project_root,
                trade_date=trade_date,
                strategy_lab_rows=strategy_lab_rows,
                manifest_payload=manifest_payload,
                source_artifact=research_dir / "strategy_lab_candidates.csv",
            )
        )
    if screener_rows:
        rows.extend(
            screener_rows_to_registry(
                project_root=project_root,
                trade_date=trade_date,
                rows=screener_rows[:20],
                source_artifact=project_root / "results" / "screener_latest.csv",
            )
        )
    if third_pass_summary_rows:
        rows.extend(
            third_pass_rows_to_registry(
                project_root=project_root,
                trade_date=trade_date,
                summary_rows=third_pass_summary_rows,
                selection_rows=third_pass_selection_rows,
                source_artifact=project_root / "reports" / "third_pass_strategy_summary.csv",
            )
        )
    if portfolio_summary_rows:
        rows.extend(
            portfolio_rows_to_registry(
                project_root=project_root,
                trade_date=trade_date,
                summary_rows=portfolio_summary_rows,
                sensitivity_rows=portfolio_sensitivity_rows,
                source_artifact=project_root / "results" / "portfolio_strategy_summary.csv",
            )
        )
    rows.sort(
        key=lambda row: (
            0 if str(row.get("status") or "") == "paper_candidate" else 1,
            0 if str(row.get("paper_route") or "") == "paper_autopolicy" else 1,
            -(safe_int(row.get("priority"), 0)),
            safe_int(row.get("rank"), 10**6),
            str(row.get("candidate_label") or ""),
        )
    )
    return rows


def summarize_registry(rows: list[dict], trade_date: str) -> dict:
    by_source: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_route: dict[str, int] = {}
    for row in rows:
        by_source[str(row.get("registry_source") or "unknown")] = by_source.get(str(row.get("registry_source") or "unknown"), 0) + 1
        by_status[str(row.get("status") or "unknown")] = by_status.get(str(row.get("status") or "unknown"), 0) + 1
        by_route[str(row.get("paper_route") or "unknown")] = by_route.get(str(row.get("paper_route") or "unknown"), 0) + 1
    return {
        "trade_date": trade_date,
        "generated_at": now_str(),
        "rows": len(rows),
        "paper_candidates": by_status.get("paper_candidate", 0),
        "validated": by_status.get("validated", 0),
        "research_only": by_status.get("research_only", 0),
        "autopromote_ready": sum(1 for row in rows if normalize_bool(row.get("autopromote_ready"))),
        "by_source": by_source,
        "by_status": by_status,
        "by_paper_route": by_route,
    }


def markdown_table(rows: list[dict], columns: list[str], limit: int = 20) -> str:
    subset = rows[:limit]
    if not subset:
        return "No registry rows."
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


def render_registry_markdown(rows: list[dict], summary: dict) -> str:
    return "\n".join(
        [
            "# Research Strategy Registry",
            "",
            f"- trade_date: {summary['trade_date']}",
            f"- rows: {summary['rows']}",
            f"- paper_candidates: {summary['paper_candidates']}",
            f"- validated: {summary['validated']}",
            f"- research_only: {summary['research_only']}",
            f"- autopromote_ready: {summary['autopromote_ready']}",
            "",
            markdown_table(
                rows,
                [
                    "rank",
                    "candidate_label",
                    "registry_source",
                    "status",
                    "paper_route",
                    "scenario_anchor",
                    "stability_days",
                    "delta_total_rub",
                    "latest_day_delta_rub",
                ],
            ),
        ]
    )


def persist_registry_outputs(*, rows: list[dict], summary: dict, directories: list[Path]) -> None:
    for directory in directories:
        write_csv_rows(directory / "research_strategy_registry.csv", rows, fieldnames=REGISTRY_FIELDS)
        write_json(directory / "research_strategy_registry_summary.json", summary)
        write_text(directory / "research_strategy_registry.md", render_registry_markdown(rows, summary))


def load_optional_manifest(project_root: Path, trade_date: str, manifest_payload: dict | None) -> dict:
    if isinstance(manifest_payload, dict):
        return dict(manifest_payload)
    latest_path = project_root / "reports" / "autonomy" / "latest" / "latest_daily_manifest.json"
    trade_path = project_root / "reports" / "autonomy" / "research" / trade_date / "manifest.json"
    for path in [latest_path, trade_path]:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
    return {"trade_date": trade_date}


def resolve_trade_date(project_root: Path, trade_date: str) -> str:
    text = str(trade_date or "").strip()
    if text:
        return text
    latest_manifest = project_root / "reports" / "autonomy" / "latest" / "latest_daily_manifest.json"
    if latest_manifest.exists():
        try:
            payload = json.loads(latest_manifest.read_text(encoding="utf-8"))
            candidate = str(payload.get("trade_date") or "").strip()
            if candidate:
                return candidate
        except Exception:
            pass
    research_root = project_root / "reports" / "autonomy" / "research"
    if research_root.exists():
        dated = sorted(path.name for path in research_root.iterdir() if path.is_dir())
        if dated:
            return dated[-1]
    raise RuntimeError("Unable to resolve trade date for research strategy registry")


def build_and_persist_research_strategy_registry(
    *,
    project_root: Path,
    trade_date: str,
    manifest_payload: dict | None = None,
    strategy_lab_rows: list[dict] | None = None,
    research_dir: Path | None = None,
    latest_dir: Path | None = None,
    bundle_dir: Path | None = None,
) -> tuple[list[dict], dict]:
    trade_date = resolve_trade_date(project_root, trade_date)
    research_dir = research_dir or project_root / "reports" / "autonomy" / "research" / trade_date
    latest_dir = latest_dir or project_root / "reports" / "autonomy" / "latest"
    canonical_dir = project_root / "data" / "processed"
    source_manifest = load_optional_manifest(project_root, trade_date, manifest_payload)
    strategy_lab_rows = list(strategy_lab_rows) if strategy_lab_rows is not None else read_csv_rows(research_dir / "strategy_lab_candidates.csv")
    screener_rows = read_csv_rows(project_root / "results" / "screener_latest.csv")
    third_pass_summary_rows = read_csv_rows(project_root / "reports" / "third_pass_strategy_summary.csv")
    third_pass_selection_rows = read_csv_rows(project_root / "reports" / "third_pass_feature_selection_log.csv")
    portfolio_summary_rows = read_csv_rows(project_root / "results" / "portfolio_strategy_summary.csv")
    portfolio_sensitivity_rows = read_csv_rows(project_root / "results" / "portfolio_strategy_sensitivity.csv")
    rows = build_registry_rows(
        project_root=project_root,
        trade_date=trade_date,
        manifest_payload=source_manifest,
        strategy_lab_rows=strategy_lab_rows,
        screener_rows=screener_rows,
        third_pass_summary_rows=third_pass_summary_rows,
        third_pass_selection_rows=third_pass_selection_rows,
        portfolio_summary_rows=portfolio_summary_rows,
        portfolio_sensitivity_rows=portfolio_sensitivity_rows,
    )
    summary = summarize_registry(rows, trade_date)
    directories = [research_dir, latest_dir, canonical_dir]
    if bundle_dir is not None:
        directories.append(bundle_dir)
    persist_registry_outputs(rows=rows, summary=summary, directories=directories)
    return rows, summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a research-to-paper strategy registry from autonomy and research outputs.")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--trade-date", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows, summary = build_and_persist_research_strategy_registry(
        project_root=args.project_root,
        trade_date=str(args.trade_date or ""),
    )
    print(
        f"[{now_str()}] research_strategy_registry trade_date={summary['trade_date']} "
        f"rows={summary['rows']} paper_candidates={summary['paper_candidates']}",
        flush=True,
    )
    if rows:
        print(f"top_candidate={rows[0].get('candidate_label')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
