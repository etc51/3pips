from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from autonomy_common import now_str, parse_dt, send_email, tail_text, write_json  # noqa: E402
import watchdog_policy as intraday_policy  # noqa: E402


def log(path: Path, message: str) -> None:
    line = f"[{now_str()}] {message}"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def service_active(service_name: str) -> bool:
    result = subprocess.run(["systemctl", "is-active", service_name], capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == "active"


def unit_enabled(unit_name: str) -> bool:
    result = subprocess.run(["systemctl", "is-enabled", unit_name], capture_output=True, text=True)
    return result.returncode == 0


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
        with urlopen(dashboard_health_url(url), timeout=5) as response:
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


def check_timer_units(timer_names: list[str]) -> list[str]:
    issues: list[str] = []
    for name in timer_names:
        if not name:
            continue
        if not unit_enabled(name):
            issues.append(f"timer_disabled[{name}]")
        if not service_active(name):
            issues.append(f"timer_inactive[{name}]")
    return issues


def check_git_autoupdate_status(path: Path, max_age_sec: int) -> list[str]:
    if not path.exists():
        return [f"missing_git_autoupdate_status[{path.name}]"]
    try:
        age_sec = int(max(0.0, time.time() - path.stat().st_mtime))
    except FileNotFoundError:
        return [f"missing_git_autoupdate_status[{path.name}]"]
    if age_sec > max_age_sec:
        return [f"stale_git_autoupdate_status[{path.name}] age={age_sec}s"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"bad_git_autoupdate_status[{path.name}] {exc}"]
    if not isinstance(payload, dict):
        return [f"bad_git_autoupdate_status[{path.name}] not_dict"]

    issues: list[str] = []
    updated_at = parse_dt(str(payload.get("updated_at") or ""))
    if updated_at is None:
        issues.append(f"missing_git_autoupdate_timestamp[{path.name}]")
    outcome = str(payload.get("outcome") or "")
    reason = str(payload.get("reason") or "")
    if outcome == "failed":
        issues.append(f"git_autoupdate_failed[{reason or 'unknown'}]")
    elif outcome == "skipped" and reason == "dirty_worktree":
        issues.append("git_autoupdate_blocked[dirty_worktree]")
    elif bool(payload.get("pending_restart_exists")) and age_sec > max_age_sec // 2:
        issues.append(f"git_autoupdate_pending_restart_stale[{path.name}] age={age_sec}s")
    elif bool(payload.get("rollout_lock_exists")) and age_sec > max_age_sec // 2:
        issues.append(f"git_autoupdate_rollout_lock_stale[{path.name}] age={age_sec}s")
    return issues


def check_automation_health(timer_names: list[str], git_autoupdate_status_path: Path, git_autoupdate_max_age_sec: int) -> list[str]:
    issues = check_timer_units(timer_names)
    issues.extend(check_git_autoupdate_status(git_autoupdate_status_path, git_autoupdate_max_age_sec))
    return issues


def check_latest_autonomy_outputs(project_root: Path, latest_trade_date_value: str, max_age_sec: int) -> list[str]:
    latest_root = project_root / "reports" / "autonomy" / "latest"
    required_names = [
        "latest_auto_policy.json",
        "latest_nightly_cycle_status.json",
        "latest_manifest.json",
        "research_strategy_registry_summary.json",
        "paper_candidate_shortlist_summary.json",
        "research_strategy_targets_summary.json",
    ]
    payloads: dict[str, dict] = {}
    issues: list[str] = []
    now_epoch = time.time()

    for name in required_names:
        path = latest_root / name
        if not path.exists():
            issues.append(f"missing_latest_artifact[{name}]")
            continue
        age_sec = int(max(0.0, now_epoch - path.stat().st_mtime))
        if age_sec > max_age_sec:
            issues.append(f"stale_latest_artifact[{name}] age={age_sec}s")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            issues.append(f"bad_latest_artifact[{name}] {exc}")
            continue
        if not isinstance(payload, dict):
            issues.append(f"bad_latest_artifact[{name}] not_dict")
            continue
        payloads[name] = payload

    auto_policy = payloads.get("latest_auto_policy.json")
    if auto_policy is not None and latest_trade_date_value:
        auto_trade_date = str(auto_policy.get("trade_date") or "")
        if auto_trade_date != latest_trade_date_value:
            issues.append(f"latest_auto_policy_trade_date_mismatch[{auto_trade_date or '-'}!={latest_trade_date_value}]")

    nightly = payloads.get("latest_nightly_cycle_status.json")
    if nightly is not None:
        nightly_trade_date = str(nightly.get("trade_date") or "")
        if latest_trade_date_value and nightly_trade_date != latest_trade_date_value:
            issues.append(f"nightly_trade_date_mismatch[{nightly_trade_date or '-'}!={latest_trade_date_value}]")
        nightly_status = str(nightly.get("status") or "")
        if nightly_status != "ok":
            issues.append(f"nightly_status_not_ok[{nightly_status or '-'}]")
        summary_stage = ((nightly.get("stages") or {}).get("summary") or {})
        summary_status = str(summary_stage.get("status") or "")
        if summary_status != "ok":
            issues.append(f"nightly_summary_status_not_ok[{summary_status or '-'}]")
        if not bool(summary_stage.get("archive_ready")):
            issues.append("nightly_archive_not_ready")

    manifest = payloads.get("latest_manifest.json")
    if manifest is not None:
        manifest_trade_date = str(manifest.get("trade_date") or "")
        if latest_trade_date_value and manifest_trade_date != latest_trade_date_value:
            issues.append(f"latest_manifest_trade_date_mismatch[{manifest_trade_date or '-'}!={latest_trade_date_value}]")
        for key in [
            "nightly_cycle_status",
            "archive",
            "research_strategy_registry",
            "paper_candidate_shortlist",
            "research_strategy_targets",
        ]:
            if key not in manifest:
                issues.append(f"latest_manifest_missing_key[{key}]")
        manifest_nightly = manifest.get("nightly_cycle_status")
        if isinstance(manifest_nightly, dict):
            manifest_nightly_status = str(manifest_nightly.get("status") or "")
            if manifest_nightly_status != "ok":
                issues.append(f"latest_manifest_nightly_status_not_ok[{manifest_nightly_status or '-'}]")
        else:
            issues.append("latest_manifest_bad_nightly_cycle_status")

    return issues


def fingerprint(issues: list[str]) -> str:
    joined = " | ".join(sorted(issues))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def dashboard_only_issue(issues: list[str]) -> bool:
    return len(issues) == 1 and str(issues[0]).startswith("dashboard_down[")


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


def load_active_rollout_lock(path: Path, max_age_sec: int) -> tuple[dict, float]:
    if not path.exists():
        return {}, 0.0
    age_sec = max(0.0, time.time() - path.stat().st_mtime)
    if age_sec > max_age_sec:
        return {}, age_sec
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"reason": "unreadable_lock"}, age_sec
    return (payload if isinstance(payload, dict) else {}), age_sec


