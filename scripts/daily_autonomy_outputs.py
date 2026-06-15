from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from autonomy_common import ensure_dir, now_str, tail_text, write_csv_rows, write_json, write_text


def build_strategy_lab_counts(strategy_lab: list[dict]) -> dict[str, int]:
    return {
        "total": len(strategy_lab),
        "runtime_policy": sum(1 for row in strategy_lab if str(row.get("action_type") or "") == "runtime_policy"),
        "shadow_backtest": sum(1 for row in strategy_lab if str(row.get("action_type") or "") == "shadow_backtest"),
        "research_then_runtime": sum(1 for row in strategy_lab if str(row.get("action_type") or "") == "research_then_runtime"),
        "autopromote_ready": sum(1 for row in strategy_lab if bool(row.get("autopromote_ready"))),
    }


def write_analysis_outputs(
    analysis_dir: Path,
    *,
    trade_date: str,
    overall: dict,
    open_summary: dict,
    recommendations: list[str],
    best_consensus_scenario: dict,
    strategy_lab: list[dict],
    microstructure_summary: list[dict],
    runtime_trade_model: dict,
    summary_md: str,
    by_portfolio: list[dict],
    by_group: list[dict],
    by_ticker: list[dict],
    by_family: list[dict],
    by_hour: list[dict],
    worst_trades: list[dict],
    best_tickers: list[dict],
    worst_tickers: list[dict],
    worst_families: list[dict],
    roll_watch: list[dict],
    day_history: list[dict],
    recurring_tickers: list[dict],
    recurring_families: list[dict],
    margin_timeline: list[dict],
    margin_summary: list[dict],
    auto_policy: dict,
    restriction_rows: list[dict],
    render_auto_policy_markdown: Callable[[dict], str],
) -> None:
    write_text(analysis_dir / "daily_summary.md", summary_md)
    write_json(
        analysis_dir / "daily_summary.json",
        {
            "trade_date": trade_date,
            "generated_at": now_str(),
            "overall": overall,
            "open_positions": open_summary,
            "recommendations": recommendations,
            "best_consensus_scenario": best_consensus_scenario,
            "strategy_lab_top": strategy_lab[:10],
            "microstructure_top": microstructure_summary[:10],
            "margin_mode": runtime_trade_model.get("margin_mode"),
            "fee_model": runtime_trade_model.get("fee_model"),
        },
    )
    write_csv_rows(analysis_dir / "by_portfolio.csv", by_portfolio)
    write_csv_rows(analysis_dir / "by_group.csv", by_group)
    write_csv_rows(analysis_dir / "by_ticker.csv", by_ticker)
    write_csv_rows(analysis_dir / "by_family.csv", by_family)
    write_csv_rows(analysis_dir / "by_hour.csv", by_hour)
    write_csv_rows(analysis_dir / "worst_trades.csv", worst_trades)
    write_csv_rows(analysis_dir / "best_tickers.csv", best_tickers)
    write_csv_rows(analysis_dir / "worst_tickers.csv", worst_tickers)
    write_csv_rows(analysis_dir / "worst_families.csv", worst_families)
    write_csv_rows(analysis_dir / "roll_watch.csv", roll_watch)
    write_csv_rows(analysis_dir / "day_history.csv", day_history)
    write_csv_rows(analysis_dir / "recurring_killer_tickers.csv", recurring_tickers)
    write_csv_rows(analysis_dir / "recurring_killer_families.csv", recurring_families)
    write_csv_rows(analysis_dir / "microstructure_summary.csv", microstructure_summary)
    write_csv_rows(analysis_dir / "margin_timeline.csv", margin_timeline)
    write_csv_rows(analysis_dir / "margin_summary.csv", margin_summary)
    write_text(analysis_dir / "recommendations.md", "\n".join(f"- {line}" for line in recommendations) + ("\n" if recommendations else ""))
    write_json(analysis_dir / "auto_policy.json", auto_policy)
    write_text(analysis_dir / "auto_policy.md", render_auto_policy_markdown(auto_policy))
    write_csv_rows(analysis_dir / "restrictions_runtime.csv", restriction_rows)


