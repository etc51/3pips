from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import rebuild_research_only as rro  # noqa: E402


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


class RebuildResearchOnlyTest(unittest.TestCase):
    def test_main_rebuilds_research_without_touching_latest_auto_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            run_dir = project_root / "reports" / "paper_runs" / "v7_live_20260525"
            latest_dir = project_root / "reports" / "autonomy" / "latest"
            run_dir.mkdir(parents=True, exist_ok=True)
            latest_dir.mkdir(parents=True, exist_ok=True)

            write_csv(
                run_dir / "classic_core_multi_futures_paper_trades.csv",
                [
                    {"closed_at": "2026-06-14 11:00:00", "contour": "strict", "secid": "GLZ6", "qty": 1, "net_rub": 100.0},
                    {"closed_at": "2026-06-14 12:00:00", "contour": "aggressive", "secid": "GLM6", "qty": 1, "net_rub": -60.0},
                    {"closed_at": "2026-06-15 10:30:00", "contour": "strict", "secid": "GLZ6", "qty": 1, "net_rub": 250.0},
                    {"closed_at": "2026-06-15 12:30:00", "contour": "aggressive", "secid": "GLM6", "qty": 1, "net_rub": -300.0},
                    {"closed_at": "2026-06-15 16:00:00", "contour": "strict", "secid": "BRQ6", "qty": 1, "net_rub": 150.0},
                ],
            )
            write_csv(
                project_root / "reports" / "futures_scalp_profiles_v7_paper_20260525.csv",
                [
                    {"ticker": "GLZ6", "v7_family": "GL"},
                    {"ticker": "GLM6", "v7_family": "GL"},
                    {"ticker": "BRQ6", "v7_family": "BR"},
                ],
            )

            latest_auto_policy = {
                "active": {"entry_no_new_after": "11:59", "strict_only_families": ["GL"], "allow_aggressive_group_families": []},
                "summary": {"active_rule_count": 1},
            }
            latest_manifest = {"candidate_gate": {"pending_count": 3}, "nightly_cycle_status": {"status": "ok"}}
            (latest_dir / "latest_auto_policy.json").write_text(json.dumps(latest_auto_policy, ensure_ascii=False, indent=2), encoding="utf-8")
            (latest_dir / "latest_daily_manifest.json").write_text(json.dumps(latest_manifest, ensure_ascii=False, indent=2), encoding="utf-8")

            argv = [
                "rebuild_research_only.py",
                "--project-root",
                str(project_root),
                "--trade-date",
                "latest",
            ]

            with patch.object(sys, "argv", argv):
                rc = rro.main()

            self.assertEqual(rc, 0)
            self.assertEqual(
                json.loads((latest_dir / "latest_auto_policy.json").read_text(encoding="utf-8")),
                latest_auto_policy,
            )
            rebuilt_manifest = json.loads((latest_dir / "latest_daily_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(rebuilt_manifest["candidate_gate"], latest_manifest["candidate_gate"])
            self.assertEqual(rebuilt_manifest["research_rebuild"]["mode"], "research_only")
            self.assertEqual(rebuilt_manifest["trade_date"], "2026-06-15")

            research_dir = project_root / "reports" / "autonomy" / "research" / "2026-06-15"
            self.assertTrue((research_dir / "policy_sweep_latest_day.csv").exists())
            self.assertTrue((latest_dir / "research_strategy_registry.csv").exists())
            self.assertTrue((latest_dir / "paper_candidate_shortlist.csv").exists())
            self.assertTrue((latest_dir / "research_strategy_targets.csv").exists())

            with (research_dir / "policy_sweep_latest_day.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertIn("avg_win_rub", rows[0])
            self.assertIn("avg_loss_rub", rows[0])
            self.assertIn("top3_loss_rub", rows[0])

            with (latest_dir / "research_strategy_registry.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                registry_rows = list(csv.DictReader(handle))
            self.assertIn("sample_avg_win_rub", registry_rows[0])


if __name__ == "__main__":
    unittest.main()
