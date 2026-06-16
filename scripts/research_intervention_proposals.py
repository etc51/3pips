from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from autonomy_common import now_str, safe_float, safe_int, write_csv_rows, write_json, write_text
from research_strategy_registry import (
    derive_payout_fields,
    maybe_float,
    maybe_int,
    normalize_bool,
    relpath_text,
    resolve_trade_date,
    rows_by_scenario,
    safe_read_rows,
    slug_token,
)


PROPOSAL_FIELDS = [
    "trade_date",
    "proposal_rank",
    "proposal_id",
    "source_type",
    "source_id",
    "candidate_label",
    "intervention_family",
    "intervention_scope",
    "proposal_mode",
    "target_stage",
    "activation_state",
    "priority",
    "action_type",
    "safe_mode",
    "scenario_anchor",
    "requires_explicit_user_approval",
    "runtime_mutation_allowed",
    "live_mode_allowed",
    "trigger_metric",
    "trigger_value",
    "scope_net_rub",
    "expected_benefit",
    "risk_focus",
    "latest_day_expectancy_rub",
    "sample_expectancy_rub",
    "sample_avg_win_rub",
    "sample_avg_loss_rub",
    "sample_top3_loss_rub",
    "latest_day_top3_loss_rub",
    "delta_vs_base_rub",
    "skipped_losses",
    "skipped_wins",
    "portfolio_group",
    "contour",
    "model",
    "evidence_score",
    "actionability_tier",
    "required_features",
    "recommended_next_step",
    "evidence",
    "instructions_json",
    "evidence_json",
]


def bool_text(value: bool) -> str:
    return "True" if value else "False"


def json_text(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def markdown_table(rows: list[dict], columns: list[str], limit: int = 20) -> str:
    subset = rows[:limit]
    if not subset:
        return "No intervention proposals."
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


def parse_named_number(text: object, name: str) -> float | None:
    match = re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}=(-?\d+(?:\.\d+)?)", str(text or ""))
    if not match:
        return None
    try:
        return round(float(match.group(1)), 6)
    except Exception:
        return None


def parse_ratio(text: object) -> float | None:
    avg_loss = parse_named_number(text, "avg_loss")
    avg_win = parse_named_number(text, "avg_win")
    if avg_loss is None or avg_win in (None, 0):
        return None
    return round(abs(avg_loss) / abs(avg_win), 6)


def parse_scope_net_rub(text: object) -> float | None:
    return parse_named_number(text, "net")


def strategy_lab_context(
    row: dict,
    latest_day_by_scenario: dict[str, dict],
    all_sample_by_scenario: dict[str, dict],
) -> tuple[dict, dict, dict, dict]:
    anchor = str(row.get("scenario_anchor") or "")
    latest_day = latest_day_by_scenario.get(anchor, {})
    all_sample = all_sample_by_scenario.get(anchor, {})
    latest_day_payout = derive_payout_fields(latest_day)
    sample_payout = derive_payout_fields(all_sample)
    return latest_day, all_sample, latest_day_payout, sample_payout


def strategy_lab_family(row: dict) -> tuple[str, str, str, str]:
    hypothesis_id = str(row.get("hypothesis_id") or "")
    category = str(row.get("category") or "")
    if hypothesis_id == "shadow_opening_range_continuation" or category == "shadow_strategy":
        return "session_window", "late_session_underperformance", "late_session", "shadow_backtest_only"
    if hypothesis_id == "shadow_tail_risk_normalized_exit" or category == "exit_model":
        return "exit_tail_risk", "avg_loss_to_avg_win_ratio", "tail_loss", "shadow_backtest_only"
    if hypothesis_id == "runtime_family_regime_routing" or category == "regime_routing":
        return "family_regime", "killer_day_share_pct", "destructive_families", "research_then_manual_paper_release"
    if hypothesis_id == "shadow_vwap_reversion_family_probe" or category == "new_strategy":
        return "new_alpha_probe", "weak_family_negative_pnl", "weak_family_payout", "shadow_backtest_only"
    if hypothesis_id == "microstructure_spread_adaptive_gate" or category == "microstructure":
        return "microstructure_entry_gate", "median_spread_ratio", "hostile_spread", "shadow_backtest_only"
    return "", "", "", ""