def write_research_outputs(
    research_dir: Path,
    *,
    research_day: list[dict],
    research_all: list[dict],
    scenario_history: list[dict],
    scenario_consensus: list[dict],
    optimizer_candidates: list[dict],
    strategy_lab: list[dict],
    markdown_top: Callable[[str, list[dict], list[str], int], str],
) -> dict[str, int]:
    latest_day_rows = [dict(row, sample="latest_day") for row in research_day]
    all_sample_rows = [dict(row, sample="all_sample") for row in research_all]

    write_csv_rows(research_dir / "policy_sweep_latest_day.csv", latest_day_rows)
    write_csv_rows(research_dir / "policy_sweep_all_sample.csv", all_sample_rows)
    write_csv_rows(research_dir / "policy_sweep_daily_history.csv", scenario_history)
    write_csv_rows(research_dir / "policy_sweep_consensus.csv", scenario_consensus)
    write_text(
        research_dir / "research_summary.md",
        markdown_top("Research Top: Latest Day", research_day, ["scenario", "trades", "win_rate_pct", "net_rub", "expectancy_rub", "note"], limit=15)
        + "\n"
        + markdown_top("Research Top: All Sample", research_all, ["scenario", "trades", "win_rate_pct", "net_rub", "expectancy_rub", "note"], limit=15)
        + "\n"
        + markdown_top("Research Top: Consensus", scenario_consensus, ["scenario", "days", "beat_base_days", "delta_total_rub", "median_daily_net_rub", "worst_day_rub", "note"], limit=15),
    )
    write_csv_rows(research_dir / "optimizer_candidates.csv", optimizer_candidates)
    write_text(
        research_dir / "optimizer_summary.md",
        markdown_top(
            "Optimizer Candidates",
            optimizer_candidates,
            ["source", "scenario", "candidate_type", "recommended_use", "net_rub", "expectancy_rub", "delta_total_rub", "beat_base_days", "note"],
            limit=20,
        ),
    )
    strategy_lab_counts = build_strategy_lab_counts(strategy_lab)
    write_csv_rows(research_dir / "strategy_lab_candidates.csv", strategy_lab)
    write_text(
        research_dir / "strategy_lab_summary.md",
        "\n".join(
            [
                "# Strategy Lab",
                "",
                f"- total_candidates: {strategy_lab_counts['total']}",
                f"- runtime_policy: {strategy_lab_counts['runtime_policy']}",
                f"- shadow_backtest: {strategy_lab_counts['shadow_backtest']}",
                f"- research_then_runtime: {strategy_lab_counts['research_then_runtime']}",
                f"- autopromote_ready: {strategy_lab_counts['autopromote_ready']}",
                "",
                markdown_top(
                    "Top Strategy Lab Candidates",
                    strategy_lab,
                    ["rank", "candidate", "category", "action_type", "safe_mode", "autopromote_ready", "evidence", "recommended_next_step"],
                    limit=20,
                ),
            ]
        ),
    )
    return strategy_lab_counts


