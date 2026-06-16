from __future__ import annotations

import csv
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import autonomy_common as ac  # noqa: E402


class AutonomyCommonAtomicWriteTest(unittest.TestCase):
    def test_write_json_replaces_file_without_leaving_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest_auto_policy.json"
            path.write_text('{"old": true}', encoding="utf-8")

            ac.write_json(path, {"status": "ok", "count": 3})

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"status": "ok", "count": 3})
            self.assertEqual(list(path.parent.glob(f"{path.name}.tmp.*")), [])

    def test_write_text_replaces_file_without_leaving_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily_summary.md"
            path.write_text("old\n", encoding="utf-8")

            ac.write_text(path, "# Summary\n\nfresh\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "# Summary\n\nfresh\n")
            self.assertEqual(list(path.parent.glob(f"{path.name}.tmp.*")), [])

    def test_write_csv_rows_replaces_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "by_portfolio.csv"
            path.write_text("broken", encoding="utf-8")

            ac.write_csv_rows(
                path,
                [
                    {"portfolio": "classic_core", "net_rub": 100.5},
                    {"portfolio": "neo", "net_rub": 250.0},
                ],
            )

            with path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["portfolio"], "classic_core")
            self.assertEqual(rows[0]["net_rub"], "100.5")
            self.assertEqual(rows[1]["portfolio"], "neo")
            self.assertEqual(rows[1]["net_rub"], "250.0")
            self.assertEqual(list(path.parent.glob(f"{path.name}.tmp.*")), [])

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits are required")
    def test_write_json_sets_shared_read_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server_watchdog_state.json"

            ac.write_json(path, {"status": "ok"})

            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, ac.DEFAULT_SHARED_FILE_MODE)


if __name__ == "__main__":
    unittest.main()
