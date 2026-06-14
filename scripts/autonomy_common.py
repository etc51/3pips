from __future__ import annotations

import csv
import json
import os
import smtplib
import ssl
import zipfile
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except Exception:
            continue
    return None


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value: object, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def read_csv_rows(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv_rows(path: Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    ensure_dir(path.parent)
    if fieldnames is None:
        ordered: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    ordered.append(key)
        fieldnames = ordered
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames or [])
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: object) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    path.write_text(text, encoding="utf-8")


def tail_text(path: Path, lines: int = 300) -> str:
    if not path.exists():
        return ""
    data = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return "\n".join(data[-lines:])


def build_zip(zip_path: Path, root_dir: Path) -> None:
    ensure_dir(zip_path.parent)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(root_dir.rglob("*")):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(root_dir))


def smtp_settings(default_recipient: str = "") -> dict:
    recipient = os.getenv("ALERT_EMAIL_TO") or default_recipient
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    sender = (os.getenv("SMTP_FROM") or username).strip()
    host = (os.getenv("SMTP_HOST") or "smtp.yandex.ru").strip()
    port = int(os.getenv("SMTP_PORT") or "465")
    mode = (os.getenv("SMTP_MODE") or "ssl").strip().lower()
    enabled = bool(recipient and username and password and sender and host and port)
    return {
        "enabled": enabled,
        "recipient": recipient,
        "username": username,
        "password": password,
        "sender": sender,
        "host": host,
        "port": port,
        "mode": mode,
    }


def send_email(
    subject: str,
    body: str,
    recipient: str = "",
    attachments: list[Path] | None = None,
) -> tuple[bool, str]:
    settings = smtp_settings(default_recipient=recipient)
    if not settings["enabled"]:
        return False, "disabled_missing_smtp"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings["sender"]
    msg["To"] = settings["recipient"]
    msg.set_content(body)

    for path in attachments or []:
        if not path.exists():
            continue
        data = path.read_bytes()
        msg.add_attachment(
            data,
            maintype="application",
            subtype="zip" if path.suffix.lower() == ".zip" else "octet-stream",
            filename=path.name,
        )

    try:
        if settings["mode"] == "starttls":
            with smtplib.SMTP(settings["host"], settings["port"], timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(settings["username"], settings["password"])
                smtp.send_message(msg)
        else:
            with smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=30, context=ssl.create_default_context()) as smtp:
                smtp.login(settings["username"], settings["password"])
                smtp.send_message(msg)
        return True, "sent"
    except Exception as exc:
        return False, f"send_failed:{exc}"
