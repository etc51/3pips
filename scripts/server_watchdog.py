from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime
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


def service_age_sec(service_name: str) -> float | None:
    result = subprocess.run(
        ["systemctl", "show", service_name, "-p", "ActiveEnterTimestampMonotonic", "--value"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw or raw == "0":
        return None
    try:
        started = int(raw) / 1_000_000.0
        return max(0.0, time.monotonic() - started)
    except Exception:
        return None


def dashboard_ok(url: str) -> tuple[bool, str]:
    try:
        with urlopen(url, timeout=5) as response:
            return True, f"http_{response.status}"
    except URLError as exc:
        return False, f"urlerror:{exc}"
    except Exception as exc:
        return False, f"error:{exc}"


def check_run_health(run_dir: Path, health_stale_sec: int, svc_age_sec: float | None = None, startup_grace_sec: int = 0) -> list[str]:
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
            if svc_age_sec is not None and svc_age_sec < startup_grace_sec:
                continue
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


def merge_state(base: dict, **updates) -> dict:
    merged = dict(base) if isinstance(base, dict) else {}
    merged.update(updates)
    return merged


def latest_log_excerpt(runtime_dir: Path) -> str:
    parts = []
    for name in ["v7_paper_supervisor_20260525.log", "server_watchdog.log"]:
        path = runtime_dir / name
        if path.exists():
            parts.append(f"===== {name} =====\n{tail_text(path, lines=60)}")
    return "\n\n".join(parts)


def latest_trade_date(run_dir: Path) -> str:
    latest = ""
    for path in sorted(run_dir.glob("*_multi_futures_paper_trades.csv")):
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    closed_at = str(row.get("closed_at") or "")
                    trade_date = closed_at[:10] if len(closed_at) >= 10 else ""
                    if trade_date and trade_date > latest:
                        latest = trade_date
        except Exception:
            continue
    return latest


def load_latest_autonomy_trade_date(project_root: Path) -> str:
    path = project_root / "reports" / "autonomy" / "latest" / "latest_auto_policy.json"
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("trade_date") or "")


def run_daily_autonomy(service_name: str) -> tuple[bool, str]:
    result = subprocess.run(["systemctl", "start", service_name], capture_output=True, text=True)
    ok = result.returncode == 0
    summary = f"rc={result.returncode} stdout_len={len(result.stdout)} stderr_len={len(result.stderr)}"
    return ok, summary


def should_check_daily_autonomy(now_local: datetime, state: dict) -> bool:
    if now_local.hour == 0 and now_local.minute < 5:
        return False
    last_date = str(state.get("last_autonomy_check_date") or "")
    return last_date != now_local.date().isoformat()


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
    parser.add_argument("--daily-autonomy-service-name", default="3pips-daily-autonomy.service")
    parser.add_argument("--dashboard-url", default="http://127.0.0.1:8768/")
    parser.add_argument("--health-stale-sec", type=int, default=180)
    parser.add_argument("--startup-wait-sec", type=int, default=25)
    parser.add_argument("--startup-grace-sec", type=int, default=180)
    parser.add_argument("--daily-autonomy-wait-sec", type=int, default=20)
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
    state = load_state(state_path)

    log(log_path, f"watchdog_start run_name={args.run_name} service={args.service_name} dashboard={args.dashboard_url} health_stale_sec={args.health_stale_sec} startup_grace_sec={args.startup_grace_sec} no_remediate={args.no_remediate}")

    now_local = datetime.now()
    if should_check_daily_autonomy(now_local, state):
        trade_date = latest_trade_date(run_dir)
        autonomy_trade_date = load_latest_autonomy_trade_date(project_root)
        if trade_date and autonomy_trade_date != trade_date:
            log(log_path, f"autonomy_backfill_needed latest_trades={trade_date} latest_autonomy={autonomy_trade_date or '-'}")
            if not args.no_remediate:
                started, summary = run_daily_autonomy(args.daily_autonomy_service_name)
                log(log_path, f"autonomy_backfill_start service={args.daily_autonomy_service_name} {summary}")
                if started:
                    time.sleep(max(5, args.daily_autonomy_wait_sec))
                    autonomy_trade_date = load_latest_autonomy_trade_date(project_root)
                    if autonomy_trade_date == trade_date:
                        state["last_autonomy_check_date"] = now_local.date().isoformat()
                        state["last_autonomy_trade_date"] = autonomy_trade_date
                        save_state(state_path, state)
                        log(log_path, f"autonomy_backfill_recovered trade_date={trade_date}")
                    else:
                        log(log_path, f"autonomy_backfill_pending latest_trades={trade_date} latest_autonomy={autonomy_trade_date or '-'}")
            else:
                log(log_path, "autonomy_backfill_skipped no_remediate=true")
        else:
            state["last_autonomy_check_date"] = now_local.date().isoformat()
            state["last_autonomy_trade_date"] = autonomy_trade_date or trade_date
            save_state(state_path, state)
            log(log_path, f"autonomy_backfill_ok trade_date={trade_date or '-'} autonomy={autonomy_trade_date or '-'}")

    issues: list[str] = []
    svc_age = service_age_sec(args.service_name)
    if not service_active(args.service_name):
        issues.append(f"service_inactive[{args.service_name}]")
    ok, dash_status = dashboard_ok(args.dashboard_url)
    if not ok and not (svc_age is not None and svc_age < args.startup_grace_sec):
        issues.append(f"dashboard_down[{dash_status}]")
    issues.extend(check_run_health(run_dir, args.health_stale_sec, svc_age_sec=svc_age, startup_grace_sec=args.startup_grace_sec))

    if not issues:
        save_state(
            state_path,
            merge_state(
                state,
                status="ok",
                fingerprint="",
                last_email_epoch=0.0,
                last_change=now_str(),
                last_summary="healthy",
            ),
        )
        log(log_path, "healthy status=ok")
        return 0

    log(log_path, f"incident_detected count={len(issues)} summary={' ; '.join(issues)}")
    if not args.no_remediate:
        restarted, summary = restart_service(args.service_name)
        log(log_path, f"remediate name=systemctl_restart {summary}")
        if restarted:
            time.sleep(max(5, args.startup_wait_sec))
            svc_age = service_age_sec(args.service_name)
            if svc_age is not None and svc_age < args.startup_grace_sec:
                save_state(
                    state_path,
                    merge_state(
                        state,
                        status="warming_up",
                        fingerprint="",
                        last_email_epoch=0.0,
                        last_change=now_str(),
                        last_summary=f"service_warming_up age={svc_age:.1f}s",
                    ),
                )
                log(log_path, f"incident_auto_recovered status=warming_up age={svc_age:.1f}s")
                return 0
            retry_issues: list[str] = []
            if not service_active(args.service_name):
                retry_issues.append(f"service_inactive[{args.service_name}]")
            ok, dash_status = dashboard_ok(args.dashboard_url)
            if not ok:
                retry_issues.append(f"dashboard_down[{dash_status}]")
            retry_issues.extend(check_run_health(run_dir, args.health_stale_sec, svc_age_sec=svc_age, startup_grace_sec=args.startup_grace_sec))
            if not retry_issues:
                save_state(
                    state_path,
                    merge_state(
                        state,
                        status="ok",
                        fingerprint="",
                        last_email_epoch=0.0,
                        last_change=now_str(),
                        last_summary="incident_auto_recovered",
                    ),
                )
                log(log_path, "incident_auto_recovered email_status=not_needed")
                return 0
            issues = retry_issues

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
        merge_state(
            state,
            status="incident",
            fingerprint=fp,
            last_email_epoch=time.time() if email_ok else float(state.get("last_email_epoch") or 0.0),
            last_change=now_str(),
            last_summary=" ; ".join(issues),
            email_status=email_status,
        ),
    )
    log(log_path, f"incident_unrecovered count={len(issues)} email_status={email_status}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
