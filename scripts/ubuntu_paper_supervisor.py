#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen

from autonomy_common import write_json, write_text


RUN_NAME = "v7_live_20260525"
MARGIN_MODE = "leveraged_paper"
BROKER = "tbank"
TARIFF = "premium"
FEE_MODEL = {
    "broker": BROKER,
    "tariff": TARIFF,
    "futures_rate_per_side_pct": 0.025,
    "futures_rate_per_side_fraction": 0.00025,
    "rate_tiers_daily_turnover_rub": [
        {"up_to_rub": 12_000_000, "rate_pct_per_side": 0.025},
        {"up_to_rub": 17_000_000, "rate_pct_per_side": 0.020},
        {"above_rub": 17_000_000, "rate_pct_per_side": 0.015},
    ],
    "note": "Premium futures fee model: conservative 0.025% of contract notional per side for turnover up to 12M RUB/day; real fee can be lower at higher daily turnover.",
}
PORTFOLIOS = {
    "classic_core": ["PTZ6", "PDU6", "SiM7", "BRU6", "SVH7", "BRQ6", "PTM6", "BTN6", "BTM6", "BTK6", "PTU6", "LKU6", "BRV6"],
    "gl_watch": ["GLH7", "GLZ6", "GLM6"],
    "neo": ["AMDperpA", "COINperpA", "TSLAperpA"],
    "tail_research": ["BRN6", "PDM6", "MMH7", "SiH7", "MMZ6", "BMN6", "BMM6", "BMV6", "BMX6", "BMU6", "S1H7", "BRX6", "BMQ6", "S1Z6", "SVZ6"],
}