def copy_bundle_outputs(
    bundle_dir: Path,
    *,
    day_rows: list[dict],
    shadow_rows: list[dict],
    run_dir: Path,
    runtime_dir: Path,
    analysis_dir: Path,
    research_dir: Path,
) -> None:
    raw_dir = bundle_dir / "raw"
    ensure_dir(raw_dir)
    write_csv_rows(raw_dir / "day_primary_trades.csv", day_rows)
    if shadow_rows:
        write_csv_rows(raw_dir / "day_shadow_trades.csv", shadow_rows)

    for pattern in ["*_health.json", "*_paper_open_positions.json", "*_instrument_specs.csv", "*_startup_status.csv", "*_roll_state.json"]:
        for path in run_dir.glob(pattern):
            shutil.copy2(path, raw_dir / path.name)
    for pattern in ["*_wide_spread_review.csv", "*_shadow_exit_models.csv", "*_entry_shadow_models.csv"]:
        for path in run_dir.glob(pattern):
            shutil.copy2(path, raw_dir / path.name)

    write_text(raw_dir / "v7_paper_supervisor_20260525.tail.log", tail_text(runtime_dir / "v7_paper_supervisor_20260525.log", lines=500))
    write_text(raw_dir / "server_watchdog.tail.log", tail_text(runtime_dir / "server_watchdog.log", lines=500))

    shutil.copy2(analysis_dir / "daily_summary.md", bundle_dir / "daily_summary.md")
    shutil.copy2(analysis_dir / "auto_policy.md", bundle_dir / "auto_policy.md")
    shutil.copy2(research_dir / "research_summary.md", bundle_dir / "research_summary.md")
    shutil.copy2(research_dir / "optimizer_summary.md", bundle_dir / "optimizer_summary.md")
    shutil.copy2(research_dir / "strategy_lab_summary.md", bundle_dir / "strategy_lab_summary.md")
    for path in [
        research_dir / "strategy_review_summary.md",
        research_dir / "strategy_review_candidates.csv",
        analysis_dir / "day_history.csv",
        analysis_dir / "by_portfolio.csv",
        analysis_dir / "microstructure_summary.csv",
        analysis_dir / "margin_timeline.csv",
        analysis_dir / "margin_summary.csv",
        analysis_dir / "restrictions_runtime.csv",
        analysis_dir / "recurring_killer_tickers.csv",
        analysis_dir / "recurring_killer_families.csv",
        analysis_dir / "worst_tickers.csv",
        analysis_dir / "worst_families.csv",
        research_dir / "policy_sweep_latest_day.csv",
        research_dir / "policy_sweep_all_sample.csv",
        research_dir / "policy_sweep_daily_history.csv",
        research_dir / "policy_sweep_consensus.csv",
        research_dir / "optimizer_candidates.csv",
        research_dir / "strategy_lab_candidates.csv",
    ]:
        if path.exists():
            shutil.copy2(path, bundle_dir / path.name)


def build_manifest_payload(
    *,
    trade_date: str,
    overall: dict,
    open_summary: dict,
    runtime_trade_model: dict,
    margin_summary: list[dict],
    recommendations: list[str],
    research_day: list[dict],
    research_all: list[dict],
    best_consensus_scenario: dict,
    day_history: list[dict],
    worst_tickers: list[dict],
    worst_families: list[dict],
    recurring_tickers: list[dict],
    recurring_families: list[dict],
    microstructure_summary: list[dict],
    scenario_consensus: list[dict],
    optimizer_candidates: list[dict],
    strategy_lab: list[dict],
    strategy_lab_counts: dict[str, int],
    restriction_rows: list[dict],
    roll_watch: list[dict],
    auto_policy: dict,
) -> dict:
    return {
        "trade_date": trade_date,
        "generated_at": now_str(),
        "overall": overall,
        "open_positions": open_summary,
        "margin_mode": runtime_trade_model.get("margin_mode"),
        "fee_model": runtime_trade_model.get("fee_model"),
        "portfolio_margin_day": margin_summary,
        "recommendations": recommendations,
        "best_latest_day_scenario": research_day[0] if research_day else {},
        "best_all_sample_scenario": research_all[0] if research_all else {},
        "best_consensus_scenario": best_consensus_scenario,
        "day_history_tail": day_history[-10:],
        "day_class_counts": {
            "good_day": sum(1 for row in day_history if row.get("day_class") == "good_day"),
            "bad_day": sum(1 for row in day_history if row.get("day_class") == "bad_day"),
            "killer_day": sum(1 for row in day_history if row.get("day_class") == "killer_day"),
        },
        "top_killer_tickers": worst_tickers[:3],
        "top_killer_families": worst_families[:3],
        "recurring_killer_tickers": recurring_tickers[:5],
        "recurring_killer_families": recurring_families[:5],
        "microstructure_top": microstructure_summary[:10],
        "research_consensus_top": scenario_consensus[:10],
        "optimizer_top": optimizer_candidates[:10],
        "strategy_lab_top": strategy_lab[:10],
        "strategy_lab_counts": strategy_lab_counts,
        "restrictions_runtime": restriction_rows,
        "roll_watch": roll_watch[:12],
        "auto_policy": auto_policy,
    }


def persist_nightly_cycle_status(nightly_cycle_status: dict, *paths: Path) -> None:
    for path in paths:
        write_json(path, nightly_cycle_status)
