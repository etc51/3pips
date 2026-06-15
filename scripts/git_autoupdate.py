from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

from autonomy_common import now_str


MSK = ZoneInfo("Europe/Moscow")
ENTRY_WINDOW_START = dt_time(10, 15)
ENTRY_WINDOW_END = dt_time(17, 45)


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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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
    parser.add_argument("--pending-restart-path", default="")
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    runtime_dir = project_root / "reports" / "runtime"
    log_path = Path(args.log_path) if args.log_path else runtime_dir / "git_autoupdate.log"
    pending_restart_path = Path(args.pending_restart_path) if args.pending_restart_path else runtime_dir / "git_autoupdate_pending_restart.json"

    status = run(git_cmd(project_root, "status", "--porcelain", "--untracked-files=no"), project_root)
    if status.returncode != 0:
        log(log_path, f"skip reason=status_failed rc={status.returncode} stderr={status.stderr.strip()[:300]}")
        return 0
    if status.stdout.strip():
        log(log_path, "skip reason=dirty_worktree")
        return 0

    fetch = run(git_cmd(project_root, "fetch", args.remote, args.branch), project_root)
    if fetch.returncode != 0:
        log(log_path, f"skip reason=fetch_failed rc={fetch.returncode} stderr={fetch.stderr.strip()[:300]}")
        return 0

    head = run(git_cmd(project_root, "rev-parse", "HEAD"), project_root)
    remote_head = run(git_cmd(project_root, "rev-parse", f"{args.remote}/{args.branch}"), project_root)
    if head.returncode != 0 or remote_head.returncode != 0:
        log(log_path, "skip reason=rev_parse_failed")
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
            return 0
        ff = run(git_cmd(project_root, "merge", "--ff-only", remote_sha), project_root)
        if ff.returncode != 0:
            log(log_path, f"fail reason=ff_merge_failed rc={ff.returncode} stderr={ff.stderr.strip()[:300]}")
            return 1

        needs_dependency_refresh = requirements_changed

        deploy_dir = project_root / "deploy"
        unit_names = [
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
        units_installed = 0
        for name in unit_names:
            src = deploy_dir / name
            dst = Path("/etc/systemd/system") / name
            if src.exists():
                shutil.copy2(src, dst)
                units_installed += 1
        if units_installed:
            daemon_reload = run(["systemctl", "daemon-reload"], project_root)
            log(log_path, f"daemon_reload rc={daemon_reload.returncode} units_installed={units_installed}")
        need_restart = True
        update_reason = f"updated old={previous_head} new={remote_sha}"
    elif pending_payload:
        need_restart = True
        update_reason = f"pending_restart old={pending_payload.get('old_head','')} new={pending_payload.get('new_head', head_sha)}"
        needs_dependency_refresh = not bool(pending_payload.get("deps_ready", True))
    else:
        log(log_path, f"skip reason=up_to_date head={head_sha}")
        return 0

    if needs_dependency_refresh:
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
        return 0

    restart = run(["systemctl", "restart", args.service_name], project_root)
    if restart.returncode != 0:
        log(log_path, f"fail reason=service_restart rc={restart.returncode} stderr={restart.stderr.strip()[:300]}")
        return 1
    time.sleep(max(5, args.restart_wait_sec))

    active = run(["systemctl", "is-active", args.service_name], project_root)
    if active.returncode != 0 or active.stdout.strip() != "active":
        log(log_path, f"fail reason=service_not_active status={active.stdout.strip()} stderr={active.stderr.strip()[:300]}")
        return 1

    if pending_restart_path.exists():
        pending_restart_path.unlink()
    log(log_path, f"{update_reason} service={args.service_name} restart={why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