class Supervisor:
    def __init__(self, args: argparse.Namespace) -> None:
        self.root = Path(args.project_root).resolve()
        self.python = args.python
        self.dashboard_host = args.dashboard_host
        self.dashboard_port = int(args.dashboard_port)
        self.loop_sec = int(args.loop_sec)
        self.stale_sec = int(args.stale_sec)
        self.startup_grace_sec = int(args.startup_grace_sec)
        self.once = bool(args.once)
        self.runtime_dir = self.root / "reports" / "runtime"
        self.run_dir = self.root / "reports" / "paper_runs" / RUN_NAME
        self.log_path = self.runtime_dir / "v7_paper_supervisor_20260525.log"
        self.pid_path = self.runtime_dir / "v7_paper_supervisor_20260525.pid"
        self.start_times: dict[str, float] = {}
        self.dashboard_failures = 0

    def setup(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if not self.once:
            self.pid_path.write_text(str(os.getpid()), encoding="ascii")
        self.write_portfolio_config()
        for name in PORTFOLIOS:
            self.ensure_open_positions_file(name)
        self.log(
            f"supervisor_start once={self.once} root={self.root} "
            f"stale_sec={self.stale_sec} startup_grace_sec={self.startup_grace_sec}"
        )

    def log(self, message: str) -> None:
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        print(line, flush=True)

    def bot_pid_path(self, name: str) -> Path:
        return self.runtime_dir / f"v7_paper_{name}.pid"

    def dashboard_pid_path(self) -> Path:
        return self.runtime_dir / "v7_paper_dashboard_20260525.pid"

    def ensure_open_positions_file(self, name: str) -> None:
        path = self.run_dir / f"{name}_paper_open_positions.json"
        if not path.exists():
            write_text(path, "[]\n")

    def backup_open_positions_file(self, name: str) -> None:
        path = self.run_dir / f"{name}_paper_open_positions.json"
        if not path.exists():
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = self.run_dir / f"{name}_paper_open_positions_before_restart_{stamp}.json"
        backup.write_bytes(path.read_bytes())

    def write_portfolio_config(self) -> None:
        payload = {
            "run_name": RUN_NAME,
            "broker": BROKER,
            "tariff": TARIFF,
            "margin_mode": MARGIN_MODE,
            "fee_model": FEE_MODEL,
            "capital_per_contour": 800000,
            "profiles_csv": "reports/futures_scalp_profiles_v7_paper_20260525.csv",
            "portfolios": {
                name: {"capital": 800000, "tickers": tickers}
                for name, tickers in PORTFOLIOS.items()
            },
        }
        path = self.run_dir / "portfolio_config.json"
        write_json(path, payload)

    def ensure_bot_runtime_logs(self, name: str) -> None:
        for suffix in ("multi_paper.log", "multi_paper.err.log"):
            path = self.run_dir / f"{name}_{suffix}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)

    def read_pid(self, path: Path) -> int | None:
        try:
            return int(path.read_text(encoding="ascii").strip())
        except Exception:
            return None

    def process_alive(self, pid: int | None) -> bool:
        if pid is None or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def stop_pid(self, pid: int | None, reason: str) -> None:
        if not self.process_alive(pid):
            return
        self.log(f"stop pid={pid} reason={reason}")
        try:
            os.killpg(pid, signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                return
        deadline = time.time() + 10
        while time.time() < deadline:
            if not self.process_alive(pid):
                return
            time.sleep(0.2)
        try:
            os.killpg(pid, signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass

    def process_age_sec(self, name: str) -> int:
        started = self.start_times.get(name)
        if started is None:
            return 999999
        return int(time.time() - started)

    def file_age_sec(self, path: Path) -> int | None:
        if not path.exists():
            return None
        return int(time.time() - path.stat().st_mtime)

    def bot_args(self, name: str, secids: list[str]) -> list[str]:
        args = [
            "src/multi_futures_paper.py",
            "--paper-only",
            "--secids",
            *secids,
            "--runtime-sec", "86400",
            "--report-sec", "600",
            "--seed-minutes", "240",
            "--orderbook-depth", "10",
            "--profiles", "reports/futures_scalp_profiles_v7_paper_20260525.csv",
            "--paper-capital", "800000",
            "--max-total-margin-pct", "0.80",
            "--max-position-margin-pct", "0.20",
            "--max-full-stop-rub", "1000",
            "--stop-limit-emergency-ticks", "2",
            "--actual-exit-model", "candle_like",
            "--stream-stale-sec", "15",
            "--fallback-poll-sec", "2",
            "--no-trade-before", "10:15",
            "--no-new-after", "17:45",
            "--force-close-at", "23:50",
            "--no-new-expiry-days", "10",
            "--expiry-force-close-days", "3",
            "--roll-observe-days", "21",
            "--roll-state-log", f"reports/paper_runs/{RUN_NAME}/{name}_roll_state.json",
            "--snapshot-sec", "10",
            "--log", f"reports/paper_runs/{RUN_NAME}/{name}_multi_futures_paper_trades.csv",
            "--snapshot-log", f"reports/paper_runs/{RUN_NAME}/{name}_live_orderbook_snapshots.csv",
            "--open-positions-log", f"reports/paper_runs/{RUN_NAME}/{name}_paper_open_positions.json",
            "--instrument-specs-log", f"reports/paper_runs/{RUN_NAME}/{name}_instrument_specs.csv",
            "--startup-status-log", f"reports/paper_runs/{RUN_NAME}/{name}_startup_status.csv",
            "--shadow-log", f"reports/paper_runs/{RUN_NAME}/{name}_shadow_exit_models.csv",
            "--health-log", f"reports/paper_runs/{RUN_NAME}/{name}_health.json",
            "--auto-policy-path", "reports/autonomy/latest/latest_auto_policy.json",
            "--auto-policy-reload-sec", "30",
        ]
        if name == "neo":
            args = [arg for arg in args if arg not in {"--no-trade-before", "10:15", "--no-new-after", "17:45"}]
            args += ["--no-new-after", "19:00"]
        return args

    def start_process(self, name: str, argv: list[str], pid_path: Path, reason: str) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if name.startswith("bot="):
            bot_name = name.split("=", 1)[1]
            stdout = self.run_dir / f"{bot_name}_multi_paper.log"
            stderr = self.run_dir / f"{bot_name}_multi_paper.err.log"
        else:
            stdout = self.run_dir / f"{name}_supervisor_{stamp}.stdout.log"
            stderr = self.run_dir / f"{name}_supervisor_{stamp}.stderr.log"
        cmd = [self.python, *argv]
        self.log(f"start {name} reason={reason} cmd={' '.join(cmd)}")
        out = stdout.open("ab")
        err = stderr.open("ab")
        proc = subprocess.Popen(
            cmd,
            cwd=self.root,
            stdout=out,
            stderr=err,
            start_new_session=True,
            close_fds=True,
        )
        pid_path.write_text(str(proc.pid), encoding="ascii")
        self.start_times[name] = time.time()
        self.log(f"started {name} pid={proc.pid}")

    def restart_bot(self, name: str, reason: str) -> None:
        pid_path = self.bot_pid_path(name)
        self.ensure_open_positions_file(name)
        self.ensure_bot_runtime_logs(name)
        self.backup_open_positions_file(name)
        self.stop_pid(self.read_pid(pid_path), reason)
        self.start_process(f"bot={name}", self.bot_args(name, PORTFOLIOS[name]), pid_path, reason)

    def restart_dashboard(self, reason: str) -> None:
        pid_path = self.dashboard_pid_path()
        self.dashboard_failures = 0
        self.stop_pid(self.read_pid(pid_path), reason)
        argv = [
            "src/paper_dashboard.py",
            "--host", self.dashboard_host,
            "--port", str(self.dashboard_port),
            "--dir", f"reports/paper_runs/{RUN_NAME}",
        ]
        self.start_process("dashboard", argv, pid_path, reason)

    def check_bot(self, name: str) -> None:
        pid = self.read_pid(self.bot_pid_path(name))
        if not self.process_alive(pid):
            self.restart_bot(name, "missing_process")
            return
        age = self.process_age_sec(f"bot={name}")
        health_path = self.run_dir / f"{name}_health.json"
        health_age = self.file_age_sec(health_path)
        if health_age is None:
            if age < self.startup_grace_sec:
                self.log(f"wait bot={name} reason=missing_health startup_age_sec={age}")
                return
            self.restart_bot(name, "missing_health")
            return
        if health_age > self.stale_sec and age >= self.startup_grace_sec:
            self.restart_bot(name, f"stale_health_{health_age}s")
            return
        snapshot_age = self.file_age_sec(self.run_dir / f"{name}_live_orderbook_snapshots.csv")
        if snapshot_age is not None and snapshot_age > 2 * self.stale_sec and age >= self.startup_grace_sec:
            self.restart_bot(name, f"stale_snapshot_{snapshot_age}s")
            return
        self.log(f"ok bot={name} pid={pid} age_sec={age} health_age_sec={health_age}")

    def dashboard_http_ok(self) -> bool:
        try:
            with urlopen(f"http://127.0.0.1:{self.dashboard_port}/healthz", timeout=3) as response:
                return 200 <= response.status < 500
        except Exception:
            return False

    def check_dashboard(self) -> None:
        pid = self.read_pid(self.dashboard_pid_path())
        if not self.process_alive(pid):
            self.dashboard_failures = 0
            self.restart_dashboard("missing_process")
            return
        if not self.dashboard_http_ok():
            self.dashboard_failures += 1
            if self.dashboard_failures < 2:
                self.log(f"wait dashboard pid={pid} reason=http_check_failed failures={self.dashboard_failures}")
                return
            self.restart_dashboard(f"http_check_failed_{self.dashboard_failures}x")
            return
        self.dashboard_failures = 0
        self.log(f"ok dashboard pid={pid}")

    def run(self) -> None:
        self.setup()
        while True:
            for name in PORTFOLIOS:
                self.check_bot(name)
            self.check_dashboard()
            if self.once:
                return
            time.sleep(self.loop_sec)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--python", default="/usr/bin/python3")
    parser.add_argument("--dashboard-host", default="0.0.0.0")
    parser.add_argument("--dashboard-port", default="8768")
    parser.add_argument("--loop-sec", type=int, default=15)
    parser.add_argument("--stale-sec", type=int, default=90)
    parser.add_argument("--startup-grace-sec", type=int, default=180)
    parser.add_argument("--once", action="store_true")
    Supervisor(parser.parse_args()).run()


if __name__ == "__main__":
    main()
