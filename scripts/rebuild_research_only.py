from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import daily_autonomy_runner as dar  # noqa: E402
from autonomy_common import ensure_dir, now_str, write_json, write_text  # noqa: E402
from daily_autonomy_outputs import write_research_outputs  # noqa: E402
from paper_candidate_shortlist import build_and_persist_paper_candidate_shortlist  # noqa: E402
from research_microstructure_counterfactual import build_and_persist_microstructure_counterfactual  # noqa: E402
from research_intervention_proposals import build_and_persist_research_intervention_proposals  # noqa: E402
from research_microstructure_gate import build_and_persist_microstructure_gate_research  # noqa: E402
from research_strategy_registry import build_and_persist_research_strategy_registry  # noqa: E402
from research_strategy_targets import build_and_persist_research_strategy_targets  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild research/autonomy artifacts without touching latest_auto_policy or runtime candidate state."
    )
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--run-name", default="v7_live_20260525")
    parser.add_argument("--profiles", default="")
    parser.add_argument("--trade-date", default="latest")
    return parser.parse_args()


def merge_latest_manifest(existing: dict, updates: dict) -> dict:
    merged = dict(existing)
    merged.update(updates)
    return merged


def main() -> int:
    args = parse_args()
    project_root = Path(args.project_root).resolve()
    run_dir = project_root / "reports" / "paper_runs" / args.run_name
    profiles_path = Path(args.profiles) if args.profiles else project_root / "reports" / "futures_scalp_profiles_v7_paper_20260525.csv"
    autonomy_root = project_root / "reports" / "autonomy"
    analysis_root = autonomy_root / "analysis"
    research_root = autonomy_root / "research"
    manifest_root = autonomy_root / "latest"
    latest_manifest_path = manifest_root / "latest_daily_manifest.json"
    latest_manifest_alias_path = manifest_root / "latest_manifest.json"
    latest_auto_policy_path = manifest_root / "latest_auto_policy.json"

    ensure_dir(analysis_root)
    ensure_dir(research_root)
    ensure_dir(manifest_root)

    all_rows = dar.load_primary_trades(run_dir)
    if not all_rows:
        write_text(manifest_root / "latest_research_rebuild.md", "# Research Rebuild\n\nNo trade rows found.\n")
        return 0

    trade_date = str(args.trade_date or "").strip()
    if trade_date == "latest":
        trade_date = dar.latest_trade_date(all_rows) or ""
    if not trade_date:
        write_text(manifest_root / "latest_research_rebuild.md", "# Research Rebuild\n\nNo trade date found.\n")
        return 0

    day_rows = dar.filter_trade_date(all_rows, trade_date)
    profiles = dar.load_profiles(profiles_path)
    all_wide_spread_rows = dar.load_wide_spread_reviews(run_dir)
    all_entry_shadow_rows = dar.load_entry_shadow_rows(run_dir)

    dar.annotate_trade_rows(all_rows, profiles)
    dar.annotate_trade_rows(day_rows, profiles)

    day_history = dar.build_day_history(all_rows, profiles)
    recurring_tickers = dar.build_recurring_killers(day_history, "worst_ticker")
    recurring_families = dar.build_recurring_killers(day_history, "worst_family")
    scenario_history = dar.build_scenario_history(all_rows, profiles)
    scenario_consensus = dar.summarize_scenario_history(scenario_history)

    research_dir = research_root / trade_date
    ensure_dir(research_dir)

    overall = dar.metrics(day_rows)
    by_group = dar.grouped_metrics(day_rows, lambda row: row["group_key"])
    by_portfolio = dar.grouped_metrics(day_rows, lambda row: str(row.get("portfolio_group") or ""))
    by_ticker = dar.grouped_metrics(day_rows, lambda row: str(row.get("secid") or ""))
    by_family = dar.grouped_metrics(day_rows, lambda row: row["family"])
    by_hour = dar.grouped_metrics(day_rows, dar.hour_bucket)
    all_group_family_metrics = dar.metrics_map(
        all_rows,
        lambda row: f"{str(row.get('portfolio_group') or '').upper()}/{str(row.get('contour') or '').upper()}::{str(row.get('family') or '').upper()}",
    )
    microstructure_summary = dar.build_microstructure_summary(all_wide_spread_rows, all_group_family_metrics)
    best_tickers = dar.ranked_tail([row for row in by_ticker if dar.safe_float(row.get("net_rub")) > 0], limit=10, reverse=True)
    worst_tickers = dar.ranked_tail([row for row in by_ticker if dar.safe_float(row.get("net_rub")) < 0], limit=10, reverse=False)
    worst_families = dar.ranked_tail([row for row in by_family if dar.safe_float(row.get("net_rub")) < 0], limit=10, reverse=False)
    open_positions = dar.load_open_position_snapshot(run_dir)
    open_summary = dar.summarize_open_positions(open_positions)
    roll_watch = dar.load_roll_watch(run_dir)
    margin_timeline = dar.load_margin_timeline(run_dir, trade_date)
    fallback_margin_timeline = dar.load_margin_snapshot_fallback(run_dir)
    existing_margin_portfolios = {str(row.get("portfolio") or "") for row in margin_timeline}
    for row in fallback_margin_timeline:
        portfolio = str(row.get("portfolio") or "")
        if portfolio and portfolio not in existing_margin_portfolios:
            margin_timeline.append(row)
    margin_summary = dar.summarize_margin_day(day_rows, margin_timeline, run_dir)
    runtime_trade_model = dar.load_runtime_trade_model(run_dir)

    research_day = dar.build_research_scenarios(all_rows, day_rows, profiles)
    research_all = dar.build_research_scenarios(all_rows, all_rows, profiles)
    recommendations = dar.build_recommendations(
        overall,
        by_ticker,
        by_family,
        by_hour,
        microstructure_summary,
        research_day,
        research_all,
        scenario_consensus,
        day_history,
        margin_summary,
    )

    current_auto_policy = dar.load_json(latest_auto_policy_path)
    strategy_lab = dar.build_strategy_lab(
        all_rows=all_rows,
        day_rows=day_rows,
        by_group=by_group,
        by_family=by_family,
        by_hour=by_hour,
        microstructure_summary=microstructure_summary,
        day_history=day_history,
        research_day=research_day,
        research_all=research_all,
        research_consensus=scenario_consensus,
        auto_policy=current_auto_policy,
    )
    optimizer_candidates = dar.build_optimizer_candidates(research_day, research_all, scenario_consensus)
    strategy_lab_counts = write_research_outputs(
        research_dir,
        research_day=research_day,
        research_all=research_all,
        scenario_history=scenario_history,
        scenario_consensus=scenario_consensus,
        optimizer_candidates=optimizer_candidates,
        strategy_lab=strategy_lab,
        markdown_top=dar.markdown_top,
    )
    strategy_review = dar.build_strategy_review(
        trade_date=trade_date,
        research_dir=research_dir,
        run_dir=run_dir,
        strategy_lab=strategy_lab,
        research_day=research_day,
        research_all=research_all,
        research_consensus=scenario_consensus,
        auto_policy=current_auto_policy,
        restriction_rows=[],
        runtime_trade_model=runtime_trade_model,
    )

    best_consensus_scenario = dar.pick_best_consensus_scenario(scenario_consensus)
    manifest_payload = dar.build_manifest_payload(
        trade_date=trade_date,
        overall=overall,
        open_summary=open_summary,
        runtime_trade_model=runtime_trade_model,
        margin_summary=margin_summary,
        recommendations=recommendations,
        research_day=research_day,
        research_all=research_all,
        best_consensus_scenario=best_consensus_scenario,
        day_history=day_history,
        worst_tickers=worst_tickers,
        worst_families=worst_families,
        recurring_tickers=recurring_tickers,
        recurring_families=recurring_families,
        microstructure_summary=microstructure_summary,
        scenario_consensus=scenario_consensus,
        optimizer_candidates=optimizer_candidates,
        strategy_lab=strategy_lab,
        strategy_lab_counts=strategy_lab_counts,
        restriction_rows=dar.build_restriction_rows(current_auto_policy),
        roll_watch=roll_watch,
        auto_policy=current_auto_policy,
    )
    if strategy_review:
        manifest_payload["strategy_review"] = strategy_review
        if isinstance(strategy_review.get("top_models"), list):
            manifest_payload["entry_shadow_top"] = strategy_review.get("top_models")[:10]
    manifest_payload["research_rebuild"] = {
        "trade_date": trade_date,
        "generated_at": now_str(),
        "mode": "research_only",
        "run_name": args.run_name,
    }

    registry_rows, registry_summary = build_and_persist_research_strategy_registry(
        project_root=project_root,
        trade_date=trade_date,
        manifest_payload=manifest_payload,
        strategy_lab_rows=strategy_lab,
        research_dir=research_dir,
        latest_dir=manifest_root,
    )
    manifest_payload["research_strategy_registry"] = registry_summary
    manifest_payload["research_strategy_registry_top"] = registry_rows[:10]

    shortlist_rows, shortlist_summary = build_and_persist_paper_candidate_shortlist(
        project_root=project_root,
        trade_date=trade_date,
        registry_rows=registry_rows,
        research_dir=research_dir,
        latest_dir=manifest_root,
    )
    manifest_payload["paper_candidate_shortlist"] = shortlist_summary
    manifest_payload["paper_candidate_shortlist_top"] = shortlist_rows[:10]

    intervention_rows, intervention_summary = build_and_persist_research_intervention_proposals(
        project_root=project_root,
        trade_date=trade_date,
        strategy_lab_rows=strategy_lab,
        strategy_review=strategy_review,
        research_day_rows=research_day,
        research_all_rows=research_all,
        research_dir=research_dir,
        latest_dir=manifest_root,
    )
    manifest_payload["research_intervention_proposals"] = intervention_summary
    manifest_payload["research_intervention_proposals_top"] = intervention_rows[:10]
    micro_gate_rows, micro_gate_summary = build_and_persist_microstructure_gate_research(
        project_root=project_root,
        trade_date=trade_date,
        all_wide_spread_rows=all_wide_spread_rows,
        all_trade_rows=all_rows,
        research_dir=research_dir,
        latest_dir=manifest_root,
    )
    manifest_payload["microstructure_gate_research"] = micro_gate_summary
    manifest_payload["microstructure_gate_research_top"] = micro_gate_rows[:10]
    micro_counter_rows, micro_counter_summary = build_and_persist_microstructure_counterfactual(
        project_root=project_root,
        trade_date=trade_date,
        all_entry_shadow_rows=all_entry_shadow_rows,
        research_dir=research_dir,
        latest_dir=manifest_root,
    )
    manifest_payload["microstructure_counterfactual"] = micro_counter_summary
    manifest_payload["microstructure_counterfactual_top"] = micro_counter_rows[:10]
    entry_shadow_collection_rows, entry_shadow_collection_summary = dar.build_and_persist_entry_shadow_collection(
        project_root=project_root,
        trade_date=trade_date,
        run_dir=run_dir,
        research_dir=research_dir,
        latest_dir=manifest_root,
    )
    manifest_payload["entry_shadow_collection"] = entry_shadow_collection_summary
    manifest_payload["entry_shadow_collection_top"] = entry_shadow_collection_rows[:10]

    target_rows, target_summary = build_and_persist_research_strategy_targets(
        project_root=project_root,
        trade_date=trade_date,
        shortlist_rows=shortlist_rows,
        research_dir=research_dir,
        latest_dir=manifest_root,
    )
    manifest_payload["research_strategy_targets"] = target_summary
    manifest_payload["research_strategy_targets_top"] = target_rows[:10]

    existing_manifest = dar.load_json(latest_manifest_path)
    latest_manifest = merge_latest_manifest(existing_manifest, manifest_payload)
    write_json(latest_manifest_path, latest_manifest)
    write_json(latest_manifest_alias_path, latest_manifest)
    write_text(
        manifest_root / "latest_research_rebuild.md",
        "\n".join(
            [
                "# Research Rebuild",
                "",
                f"- trade_date: {trade_date}",
                f"- mode: research_only",
                f"- runtime_ready_candidates: {shortlist_summary.get('runtime_ready')}",
                f"- launch_ready_targets: {target_summary.get('launch_ready')}",
                f"- entry_shadow_collection_status: {str(strategy_review.get('collection_status') or ('ok' if strategy_review.get('generated') else 'not_generated'))}",
                f"- entry_shadow_rows_day: {dar.safe_int(strategy_review.get('entry_shadow_rows_day'))}",
                f"- entry_shadow_rows_all: {dar.safe_int(strategy_review.get('entry_shadow_rows_all'))}",
                f"- entry_shadow_shadow_rows_all: {dar.safe_int(strategy_review.get('shadow_rows_all'))}",
                f"- entry_shadow_candidate_count: {dar.safe_int(strategy_review.get('candidate_count'))}",
                f"- entry_shadow_missing_files: {', '.join(strategy_review.get('missing_entry_files') or []) or 'none'}",
            ]
        )
        + "\n",
    )
    print(
        f"[{now_str()}] rebuild_research_only trade_date={trade_date} "
        f"registry_rows={registry_summary['rows']} shortlist_runtime_ready={shortlist_summary['runtime_ready']} "
        f"targets_launch_ready={target_summary['launch_ready']}",
        flush=True,
    )
    if shortlist_rows:
        print(f"top_candidate={shortlist_rows[0].get('candidate_label')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
