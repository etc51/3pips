from __future__ import annotations

import csv
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

from autonomy_common import write_json
from auto_policy_merge import merge_policy_views, strip_watchdog_overrides, summarize_active_policy
from auto_policy_utils import normalize_upper_list, policy_group_blackout_windows


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


def empty_watchdog_bucket() -> dict[str, float]:
    return {"closed_net_rub": 0.0, "open_net_rub": 0.0, "losses": 0.0, "trades": 0.0, "open_positions": 0.0}


def group_family_key(portfolio: str, contour: str, family: str) -> str:
    portfolio_name = str(portfolio or "").strip().upper()
    contour_name = str(contour or "").strip().upper()
    family_name = str(family or "").strip().upper()
    if not portfolio_name or not contour_name or not family_name:
        return ""
    return f"{portfolio_name}/{contour_name}::{family_name}"


def split_group_family_key(value: str) -> tuple[str, str, str]:
    text = str(value or "").strip().upper()
    if not text:
        return "", "", ""
    try:
        head, family = text.split("::", 1)
        portfolio_name, contour_name = head.split("/", 1)
    except ValueError:
        return "", "", ""
    return portfolio_name, contour_name, family


def group_family_slice_key(value: str) -> str:
    portfolio_name, contour_name, _family = split_group_family_key(value)
    if not portfolio_name or not contour_name:
        return ""
    return f"{portfolio_name}/{contour_name}"


def concentrated_family_group_key(family: str, by_group_family: dict[str, dict[str, float]]) -> str:
    family_norm = str(family or "").strip().upper()
    if not family_norm:
        return ""
    family_rows: list[tuple[str, dict[str, float]]] = []
    for key, row in by_group_family.items():
        if key.endswith(f"::{family_norm}"):
            family_rows.append((key, row))
    if not family_rows:
        return ""
    worst_key, worst_row = min(
        family_rows,
        key=lambda item: float(item[1]["closed_net_rub"] + item[1]["open_net_rub"]),
    )
    worst_total = abs(float(worst_row["closed_net_rub"] + worst_row["open_net_rub"]))
    if worst_total < 1_000:
        return ""
    if len(family_rows) == 1:
        return worst_key
    total_negative_abs = sum(
        abs(float(row["closed_net_rub"] + row["open_net_rub"]))
        for _, row in family_rows
        if float(row["closed_net_rub"] + row["open_net_rub"]) < 0
    )
    nonnegative_other = any(
        float(row["closed_net_rub"] + row["open_net_rub"]) >= 0 and key != worst_key
        for key, row in family_rows
    )
    if total_negative_abs >= 1_000 and nonnegative_other and worst_total >= total_negative_abs * 0.55:
        return worst_key
    return ""


