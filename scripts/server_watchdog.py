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
from urllib.parse import urlsplit, urlunsplit

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


def normalize_upper_list(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        text = str(value or "").strip().upper()
        if text:
            out.append(text)
    return sorted(set(out))


def family_from_ticker(ticker: str) -> str:
    secid = str(ticker or "").strip()
    if not secid:
        return ""
    if secid.endswith("perpA"):
        return secid.upper()
    head = secid.rstrip("0123456789")
    month_codes = set("FGHJKMNQUVXZ")
    if len(head) > 1 and head[-1].upper() in month_codes:
        head = head[:-1]
    return (head or secid).upper()


def watchdog_candidate_ticker(ticker: str) -> bool:
    secid = str(ticker or "").strip().upper()
    if not secid:
        return False
    return not secid.endswith("PERPA")


def load_closed_trade_rows(run_dir: Path, trade_date: str) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(run_dir.glob("*_multi_futures_paper_trades.csv")):
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    closed_at = str(row.get("closed_at") or "")
                    if trade_date and not closed_at.startswith(trade_date):
                        continue
                    secid = str(row.get("secid") or row.get("ticker") or "").upper()
                    if not secid or not watchdog_candidate_ticker(secid):
                        continue
                    try:
                        net = float(row.get("net_rub") or 0.0)
                    except Exception:
                        continue
                    rows.append(
                        {
                            "secid": secid,
                            "family": family_from_ticker(secid),
                            "net_rub": net,
                        }
                    )
        except Exception:
            continue
    return rows


def api_state_url(dashboard_url: str) -> str:
    parts = urlsplit(dashboard_url)
    path = parts.path or ""
    if path.endswith("/api/state") or path == "/api/state":
        return dashboard_url
    clean = path[:-1] if path.endswith("/") and path != "/" else path
    return urlunsplit((parts.scheme, parts.netloc, f"{clean}/api/state", "", ""))


def load_dashboard_state(dashboard_url: str) -> dict:
    try:
        with urlopen(api_state_url(dashboard_url), timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def strip_watchdog_overrides(active: dict, overrides: dict) -> dict:
    base = dict(active) if isinstance(active, dict) else {}
    for key in ("observe_only_tickers", "observe_only_families", "strict_only_tickers", "strict_only_families"):
        values = normalize_upper_list(base.get(key))
        if key in {"observe_only_tickers", "observe_only_families"}:
            remove_values = set(normalize_upper_list(overrides.get(key)))
            values = [value for value in values if value not in remove_values]
        base[key] = values
    notes = [str(item) for item in (base.get("notes") or []) if str(item).strip()]
    remove_notes = {str(item) for item in (overrides.get("notes") or []) if str(item).strip()}
    base["notes"] = [note for note in notes if note not in remove_notes]
    return base


def merge_policy_views(base_active: dict, overrides: dict) -> dict:
    merged = dict(base_active) if isinstance(base_active, dict) else {}
    merged["observe_only_tickers"] = sorted(
        set(normalize_upper_list(base_active.get("observe_only_tickers"))) | set(normalize_upper_list(overrides.get("observe_only_tickers")))
    )
    merged["observe_only_families"] = sorted(
        set(normalize_upper_list(base_active.get("observe_only_families"))) | set(normalize_upper_list(overrides.get("observe_only_families")))
    )
    merged["strict_only_tickers"] = normalize_upper_list(base_active.get("strict_only_tickers"))
    merged["strict_only_families"] = normalize_upper_list(base_active.get("strict_only_families"))
    base_notes = [str(item) for item in (base_active.get("notes") or []) if str(item).strip()]
    override_notes = [str(item) for item in (overrides.get("notes") or []) if str(item).strip()]
    merged["notes"] = list(dict.fromkeys(base_notes + override_notes))[:12]
    return merged


def compute_intraday_watchdog_overrides(run_dir: Path, trade_date: str, dashboard_state: dict | None = None) -> dict:
    rows = load_closed_trade_rows(run_dir, trade_date)
    by_ticker: dict[str, dict[str, float]] = {}
    by_family: dict[str, dict[str, float]] = {}
    for row in rows:
        secid = row["secid"]
        family = row["family"]
        net = float(row["net_rub"])
        ticker_bucket = by_ticker.setdefault(secid, {"closed_net_rub": 0.0, "open_net_rub": 0.0, "losses": 0.0, "trades": 0.0, "open_positions": 0.0})
        family_bucket = by_family.setdefault(family, {"closed_net_rub": 0.0, "open_net_rub": 0.0, "losses": 0.0, "trades": 0.0, "open_positions": 0.0})
        ticker_bucket["closed_net_rub"] += net
        ticker_bucket["trades"] += 1
        family_bucket["closed_net_rub"] += net
        family_bucket["trades"] += 1
        if net < 0:
            ticker_bucket["losses"] += 1
            family_bucket["losses"] += 1

    if isinstance(dashboard_state, dict):
        for row in dashboard_state.get("open_positions") or []:
            if not isinstance(row, dict):
                continue
            secid = str(row.get("ticker") or row.get("secid") or "").upper()
            if not secid or not watchdog_candidate_ticker(secid):
                continue
            try:
                open_net = float(row.get("unrealized_net_rub"))
            except Exception:
                continue
            family = family_from_ticker(secid)
            ticker_bucket = by_ticker.setdefault(secid, {"closed_net_rub": 0.0, "open_net_rub": 0.0, "losses": 0.0, "trades": 0.0, "open_positions": 0.0})
            family_bucket = by_family.setdefault(family, {"closed_net_rub": 0.0, "open_net_rub": 0.0, "losses": 0.0, "trades": 0.0, "open_positions": 0.0})
            ticker_bucket["open_net_rub"] += open_net
            ticker_bucket["open_positions"] += 1
            family_bucket["open_net_rub"] += open_net
            family_bucket["open_positions"] += 1

    observe_families = sorted(
        family
        for family, bucket in by_family.items()
        if (
            (bucket["losses"] >= 1 and bucket["closed_net_rub"] <= -3000.0)
            or (bucket["closed_net_rub"] + bucket["open_net_rub"] <= -3000.0 and bucket["open_positions"] >= 1)
            or (bucket["open_net_rub"] <= -2000.0 and bucket["open_positions"] >= 2)
        )
    )
    observe_tickers = sorted(
        secid
        for secid, bucket in by_ticker.items()
        if (
            (bucket["losses"] >= 1 and bucket["closed_net_rub"] <= -2000.0)
            or (bucket["closed_net_rub"] + bucket["open_net_rub"] <= -2000.0 and bucket["open_positions"] >= 1)
            or (bucket["open_net_rub"] <= -1200.0 and bucket["open_positions"] >= 1 and bucket["losses"] >= 1)
        )
        and family_from_ticker(secid) not in set(observe_families)
    )
    notes: list[str] = []
    for family in observe_families[:4]:
        total_net = round(float(by_family[family]["closed_net_rub"] + by_family[family]["open_net_rub"]), 2)
        notes.append(f"watchdog intraday: {family} -> observe-only after {total_net:.2f} RUB family damage")
    for secid in observe_tickers[:4]:
        total_net = round(float(by_ticker[secid]["closed_net_rub"] + by_ticker[secid]["open_net_rub"]), 2)
        notes.append(f"watchdog intraday: {secid} -> observe-only after {total_net:.2f} RUB ticker damage")
    return {
        "trade_date": trade_date,
        "observe_only_tickers": observe_tickers,
        "observe_only_families": observe_families,
        "notes": notes[:8],
    }


def refresh_intraday_killer_policy(project_root: Path, run_dir: Path, dashboard_url: str = "") -> tuple[bool, str]:
    policy_path = project_root / "reports" / "autonomy" / "latest" / "latest_auto_policy.json"
    if not policy_path.exists():
        return False, "policy_missing"
    try:
        payload = json.loads(policy_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"policy_bad_json {type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return False, "policy_not_dict"

    trade_date = latest_trade_date(run_dir)
    current_overrides = payload.get("watchdog_overrides") if isinstance(payload.get("watchdog_overrides"), dict) else {}
    if not trade_date:
        overrides = {"trade_date": "", "observe_only_tickers": [], "observe_only_families": [], "notes": []}
    else:
        overrides = compute_intraday_watchdog_overrides(run_dir, trade_date, load_dashboard_state(dashboard_url) if dashboard_url else {})

    active = payload.get("active") if isinstance(payload.get("active"), dict) else {}
    active_base = payload.get("active_base") if isinstance(payload.get("active_base"), dict) else strip_watchdog_overrides(active, current_overrides)
    merged_active = merge_policy_views(active_base, overrides)

    changed = (
        normalize_upper_list((current_overrides or {}).get("observe_only_tickers")) != normalize_upper_list(overrides.get("observe_only_tickers"))
        or normalize_upper_list((current_overrides or {}).get("observe_only_families")) != normalize_upper_list(overrides.get("observe_only_families"))
        or [str(item) for item in ((current_overrides or {}).get("notes") or []) if str(item).strip()] != [str(item) for item in (overrides.get("notes") or []) if str(item).strip()]
        or payload.get("active_base") != active_base
        or payload.get("active") != merged_active
    )

    payload["active_base"] = active_base
    payload["watchdog_overrides"] = overrides
    payload["active"] = merged_active
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    summary["active_rule_count"] = (
        len(merged_active.get("observe_only_tickers") or [])
        + len(merged_active.get("observe_only_families") or [])
        + len(merged_active.get("strict_only_tickers") or [])
        + len(merged_active.get("strict_only_families") or [])
        + sum(
            1
            for key in ("entry_max_full_stop_rub", "pause_ticker_after_losses", "pause_family_after_losses", "pause_after_loss_minutes")
            if merged_active.get(key) not in (None, "")
        )
    )
    summary["active_notes_count"] = len(merged_active.get("notes") or [])
    payload["summary"] = summary
    if changed:
        write_json(policy_path, payload)
    return changed, f"trade_date={trade_date or '-'} families={','.join(overrides.get('observe_only_families') or []) or '-'} tickers={','.join(overrides.get('observe_only_tickers') or []) or '-'}"


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

    changed, summary = refresh_intraday_killer_policy(project_root, run_dir, args.dashboard_url)
    log(log_path, f"intraday_policy_refresh changed={changed} {summary}")

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