def strategy_lab_trigger_value(row: dict, intervention_family: str, all_sample: dict, sample_payout: dict) -> float | str:
    evidence = str(row.get("evidence") or "")
    scope_net_rub = parse_scope_net_rub(evidence)
    if intervention_family == "session_window":
        late_net = parse_named_number(evidence, "late_net")
        return late_net if late_net is not None else ""
    if intervention_family == "exit_tail_risk":
        ratio = parse_ratio(evidence)
        if ratio is not None:
            return ratio
        avg_loss = sample_payout.get("avg_loss_rub")
        avg_win = sample_payout.get("avg_win_rub")
        if avg_loss not in (None, 0) and avg_win not in (None, 0):
            return round(abs(safe_float(avg_loss)) / abs(safe_float(avg_win)), 6)
        return ""
    if intervention_family == "family_regime":
        match = re.search(r"\((\d+(?:\.\d+)?)%\)", evidence)
        if match:
            try:
                return round(float(match.group(1)), 6)
            except Exception:
                return ""
        return ""
    if intervention_family == "new_alpha_probe":
        return scope_net_rub if scope_net_rub is not None else maybe_float(all_sample.get("net_rub"))
    if intervention_family == "microstructure_entry_gate":
        ratio = parse_named_number(evidence, "median_ratio")
        return ratio if ratio is not None else ""
    return ""


def strategy_lab_expected_benefit(
    intervention_family: str,
    row: dict,
    latest_day: dict,
    all_sample: dict,
    latest_day_payout: dict,
    sample_payout: dict,
) -> str:
    del row
    if intervention_family == "session_window":
        return "Concentrate entries inside the strongest Moscow morning window and remove late-session drag."
    if intervention_family == "exit_tail_risk":
        avg_loss = sample_payout.get("avg_loss_rub")
        avg_win = sample_payout.get("avg_win_rub")
        top3 = sample_payout.get("top3_loss_rub")
        return (
            f"Compress tail damage by improving avg_loss_rub={avg_loss} and top3_loss_rub={top3} "
            f"without sacrificing avg_win_rub={avg_win}."
        )
    if intervention_family == "family_regime":
        return "Downgrade destructive families before they create another killer day sequence."
    if intervention_family == "new_alpha_probe":
        return (
            f"Test a mean-reversion branch where latest-day family expectancy is weak "
            f"(latest_day_expectancy_rub={maybe_float(latest_day.get('expectancy_rub'))})."
        )
    if intervention_family == "microstructure_entry_gate":
        return (
            f"Reduce fills during hostile spread states while preserving viable slices "
            f"(sample_expectancy_rub={maybe_float(all_sample.get('expectancy_rub'))}, "
            f"latest_day_top3_loss_rub={latest_day_payout.get('top3_loss_rub')})."
        )
    return ""


def strategy_lab_instruction_payload(intervention_family: str, row: dict) -> dict:
    scope = str(row.get("scope") or "")
    candidate = str(row.get("candidate") or "")
    if intervention_family == "session_window":
        return {
            "proposal_mode": "research_only",
            "shadow_backtest": {
                "entry_window_moscow": ["10:15", "13:00"],
                "disable_new_entries_after": "13:00",
                "compare_against": "base",
                "focus_metrics": ["expectancy_rub", "top3_loss_rub", "trade_count"],
            },
        }
    if intervention_family == "exit_tail_risk":
        return {
            "proposal_mode": "research_only",
            "exit_models": ["earlier_trailing_stop", "time_stop", "volatility_normalized_stop"],
            "compare_metrics": ["avg_loss_rub", "top3_loss_rub", "profit_factor", "expectancy_rub"],
            "scope": scope,
        }
    if intervention_family == "family_regime":
        return {
            "proposal_mode": "research_then_runtime",
            "family_buckets": ["stable", "mixed", "destructive"],
            "destructive_action": "observe_only",
            "mixed_action": "micro_or_shadow_until_family_sample_stabilizes",
            "scope": scope,
        }
    if intervention_family == "new_alpha_probe":
        return {
            "proposal_mode": "research_only",
            "alpha_family": "vwap_reversion",
            "candidate_label": candidate,
            "required_signals": ["session_vwap", "deviation_zscore", "spread_filter", "family_label"],
            "scope": scope,
        }
    if intervention_family == "microstructure_entry_gate":
        return {
            "proposal_mode": "research_only",
            "gate_metric": "spread_to_stop_ratio",
            "dynamic_threshold_source": "recent_median_spread_ratio",
            "scope": scope,
            "compare_metrics": ["expectancy_rub", "top3_loss_rub", "trade_count"],
        }
    return {"proposal_mode": "research_only"}