def ticker_fully_covered(
    secid: str,
    ticker_group_families: dict[str, set[str]],
    covered_portfolios: set[str],
    covered_group_families: set[str],
    covered_families: set[str],
) -> bool:
    slices = ticker_group_families.get(str(secid or "").strip().upper()) or set()
    if not slices:
        return False
    for slice_key in slices:
        portfolio_name, _contour_name, family_name = split_group_family_key(slice_key)
        if portfolio_name in covered_portfolios:
            continue
        if slice_key in covered_group_families:
            continue
        if family_name in covered_families:
            continue
        return False
    return True


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
                    portfolio_group = str(row.get("portfolio_group") or path.stem.removesuffix("_multi_futures_paper_trades") or "").upper()
                    contour = str(row.get("contour") or "").strip().upper()
                    rows.append(
                        {
                            "secid": secid,
                            "family": str(row.get("family") or family_from_ticker(secid)).upper(),
                            "portfolio_group": portfolio_group,
                            "contour": contour,
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
    if path in {"", "/"}:
        clean = ""
    else:
        clean = path[:-1] if path.endswith("/") else path
    return urlunsplit((parts.scheme, parts.netloc, f"{clean}/api/state", "", ""))


def load_dashboard_state(dashboard_url: str) -> dict:
    try:
        with urlopen(api_state_url(dashboard_url), timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def compute_intraday_watchdog_overrides(
    run_dir: Path,
    trade_date: str,
    dashboard_state: dict | None = None,
    base_active: dict | None = None,
) -> dict:
    base_active = base_active if isinstance(base_active, dict) else {}
    covered_group_blackout_slices = set(policy_group_blackout_windows(base_active))
    rows = load_closed_trade_rows(run_dir, trade_date)
    by_ticker: dict[str, dict[str, float]] = {}
    by_family: dict[str, dict[str, float]] = {}
    by_group_family: dict[str, dict[str, float]] = {}
    ticker_group_families: dict[str, set[str]] = {}
    for row in rows:
        secid = row["secid"]
        family = row["family"]
        portfolio_group = row["portfolio_group"]
        contour = row["contour"]
        net = float(row["net_rub"])
        ticker_bucket = by_ticker.setdefault(secid, empty_watchdog_bucket())
        family_bucket = by_family.setdefault(family, empty_watchdog_bucket())
        slice_key = group_family_key(portfolio_group, contour, family)
        group_family_bucket = by_group_family.setdefault(slice_key, empty_watchdog_bucket()) if slice_key else None
        ticker_bucket["closed_net_rub"] += net
        ticker_bucket["trades"] += 1
        family_bucket["closed_net_rub"] += net
        family_bucket["trades"] += 1
        if group_family_bucket is not None:
            group_family_bucket["closed_net_rub"] += net
            group_family_bucket["trades"] += 1
            ticker_group_families.setdefault(secid, set()).add(slice_key)
        if net < 0:
            ticker_bucket["losses"] += 1
            family_bucket["losses"] += 1
            if group_family_bucket is not None:
                group_family_bucket["losses"] += 1

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
            portfolio_group = str(row.get("portfolio") or row.get("portfolio_group") or "").upper()
            contour = str(row.get("contour") or "").upper()
            ticker_bucket = by_ticker.setdefault(secid, empty_watchdog_bucket())
            family_bucket = by_family.setdefault(family, empty_watchdog_bucket())
            slice_key = group_family_key(portfolio_group, contour, family)
            group_family_bucket = by_group_family.setdefault(slice_key, empty_watchdog_bucket()) if slice_key else None
            ticker_bucket["open_net_rub"] += open_net
            ticker_bucket["open_positions"] += 1
            family_bucket["open_net_rub"] += open_net
            family_bucket["open_positions"] += 1
            if group_family_bucket is not None:
                group_family_bucket["open_net_rub"] += open_net
                group_family_bucket["open_positions"] += 1
                ticker_group_families.setdefault(secid, set()).add(slice_key)

    observe_group_families: list[str] = []
    localized_blackout_notes: list[str] = []
    for key, bucket in sorted(by_group_family.items()):
        if not key:
            continue
        triggered = (
            (bucket["losses"] >= 1 and bucket["closed_net_rub"] <= -1500.0)
            or (bucket["losses"] >= 2 and bucket["closed_net_rub"] <= -1000.0)
            or (bucket["closed_net_rub"] + bucket["open_net_rub"] <= -1500.0 and bucket["open_positions"] >= 1)
            or (bucket["open_net_rub"] <= -1200.0 and bucket["open_positions"] >= 1)
        )
        if not triggered:
            continue
        slice_key = group_family_slice_key(key)
        if slice_key in covered_group_blackout_slices:
            localized_blackout_notes.append(
                f"watchdog intraday: {key} stays inside active group blackout {slice_key}"
            )
            continue
        observe_group_families.append(key)

    observe_families: list[str] = []
    for family, bucket in sorted(by_family.items()):
        triggered = (
            (bucket["losses"] >= 1 and bucket["closed_net_rub"] <= -3000.0)
            or (bucket["losses"] >= 2 and bucket["trades"] >= 3 and bucket["closed_net_rub"] <= -1000.0)
            or (bucket["closed_net_rub"] + bucket["open_net_rub"] <= -3000.0 and bucket["open_positions"] >= 1)
            or (bucket["open_net_rub"] <= -2000.0 and bucket["open_positions"] >= 2)
        )
        if not triggered:
            continue
        concentrated_key = concentrated_family_group_key(family, by_group_family)
        if concentrated_key:
            slice_key = group_family_slice_key(concentrated_key)
            if slice_key in covered_group_blackout_slices:
                localized_blackout_notes.append(
                    f"watchdog intraday: {family} stays local in {concentrated_key} and is already covered by group blackout"
                )
                continue
            observe_group_families.append(concentrated_key)
            continue
        observe_families.append(family)
    observe_group_families = sorted(set(observe_group_families))
    observe_families = sorted(set(observe_families))
    observe_tickers = sorted(
        secid
        for secid, bucket in by_ticker.items()
        if (
            (bucket["losses"] >= 1 and bucket["closed_net_rub"] <= -2000.0)
            or (bucket["losses"] >= 2 and bucket["closed_net_rub"] <= -1500.0)
            or (bucket["closed_net_rub"] + bucket["open_net_rub"] <= -2000.0 and bucket["open_positions"] >= 1)
            or (bucket["open_net_rub"] <= -1200.0 and bucket["open_positions"] >= 1 and bucket["losses"] >= 1)
        )
        and family_from_ticker(secid) not in set(observe_families)
    )

    covered_portfolios = set(normalize_upper_list(base_active.get("observe_only_portfolios")))
    covered_group_families = set(normalize_upper_list(base_active.get("observe_only_group_families")))
    covered_families = set(normalize_upper_list(base_active.get("observe_only_families")))
    observe_group_families = sorted(
        key
        for key in observe_group_families
        if key not in covered_group_families
        and split_group_family_key(key)[0] not in covered_portfolios
        and split_group_family_key(key)[2] not in covered_families
    )
    effective_group_families = covered_group_families | set(observe_group_families)
    effective_families = covered_families | set(observe_families)
    observe_tickers = sorted(
        secid
        for secid in observe_tickers
        if not ticker_fully_covered(secid, ticker_group_families, covered_portfolios, effective_group_families, effective_families)
    )

    notes: list[str] = []
    for key in observe_group_families[:6]:
        notes.append(f"watchdog intraday: {key} -> observe-only after slice damage threshold")
    notes.extend(localized_blackout_notes[:4])
    for family in observe_families[:4]:
        notes.append(f"watchdog intraday: {family} -> observe-only after family damage threshold")
    for secid in observe_tickers[:4]:
        notes.append(f"watchdog intraday: {secid} -> observe-only after ticker damage threshold")
    return {
        "trade_date": trade_date,
        "observe_only_group_families": observe_group_families,
        "observe_only_tickers": observe_tickers,
        "observe_only_families": observe_families,
        "entry_blackout_group_windows": {},
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
    active = payload.get("active") if isinstance(payload.get("active"), dict) else {}
    active_base = payload.get("active_base") if isinstance(payload.get("active_base"), dict) else strip_watchdog_overrides(active, current_overrides)
    if not trade_date:
        overrides = {
            "trade_date": "",
            "observe_only_group_families": [],
            "observe_only_tickers": [],
            "observe_only_families": [],
            "entry_blackout_group_windows": {},
            "notes": [],
        }
    else:
        overrides = compute_intraday_watchdog_overrides(
            run_dir,
            trade_date,
            load_dashboard_state(dashboard_url) if dashboard_url else {},
            active_base,
        )
    merged_active = merge_policy_views(active_base, overrides)

    changed = (
        normalize_upper_list((current_overrides or {}).get("observe_only_group_families")) != normalize_upper_list(overrides.get("observe_only_group_families"))
        or normalize_upper_list((current_overrides or {}).get("observe_only_tickers")) != normalize_upper_list(overrides.get("observe_only_tickers"))
        or normalize_upper_list((current_overrides or {}).get("observe_only_families")) != normalize_upper_list(overrides.get("observe_only_families"))
        or policy_group_blackout_windows(current_overrides) != policy_group_blackout_windows(overrides)
        or [str(item) for item in ((current_overrides or {}).get("notes") or []) if str(item).strip()] != [str(item) for item in (overrides.get("notes") or []) if str(item).strip()]
        or payload.get("active_base") != active_base
        or payload.get("active") != merged_active
    )

    payload["active_base"] = active_base
    payload["watchdog_overrides"] = overrides
    payload["active"] = merged_active
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    summary.update(summarize_active_policy(merged_active))
    payload["summary"] = summary
    if changed:
        write_json(policy_path, payload)
    return changed, (
        f"trade_date={trade_date or '-'} "
        f"group_families={','.join(overrides.get('observe_only_group_families') or []) or '-'} "
        f"families={','.join(overrides.get('observe_only_families') or []) or '-'} "
        f"tickers={','.join(overrides.get('observe_only_tickers') or []) or '-'}"
    )
