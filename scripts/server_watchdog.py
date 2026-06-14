#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import smtplib
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.request import urlopen


RUN_NAME_DEFAULT = "v7_live_20260525"
CONTOURS = ("classic_core", "gl_watch", "neo", "tail_research", "stock_watch")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def read_secret_value(env_name: str, file_env_name: str) -> str:
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    file_path = os.environ.get(file_env_name, "").strip()
    if not file_path:
        return ""
    path = Path(file_path)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore").strip()


def iso_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def to_epoch(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def tail_lines(path: Path, count: int = 40) -> list[str]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []
    if len(lines) <= count:
        return lines
    return lines[-count:]


def parse_docker_started_at(raw: str) -> float | None:
    text = (raw or "").strip()
    if not text or text == "0001-01-01T00:00:00Z":
        return None
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


class Watchdog:
    def __init__(self, args: argparse.Namespace) -> None:
        self.root = Path(args.project_root).resolve()
        self.run_name = args.run_name
        self.runtime_dir = self.root / "reports" / "runtime"
        self.run_dir = self.root / "reports" / "paper_runs" / self.run_name
        self.log_path = self.runtime_dir / "server_watchdog.log"
        self.state_path = self.runtime_dir / "server_watchdog_state.json"
        self.docker_env_file = args.docker_env_file
        self.dashboard_port = args.dashboard_port
        self.health_stale_sec = args.health_stale_sec
        self.snapshot_stale_sec = args.snapshot_stale_sec
        self.startup_grace_sec = args.startup_grace_sec
        self.remediation_wait_sec = args.remediation_wait_sec
        self.reminder_sec = args.reminder_sec
        self.once = bool(args.once)
        self.no_remediate = bool(args.no_remediate)
        self.paper_container = args.paper_container
        self.archive_container = args.archive_container
        self.dashboard_url = f"http://127.0.0.1:{self.dashboard_port}/"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

    def log(self, message: str) -> None:
        line = f"[{iso_now()}] {message}"
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, flush=True)

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "status": "healthy",
                "fingerprint": "",
                "last_email_epoch": 0.0,
                "last_change": iso_now(),
                "last_summary": "",
            }
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
        return {
            "status": "healthy",
            "fingerprint": "",
            "last_email_epoch": 0.0,
            "last_change": iso_now(),
            "last_summary": "",
        }

    def save_state(self, state: dict[str, Any]) -> None:
        self.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def run_cmd(self, argv: list[str], timeout: int = 120) -> CommandResult:
        proc = subprocess.run(
            argv,
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
        )
        return CommandResult(
            argv=argv,
            returncode=proc.returncode,
            stdout=proc.stdout.strip(),
            stderr=proc.stderr.strip(),
        )

    def docker_compose(self, *args: str, timeout: int = 300) -> CommandResult:
        argv = ["docker", "compose"]
        if self.docker_env_file:
            argv += ["--env-file", self.docker_env_file]
        argv += list(args)
        return self.run_cmd(argv, timeout=timeout)

    def inspect_container(self, name: str) -> dict[str, Any]:
        result = self.run_cmd(
            [
                "docker",
                "inspect",
                "-f",
                "{{.State.Running}}|{{.State.Status}}|{{.State.StartedAt}}",
                name,
            ],
            timeout=30,
        )
        if result.returncode != 0:
            return {
                "name": name,
                "exists": False,
                "running": False,
                "status": "missing",
                "age_sec": None,
                "started_at": "",
                "error": result.stderr or result.stdout,
            }
        raw_running, raw_status, raw_started_at = (result.stdout.split("|", 2) + ["", "", ""])[:3]
        started_epoch = parse_docker_started_at(raw_started_at)
        age_sec = None if started_epoch is None else int(time.time() - started_epoch)
        return {
            "name": name,
            "exists": True,
            "running": raw_running.strip().lower() == "true",
            "status": raw_status.strip(),
            "age_sec": age_sec,
            "started_at": raw_started_at.strip(),
            "error": "",
        }

    def dashboard_ok(self) -> tuple[bool, str]:
        try:
            with urlopen(self.dashboard_url, timeout=5) as response:
                ok = 200 <= response.status < 500
                return ok, f"http_status={response.status}"
        except Exception as exc:
            return False, str(exc)

    def file_age_sec(self, path: Path) -> int | None:
        if not path.exists():
            return None
        return int(time.time() - path.stat().st_mtime)

    def collect_health_issues(self, container_age_sec: int | None) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        if not self.run_dir.exists():
            if container_age_sec is None or container_age_sec >= self.startup_grace_sec:
                issues.append(
                    {
                        "kind": "run_dir_missing",
                        "target": self.run_dir.as_posix(),
                        "detail": "run directory is missing",
                    }
                )
            return issues

        for contour in CONTOURS:
            health_path = self.run_dir / f"{contour}_health.json"
            health_age = self.file_age_sec(health_path)
            if health_age is None:
                if container_age_sec is None or container_age_sec >= self.startup_grace_sec:
                    issues.append(
                        {
                            "kind": "health_missing",
                            "target": contour,
                            "detail": f"{health_path.name} missing",
                        }
                    )
                continue
            if health_age > self.health_stale_sec:
                issues.append(
                    {
                        "kind": "health_stale",
                        "target": contour,
                        "detail": f"{health_path.name} age={health_age}s",
                    }
                )

            snapshot_path = self.run_dir / f"{contour}_live_orderbook_snapshots.csv"
            snapshot_age = self.file_age_sec(snapshot_path)
            if snapshot_age is not None and snapshot_age > self.snapshot_stale_sec:
                issues.append(
                    {
                        "kind": "snapshot_stale",
                        "target": contour,
                        "detail": f"{snapshot_path.name} age={snapshot_age}s",
                    }
                )
        return issues

    def collect_status(self) -> dict[str, Any]:
        paper = self.inspect_container(self.paper_container)
        archive = self.inspect_container(self.archive_container)
        dashboard_ok, dashboard_detail = self.dashboard_ok()

        issues: list[dict[str, Any]] = []
        if not paper["running"]:
            issues.append(
                {
                    "kind": "paper_container_down",
                    "target": self.paper_container,
                    "detail": paper.get("error") or paper.get("status"),
                }
            )
        if not archive["running"]:
            issues.append(
                {
                    "kind": "archive_container_down",
                    "target": self.archive_container,
                    "detail": archive.get("error") or archive.get("status"),
                }
            )
        if paper["running"] and not dashboard_ok:
            if paper.get("age_sec") is None or int(paper["age_sec"]) >= self.startup_grace_sec:
                issues.append(
                    {
                        "kind": "dashboard_down",
                        "target": str(self.dashboard_port),
                        "detail": dashboard_detail,
                    }
                )
        if paper["running"]:
            issues.extend(self.collect_health_issues(paper.get("age_sec")))

        summary_parts = [f"{item['kind']}:{item['target']}" for item in issues]
        fingerprint = hashlib.sha256("|".join(sorted(summary_parts)).encode("utf-8")).hexdigest() if summary_parts else ""
        return {
            "checked_at": iso_now(),
            "healthy": not issues,
            "issues": issues,
            "fingerprint": fingerprint,
            "paper": paper,
            "archive": archive,
            "dashboard": {
                "url": self.dashboard_url,
                "ok": dashboard_ok,
                "detail": dashboard_detail,
            },
        }

    def format_issue_block(self, status: dict[str, Any]) -> str:
        lines = []
        for item in status["issues"]:
            lines.append(f"- {item['kind']} [{item['target']}] {item['detail']}")
        return "\n".join(lines) if lines else "- no issues"

    def tail_diagnostics(self) -> str:
        chunks: list[str] = []
        supervisor_log = self.runtime_dir / "v7_paper_supervisor_20260525.log"
        supervisor_tail = tail_lines(supervisor_log, 30)
        if supervisor_tail:
            chunks.append("Supervisor tail:\n" + "\n".join(supervisor_tail))

        for container in (self.paper_container, self.archive_container):
            result = self.run_cmd(["docker", "logs", "--tail", "40", container], timeout=45)
            text = (result.stdout + ("\n" + result.stderr if result.stderr else "")).strip()
            if text:
                chunks.append(f"Docker logs {container}:\n{text}")
        return "\n\n".join(chunks).strip()

    def send_email(self, subject: str, body: str) -> dict[str, Any]:
        if not env_bool("WATCHDOG_EMAIL_ENABLED", False):
            return {"enabled": False, "sent": False, "status": "disabled"}

        smtp_host = os.environ.get("WATCHDOG_SMTP_HOST", "").strip() or os.environ.get("ARCHIVE_SMTP_HOST", "smtp.yandex.ru").strip() or "smtp.yandex.ru"
        smtp_port = int((os.environ.get("WATCHDOG_SMTP_PORT", "").strip() or os.environ.get("ARCHIVE_SMTP_PORT", "465").strip() or "465"))
        smtp_user = os.environ.get("WATCHDOG_SMTP_USER", "").strip() or os.environ.get("ARCHIVE_SMTP_USER", "etc00051@yandex.ru").strip() or "etc00051@yandex.ru"
        smtp_password = read_secret_value("WATCHDOG_SMTP_PASSWORD", "WATCHDOG_SMTP_PASSWORD_FILE")
        if not smtp_password:
            smtp_password = read_secret_value("ARCHIVE_SMTP_PASSWORD", "ARCHIVE_SMTP_PASSWORD_FILE")
        mail_from = os.environ.get("WATCHDOG_EMAIL_FROM", "").strip() or os.environ.get("ARCHIVE_EMAIL_FROM", smtp_user).strip() or smtp_user
        mail_to = os.environ.get("WATCHDOG_EMAIL_TO", "").strip() or os.environ.get("ARCHIVE_EMAIL_TO", "etc00051@yandex.ru").strip() or "etc00051@yandex.ru"
        use_ssl = env_bool("WATCHDOG_SMTP_USE_SSL", env_bool("ARCHIVE_SMTP_USE_SSL", True))
        use_starttls = env_bool("WATCHDOG_SMTP_STARTTLS", env_bool("ARCHIVE_SMTP_STARTTLS", False))
        if not smtp_password:
            return {"enabled": True, "sent": False, "status": "missing_password"}

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = mail_from
        msg["To"] = mail_to
        msg.set_content(body)

        if use_ssl:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=120) as smtp:
                smtp.login(smtp_user, smtp_password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=120) as smtp:
                smtp.ehlo()
                if use_starttls:
                    smtp.starttls()
                    smtp.ehlo()
                smtp.login(smtp_user, smtp_password)
                smtp.send_message(msg)
        return {
            "enabled": True,
            "sent": True,
            "status": "sent",
            "to": mail_to,
            "from": mail_from,
        }

    def remediation_actions(self, status: dict[str, Any]) -> list[dict[str, Any]]:
        kinds = {item["kind"] for item in status["issues"]}
        if not kinds or self.no_remediate:
            return []
        actions: list[dict[str, Any]] = []
        if "paper_container_down" in kinds and "archive_container_down" in kinds:
            actions.append(
                {
                    "name": "compose_up_all",
                    "argv": ["up", "-d", "--build", "paper", "archive"],
                }
            )
            return actions
        if "paper_container_down" in kinds:
            actions.append(
                {
                    "name": "compose_up_paper",
                    "argv": ["up", "-d", "--build", "paper"],
                }
            )
            return actions
        if "archive_container_down" in kinds:
            actions.append(
                {
                    "name": "compose_up_archive",
                    "argv": ["up", "-d", "archive"],
                }
            )
            return actions
        if "dashboard_down" in kinds:
            actions.append({"name": "restart_paper", "argv": ["restart", "paper"]})
            actions.append(
                {
                    "name": "compose_up_paper_refresh",
                    "argv": ["up", "-d", "--build", "paper"],
                }
            )
            return actions
        elif kinds & {"dashboard_down", "run_dir_missing", "health_missing", "health_stale", "snapshot_stale"}:
            actions.append({"name": "restart_paper", "argv": ["restart", "paper"]})
            actions.append(
                {
                    "name": "compose_up_paper_refresh",
                    "argv": ["up", "-d", "--build", "paper"],
                }
            )
        return actions

    def execute_remediation(self, status: dict[str, Any]) -> list[dict[str, Any]]:
        steps = self.remediation_actions(status)
        results: list[dict[str, Any]] = []
        if not steps:
            return results
        for index, step in enumerate(steps):
            result = self.docker_compose(*step["argv"])
            payload = {
                "name": step["name"],
                "argv": " ".join(shlex.quote(part) for part in result.argv),
                "returncode": result.returncode,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            }
            results.append(payload)
            self.log(
                f"remediate name={step['name']} rc={result.returncode} "
                f"stdout_len={len(result.stdout)} stderr_len={len(result.stderr)}"
            )
            if result.returncode == 0 and index < len(steps) - 1:
                time.sleep(self.remediation_wait_sec)
                probe = self.collect_status()
                if probe["healthy"]:
                    results.append(
                        {
                            "name": "post_action_probe",
                            "returncode": 0,
                            "stdout": "healthy_after_action",
                            "stderr": "",
                        }
                    )
                    break
            elif result.returncode != 0:
                break
        return results

    def build_email_body(
        self,
        mode: str,
        before: dict[str, Any],
        after: dict[str, Any] | None,
        actions: list[dict[str, Any]],
    ) -> str:
        host = socket.gethostname()
        lines = [
            f"3pips server watchdog: {mode}",
            f"Host: {host}",
            f"Project: {self.root}",
            f"Run: {self.run_name}",
            f"Time: {iso_now()}",
            "",
            "Problems before remediation:",
            self.format_issue_block(before),
            "",
            f"Dashboard: {before['dashboard']['url']} ({before['dashboard']['detail']})",
            f"Paper container: running={before['paper']['running']} status={before['paper']['status']} age_sec={before['paper']['age_sec']}",
            f"Archive container: running={before['archive']['running']} status={before['archive']['status']} age_sec={before['archive']['age_sec']}",
        ]
        if actions:
            lines += ["", "Actions:"]
            for action in actions:
                lines.append(f"- {action['name']} rc={action.get('returncode')} argv={action.get('argv', '')}")
        if after is not None:
            lines += [
                "",
                f"Healthy after remediation: {after['healthy']}",
            ]
            if not after["healthy"]:
                lines += ["Remaining problems:", self.format_issue_block(after)]
        diagnostics = self.tail_diagnostics()
        if diagnostics:
            lines += ["", diagnostics]
        return "\n".join(lines).strip() + "\n"

    def handle_healthy(self, state: dict[str, Any], status: dict[str, Any]) -> None:
        if state.get("status") == "incident":
            subject = f"3pips recovery on {socket.gethostname()}"
            body = "\n".join(
                [
                    "3pips server watchdog: recovery",
                    f"Host: {socket.gethostname()}",
                    f"Project: {self.root}",
                    f"Run: {self.run_name}",
                    f"Time: {iso_now()}",
                    "",
                    "All monitored services are healthy again.",
                    f"Dashboard: {self.dashboard_url}",
                ]
            )
            email_result = self.send_email(subject, body)
            self.log(f"recovery_email status={email_result.get('status')} sent={email_result.get('sent')}")
        state.update(
            {
                "status": "healthy",
                "fingerprint": "",
                "last_change": iso_now(),
                "last_summary": "healthy",
            }
        )
        self.save_state(state)
        self.log("healthy status=ok")

    def handle_incident(self, state: dict[str, Any], before: dict[str, Any]) -> None:
        actions = self.execute_remediation(before)
        if actions:
            time.sleep(self.remediation_wait_sec)
        after = self.collect_status()

        if after["healthy"]:
            subject = f"3pips auto-recovered on {socket.gethostname()}"
            body = self.build_email_body("auto_recovered", before, after, actions)
            email_result = self.send_email(subject, body)
            self.log(f"incident_auto_recovered email_status={email_result.get('status')}")
            state.update(
                {
                    "status": "healthy",
                    "fingerprint": "",
                    "last_change": iso_now(),
                    "last_summary": "auto_recovered",
                    "last_email_epoch": time.time() if email_result.get("sent") else state.get("last_email_epoch", 0.0),
                }
            )
            self.save_state(state)
            return

        now = time.time()
        state_changed = state.get("fingerprint") != before["fingerprint"] or state.get("status") != "incident"
        due_reminder = now - to_epoch(str(state.get("last_email_epoch", 0.0))) >= self.reminder_sec
        if state_changed or due_reminder:
            subject = f"3pips alert on {socket.gethostname()}"
            body = self.build_email_body("incident", before, after, actions)
            email_result = self.send_email(subject, body)
            self.log(f"incident_email status={email_result.get('status')} sent={email_result.get('sent')}")
            if email_result.get("sent"):
                state["last_email_epoch"] = now
        state.update(
            {
                "status": "incident",
                "fingerprint": before["fingerprint"],
                "last_change": iso_now(),
                "last_summary": self.format_issue_block(after),
            }
        )
        self.save_state(state)
        self.log(f"incident_open issues={len(after['issues'])}")

    def run(self) -> int:
        self.log(
            "watchdog_start "
            f"run_name={self.run_name} dashboard_port={self.dashboard_port} "
            f"health_stale_sec={self.health_stale_sec} snapshot_stale_sec={self.snapshot_stale_sec} "
            f"startup_grace_sec={self.startup_grace_sec} no_remediate={self.no_remediate}"
        )
        state = self.load_state()
        status = self.collect_status()
        if status["healthy"]:
            self.handle_healthy(state, status)
        else:
            self.handle_incident(state, status)
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default="/opt/3pips")
    parser.add_argument("--run-name", default=os.environ.get("WATCHDOG_RUN_NAME") or os.environ.get("AUTOUPDATE_RUN_NAME") or RUN_NAME_DEFAULT)
    parser.add_argument("--docker-env-file", default=os.environ.get("DOCKER_ENV_FILE", "/etc/3pips/3pips.env"))
    parser.add_argument("--dashboard-port", type=int, default=int(os.environ.get("WATCHDOG_DASHBOARD_PORT", "8768")))
    parser.add_argument("--health-stale-sec", type=int, default=int(os.environ.get("WATCHDOG_HEALTH_STALE_SEC", "600")))
    parser.add_argument("--snapshot-stale-sec", type=int, default=int(os.environ.get("WATCHDOG_SNAPSHOT_STALE_SEC", "1800")))
    parser.add_argument("--startup-grace-sec", type=int, default=int(os.environ.get("WATCHDOG_STARTUP_GRACE_SEC", "300")))
    parser.add_argument("--remediation-wait-sec", type=int, default=int(os.environ.get("WATCHDOG_REMEDIATION_WAIT_SEC", "20")))
    parser.add_argument("--reminder-sec", type=int, default=int(os.environ.get("WATCHDOG_REMINDER_SEC", "21600")))
    parser.add_argument("--paper-container", default=os.environ.get("WATCHDOG_PAPER_CONTAINER", "3pips-paper"))
    parser.add_argument("--archive-container", default=os.environ.get("WATCHDOG_ARCHIVE_CONTAINER", "3pips-archive"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-remediate", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    watchdog = Watchdog(args)
    raise SystemExit(watchdog.run())


if __name__ == "__main__":
    main()