def strategy_lab_rows_to_proposals(
    *,
    trade_date: str,
    strategy_lab_rows: list[dict],
    research_day_rows: list[dict],
    research_all_rows: list[dict],
) -> list[dict]:
    latest_day_by_scenario = rows_by_scenario(research_day_rows)
    all_sample_by_scenario = rows_by_scenario(research_all_rows)
    rows: list[dict] = []
    for row in strategy_lab_rows:
        action_type = str(row.get("action_type") or "")
        if action_type == "runtime_policy":
            continue
        intervention_family, trigger_metric, risk_focus, target_stage = strategy_lab_family(row)
        if not intervention_family:
            continue
        evidence_text = str(row.get("evidence") or "")
        scope_net_rub = parse_scope_net_rub(evidence_text)
        latest_day, all_sample, latest_day_payout, sample_payout = strategy_lab_context(
            row,
            latest_day_by_scenario,
            all_sample_by_scenario,
        )
        source_id = str(row.get("hypothesis_id") or row.get("candidate") or "")
        evidence_payload = {
            "latest_day": latest_day,
            "all_sample": all_sample,
            "latest_day_payout": latest_day_payout,
            "sample_payout": sample_payout,
        }
        rows.append(
            {
                "trade_date": trade_date,
                "proposal_rank": 0,
                "proposal_id": f"{trade_date}|strategy_lab|{slug_token(source_id)}",
                "source_type": "strategy_lab",
                "source_id": source_id,
                "candidate_label": str(row.get("candidate") or ""),
                "intervention_family": intervention_family,
                "intervention_scope": str(row.get("scope") or ""),
                "proposal_mode": "proposal_only",
                "target_stage": target_stage,
                "activation_state": "proposal_only",
                "priority": maybe_int(row.get("priority")) or 0,
                "action_type": action_type,
                "safe_mode": str(row.get("safe_mode") or ""),
                "scenario_anchor": str(row.get("scenario_anchor") or ""),
                "requires_explicit_user_approval": True,
                "runtime_mutation_allowed": False,
                "live_mode_allowed": False,
                "trigger_metric": trigger_metric,
                "trigger_value": strategy_lab_trigger_value(row, intervention_family, all_sample, sample_payout),
                "scope_net_rub": scope_net_rub,
                "expected_benefit": strategy_lab_expected_benefit(
                    intervention_family,
                    row,
                    latest_day,
                    all_sample,
                    latest_day_payout,
                    sample_payout,
                ),
                "risk_focus": risk_focus,
                "latest_day_expectancy_rub": maybe_float(latest_day.get("expectancy_rub")),
                "sample_expectancy_rub": maybe_float(all_sample.get("expectancy_rub")),
                "sample_avg_win_rub": sample_payout.get("avg_win_rub"),
                "sample_avg_loss_rub": sample_payout.get("avg_loss_rub"),
                "sample_top3_loss_rub": sample_payout.get("top3_loss_rub"),
                "latest_day_top3_loss_rub": latest_day_payout.get("top3_loss_rub"),
                "delta_vs_base_rub": maybe_float(all_sample.get("delta_vs_base_rub") or latest_day.get("delta_vs_base_rub")),
                "skipped_losses": None,
                "skipped_wins": None,
                "portfolio_group": "",
                "contour": "",
                "model": "",
                "evidence_score": 0,
                "actionability_tier": "",
                "required_features": str(row.get("required_features") or ""),
                "recommended_next_step": str(row.get("recommended_next_step") or ""),
                "evidence": evidence_text,
                "instructions_json": json_text(strategy_lab_instruction_payload(intervention_family, row)),
                "evidence_json": json_text(evidence_payload),
            }
        )
    return rows


def load_strategy_review_rows(research_dir: Path) -> dict:
    path = research_dir / "strategy_review_candidates.csv"
    return {"candidates": safe_read_rows(path)}


