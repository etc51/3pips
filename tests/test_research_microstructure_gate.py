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

import research_microstructure_gate as rmg  # noqa: E402


class ResearchMicrostructureGateTest(unittest.TestCase):
    def test_build_and_persist_selects_best_threshold_for_negative_slice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            trade_date = "2026-06-15"
            research_dir = project_root / "reports" / "autonomy" / "research" / trade_date
            latest_dir = project_root / "reports" / "autonomy" / "latest"
            research_dir.mkdir(parents=True, exist_ok=True)
            latest_dir.mkdir(parents=True, exist_ok=True)

            wide_rows: list[dict] = []
            for index in range(90):
                ratio = 0.3 if index < 60 else 0.6
                wide_rows.append(
                    {
                        "snapshot_time": f"{trade_date} 10:{index % 60:02d}:00",
                        "portfolio_group": "TAIL_RESEARCH",
                        "contour": "aggressive",
                        "family": "MM",
                        "group": "TAIL_RESEARCH/AGGRESSIVE::MM",
                        "spread_to_stop_ratio": ratio,
                        "spread_class": "SPREAD_WATCH",
                    }
                )
            for index in range(120):
                ratio = 0.9 if index < 90 else 1.2
                wide_rows.append(
                    {
                        "snapshot_time": f"{trade_date} 11:{index % 60:02d}:00",
                        "portfolio_group": "TAIL_RESEARCH",
                        "contour": "aggressive",
                        "family": "MM",
                        "group": "TAIL_RESEARCH/AGGRESSIVE::MM",
                        "spread_to_stop_ratio": ratio,
                        "spread_class": "SPREAD_HEAVY",
                    }
                )
            for index in range(90):
                ratio = 1.5 if index < 60 else 2.1
                wide_rows.append(
                    {
                        "snapshot_time": f"{trade_date} 12:{index % 60:02d}:00",
                        "portfolio_group": "TAIL_RESEARCH",
                        "contour": "aggressive",
                        "family": "MM",
                        "group": "TAIL_RESEARCH/AGGRESSIVE::MM",
                        "spread_to_stop_ratio": ratio,
                        "spread_class": "SPREAD_DOMINATES",
                    }
                )

            all_trade_rows = [
                {
                    "closed_at": f"{trade_date} 14:00:00",
                    "portfolio_group": "TAIL_RESEARCH",
                    "contour": "AGGRESSIVE",
                    "family": "MM",
                    "net_rub": -1200.0,
                },
                {
                    "closed_at": f"{trade_date} 15:00:00",
                    "portfolio_group": "TAIL_RESEARCH",
                    "contour": "AGGRESSIVE",
                    "family": "MM",
                    "net_rub": -800.0,
                },
                {
                    "closed_at": "2026-06-14 15:00:00",
                    "portfolio_group": "TAIL_RESEARCH",
                    "contour": "AGGRESSIVE",
                    "family": "MM",
                    "net_rub": -600.0,
                },
            ]

            rows, summary = rmg.build_and_persist_microstructure_gate_research(
                project_root=project_root,
                trade_date=trade_date,
                all_wide_spread_rows=wide_rows,
                all_trade_rows=all_trade_rows,
                research_dir=research_dir,
                latest_dir=latest_dir,
            )

            self.assertTrue(rows)
            self.assertEqual(summary["trade_date"], trade_date)
            self.assertGreater(summary["backtest_candidates"], 0)
            self.assertEqual(summary["evaluation_state"], "review_event_proxy")
            self.assertTrue((research_dir / "microstructure_gate_research.csv").exists())
            self.assertTrue((latest_dir / "microstructure_gate_research_summary.json").exists())

            latest_day_top = next(row for row in rows if row["sample"] == "latest_day")
            self.assertEqual(latest_day_top["group"], "TAIL_RESEARCH/AGGRESSIVE::MM")
            self.assertEqual(float(latest_day_top["threshold_ratio"]), 0.75)
            self.assertEqual(latest_day_top["candidate_status"], "backtest_candidate")
            self.assertGreater(float(latest_day_top["toxic_capture_pct"]), 95.0)
            self.assertGreater(float(latest_day_top["watch_preserve_pct"]), 95.0)
            self.assertEqual(latest_day_top["evaluation_state"], "review_event_proxy")

            with (latest_dir / "microstructure_gate_research.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            self.assertTrue(csv_rows)
            self.assertIn("experiment_score", csv_rows[0])
            self.assertIn("candidate_status", csv_rows[0])

            summary_payload = json.loads((latest_dir / "microstructure_gate_research_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary_payload["top_candidate_group"], "TAIL_RESEARCH/AGGRESSIVE::MM")
            self.assertEqual(summary_payload["top_candidate_threshold"], 0.75)
            self.assertEqual(summary_payload["top_candidate_status"], "backtest_candidate")

    def test_summary_prefers_actionable_all_sample_candidate_over_latest_day_monitor_row(self) -> None:
        rows = [
            {
                "sample": "latest_day",
                "group": "TAIL_RESEARCH/AGGRESSIVE::BM",
                "threshold_ratio": 0.5,
                "candidate_status": "monitor_only",
                "experiment_score": 98.0,
            },
            {
                "sample": "all_sample",
                "group": "TAIL_RESEARCH/AGGRESSIVE::MM",
                "threshold_ratio": 0.75,
                "candidate_status": "backtest_candidate",
                "experiment_score": 88.0,
            },
        ]
        summary = rmg.summarize_research(rows, "2026-06-15")
        self.assertEqual(summary["top_candidate_group"], "TAIL_RESEARCH/AGGRESSIVE::MM")
        self.assertEqual(summary["top_candidate_threshold"], 0.75)
        self.assertEqual(summary["top_candidate_sample"], "all_sample")
        self.assertEqual(summary["top_candidate_status"], "backtest_candidate")


if __name__ == "__main__":
    unittest.main()
