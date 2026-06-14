#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import smtplib
import socket
import tarfile
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


RUN_NAME = "v7_live_20260525"

RAW_CSV_PATTERNS = [
    "*_multi_futures_paper_trades.csv",
    "*_paper_trades.csv",
    "*_entry_audit.csv",
    "*_shadow_exit_models.csv",
    "*_gpt_shadow_trades.csv",
    "*_live_orderbook_snapshots.csv",
    "*_wide_spread_review.csv",
]
SMALL_STATE_PATTERNS = [
    "*_health.json",
    "*_risk_policy_state.json",
    "*_paper_open_positions.json",
    "*_startup_status.csv",
    "*_instrument_specs.csv",
    "*_roll_state.json",
]
DATE_COLUMN_CANDIDATES = [
    "closed_at",
    "event_time",
    "snapshot_time",
    "opened_at",
    "timestamp",
    "created_at",
]
TEXT_LOG_SUFFIXES = (".log", ".err.log", ".stdout.log", ".stderr.log", ".jsonl")
STATIC_REFERENCE_PATHS = [
    Path("reports") / "futures_scalp_profiles_v7_paper_20260525.csv",
    Path("reports") / "futures_scalp_profiles_v7_paper_20260525_gpt_shadow_params.csv",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_files(root: Path, paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if not path.exists():
            continue
        if path.is_file():
            files.append(path)
            continue
        for child in path.rglob("*"):
            if child.is_file():
                files.append(child)
    unique = sorted({p.resolve() for p in files})
    return unique


def prune_archives(archive_dir: Path, keep_days: int) -> int:
    if keep_days <= 0 or not archive_dir.exists():
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    patterns = ("3pips_paper_*.tar.gz", "3pips_daily_raw_*.zip")
    for pattern in patterns:
        for path in archive_dir.glob(pattern):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except FileNotFoundError:
                continue
    return removed


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


def ensure_dir_clean(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def match_day(text: str, day_iso: str, day_compact: str) -> bool:
    return day_iso in text or day_compact in text


def collect_text_sources(root_dir: Path) -> list[Path]:
    if not root_dir.exists():
        return []
    files: list[Path] = []
    for path in root_dir.rglob("*"):
        if not path.is_file():
            continue
        name = path.name
        if name.startswith("tmp_") or name.endswith(".pid"):
            continue
        if name.endswith(TEXT_LOG_SUFFIXES):
            files.append(path)
    return sorted(files)


def filter_csv_by_day(source: Path, dest: Path, day_iso: str, day_compact: str) -> dict:
    with source.open("r", encoding="utf-8", errors="ignore", newline="") as src:
        reader = csv.DictReader(src)
        fieldnames = reader.fieldnames or []
        if not fieldnames:
            return {"copied": False, "reason": "no_header", "rows": 0}
        date_column = next((name for name in DATE_COLUMN_CANDIDATES if name in fieldnames), "")
        if not date_column:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            return {"copied": True, "mode": "full_copy", "rows": None, "date_column": ""}

        tmp_dest = dest.with_suffix(dest.suffix + ".tmp")
        rows = 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tmp_dest.open("w", encoding="utf-8", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=fieldnames)
            writer.writeheader()
            for row in reader:
                raw_value = str(row.get(date_column, ""))
                if match_day(raw_value, day_iso, day_compact):
                    writer.writerow(row)
                    rows += 1
        if rows == 0:
            tmp_dest.unlink(missing_ok=True)
            return {"copied": False, "rows": 0, "date_column": date_column}
        tmp_dest.replace(dest)
        return {"copied": True, "rows": rows, "date_column": date_column}


def filter_text_by_day(source: Path, dest: Path, day_iso: str, day_compact: str) -> dict:
    tmp_dest = dest.with_suffix(dest.suffix + ".tmp")
    lines = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with source.open("r", encoding="utf-8", errors="ignore") as src, tmp_dest.open(
        "w", encoding="utf-8"
    ) as out:
        for line in src:
            if match_day(line, day_iso, day_compact):
                out.write(line)
                lines += 1
    if lines == 0:
        tmp_dest.unlink(missing_ok=True)
        return {"copied": False, "lines": 0}
    tmp_dest.replace(dest)
    return {"copied": True, "lines": lines}


def build_daily_raw_package(root: Path, run_name: str, archive_dir: Path, day_iso: str) -> tuple[Path, dict]:
    day_compact = day_iso.replace("-", "")
    run_dir = root / "reports" / "paper_runs" / run_name
    runtime_dir = root / "reports" / "runtime"
    build_root = archive_dir / "_daily_raw_build" / f"{run_name}_{day_compact}"
    payload_root = build_root / f"3pips_daily_raw_{run_name}_{day_compact}"
    ensure_dir_clean(payload_root)

    summary: dict[str, object] = {
        "day": day_iso,
        "run_name": run_name,
        "csv_files": [],
        "text_logs": [],
        "state_files": [],
        "reference_files": [],
    }
    seen_csv: set[Path] = set()
    seen_state: set[Path] = set()

    for pattern in RAW_CSV_PATTERNS:
        for source in sorted(run_dir.glob(pattern)):
            if source in seen_csv:
                continue
            seen_csv.add(source)
            rel = source.relative_to(root)
            dest = payload_root / rel
            result = filter_csv_by_day(source, dest, day_iso, day_compact)
            if result.get("copied"):
                summary["csv_files"].append(
                    {
                        "path": str(rel).replace("\\", "/"),
                        "rows": result.get("rows"),
                        "date_column": result.get("date_column", ""),
                    }
                )

    for pattern in SMALL_STATE_PATTERNS:
        for source in sorted(run_dir.glob(pattern)):
            if source in seen_state:
                continue
            seen_state.add(source)
            rel = source.relative_to(root)
            dest = payload_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            summary["state_files"].append(str(rel).replace("\\", "/"))

    for source in collect_text_sources(run_dir):
        rel = source.relative_to(root)
        dest = payload_root / rel
        result = filter_text_by_day(source, dest, day_iso, day_compact)
        if result.get("copied"):
            summary["text_logs"].append(
                {
                    "path": str(rel).replace("\\", "/"),
                    "lines": result.get("lines", 0),
                }
            )

    for source in collect_text_sources(runtime_dir):
        rel = source.relative_to(root)
        dest = payload_root / rel
        result = filter_text_by_day(source, dest, day_iso, day_compact)
        if result.get("copied"):
            summary["text_logs"].append(
                {
                    "path": str(rel).replace("\\", "/"),
                    "lines": result.get("lines", 0),
                }
            )

    for rel_path in STATIC_REFERENCE_PATHS:
        source = root / rel_path
        if not source.exists():
            continue
        dest = payload_root / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        summary["reference_files"].append(str(rel_path).replace("\\", "/"))

    portfolio_config = run_dir / "portfolio_config.json"
    if portfolio_config.exists():
        rel = portfolio_config.relative_to(root)
        dest = payload_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(portfolio_config, dest)
        summary["reference_files"].append(str(rel).replace("\\", "/"))

    payload_files = [path for path in payload_root.rglob("*") if path.is_file()]
    summary["file_count"] = len(payload_files)
    summary["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    manifest_path = payload_root / "DAILY_RAW_MANIFEST.json"
    manifest_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    archive_path = archive_dir / f"3pips_daily_raw_{run_name}_{day_compact}.zip"
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(payload_root.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(payload_root).as_posix())

    shutil.rmtree(build_root, ignore_errors=True)
    summary["archive"] = str(archive_path)
    summary["size"] = archive_path.stat().st_size
    return archive_path, summary


def send_archive_email(archive_path: Path, day_iso: str, run_name: str) -> dict:
    if not env_bool("ARCHIVE_EMAIL_ENABLED", False):
        return {"enabled": False, "sent": False, "status": "disabled"}

    smtp_host = os.environ.get("ARCHIVE_SMTP_HOST", "smtp.yandex.ru").strip() or "smtp.yandex.ru"
    smtp_port = int(os.environ.get("ARCHIVE_SMTP_PORT", "465").strip() or "465")
    smtp_user = os.environ.get("ARCHIVE_SMTP_USER", "etc00051@yandex.ru").strip() or "etc00051@yandex.ru"
    smtp_password = read_secret_value("ARCHIVE_SMTP_PASSWORD", "ARCHIVE_SMTP_PASSWORD_FILE")
    mail_from = os.environ.get("ARCHIVE_EMAIL_FROM", smtp_user).strip() or smtp_user
    mail_to = os.environ.get("ARCHIVE_EMAIL_TO", "etc00051@yandex.ru").strip() or "etc00051@yandex.ru"
    use_ssl = env_bool("ARCHIVE_SMTP_USE_SSL", True)
    use_starttls = env_bool("ARCHIVE_SMTP_STARTTLS", False)

    if not smtp_password:
        raise RuntimeError("ARCHIVE_EMAIL_ENABLED=1, but SMTP password is missing")

    host = socket.gethostname()
    msg = EmailMessage()
    msg["Subject"] = f"3pips raw archive {run_name} {day_iso}"
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg.set_content(
        "\n".join(
            [
                f"Daily raw archive for {run_name}",
                f"Date: {day_iso}",
                f"Host: {host}",
                f"File: {archive_path.name}",
                "",
                "Attachment contains filtered trade data, diagnostics, snapshots, logs, and references.",
            ]
        )
    )
    msg.add_attachment(
        archive_path.read_bytes(),
        maintype="application",
        subtype="zip",
        filename=archive_path.name,
    )

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
        "host": smtp_host,
        "port": smtp_port,
        "size": archive_path.stat().st_size,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--run-name", default=RUN_NAME)
    parser.add_argument("--archive-dir", default="")
    parser.add_argument("--keep-days", type=int, default=14)
    parser.add_argument("--daily-raw-day", default="")
    args = parser.parse_args()

    root = Path(args.project_root).resolve()
    run_dir = root / "reports" / "paper_runs" / args.run_name
    runtime_dir = root / "reports" / "runtime"
    archive_dir = Path(args.archive_dir).resolve() if args.archive_dir else root / "reports" / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = archive_dir / f"3pips_paper_{args.run_name}_{stamp}.tar.gz"

    include_paths = [
        run_dir,
        runtime_dir / "v7_paper_supervisor_20260525.log",
        runtime_dir / "v7_paper_supervisor_20260525.pid",
        runtime_dir / "v7_paper_dashboard_20260525.pid",
        *[root / rel for rel in STATIC_REFERENCE_PATHS],
    ]
    files = iter_files(root, include_paths)
    manifest = {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "project_root": str(root),
        "run_name": args.run_name,
        "file_count": len(files),
        "files": [],
    }

    with tarfile.open(archive_path, "w:gz") as tar:
        for path in files:
            rel = path.relative_to(root)
            stat = path.stat()
            manifest["files"].append(
                {
                    "path": str(rel).replace("\\", "/"),
                    "size": stat.st_size,
                    "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "sha256": sha256_file(path),
                }
            )
            tar.add(path, arcname=str(rel).replace("\\", "/"))

        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo("ARCHIVE_MANIFEST.json")
        info.size = len(manifest_bytes)
        info.mtime = time.time()
        tar.addfile(info, io.BytesIO(manifest_bytes))

    day_iso = args.daily_raw_day.strip() or datetime.now().strftime("%Y-%m-%d")
    daily_raw_archive, daily_raw_summary = build_daily_raw_package(root, args.run_name, archive_dir, day_iso)
    email_result = send_archive_email(daily_raw_archive, day_iso, args.run_name)

    removed = prune_archives(archive_dir, args.keep_days)
    print(
        json.dumps(
            {
                "archive": str(archive_path),
                "files": len(files),
                "size": archive_path.stat().st_size,
                "daily_raw_archive": str(daily_raw_archive),
                "daily_raw_size": daily_raw_archive.stat().st_size,
                "daily_raw_summary": daily_raw_summary,
                "email": email_result,
                "removed_old_archives": removed,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
