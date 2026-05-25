from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path


APP_NAME = "3pips Paper"
DASHBOARD_URL = "http://127.0.0.1:8768/"
RUNTIME_DIR = Path("reports") / "runtime"
RUN_DIR = Path("reports") / "paper_runs" / "v7_live_20260525"
PORTFOLIOS = ["classic_core", "gl_watch", "neo", "tail_research"]


def executable_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_project_root() -> Path:
    env_root = os.environ.get("PIPS_PROJECT_ROOT")
    if env_root:
        root = Path(env_root).expanduser().resolve()
        if is_project_root(root):
            return root
    candidates = [
        Path.cwd(),
        executable_dir(),
        executable_dir().parent,
        executable_dir().parent.parent,
        Path(__file__).resolve().parents[1] if not getattr(sys, "frozen", False) else executable_dir(),
    ]
    for root in candidates:
        root = root.resolve()
        if is_project_root(root):
            return root
    raise RuntimeError("Project root not found. Set PIPS_PROJECT_ROOT to the project folder.")


def is_project_root(path: Path) -> bool:
    return (
        (path / "src" / "multi_futures_paper.py").exists()
        and (path / "src" / "paper_dashboard.py").exists()
        and (path / "scripts" / "watch_v7_paper_contours_20260525.ps1").exists()
    )


def python_exe(root: Path) -> str:
    env_python = os.environ.get("PIPS_PYTHON")
    if env_python and Path(env_python).exists():
        return env_python
    bundled = Path("D:/piton/python.exe")
    if bundled.exists():
        return str(bundled)
    venv = root / ".venv" / "Scripts" / "python.exe"
    if venv.exists():
        return str(venv)
    return "python"


def pid_path(root: Path, name: str) -> Path:
    return root / RUNTIME_DIR / name


def read_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="ascii").strip()
        return int(text)
    except Exception:
        return None


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            timeout=5,
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def process_status(root: Path) -> dict:
    supervisor_pid = read_pid(pid_path(root, "v7_paper_supervisor_20260525.pid"))
    dashboard_pid = read_pid(pid_path(root, "v7_paper_dashboard_20260525.pid"))
    bots = {}
    for name in PORTFOLIOS:
        pid = read_pid(pid_path(root, f"v7_paper_{name}.pid"))
        bots[name] = {"pid": pid, "alive": pid_alive(pid), "health": read_health(root, name)}
    return {
        "root": str(root),
        "dashboard_url": DASHBOARD_URL,
        "supervisor": {"pid": supervisor_pid, "alive": pid_alive(supervisor_pid)},
        "dashboard": {"pid": dashboard_pid, "alive": pid_alive(dashboard_pid)},
        "bots": bots,
    }


def read_health(root: Path, name: str) -> dict:
    path = root / RUN_DIR / f"{name}_health.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def start_supervisor(root: Path) -> str:
    status = process_status(root)
    if status["supervisor"]["alive"]:
        return "Supervisor already running."
    (root / RUNTIME_DIR).mkdir(parents=True, exist_ok=True)
    stdout = root / RUNTIME_DIR / "windows_app_supervisor.stdout.log"
    stderr = root / RUNTIME_DIR / "windows_app_supervisor.stderr.log"
    script = root / "scripts" / "watch_v7_paper_contours_20260525.ps1"
    args = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-ProjectRoot",
        str(root),
        "-PythonExe",
        python_exe(root),
        "-DashboardPort",
        "8768",
    ]
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    with stdout.open("ab") as out, stderr.open("ab") as err:
        subprocess.Popen(args, cwd=str(root), stdout=out, stderr=err, creationflags=flags)
    return "Supervisor started."


def stop_process_tree(pid: int | None) -> bool:
    if not pid or not pid_alive(pid):
        return False
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=flags)
    return True


def stop_all(root: Path) -> str:
    names = ["v7_paper_supervisor_20260525.pid", "v7_paper_dashboard_20260525.pid"]
    names += [f"v7_paper_{name}.pid" for name in PORTFOLIOS]
    stopped = []
    for name in names:
        pid = read_pid(pid_path(root, name))
        if stop_process_tree(pid):
            stopped.append(f"{name}:{pid}")
    return "Stopped: " + ", ".join(stopped) if stopped else "Nothing to stop."


def dashboard_api_state() -> dict:
    with urllib.request.urlopen(DASHBOARD_URL + "api/state", timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def compact_status(root: Path) -> str:
    status = process_status(root)
    lines = [
        APP_NAME,
        f"Project: {status['root']}",
        f"Supervisor: {'ON' if status['supervisor']['alive'] else 'OFF'} pid={status['supervisor']['pid']}",
        f"Dashboard: {'ON' if status['dashboard']['alive'] else 'OFF'} pid={status['dashboard']['pid']}",
    ]
    for name, item in status["bots"].items():
        health = item.get("health") or {}
        net = health.get("closed_net", "-")
        open_positions = health.get("open_positions", "-")
        stream_age = health.get("last_stream_age_sec", "-")
        lines.append(
            f"{name}: {'ON' if item['alive'] else 'OFF'} pid={item['pid']} "
            f"net={net} open={open_positions} stream_age={stream_age}"
        )
    return "\n".join(lines)


def run_gui(root: Path) -> None:
    import tkinter as tk
    from tkinter import messagebox

    window = tk.Tk()
    window.title(APP_NAME)
    window.geometry("720x520")
    window.minsize(620, 420)

    title = tk.Label(window, text=APP_NAME, font=("Segoe UI", 16, "bold"))
    title.pack(anchor="w", padx=14, pady=(12, 2))

    root_label = tk.Label(window, text=str(root), font=("Segoe UI", 9), fg="#666")
    root_label.pack(anchor="w", padx=14, pady=(0, 10))

    buttons = tk.Frame(window)
    buttons.pack(fill="x", padx=12, pady=4)

    output = tk.Text(window, height=18, wrap="word", font=("Consolas", 10))
    output.pack(fill="both", expand=True, padx=12, pady=12)

    def write(text: str) -> None:
        output.delete("1.0", tk.END)
        output.insert(tk.END, text)

    def refresh() -> None:
        try:
            write(compact_status(root))
        except Exception as exc:
            write(f"Status error: {exc}")

    def start() -> None:
        try:
            msg = start_supervisor(root)
            time.sleep(1)
            write(msg + "\n\n" + compact_status(root))
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def stop() -> None:
        if not messagebox.askyesno(APP_NAME, "Остановить supervisor, dashboard и paper-ботов?"):
            return
        try:
            msg = stop_all(root)
            time.sleep(1)
            write(msg + "\n\n" + compact_status(root))
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def open_dash() -> None:
        webbrowser.open(DASHBOARD_URL)

    tk.Button(buttons, text="Старт", width=14, command=start).pack(side="left", padx=4)
    tk.Button(buttons, text="Открыть дашборд", width=18, command=open_dash).pack(side="left", padx=4)
    tk.Button(buttons, text="Обновить статус", width=16, command=refresh).pack(side="left", padx=4)
    tk.Button(buttons, text="Стоп", width=14, command=stop).pack(side="left", padx=4)

    refresh()
    window.mainloop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--status-json", action="store_true")
    parser.add_argument("--open-dashboard", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = find_project_root()
    if args.start:
        print(start_supervisor(root))
        return 0
    if args.stop:
        print(stop_all(root))
        return 0
    if args.status_json:
        print(json.dumps(process_status(root), ensure_ascii=False, indent=2))
        return 0
    if args.open_dashboard:
        webbrowser.open(DASHBOARD_URL)
        return 0
    run_gui(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
