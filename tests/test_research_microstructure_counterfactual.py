from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import research_microstructure_counterfactual as rmc  # noqa: E402


class ResearchMicrostructureCounterfactualTest(unittest.TestCase):
    def test_build_and_persist_dedupes_entry_rows_and_prefers_actionable_all_sample_candidate(self) -> None:
        rows: list[dict] = []

        def add_entry(entry_id: str, closed_at: str, spread_ratio: float, net_rub: float) -> None:
            for model in ["tv_ema_rsi_adx_trend", "forum_chop_donchian_guard"]:
                rows.append(
                    {
                        "entry_id": entry_id,
                        "opened_at": closed_at.replace(":00", ":30", 1),
                        "closed_at": closed_at,
                        "portfolio_group": "TAIL_RESEARCH",
                        "contour": "AGGRESSIVE",
                        "family": "MM",
                        "secid": "MMH7",
                        "model": model,
                        "allow": "true",
                        "spread_to_stop_ratio": spread_ratio,
                        "net_rub": net_rub,
                    }
                )

        add_entry("e01", "2026-06-14 10:05:00", 1.40, -300.0)
        add_entry("e02", "2026-06-14 10:25:00", 1.50, -250.0)
        add_entry("e03", "2026-06-14 10:45:00", 1.60, -200.0)
        add_entry("e04", "2026-06-14 11:05:00", 0.20, 200.0)
        add_entry("e05", "2026-06-14 11:25:00", 0.30, 150.0)
        add_entry("e06", "2026-06-15 10:05:00", 0.40, 100.0)
        add_entry("e07", "2026-06-15 10:25:00", 0.50, 80.0)
        add_entry("e08", "2026-06-15 10:45:00", 0.60, 60.0)
        add_entry("e09", "2026-06-15 11:05:00", 0.70, -50.0)
        add_entry("e10", "2026-06-15 11:25:00", 0.80, 40.0)

        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            research_dir = project_root / "reports" / "autonomy" / "research" / "2026-06-15"
            latest_dir = project_root / "reports" / "autonomy" / "latest"
            counter_rows, summary = rmc.build_and_persist_microstructure_counterfactual(
                project_root=project_root,
                trade_date="2026-06-15",
                all_entry_shadow_rows=rows,
                research_dir=research_dir,
                latest_dir=latest_dir,
            )

            self.assertTrue(counter_rows)
            self.assertEqual(summary["unique_entries"], 10)
            self.assertEqual(summary["evaluation_state"], "trade_level_counterfactual")
            self.assertEqual(summary["top_candidate_group"], "TAIL_RESEARCH/AGGRESSIVE::MM")
            self.assertEqual(summary["top_candidate_threshold"], 0.75)
            self.assertEqual(summary["top_candidate_sample"], "all_sample")
            self.assertEqual(summary["top_candidate_status"], "candidate")

            top_row = counter_rows[0]
            self.assertEqual(top_row["unique_entries"], 10)
            self.assertEqual(top_row["candidate_status"], "candidate")
            self.assertEqual(top_row["sample"], "all_sample")

            latest_summary = json.loads((latest_dir / "microstructure_counterfactual_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(latest_summary["top_candidate_status"], "candidate")
            self.assertTrue((latest_dir / "microstructure_counterfactual.csv").exists())
            self.assertTrue((latest_dir / "microstructure_counterfactual.md").exists())


if __name__ == "__main__":
    unittest.main()