def strategy_review_priority(row: dict) -> int:
    delta = safe_float(row.get("delta_vs_base_rub"))
    return max(70, min(96, 80 + int(delta // 750))) if delta > 0 else 70


def strategy_review_rows_to_proposals(*, trade_date: str, strategy_review: dict) -> list[dict]:
    rows: list[dict] = []
    for row in list(strategy_review.get("candidates") or []):
        source_id = str(row.get("candidate") or "")
        delta_vs_base_rub = maybe_float(row.get("delta_vs_base_rub"))
        skipped_losses = maybe_int(row.get("skipped_losses"))
        skipped_wins = maybe_int(row.get("skipped_wins"))
        instructions = {
            "proposal_mode": "proposal_only",
            "entry_shadow_gate": {
                "portfolio_group": str(row.get("portfolio_group") or ""),
                "contour": str(row.get("contour") or ""),
                "model": str(row.get("model") or ""),
                "compare_metric": "delta_vs_base_rub",
                "followup_days_before_runtime": 2,
            },
        }
        rows.append(
            {
                "trade_date": trade_date,
                "proposal_rank": 0,
                "proposal_id": f"{trade_date}|strategy_review|{slug_token(source_id)}",
                "source_type": "strategy_review",
                "source_id": source_id,
                "candidate_label": str(row.get("candidate") or ""),
                "intervention_family": "entry_shadow_gate",
                "intervention_scope": f"{str(row.get('portfolio_group') or '')}/{str(row.get('contour') or '')}",
                "proposal_mode": "proposal_only",
                "target_stage": "candidate_runtime_review",
                "activation_state": "proposal_only",
                "priority": strategy_review_priority(row),
                "action_type": str(row.get("recommended_action") or "research_then_runtime"),
                "safe_mode": "research_only",
                "scenario_anchor": "",
                "requires_explicit_user_approval": True,
                "runtime_mutation_allowed": False,
                "live_mode_allowed": False,
                "trigger_metric": "delta_vs_base_rub",
                "trigger_value": delta_vs_base_rub,
                "scope_net_rub": None,
                "expected_benefit": (
                    f"Skip {skipped_losses or 0} losses against {skipped_wins or 0} skipped winners "
                    f"while improving expectancy by {delta_vs_base_rub} RUB vs base."
                ),
                "risk_focus": "entry_quality",
                "latest_day_expectancy_rub": None,
                "sample_expectancy_rub": None,
                "sample_avg_win_rub": None,
                "sample_avg_loss_rub": None,
                "sample_top3_loss_rub": None,
                "latest_day_top3_loss_rub": None,
                "delta_vs_base_rub": delta_vs_base_rub,
                "skipped_losses": skipped_losses,
                "skipped_wins": skipped_wins,
                "portfolio_group": str(row.get("portfolio_group") or ""),
                "contour": str(row.get("contour") or ""),
                "model": str(row.get("model") or ""),
                "evidence_score": 0,
                "actionability_tier": "",
                "required_features": "entry shadow rows, base-vs-model comparison, follow-up day history",
                "recommended_next_step": "Keep this as a proposal-only entry gate until a human explicitly approves any runtime candidate release.",
                "evidence": str(row.get("note") or ""),
                "instructions_json": json_text(instructions),
                "evidence_json": json_text({"strategy_review_candidate": row}),
            }
        )
    return rows


def proposal_evidence_score(row: dict) -> int:
    score = 0
    if maybe_float(row.get("trigger_value")) is not None:
        score += 3
    if maybe_float(row.get("delta_vs_base_rub")) is not None:
        score += 3
    if maybe_float(row.get("sample_expectancy_rub")) is not None:
        score += 2
    if maybe_float(row.get("sample_top3_loss_rub")) is not None:
        score += 2
    if maybe_float(row.get("latest_day_top3_loss_rub")) is not None:
        score += 1
    if maybe_float(row.get("sample_avg_win_rub")) is not None and maybe_float(row.get("sample_avg_loss_rub")) is not None:
        score += 1
    if maybe_float(row.get("scope_net_rub")) is not None:
        score += 1
    if (maybe_int(row.get("skipped_losses")) or 0) > (maybe_int(row.get("skipped_wins")) or 0):
        score += 1
    return score


def proposal_actionability_tier(row: dict, evidence_score: int) -> str:
    if str(row.get("source_type") or "") == "strategy_review":
        return "runtime_candidate_review"
    intervention_family = str(row.get("intervention_family") or "")
    if intervention_family == "new_alpha_probe":
        return "exploratory"
    if evidence_score >= 6:
        return "backtest_ready"
    if evidence_score >= 3:
        return "research_plan_ready"
    return "needs_more_evidence"


def normalize_proposal_rows(rows: list[dict]) -> tuple[list[dict], int]:
    tier_order = {
        "runtime_candidate_review": 0,
        "backtest_ready": 1,
        "research_plan_ready": 2,
        "exploratory": 3,
        "needs_more_evidence": 4,
    }
    filtered_low_evidence_rows = 0
    ranked_rows: list[dict] = []
    for row in rows:
        scored = dict(row)
        evidence_score = proposal_evidence_score(scored)
        if str(scored.get("source_type") or "") == "strategy_lab" and evidence_score < 3:
            filtered_low_evidence_rows += 1
            continue
        scored["evidence_score"] = evidence_score
        scored["actionability_tier"] = proposal_actionability_tier(scored, evidence_score)
        ranked_rows.append(scored)
    ranked_rows.sort(
        key=lambda row: (
            tier_order.get(str(row.get("actionability_tier") or ""), 99),
            -(maybe_int(row.get("evidence_score")) or 0),
            -(maybe_int(row.get("priority")) or 0),
            0 if str(row.get("source_type") or "") == "strategy_review" else 1,
            -(maybe_float(row.get("delta_vs_base_rub")) or 0.0),
            maybe_float(row.get("scope_net_rub")) if maybe_float(row.get("scope_net_rub")) is not None else 0.0,
            str(row.get("candidate_label") or ""),
        )
    )
    for index, row in enumerate(ranked_rows, start=1):
        row["proposal_rank"] = index
        row["requires_explicit_user_approval"] = bool_text(normalize_bool(row.get("requires_explicit_user_approval")))
        row["runtime_mutation_allowed"] = bool_text(normalize_bool(row.get("runtime_mutation_allowed")))
        row["live_mode_allowed"] = bool_text(normalize_bool(row.get("live_mode_allowed")))
    return ranked_rows, filtered_low_evidence_rows


def summarize_proposals(rows: list[dict], trade_date: str, project_root: Path, research_dir: Path, filtered_low_evidence_rows: int) -> dict:
    by_family: dict[str, int] = {}
    by_source_type: dict[str, int] = {}
    by_actionability_tier: dict[str, int] = {}
    for row in rows:
        family = str(row.get("intervention_family") or "unknown")
        source_type = str(row.get("source_type") or "unknown")
        actionability_tier = str(row.get("actionability_tier") or "unknown")
        by_family[family] = by_family.get(family, 0) + 1
        by_source_type[source_type] = by_source_type.get(source_type, 0) + 1
        by_actionability_tier[actionability_tier] = by_actionability_tier.get(actionability_tier, 0) + 1
    summary_path = research_dir / "research_intervention_proposals.md"
    artifacts = [
        relpath_text(project_root, summary_path),
        relpath_text(project_root, research_dir / "research_intervention_proposals.csv"),
        relpath_text(project_root, research_dir / "research_intervention_proposals_summary.json"),
    ]
    return {
        "trade_date": trade_date,
        "generated_at": now_str(),
        "generated": True,
        "rows": len(rows),
        "strategy_lab_rows": sum(1 for row in rows if str(row.get("source_type") or "") == "strategy_lab"),
        "strategy_review_rows": sum(1 for row in rows if str(row.get("source_type") or "") == "strategy_review"),
        "explicit_user_approval_required": sum(1 for row in rows if normalize_bool(row.get("requires_explicit_user_approval"))),
        "runtime_mutation_allowed": sum(1 for row in rows if normalize_bool(row.get("runtime_mutation_allowed"))),
        "live_mode_allowed": sum(1 for row in rows if normalize_bool(row.get("live_mode_allowed"))),
        "evidence_backed_rows": sum(1 for row in rows if (maybe_int(row.get("evidence_score")) or 0) >= 5),
        "filtered_low_evidence_rows": filtered_low_evidence_rows,
        "top_candidate": str(rows[0].get("candidate_label") or "") if rows else "",
        "by_intervention_family": by_family,
        "by_source_type": by_source_type,
        "by_actionability_tier": by_actionability_tier,
        "summary_path": artifacts[0],
        "artifacts": artifacts,
    }


def render_proposals_markdown(rows: list[dict], summary: dict) -> str:
    return "\n".join(
        [
            "# Research Intervention Proposals",
            "",
            f"- trade_date: {summary['trade_date']}",
            f"- rows: {summary['rows']}",
            f"- strategy_lab_rows: {summary['strategy_lab_rows']}",
            f"- strategy_review_rows: {summary['strategy_review_rows']}",
            f"- explicit_user_approval_required: {summary['explicit_user_approval_required']}",
            f"- runtime_mutation_allowed: {summary['runtime_mutation_allowed']}",
            f"- live_mode_allowed: {summary['live_mode_allowed']}",
            f"- evidence_backed_rows: {summary['evidence_backed_rows']}",
            f"- filtered_low_evidence_rows: {summary['filtered_low_evidence_rows']}",
            f"- top_candidate: {summary['top_candidate']}",
            "",
            markdown_table(
                rows,
                [
                    "proposal_rank",
                    "candidate_label",
                    "actionability_tier",
                    "evidence_score",
                    "intervention_family",
                    "source_type",
                    "target_stage",
                    "trigger_metric",
                    "trigger_value",
                    "expected_benefit",
                ],
            ),
        ]
    )


def persist_proposal_outputs(*, rows: list[dict], summary: dict, directories: list[Path]) -> None:
    for directory in directories:
        write_csv_rows(directory / "research_intervention_proposals.csv", rows, fieldnames=PROPOSAL_FIELDS)
        write_json(directory / "research_intervention_proposals_summary.json", summary)
        write_text(directory / "research_intervention_proposals.md", render_proposals_markdown(rows, summary))


def build_and_persist_research_intervention_proposals(
    *,
    project_root: Path,
    trade_date: str,
    strategy_lab_rows: list[dict] | None = None,
    strategy_review: dict | None = None,
    research_day_rows: list[dict] | None = None,
    research_all_rows: list[dict] | None = None,
    research_dir: Path | None = None,
    latest_dir: Path | None = None,
    bundle_dir: Path | None = None,
) -> tuple[list[dict], dict]:
    trade_date = resolve_trade_date(project_root, trade_date)
    research_dir = research_dir or project_root / "reports" / "autonomy" / "research" / trade_date
    latest_dir = latest_dir or project_root / "reports" / "autonomy" / "latest"
    strategy_lab_rows = list(strategy_lab_rows) if strategy_lab_rows is not None else safe_read_rows(research_dir / "strategy_lab_candidates.csv")
    strategy_review = dict(strategy_review) if isinstance(strategy_review, dict) else load_strategy_review_rows(research_dir)
    research_day_rows = list(research_day_rows) if research_day_rows is not None else safe_read_rows(research_dir / "policy_sweep_latest_day.csv")
    research_all_rows = list(research_all_rows) if research_all_rows is not None else safe_read_rows(research_dir / "policy_sweep_all_sample.csv")

    rows = strategy_lab_rows_to_proposals(
        trade_date=trade_date,
        strategy_lab_rows=strategy_lab_rows,
        research_day_rows=research_day_rows,
        research_all_rows=research_all_rows,
    )
    rows.extend(strategy_review_rows_to_proposals(trade_date=trade_date, strategy_review=strategy_review))
    rows, filtered_low_evidence_rows = normalize_proposal_rows(rows)
    summary = summarize_proposals(rows, trade_date, project_root, research_dir, filtered_low_evidence_rows)
    directories = [research_dir, latest_dir]
    if bundle_dir is not None:
        directories.append(bundle_dir)
    persist_proposal_outputs(rows=rows, summary=summary, directories=directories)
    return rows, summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build research-only intervention proposals from autonomy artifacts.")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--trade-date", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows, summary = build_and_persist_research_intervention_proposals(
        project_root=args.project_root,
        trade_date=str(args.trade_date or ""),
    )
    print(
        f"[{now_str()}] research_intervention_proposals trade_date={summary['trade_date']} "
        f"rows={summary['rows']} explicit_user_approval_required={summary['explicit_user_approval_required']}",
        flush=True,
    )
    if rows:
        print(f"top_proposal={rows[0].get('candidate_label')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
