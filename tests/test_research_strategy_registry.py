from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import research_strategy_registry as rsr  # noqa: E402


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class ResearchStrategyRegistryTest(unittest.TestCase):
    def test_build_and_persist_from_strategy_lab_and_manifest(self) -> None:
        trade_date = "2026-06-16"
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            research_dir = project_root / "reports" / "autonomy" / "research" / trade_date
            latest_dir = project_root / "reports" / "autonomy" / "latest"
            bundle_dir = project_root / "reports" / "autonomy" / "archives" / f"bundle_{trade_date}"
            write_csv(
                research_dir / "strategy_lab_candidates.csv",
                [
                    {
                        "hypothesis_id": "runtime_strict_primary",
                        "priority": 98,
                        "category": "runtime_policy",
                        "candidate": "strict primary baseline",
                        "scope": "all classic futures",
                        "action_type": "runtime_policy",
                        "safe_mode": "paper_autopolicy",
                        "autopromote_ready": True,
                        "evidence": "delta_total=4072.82",
                        "recommended_next_step": "keep strict baseline",
                        "required_features": "trade csv",
                        "scenario_anchor": "contour_only_strict",
                        "rank": 1,
                    },
                    {
                        "hypothesis_id": "shadow_vwap_probe",
                        "priority": 80,
                        "category": "new_strategy",
                        "candidate": "VWAP reversion probe",
                        "scope": "Si",
                        "action_type": "shadow_backtest",
                        "safe_mode": "research_only",
                        "autopromote_ready": False,
                        "evidence": "worst_family=Si",
                        "recommended_next_step": "build shadow layer",
                        "required_features": "1m candles",
                        "scenario_anchor": "blackout_1200_1559",
                        "rank": 2,
                    },
                ],
            )
            manifest_payload = {
                "trade_date": trade_date,
                "overall": {"trades": 37, "net_rub": 791.96},
                "optimizer_top": [
                    {
                        "scenario": "contour_only_strict",
                        "candidate_type": "runtime_policy",
                        "recommended_use": "candidate_runtime_tune",
                    }
                ],
                "research_consensus_top": [
                    {
                        "scenario": "contour_only_strict",
                        "days": 2,
                        "positive_days": 2,
                        "negative_days": 0,
                        "beat_base_days": 1,
                        "beat_base_pct": 50.0,
                        "delta_total_rub": 4072.82,
                        "latest_day_delta_rub": 3191.01,
                        "latest_day_rub": 4989.81,
                        "median_daily_net_rub": 3600.0,
                        "worst_day_rub": 1200.0,
                        "best_day_rub": 4989.81,
                    }
                ],
            }

            rows, summary = rsr.build_and_persist_research_strategy_registry(
                project_root=project_root,
                trade_date=trade_date,
                manifest_payload=manifest_payload,
                research_dir=research_dir,
                latest_dir=latest_dir,
                bundle_dir=bundle_dir,
            )

            self.assertEqual(summary["rows"], 2)
            self.assertEqual(summary["paper_candidates"], 1)
            self.assertEqual(rows[0]["status"], "paper_candidate")
            self.assertEqual(rows[0]["paper_route"], "paper_autopolicy")
            self.assertEqual(rows[0]["beat_base_days"], 1)
            self.assertEqual(rows[0]["delta_total_rub"], 4072.82)
            self.assertEqual(rows[1]["status"], "research_only")

            latest_csv = latest_dir / "research_strategy_registry.csv"
            latest_summary = latest_dir / "research_strategy_registry_summary.json"
            latest_md = latest_dir / "research_strategy_registry.md"
            canonical_csv = project_root / "data" / "processed" / "research_strategy_registry.csv"
            self.assertTrue(latest_csv.exists())
            self.assertTrue(latest_summary.exists())
            self.assertTrue(latest_md.exists())
            self.assertTrue(canonical_csv.exists())

            summary_payload = json.loads(latest_summary.read_text(encoding="utf-8"))
            self.assertEqual(summary_payload["rows"], 2)
            self.assertIn("strategy_lab_candidates", summary_payload["by_source"])
            self.assertIn("paper_autopolicy", summary_payload["by_paper_route"])

    def test_build_and_persist_absorbs_optional_screener_third_pass_and_portfolio_sources(self) -> None:
        trade_date = "2026-06-16"
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            research_dir = project_root / "reports" / "autonomy" / "research" / trade_date
            latest_dir = project_root / "reports" / "autonomy" / "latest"
            write_csv(research_dir / "strategy_lab_candidates.csv", [])
            write_csv(
                project_root / "results" / "screener_latest.csv",
                [
                    {
                        "family": "NG",
                        "instrument_type": "spread",
                        "series": "front_next",
                        "spread": "NGK6-NGM6",
                        "secid": "NGK6",
                        "front_secid": "NGK6",
                        "back_secid": "NGM6",
                        "action": "SHORT",
                        "pattern": "season_window",
                        "holding_days": 20,
                        "score": 11.2,
                        "ann_sharpe": 3.64,
                        "n_trades": 187,
                        "p_adj_bh": 0.0,
                    }
                ],
            )
            write_csv(
                project_root / "reports" / "third_pass_strategy_summary.csv",
                [
                    {
                        "strategy_mode": "fixed_plus1_only",
                        "portfolio_mode": "global_no_overlap",
                        "slippage_ticks_roundtrip": 2,
                        "fee_rub_per_contract_roundtrip": 2,
                        "threshold_objective": "train_mean",
                        "net_pnl_rub_sum": 22911.64,
                        "max_drawdown_rub": -4113.92,
                        "total_months": 20,
                        "positive_months": 14,
                        "worst_month_net_pnl_rub": -2200.0,
                        "best_month_net_pnl_rub": 5100.0,
                        "simple_total_return_on_go": 0.42,
                    }
                ],
            )
            write_csv(
                project_root / "reports" / "third_pass_feature_selection_log.csv",
                [
                    {
                        "strategy_mode": "fixed_plus1_only",
                        "cost_scenario": "2ticks_2rub",
                        "threshold_objective": "train_mean",
                        "selected_feature_set": "plus1_only",
                        "selected_threshold": 0.0,
                    }
                ],
            )
            write_csv(
                project_root / "results" / "portfolio_strategy_summary.csv",
                [
                    {
                        "strategy_id": "calendar_pullback",
                        "family": "NG",
                        "holding_days_param": 20,
                        "stop_mode": "atr",
                        "stop_value": 1.5,
                        "take_profit_r": "none",
                        "slippage_bps": 10.0,
                        "period": "test_2024_2026",
                        "worst_trade_rub": -1800.0,
                    }
                ],
            )
            write_csv(
                project_root / "results" / "portfolio_strategy_sensitivity.csv",
                [
                    {
                        "strategy_id": "calendar_pullback",
                        "family": "NG",
                        "holding_days_param": 20,
                        "stop_mode": "atr",
                        "stop_value": 1.5,
                        "take_profit_r": "none",
                        "slippage_bps": 10.0,
                        "test_total_return": 0.31,
                        "test_max_drawdown": -0.08,
                        "test_profit_factor": 1.6,
                        "passed_test": True,
                    }
                ],
            )

            rows, summary = rsr.build_and_persist_research_strategy_registry(
                project_root=project_root,
                trade_date=trade_date,
                manifest_payload={"trade_date": trade_date},
                research_dir=research_dir,
                latest_dir=latest_dir,
            )

            sources = {row["registry_source"] for row in rows}
            self.assertEqual(summary["rows"], 3)
            self.assertEqual(
                sources,
                {
                    "screener_latest",
                    "third_pass_strategy_summary",
                    "portfolio_strategy_summary",
                },
            )
            leadlag_row = next(row for row in rows if row["registry_source"] == "third_pass_strategy_summary")
            self.assertEqual(leadlag_row["paper_route"], "leadlag_orderbook_monitor")
            self.assertEqual(leadlag_row["status"], "paper_candidate")
            screener_row = next(row for row in rows if row["registry_source"] == "screener_latest")
            self.assertEqual(screener_row["paper_route"], "observe_only")
            portfolio_row = next(row for row in rows if row["registry_source"] == "portfolio_strategy_summary")
            self.assertEqual(portfolio_row["validation_state"], "portfolio_oos_pass")
            self.assertTrue((project_root / "data" / "processed" / "research_strategy_registry.csv").exists())

    def test_build_and_persist_enriches_strategy_lab_rows_with_policy_sweep_quality_metrics(self) -> None:
        trade_date = "2026-06-16"
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            research_dir = project_root / "reports" / "autonomy" / "research" / trade_date
            latest_dir = project_root / "reports" / "autonomy" / "latest"
            write_csv(
                research_dir / "strategy_lab_candidates.csv",
                [
                    {
                        "hypothesis_id": "runtime_strict_primary",
                        "priority": 98,
                        "category": "runtime_policy",
                        "candidate": "strict primary baseline",
                        "scope": "all classic futures",
                        "action_type": "runtime_policy",
                        "safe_mode": "paper_autopolicy",
                        "autopromote_ready": True,
                        "evidence": "delta_total=4072.82",
                        "recommended_next_step": "keep strict baseline",
                        "required_features": "trade csv",
                        "scenario_anchor": "contour_only_strict",
                        "rank": 1,
                    }
                ],
            )
            write_csv(
                research_dir / "policy_sweep_all_sample.csv",
                [
                    {
                        "scenario": "contour_only_strict",
                        "trades": 24,
                        "wins": 20,
                        "losses": 4,
                        "win_rate_pct": 83.33,
                        "net_rub": 3876.48,
                        "expectancy_rub": 161.52,
                        "profit_factor": 1.9597,
                    }
                ],
            )
            write_csv(
                research_dir / "policy_sweep_latest_day.csv",
                [
                    {
                        "scenario": "contour_only_strict",
                        "trades": 20,
                        "wins": 18,
                        "losses": 2,
                        "win_rate_pct": 90.0,
                        "net_rub": 4989.81,
                        "expectancy_rub": 249.49,
                        "profit_factor": 3.7361,
                    }
                ],
            )

            rows, _summary = rsr.build_and_persist_research_strategy_registry(
                project_root=project_root,
                trade_date=trade_date,
                manifest_payload={"trade_date": trade_date},
                research_dir=research_dir,
                latest_dir=latest_dir,
            )

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["sample_trades"], 24)
            self.assertEqual(rows[0]["sample_wins"], 20)
            self.assertEqual(rows[0]["sample_losses"], 4)
            self.assertEqual(rows[0]["sample_win_rate_pct"], 83.33)
            self.assertEqual(rows[0]["sample_net_rub"], 3876.48)
            self.assertEqual(rows[0]["sample_expectancy_rub"], 161.52)
            self.assertEqual(rows[0]["sample_profit_factor"], 1.9597)
            self.assertEqual(rows[0]["latest_day_trades"], 20)
            self.assertEqual(rows[0]["latest_day_expectancy_rub"], 249.49)
            self.assertEqual(rows[0]["latest_day_profit_factor"], 3.7361)

    def test_build_and_persist_falls_back_to_manifest_strategy_lab_top_when_csv_unavailable(self) -> None:
        trade_date = "2026-06-16"
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            research_dir = project_root / "reports" / "autonomy" / "research" / trade_date
            latest_dir = project_root / "reports" / "autonomy" / "latest"
            manifest_payload = {
                "trade_date": trade_date,
                "strategy_lab_top": [
                    {
                        "hypothesis_id": "runtime_strict_primary",
                        "priority": 98,
                        "category": "runtime_policy",
                        "candidate": "strict primary baseline",
                        "scope": "all classic futures",
                        "action_type": "runtime_policy",
                        "safe_mode": "paper_autopolicy",
                        "autopromote_ready": True,
                        "evidence": "delta_total=4072.82",
                        "recommended_next_step": "keep strict baseline",
                        "required_features": "trade csv",
                        "scenario_anchor": "contour_only_strict",
                        "rank": 1,
                    }
                ],
            }

            rows, summary = rsr.build_and_persist_research_strategy_registry(
                project_root=project_root,
                trade_date=trade_date,
                manifest_payload=manifest_payload,
                research_dir=research_dir,
                latest_dir=latest_dir,
            )

            self.assertEqual(summary["rows"], 1)
            self.assertEqual(rows[0]["registry_source"], "strategy_lab_candidates")
            self.assertEqual(rows[0]["candidate_label"], "strict primary baseline")