def latest_log_excerpt(runtime_dir: Path) -> str:
    parts = []
    for name in ["v7_paper_supervisor_20260525.log", "server_watchdog.log"]:
        path = runtime_dir / name
        if path.exists():
            parts.append(f"===== {name} =====\n{tail_text(path, lines=60)}")
    return "\n\n".join(parts)


latest_trade_date = intraday_policy.latest_trade_date
compute_intraday_watchdog_overrides = intraday_policy.compute_intraday_watchdog_overrides
refresh_intraday_killer_policy = intraday_policy.refresh_intraday_killer_policy


def dashboard_health_url(dashboard_url: str) -> str:
    parts = urlsplit(dashboard_url)
    path = parts.path or ""
    if path.endswith("/healthz") or path == "/healthz":
        return dashboard_url
    if path.endswith("/api/state") or path == "/api/state":
        clean = path[: -len("/api/state")]
    elif path in {"", "/"}:
        clean = ""
    else:
        clean = path[:-1] if path.endswith("/") else path
    return urlunsplit((parts.scheme, parts.netloc, f"{clean}/healthz", "", ""))


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


def service_restart_may_help(issues: list[str]) -> bool:
    runtime_prefixes = (
        "service_inactive[",
        "dashboard_down[",
        "no_health_files",
        "missing_health[",
        "stale_health[",
        "bad_health_json[",
        "missing_health_timestamp[",
        "bad_open_positions[",
    )
    return any(str(issue).startswith(runtime_prefixes) for issue in issues)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default="/opt/3pips")
    parser.add_argument("--run-name", default="v7_live_20260525")
    parser.add_argument("--service-name", default="3pips-paper-a26.service")
    parser.add_argument("--daily-autonomy-service-name", default="3pips-daily-autonomy.service")
    parser.add_argument("--watchdog-timer-name", default="3pips-watchdog.timer")
    parser.add_argument("--intraday-autonomy-timer-name", default="3pips-intraday-autonomy.timer")
    parser.add_argument("--daily-autonomy-timer-name", default="3pips-daily-autonomy.timer")
    parser.add_argument("--git-autoupdate-timer-name", default="3pips-git-autoupdate.timer")
    parser.add_argument("--dashboard-url", default="http://127.0.0.1:8768/")
    parser.add_argument("--health-stale-sec", type=int, default=180)
    parser.add_argument("--git-autoupdate-status-path", default="")
    parser.add_argument("--git-autoupdate-max-age-sec", type=int, default=7200)
    parser.add_argument("--autonomy-latest-max-age-sec", type=int, default=172800)
    parser.add_argument("--startup-wait-sec", type=int, default=25)
    parser.add_argument("--startup-grace-sec", type=int, default=180)
    parser.add_argument("--daily-autonomy-wait-sec", type=int, default=20)
    parser.add_argument("--state-path", default="")
    parser.add_argument("--log-path", default="")
    parser.add_argument("--maintenance-lock-path", default="")
    parser.add_argument("--maintenance-lock-max-sec", type=int, default=1800)
    parser.add_argument("--email-to", default="etc00051@yandex.ru")
    parser.add_argument("--no-remediate", action="store_true")
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    runtime_dir = project_root / "reports" / "runtime"
    run_dir = project_root / "reports" / "paper_runs" / args.run_name
    state_path = Path(args.state_path) if args.state_path else runtime_dir / "server_watchdog_state.json"
    log_path = Path(args.log_path) if args.log_path else runtime_dir / "server_watchdog.log"
    maintenance_lock_path = Path(args.maintenance_lock_path) if args.maintenance_lock_path else runtime_dir / "git_autoupdate_rollout_lock.json"
    git_autoupdate_status_path = Path(args.git_autoupdate_status_path) if args.git_autoupdate_status_path else runtime_dir / "git_autoupdate_status.json"
    hostname = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip() or "unknown-host"
    state = load_state(state_path)

    log(log_path, f"watchdog_start run_name={args.run_name} service={args.service_name} dashboard={args.dashboard_url} health_stale_sec={args.health_stale_sec} startup_grace_sec={args.startup_grace_sec} no_remediate={args.no_remediate}")

    rollout_lock, rollout_lock_age_sec = load_active_rollout_lock(maintenance_lock_path, args.maintenance_lock_max_sec)
    if rollout_lock:
        lock_reason = str(rollout_lock.get("reason") or "rollout_lock")
        save_state(
            state_path,
            merge_state(
                state,
                status="maintenance_lock",
                fingerprint="",
                last_change=now_str(),
                last_summary=f"maintenance_lock age={rollout_lock_age_sec:.1f}s reason={lock_reason}",
            ),
        )
        log(log_path, f"maintenance_lock active age={rollout_lock_age_sec:.1f}s reason={lock_reason}")
        return 0
    if maintenance_lock_path.exists() and rollout_lock_age_sec > args.maintenance_lock_max_sec:
        log(log_path, f"maintenance_lock stale age={rollout_lock_age_sec:.1f}s path={maintenance_lock_path}")

    changed, summary = refresh_intraday_killer_policy(project_root, run_dir, args.dashboard_url)
    log(log_path, f"intraday_policy_refresh changed={changed} {summary}")

    run_trade_date = latest_trade_date(run_dir)
    now_local = datetime.now()
    if should_check_daily_autonomy(now_local, state):
        autonomy_trade_date = load_latest_autonomy_trade_date(project_root)
        if run_trade_date and autonomy_trade_date != run_trade_date:
            log(log_path, f"autonomy_backfill_needed latest_trades={run_trade_date} latest_autonomy={autonomy_trade_date or '-'}")
            if not args.no_remediate:
                started, summary = run_daily_autonomy(args.daily_autonomy_service_name)
                log(log_path, f"autonomy_backfill_start service={args.daily_autonomy_service_name} {summary}")
                if started:
                    time.sleep(max(5, args.daily_autonomy_wait_sec))
                    autonomy_trade_date = load_latest_autonomy_trade_date(project_root)
                    if autonomy_trade_date == run_trade_date:
                        state["last_autonomy_check_date"] = now_local.date().isoformat()
                        state["last_autonomy_trade_date"] = autonomy_trade_date
                        save_state(state_path, state)
                        log(log_path, f"autonomy_backfill_recovered trade_date={run_trade_date}")
                    else:
                        log(log_path, f"autonomy_backfill_pending latest_trades={run_trade_date} latest_autonomy={autonomy_trade_date or '-'}")
            else:
                log(log_path, "autonomy_backfill_skipped no_remediate=true")
        else:
            state["last_autonomy_check_date"] = now_local.date().isoformat()
            state["last_autonomy_trade_date"] = autonomy_trade_date or run_trade_date
            save_state(state_path, state)
            log(log_path, f"autonomy_backfill_ok trade_date={run_trade_date or '-'} autonomy={autonomy_trade_date or '-'}")

    issues: list[str] = []
    svc_age = service_age_sec(args.service_name)
    if not service_active(args.service_name):
        issues.append(f"service_inactive[{args.service_name}]")
    ok, dash_status = dashboard_ok(args.dashboard_url)
    if not ok and not (svc_age is not None and svc_age < args.startup_grace_sec):
        issues.append(f"dashboard_down[{dash_status}]")
    issues.extend(check_run_health(run_dir, args.health_stale_sec, svc_age_sec=svc_age, startup_grace_sec=args.startup_grace_sec))
    issues.extend(
        check_automation_health(
            [
                args.watchdog_timer_name,
                args.intraday_autonomy_timer_name,
                args.daily_autonomy_timer_name,
                args.git_autoupdate_timer_name,
            ],
            git_autoupdate_status_path,
            args.git_autoupdate_max_age_sec,
        )
    )
    issues.extend(check_latest_autonomy_outputs(project_root, run_trade_date, args.autonomy_latest_max_age_sec))

    if not issues:
        save_state(
            state_path,
            merge_state(
                state,
                status="ok",
                dashboard_fail_count=0,
                fingerprint="",
                last_email_epoch=0.0,
                last_change=now_str(),
                last_summary="healthy",
            ),
        )
        log(log_path, "healthy status=ok")
        return 0

    if dashboard_only_issue(issues):
        fail_count = int(state.get("dashboard_fail_count") or 0) + 1
        if fail_count < 2:
            save_state(
                state_path,
                merge_state(
                    state,
                    status="dashboard_unstable",
                    dashboard_fail_count=fail_count,
                    fingerprint="",
                    last_change=now_str(),
                    last_summary=f"dashboard_probe_retry count={fail_count}",
                ),
            )
            log(log_path, f"dashboard_probe_retry count={fail_count} summary={issues[0]}")
            return 0
        state = merge_state(state, dashboard_fail_count=fail_count)
    else:
        state = merge_state(state, dashboard_fail_count=0)

    log(log_path, f"incident_detected count={len(issues)} summary={' ; '.join(issues)}")
    if not args.no_remediate and service_restart_may_help(issues):
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
                        dashboard_fail_count=0,
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
                        dashboard_fail_count=0,
                        fingerprint="",
                        last_email_epoch=0.0,
                        last_change=now_str(),
                        last_summary="incident_auto_recovered",
                    ),
                )
                log(log_path, "incident_auto_recovered email_status=not_needed")
                return 0
            issues = retry_issues
    elif not args.no_remediate:
        log(log_path, "remediate skipped reason=non_runtime_issue")

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
            dashboard_fail_count=0 if not dashboard_only_issue(issues) else int(state.get("dashboard_fail_count") or 0),
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
