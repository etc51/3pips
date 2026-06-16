from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from autonomy_common import now_str, write_json


MSK = ZoneInfo("Europe/Moscow")
ENTRY_WINDOW_START = dt_time(10, 15)
ENTRY_WINDOW_END = dt_time(17, 45)
SYSTEMD_UNIT_NAMES = [
    "3pips-paper-a26.service",
    "3pips-watchdog.service",
    "3pips-watchdog.timer",
    "3pips-daily-autonomy.service",
    "3pips-daily-autonomy.timer",
    "3pips-intraday-autonomy.service",
    "3pips-intraday-autonomy.timer",
    "3pips-git-autoupdate.service",
    "3pips-git-autoupdate.timer",
]
SYSTEMD_ENABLE_SERVICES = ["3pips-paper-a26.service"]
SYSTEMD_ENABLE_TIMERS = [
    "3pips-watchdog.timer",
    "3pips-daily-autonomy.timer",
    "3pips-intraday-autonomy.timer",
    "3pips-git-autoupdate.timer",
]


def log(path: Path, message: str) -> None:
    line = f"[{now_str()}] {message}"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def git_cmd(project_root: Path, *args: str) -> list[str]:
    return ["git", "-c", f"safe.directory={project_root}", *args]


def changed_paths(project_root: Path, old_rev: str, new_rev: str) -> set[str]:
    diff = run(git_cmd(project_root, "diff", "--name-only", f"{old_rev}..{new_rev}"), project_root)
    if diff.returncode != 0:
        return set()
    return {line.strip() for line in diff.stdout.splitlines() if line.strip()}


def write_pending_restart(path: Path, payload: dict[str, object]) -> None:
    write_json(path, payload)


def write_rollout_lock(path: Path, payload: dict[str, object]) -> None:
    write_json(path, payload)


def write_status(path: Path, payload: dict[str, object]) -> None:
    write_json(path, payload)


