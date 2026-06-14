from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from autonomy_common import now_str, parse_dt, send_email, tail_text, write_json  # noqa: E402


def log(path: Path, message: str) -> None:
    line = f"[{now_str()}] {message}"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def service_active(service_name: str) -> bool:
    result = subprocess.run(["systemctl", "is-active", service_name], capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == "active"


def restart_service(service_name: str) -> tuple[bool, str]:
    result = subprocess.run(["systemctl", "restart", service_name], capture_output=True, text=True)
    ok = result.returncode == 0
    summary = f"rc={result.returncode} stdout_len={len(result.stdout)} stderr_len={len(result.stderr)}"
    return ok, summary


def dashboard_ok(url: str) -> tuple[bool, str]:
    try:
        with urlopen(url, timeout=5) as response:
            return True, f"http_{response.status}"
    except URLError as exc:
        return False, f"urlerror:{exc}"
    except Exception as exc:
        return False, f"error:{exc}"


def check_run_health(run_dir: Path, health_stale_sec: int) -> list[str]:
    issues: list[str] = []
    health_files = sorted(run_dir.glob("*_health.json"))
    if not health_files:
        return ["no_health_files"]
    now = time.time()
    for path in health_files:
        if not path.exists():
            issues.append(f"missing_health[{path.name}]")
            continue
        age = int(now - path.stat().st_mtime)
        if age > health_stale_sec:
            issues.append(f"stale_health[{path.name}] age={age}s")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            issues.append(f"bad_health_json[{path.name}] {exc}")
            continue
        ts = parse_dt(str(payload.get("timestamp") or ""))
        if ts is None:
            issues.append(f"missing_health_timestamp[{path.name}]")
    for path in sorted(run_dir.glob("*_paper_open_positions.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                issues.append(f"bad_open_positions[{path.name}] not_list")
        except Exception as exc:
            issues.append(f"bad_open_positions[{path.name}] {exc}")
    return issues


def fingerprint(issues: list[str]) -> str:
    joined = " | ".join(sorted(issues))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(path: Path, payload: dict) -> None:
    write_json(path, payload)


def latest_log_excerpt(runtime_dir: Path) -> str:
    parts = []
    for name in ["v7_paper_supervisor_20260525.log", "server_watchdog.log"]:
        path = runtime_dir / name
        if path.exists():
            parts.append(f"===== {name} =====\n{tail_text(path, lines=60)}")
    return "\n\n".join(parts)


def build_email_body(hostname: str, service_name: str, dashboard_url: str, issues: list[str], runtime_dir: Path) -> str:
    lines = [
        f"Host: {hostname}",
        f"Service: {service_name}",
        f"Dashboard: {dashboard_url}",
        "",
        "Unrecovered issues:",
    ]
    lines.extend(f"- {issue}" for issue in issues)
    excerpt = latest_log_excerpt(runtime_dir)
    if excerpt:
        lines.extend(["", "Recent logs:", excerpt])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default="/opt/3pips")
    parser.add_argument("--run-name", default="v7_live_20260525")
    parser.add_argument("--service-name", default="3pips-paper-a26.service")
    parser.add_argument("--dashboard-url", default="http://127.0.0.1:8768/")
    parser.add_argument("--health-stale-sec", type=int, default=180)
    parser.add_argument("--startup-wait-sec", type=int, default=25)
    parser.add_argument("--state-path", default="")
    parser.add_argument("--log-path", default="")
    parser.add_argument("--email-to", default="etc00051@yandex.ru")
    parser.add_argument("--no-remediate", action="store_true")
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    runtime_dir = project_root / "reports" / "runtime"
    run_dir = project_root / "reports" / "paper_runs" / args.run_name
    state_path = Path(args.state_path) if args.state_path else runtime_dir / "server_watchdog_state.json"
    log_path = Path(args.log_path) if args.log_path else runtime_dir / "server_watchdog.log"
    hostname = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip() or "unknown-host"

    log(log_path, f"watchdog_start run_name={args.run_name} service={args.service_name} dashboard={args.dashboard_url} health_stale_sec={args.health_stale_sec} no_remediate={args.no_remediate}")

    issues: list[str] = []
    if not service_active(args.service_name):
        issues.append(f"service_inactive[{args.service_name}]")
    ok, dash_status = dashboard_ok(args.dashboard_url)
    if not ok:
        issues.append(f"dashboard_down[{dash_status}]")
    issues.extend(check_run_health(run_dir, args.health_stale_sec))

    if not issues:
        save_state(
            state_path,
            {
                "status": "ok",
                "fingerprint": "",
                "last_email_epoch": 0.0,
                "last_change": now_str(),
                "last_summary": "healthy",
            },
        )
        log(log_path, "healthy status=ok")
        return 0

    log(log_path, f"incident_detected count={len(issues)} summary={' ; '.join(issues)}")
    if not args.no_remediate:
        restarted, summary = restart_service(args.service_name)
        log(log_path, f"remediate name=systemctl_restart {summary}")
        if restarted:
            time.sleep(max(5, args.startup_wait_sec))
            retry_issues: list[str] = []
            if not service_active(args.service_name):
                retry_issues.append(f"service_inactive[{args.service_name}]")
            ok, dash_status = dashboard_ok(args.dashboard_url)
            if not ok:
                retry_issues.append(f"dashboard_down[{dash_status}]")
            retry_issues.extend(check_run_health(run_dir, args.health_stale_sec))
            if not retry_issues:
                save_state(
                    state_path,
                    {
                        "status": "ok",
                        "fingerprint": "",
                        "last_email_epoch": 0.0,
                        "last_change": now_str(),
                        "last_summary": "incident_auto_recovered",
                    },
                )
                log(log_path, "incident_auto_recovered email_status=not_needed")
                return 0
            issues = retry_issues

    state = load_state(state_path)
    fp = fingerprint(issues)
    email_needed = fp != state.get("fingerprint")
    email_ok = False
    email_status = "skipped_same_incident"
    if email_needed:
        subject = f"[3pips] unrecovered incident on {hostname}"
        body = build_email_body(hostname, args.service_name, args.dashboard_url, issues, runtime_dir)
        email_ok, email_status = send_email(subject, body, recipient=args.email_to)
        log(log_path, f"incident_email status={email_status}")

    save_state(
        state_path,
        {
            "status": "incident",
            "fingerprint": fp,
            "last_email_epoch": time.time() if email_ok else float(state.get("last_email_epoch") or 0.0),
            "last_change": now_str(),
            "last_summary": " ; ".join(issues),
            "email_status": email_status,
        },
    )
    log(log_path, f"incident_unrecovered count={len(issues)} email_status={email_status}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
