from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import paper_dashboard as pdash  # noqa: E402


class PaperDashboardHealthTest(unittest.TestCase):
    def test_build_health_payload_is_lightweight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            reports_root = temp_root / "reports"
            base_dir = reports_root / "paper_runs" / "v7_live_20260525"
            latest_dir = reports_root / "autonomy" / "latest"
            base_dir.mkdir(parents=True)
            latest_dir.mkdir(parents=True)

            (base_dir / "portfolio_config.json").write_text(
                json.dumps({"portfolios": {"classic_core": {"capital": 800000}}}, ensure_ascii=False),
                encoding="utf-8",
            )
            (base_dir / "classic_core_paper_open_positions.json").write_text(
                json.dumps([{"ticker": "GLZ6"}, {"ticker": "PDM6"}], ensure_ascii=False),
                encoding="utf-8",
            )
            (base_dir / "classic_core_startup_status.csv").write_text("ticker,status\nGLZ6,loaded\n", encoding="utf-8")
            (latest_dir / "latest_auto_policy.json").write_text(
                json.dumps({"trade_date": "2026-06-15", "active": {}}, ensure_ascii=False),
                encoding="utf-8",
            )

            with patch.object(pdash, "REPORTS", reports_root), patch.object(
                pdash,
                "build_state",
                side_effect=AssertionError("build_state should not be used by health payload"),
            ):
                payload = pdash.build_health_payload(base_dir)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["positions"], 2)
        self.assertEqual(payload["autonomy_trade_date"], "2026-06-15")
        self.assertEqual(payload["dir"], str(base_dir))
        self.assertTrue(payload["last_update"])


if __name__ == "__main__":
    unittest.main()