def clear_rollout_lock(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def runtime_open_positions(project_root: Path, run_name: str) -> tuple[int, list[str]]:
    run_dir = project_root / "reports" / "paper_runs" / run_name
    total = 0
    details: list[str] = []
    for path in sorted(run_dir.glob("*_health.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        count = int(payload.get("open_positions") or 0)
        total += count
        if count > 0:
            details.append(f"{path.stem.removesuffix('_health')}={count}")
    return total, details


def dashboard_health_url(dashboard_url: str) -> str:
    parts = urlsplit(dashboard_url)
    path = parts.path or ""
    if path.endswith("/healthz") or path == "/healthz":
        return dashboard_url
    if path in {"", "/"}:
        clean = ""
    else:
        clean = path[:-1] if path.endswith("/") else path
    return urlunsplit((parts.scheme, parts.netloc, f"{clean}/healthz", "", ""))


def dashboard_ok(dashboard_url: str) -> tuple[bool, str]:
    try:
        with urlopen(dashboard_health_url(dashboard_url), timeout=5) as response:
            return True, f"http_{response.status}"
    except URLError as exc:
        return False, f"urlerror:{exc}"
    except Exception as exc:
        return False, f"error:{exc}"


def runtime_health_issues(project_root: Path, run_name: str, health_stale_sec: int) -> list[str]:
    run_dir = project_root / "reports" / "paper_runs" / run_name
    issues: list[str] = []
    health_files = sorted(run_dir.glob("*_health.json"))
    if not health_files:
        return ["no_health_files"]
    now = time.time()
    for path in health_files:
        try:
            age = int(now - path.stat().st_mtime)
        except FileNotFoundError:
            issues.append(f"missing_health[{path.name}]")
            continue
        if age > health_stale_sec:
            issues.append(f"stale_health[{path.name}] age={age}s")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            issues.append(f"bad_health_json[{path.name}] {type(exc).__name__}: {exc}")
            continue
        if not isinstance(payload, dict):
            issues.append(f"bad_health_json[{path.name}] not_dict")
    for path in sorted(run_dir.glob("*_paper_open_positions.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            issues.append(f"bad_open_positions[{path.name}] {type(exc).__name__}: {exc}")
            continue
        if not isinstance(payload, list):
            issues.append(f"bad_open_positions[{path.name}] not_list")
    return issues


def verify_runtime_ready(project_root: Path, run_name: str, dashboard_url: str, health_stale_sec: int) -> list[str]:
    issues: list[str] = []
    ok, dash_status = dashboard_ok(dashboard_url)
    if not ok:
        issues.append(f"dashboard_down[{dash_status}]")
    issues.extend(runtime_health_issues(project_root, run_name, health_stale_sec))
    return issues


def active_entry_window(now_msk: datetime) -> bool:
    return now_msk.weekday() < 5 and ENTRY_WINDOW_START <= now_msk.time() <= ENTRY_WINDOW_END


def restart_allowed_now(project_root: Path, run_name: str) -> tuple[bool, str]:
    now_msk = datetime.now(MSK)
    if active_entry_window(now_msk):
        return False, f"entry_window {now_msk.strftime('%H:%M')}"
    open_count, details = runtime_open_positions(project_root, run_name)
    if open_count > 0:
        detail_suffix = f" {' '.join(details[:6])}" if details else ""
        return False, f"open_positions={open_count}{detail_suffix}"
    return True, f"safe_window {now_msk.strftime('%H:%M')}"


def install_systemd_units(project_root: Path, deploy_dir: Path) -> tuple[bool, list[str]]:
    copied_units: set[str] = set()
    notes: list[str] = []
    for name in SYSTEMD_UNIT_NAMES:
        src = deploy_dir / name
        dst = Path("/etc/systemd/system") / name
        if not src.exists():
            continue
        shutil.copy2(src, dst)
        copied_units.add(name)
    if not copied_units:
        return True, ["systemd_sync skipped copied_units=0"]

    daemon_reload = run(["systemctl", "daemon-reload"], project_root)
    notes.append(f"daemon_reload rc={daemon_reload.returncode} copied_units={len(copied_units)}")
    if daemon_reload.returncode != 0:
        return False, notes

    enable_services = [name for name in SYSTEMD_ENABLE_SERVICES if name in copied_units]
    if enable_services:
        result = run(["systemctl", "enable", *enable_services], project_root)
        notes.append(f"enable_services rc={result.returncode} units={','.join(enable_services)}")
        if result.returncode != 0:
            return False, notes

    enable_timers = [name for name in SYSTEMD_ENABLE_TIMERS if name in copied_units]
    if enable_timers:
        result = run(["systemctl", "enable", "--now", *enable_timers], project_root)
        notes.append(f"enable_timers rc={result.returncode} units={','.join(enable_timers)}")
        if result.returncode != 0:
            return False, notes

    return True, notes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default="/opt/3pips")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="rollback-20260525-a26cf99")
    parser.add_argument("--service-name", default="3pips-paper-a26.service")
    parser.add_argument("--venv-python", default="/opt/3pips/.venv-a26cf99/bin/python")
    parser.add_argument("--log-path", default="")
    parser.add_argument("--restart-wait-sec", type=int, default=20)
    parser.add_argument("--run-name", default="v7_live_20260525")
    parser.add_argument("--dashboard-url", default="http://127.0.0.1:8768/")
    parser.add_argument("--health-stale-sec", type=int, default=180)
    parser.add_argument("--pending-restart-path", default="")
    parser.add_argument("--rollout-lock-path", default="")
    parser.add_argument("--status-path", default="")
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    runtime_dir = project_root / "reports" / "runtime"
    log_path = Path(args.log_path) if args.log_path else runtime_dir / "git_autoupdate.log"
    pending_restart_path = Path(args.pending_restart_path) if args.pending_restart_path else runtime_dir / "git_autoupdate_pending_restart.json"
    rollout_lock_path = Path(args.rollout_lock_path) if args.rollout_lock_path else runtime_dir / "git_autoupdate_rollout_lock.json"
    status_path = Path(args.status_path) if args.status_path else runtime_dir / "git_autoupdate_status.json"

    head_sha = ""
    remote_sha = ""

    def emit_status(outcome: str, reason: str, **extra: object) -> None:
        payload: dict[str, object] = {
            "updated_at": now_str(),
            "outcome": outcome,
            "reason": reason,
            "project_root": str(project_root),
            "branch": args.branch,
            "remote": args.remote,
            "service_name": args.service_name,
            "run_name": args.run_name,
            "head": head_sha,
            "remote_head": remote_sha,
            "pending_restart_exists": pending_restart_path.exists(),
            "rollout_lock_exists": rollout_lock_path.exists(),
        }
        for key, value in extra.items():
            payload[key] = value
        write_status(status_path, payload)

    status = run(git_cmd(project_root, "status", "--porcelain", "--untracked-files=no"), project_root)
    if status.returncode != 0:
        log(log_path, f"skip reason=status_failed rc={status.returncode} stderr={status.stderr.strip()[:300]}")
        emit_status("skipped", "status_failed", rc=status.returncode, stderr=status.stderr.strip()[:300])
        return 0
    if status.stdout.strip():
        log(log_path, "skip reason=dirty_worktree")
        emit_status("skipped", "dirty_worktree")
        return 0

    fetch = run(git_cmd(project_root, "fetch", args.remote, args.branch), project_root)
    if fetch.returncode != 0:
        log(log_path, f"skip reason=fetch_failed rc={fetch.returncode} stderr={fetch.stderr.strip()[:300]}")
        emit_status("skipped", "fetch_failed", rc=fetch.returncode, stderr=fetch.stderr.strip()[:300])
        return 0

    head = run(git_cmd(project_root, "rev-parse", "HEAD"), project_root)
    remote_head = run(git_cmd(project_root, "rev-parse", f"{args.remote}/{args.branch}"), project_root)
    if head.returncode != 0 or remote_head.returncode != 0:
        log(log_path, "skip reason=rev_parse_failed")
        emit_status(
            "skipped",
            "rev_parse_failed",
            head_rc=head.returncode,
            remote_head_rc=remote_head.returncode,
        )
        return 0
    head_sha = head.stdout.strip()
    remote_sha = remote_head.stdout.strip()
    pending_payload: dict[str, str] = {}
    if pending_restart_path.exists():
        try:
            pending_payload = json.loads(pending_restart_path.read_text(encoding="utf-8"))
        except Exception:
            pending_payload = {}

    need_restart = False
    update_reason = ""
    previous_head = head_sha
    requirements_changed = False
    needs_dependency_refresh = False
    lock_acquired = False
    target_sha = remote_sha or head_sha

    def acquire_rollout_lock(reason: str, old_head: str, new_head: str) -> None:
        nonlocal lock_acquired
        if lock_acquired:
            return
        write_rollout_lock(
            rollout_lock_path,
            {
                "started_at": now_str(),
                "reason": reason,
                "old_head": old_head,
                "new_head": new_head,
                "service_name": args.service_name,
            },
        )
        lock_acquired = True

    try:
        if head_sha != remote_sha:
            requirements_changed = "requirements.txt" in changed_paths(project_root, previous_head, remote_sha)
            allowed, why = restart_allowed_now(project_root, args.run_name)
            if not allowed:
                pending = {
                    "updated_at": now_str(),
                    "old_head": previous_head,
                    "new_head": remote_sha,
                    "reason": "remote_update_available",
                    "deferred_because": why,
                    "deps_ready": not requirements_changed,
                    "merged": False,
                }
                write_pending_restart(pending_restart_path, pending)
                log(log_path, f"defer reason={why} update_available old={previous_head} new={remote_sha}")
                emit_status(
                    "deferred",
                    "remote_update_available",
                    old_head=previous_head,
                    new_head=remote_sha,
                    deferred_because=why,
                    deps_ready=not requirements_changed,
                    merged=False,
                )
                return 0
            acquire_rollout_lock("apply_remote_update", previous_head, remote_sha)
            ff = run(git_cmd(project_root, "merge", "--ff-only", remote_sha), project_root)
            if ff.returncode != 0:
                log(log_path, f"fail reason=ff_merge_failed rc={ff.returncode} stderr={ff.stderr.strip()[:300]}")
                emit_status("failed", "ff_merge_failed", rc=ff.returncode, stderr=ff.stderr.strip()[:300], old_head=previous_head, new_head=remote_sha)
                return 1

            needs_dependency_refresh = requirements_changed

            deploy_dir = project_root / "deploy"
            units_ok, unit_notes = install_systemd_units(project_root, deploy_dir)
            for note in unit_notes:
                log(log_path, note)
            if not units_ok:
                emit_status(
                    "failed",
                    "systemd_unit_refresh_failed",
                    old_head=previous_head,
                    new_head=remote_sha,
                    systemd_notes=unit_notes,
                )
                return 1
            need_restart = True
            update_reason = f"updated old={previous_head} new={remote_sha}"
        elif pending_payload:
            need_restart = True
            target_sha = str(pending_payload.get("new_head") or head_sha)
            update_reason = f"pending_restart old={pending_payload.get('old_head','')} new={target_sha}"
            needs_dependency_refresh = not bool(pending_payload.get("deps_ready", True))
        else:
            log(log_path, f"skip reason=up_to_date head={head_sha}")
            emit_status("ok", "up_to_date", old_head=head_sha, new_head=remote_sha)
            return 0

        if needs_dependency_refresh:
            if not lock_acquired:
                acquire_rollout_lock("apply_pending_dependencies", pending_payload.get("old_head", previous_head), target_sha)
            pip_install = run([args.venv_python, "-m", "pip", "install", "-r", "requirements.txt"], project_root)
            log(log_path, f"pip rc={pip_install.returncode} stdout_len={len(pip_install.stdout)} stderr_len={len(pip_install.stderr)}")
            if pip_install.returncode != 0:
                pending = {
                    "updated_at": now_str(),
                    "old_head": pending_payload.get("old_head", previous_head),
                    "new_head": remote_sha if remote_sha else head_sha,
                    "reason": update_reason or "pending_restart",
                    "deferred_because": "dependency_install_failed",
                    "deps_ready": False,
                }
                write_pending_restart(pending_restart_path, pending)
                log(
                    log_path,
                    f"fail reason=dependency_install rc={pip_install.returncode} stderr={pip_install.stderr.strip()[:300]}",
                )
                emit_status(
                    "failed",
                    "dependency_install_failed",
                    rc=pip_install.returncode,
                    stderr=pip_install.stderr.strip()[:300],
                    old_head=pending_payload.get("old_head", previous_head),
                    new_head=remote_sha if remote_sha else head_sha,
                )
                return 1

        allowed, why = restart_allowed_now(project_root, args.run_name)
        if not allowed:
            pending = {
                "updated_at": now_str(),
                "old_head": pending_payload.get("old_head", previous_head),
                "new_head": remote_sha if remote_sha else head_sha,
                "reason": update_reason or "pending_restart",
                "deferred_because": why,
                "deps_ready": True,
                "merged": True,
            }
            write_pending_restart(pending_restart_path, pending)
            log(log_path, f"defer reason={why} {pending['reason']}")
            emit_status(
                "deferred",
                "restart_pending_runtime",
                old_head=pending_payload.get("old_head", previous_head),
                new_head=remote_sha if remote_sha else head_sha,
                deferred_because=why,
                deps_ready=True,
                merged=True,
            )
            return 0

        if not lock_acquired:
            acquire_rollout_lock("restart_pending_runtime", pending_payload.get("old_head", previous_head), target_sha)
        restart = run(["systemctl", "restart", args.service_name], project_root)
        if restart.returncode != 0:
            log(log_path, f"fail reason=service_restart rc={restart.returncode} stderr={restart.stderr.strip()[:300]}")
            emit_status("failed", "service_restart_failed", rc=restart.returncode, stderr=restart.stderr.strip()[:300], old_head=previous_head, new_head=target_sha)
            return 1
        time.sleep(max(5, args.restart_wait_sec))

        active = run(["systemctl", "is-active", args.service_name], project_root)
        if active.returncode != 0 or active.stdout.strip() != "active":
            log(log_path, f"fail reason=service_not_active status={active.stdout.strip()} stderr={active.stderr.strip()[:300]}")
            emit_status(
                "failed",
                "service_not_active",
                status=active.stdout.strip(),
                stderr=active.stderr.strip()[:300],
                old_head=previous_head,
                new_head=target_sha,
            )
            return 1

        issues = verify_runtime_ready(project_root, args.run_name, args.dashboard_url, args.health_stale_sec)
        if issues:
            pending = {
                "updated_at": now_str(),
                "old_head": pending_payload.get("old_head", previous_head),
                "new_head": target_sha,
                "reason": update_reason or "post_restart_verification_failed",
                "deferred_because": "post_restart_verification_failed",
                "deps_ready": True,
                "merged": True,
            }
            write_pending_restart(pending_restart_path, pending)
            log(log_path, f"fail reason=post_restart_verification summary={' ; '.join(issues)}")
            emit_status(
                "failed",
                "post_restart_verification_failed",
                old_head=pending_payload.get("old_head", previous_head),
                new_head=target_sha,
                issues=issues,
            )
            return 1

        if pending_restart_path.exists():
            pending_restart_path.unlink()
        log(log_path, f"{update_reason} service={args.service_name} restart={why}")
        emit_status(
            "updated",
            "restart_completed",
            old_head=pending_payload.get("old_head", previous_head),
            new_head=target_sha,
            restart_window=why,
            update_reason=update_reason,
        )
        return 0
    finally:
        if lock_acquired:
            clear_rollout_lock(rollout_lock_path)


if __name__ == "__main__":
    raise SystemExit(main())
