from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

from autonomy_common import now_str


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default="/opt/3pips")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="rollback-20260525-a26cf99")
    parser.add_argument("--service-name", default="3pips-paper-a26.service")
    parser.add_argument("--venv-python", default="/opt/3pips/.venv-a26cf99/bin/python")
    parser.add_argument("--log-path", default="")
    parser.add_argument("--restart-wait-sec", type=int, default=20)
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    runtime_dir = project_root / "reports" / "runtime"
    log_path = Path(args.log_path) if args.log_path else runtime_dir / "git_autoupdate.log"

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
    if head_sha == remote_sha:
        log(log_path, f"skip reason=up_to_date head={head_sha}")
        return 0

    ff = run(git_cmd(project_root, "merge", "--ff-only", remote_sha), project_root)
    if ff.returncode != 0:
        log(log_path, f"fail reason=ff_merge_failed rc={ff.returncode} stderr={ff.stderr.strip()[:300]}")
        return 1

    pip_install = run([args.venv_python, "-m", "pip", "install", "-r", "requirements.txt"], project_root)
    log(log_path, f"pip rc={pip_install.returncode} stdout_len={len(pip_install.stdout)} stderr_len={len(pip_install.stderr)}")

    restart = run(["systemctl", "restart", args.service_name], project_root)
    if restart.returncode != 0:
        log(log_path, f"fail reason=service_restart rc={restart.returncode} stderr={restart.stderr.strip()[:300]}")
        return 1
    time.sleep(max(5, args.restart_wait_sec))

    active = run(["systemctl", "is-active", args.service_name], project_root)
    if active.returncode != 0 or active.stdout.strip() != "active":
        log(log_path, f"fail reason=service_not_active status={active.stdout.strip()} stderr={active.stderr.strip()[:300]}")
        return 1

    log(log_path, f"updated old={head_sha} new={remote_sha} service={args.service_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
