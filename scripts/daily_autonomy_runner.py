from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from autonomy_common import (  # noqa: E402
    build_zip,
    ensure_dir,
    now_str,
    parse_dt,
    read_csv_rows,
    safe_float,
    safe_int,
    send_email,
    smtp_settings,
    write_json,
    write_text,
)
from auto_policy_utils import (  # noqa: E402
    count_group_blackout_rules,
    format_group_blackout_windows,
    merge_blackout_windows,
    merge_group_blackout_windows,
    normalize_blackout_window,
    normalize_blackout_windows,
    normalize_clock_hhmm,
    normalize_group_blackout_slice,
    normalize_group_blackout_windows,
    normalize_upper_list,
    policy_group_blackout_windows,
)
from auto_policy_merge import merge_watchdog_overrides, summarize_active_policy  # noqa: E402
from daily_autonomy_outputs import (  # noqa: E402
    build_manifest_payload,
    copy_bundle_outputs,
    persist_nightly_cycle_status,
    write_analysis_outputs,
    write_research_outputs,
)


PREMIUM_FUTURES_RATE = 0.00025
PREMIUM_FUTURES_RATE_PCT = 0.025
PREMIUM_FEE_NOTE = "Premium futures fee model: conservative 0.025% of contract notional per side for turnover up to 12M RUB/day; real fee can be lower at higher daily turnover."
DEFAULT_MARGIN_MODE = "leveraged_paper"
DEFAULT_BROKER = "tbank"
DEFAULT_TARIFF = "premium"
DEFAULT_FEE_MODEL = {
    "broker": DEFAULT_BROKER,
    "tariff": DEFAULT_TARIFF,
    "futures_rate_per_side_pct": PREMIUM_FUTURES_RATE_PCT,
    "futures_rate_per_side_fraction": PREMIUM_FUTURES_RATE,
    "rate_tiers_daily_turnover_rub": [
        {"up_to_rub": 12_000_000, "rate_pct_per_side": 0.025},
        {"up_to_rub": 17_000_000, "rate_pct_per_side": 0.020},
        {"above_rub": 17_000_000, "rate_pct_per_side": 0.015},
    ],
    "note": PREMIUM_FEE_NOTE,
}


def trade_file_group(path: Path) -> str:
    name = path.stem
    suffix = "_multi_futures_paper_trades"
    if name.endswith(suffix):
        return name[: -len(suffix)]
    return name


def shadow_file_group(path: Path) -> str:
    name = path.stem
    suffixes = ["_gpt_shadow_trades", "_shadow_exit_models"]
    for suffix in suffixes:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def load_profiles(path: Path) -> dict[str, dict]:
    return {row["ticker"]: row for row in read_csv_rows(path)}


def load_portfolio_config(run_dir: Path) -> dict:
    path = run_dir / "portfolio_config.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_portfolio_capitals(run_dir: Path) -> dict[str, float]:
    payload = load_portfolio_config(run_dir)
    portfolios = payload.get("portfolios")
    out: dict[str, float] = {}
    if not isinstance(portfolios, dict):
        return out
    for name, config in portfolios.items():
        if not isinstance(config, dict):
            continue
        capital = safe_float(config.get("capital"))
        if capital > 0:
            out[str(name)] = capital
    return out


def load_runtime_trade_model(run_dir: Path) -> dict:
    payload = load_portfolio_config(run_dir)
    fee_model = payload.get("fee_model")
    if not isinstance(fee_model, dict):
        fee_model = dict(DEFAULT_FEE_MODEL)
    else:
        merged = dict(DEFAULT_FEE_MODEL)
        merged.update(fee_model)
        fee_model = merged
    return {
        "broker": str(payload.get("broker") or fee_model.get("broker") or DEFAULT_BROKER),
        "tariff": str(payload.get("tariff") or fee_model.get("tariff") or DEFAULT_TARIFF),
        "margin_mode": str(payload.get("margin_mode") or DEFAULT_MARGIN_MODE),
        "fee_model": fee_model,
    }


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def earlier_clock_hhmm(left: object, right: object) -> str:
    left_norm = normalize_clock_hhmm(left)
    right_norm = normalize_clock_hhmm(right)
    if not left_norm:
        return right_norm
    if not right_norm:
        return left_norm
    return left_norm if left_norm <= right_norm else right_norm


def later_clock_hhmm(left: object, right: object) -> str:
    left_norm = normalize_clock_hhmm(left)
    right_norm = normalize_clock_hhmm(right)
    if not left_norm:
        return right_norm
    if not right_norm:
        return left_norm
    return left_norm if left_norm >= right_norm else right_norm


def normalize_trade_row(row: dict) -> dict | None:
    item = dict(row)
    item.pop(None, None)
    if item.get("net_rub") in (None, "") and item.get("closed_net_rub") not in (None, ""):
        item["net_rub"] = item.get("closed_net_rub")
    if not parse_dt(str(item.get("closed_at") or "")):
        return None
    contour = str(item.get("contour") or "")
    if contour not in {"strict", "aggressive"}:
        return None
    if not str(item.get("secid") or ""):
        return None
    if safe_int(item.get("qty"), -1) <= 0:
        return None
    return item


def load_primary_trades(run_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(run_dir.glob("*_multi_futures_paper_trades.csv")):
        group = trade_file_group(path)
        for row in read_csv_rows(path):
            item = normalize_trade_row(row)
            if item is None:
                continue
            item.setdefault("portfolio_group", group)
            item["_source_file"] = path.name
            rows.append(item)
    return rows


def load_shadow_trades(run_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for pattern in ("*_gpt_shadow_trades.csv", "*_shadow_exit_models.csv"):
        for path in sorted(run_dir.glob(pattern)):
            group = shadow_file_group(path)
            for row in read_csv_rows(path):
                item = dict(row)
                item.pop(None, None)
                item.setdefault("portfolio_group", group)
                item["_source_file"] = path.name
                rows.append(item)
    return rows


def load_wide_spread_reviews(run_dir: Path) -> list[dict]:
    rows: list[dict] = []
    suffix = "_wide_spread_review"
    for path in sorted(run_dir.glob("*_wide_spread_review.csv")):
        name = path.stem
        portfolio_group = name[: -len(suffix)] if name.endswith(suffix) else name
        for row in read_csv_rows(path):
            item = dict(row)
            item.pop(None, None)
            family = str(item.get("family") or "").strip().upper()
            if not family:
                continue
            item["portfolio_group"] = str(portfolio_group or "").upper()
            item["contour"] = "aggressive"
            item["group"] = f"{item['portfolio_group']}/AGGRESSIVE::{family}"
            item["_source_file"] = path.name
            rows.append(item)
    return rows


def latest_trade_date(rows: list[dict]) -> str | None:
    dates = sorted({str(row.get("closed_at") or "")[:10] for row in rows if row.get("closed_at")})
    return dates[-1] if dates else None


def filter_trade_date(rows: list[dict], trade_date: str) -> list[dict]:
    return [row for row in rows if str(row.get("closed_at") or "").startswith(trade_date)]


def filter_snapshot_date(rows: list[dict], trade_date: str) -> list[dict]:
    return [row for row in rows if str(row.get("snapshot_time") or "").startswith(trade_date)]


def parse_trade_net(row: dict) -> float:
    return safe_float(row.get("net_rub"))


def trade_sort_key(row: dict) -> tuple:
    dt = parse_dt(str(row.get("closed_at") or "")) or parse_dt(str(row.get("opened_at") or ""))
    return (dt.isoformat() if dt else "", str(row.get("secid") or ""), str(row.get("contour") or ""))


def metrics(rows: list[dict]) -> dict:
    nets = [parse_trade_net(row) for row in rows]
    wins = [value for value in nets if value > 0]
    losses = [value for value in nets if value < 0]
    total = sum(nets)
    count = len(rows)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades": count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round((len(wins) / count * 100.0), 2) if count else 0.0,
        "net_rub": round(total, 2),
        "expectancy_rub": round(total / count, 2) if count else 0.0,
        "median_trade_rub": round(median(nets), 2) if nets else 0.0,
        "avg_win_rub": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss_rub": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
    }


def grouped_metrics(rows: list[dict], key_fn) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[key_fn(row)].append(row)
    out = []
    for key, items in groups.items():
        m = metrics(items)
        m["group"] = key
        out.append(m)
    out.sort(key=lambda row: (row["net_rub"], row["expectancy_rub"]), reverse=True)
    return out


def ranked_tail(rows: list[dict], limit: int = 10, reverse: bool = False) -> list[dict]:
    ordered = sorted(
        rows,
        key=lambda row: (safe_float(row.get("net_rub")), safe_float(row.get("expectancy_rub")), safe_int(row.get("trades"), 0)),
        reverse=reverse,
    )
    return ordered[:limit]


def family_for_row(row: dict, profiles: dict[str, dict]) -> str:
    secid = str(row.get("secid") or "")
    profile = profiles.get(secid, {})
    return str(row.get("family") or profile.get("v7_family") or secid)


def entry_dt(row: dict):
    return parse_dt(str(row.get("opened_at") or "")) or parse_dt(str(row.get("closed_at") or ""))


def hour_bucket(row: dict) -> str:
    dt = entry_dt(row)
    return f"{dt.hour:02d}:00" if dt else "unknown"


def annotate_trade_rows(rows: list[dict], profiles: dict[str, dict]) -> None:
    for row in rows:
        row["family"] = family_for_row(row, profiles)
        row["group_key"] = f"{row.get('portfolio_group', '')}/{row.get('contour', '')}"


def trade_date_value(row: dict) -> str:
    return str(row.get("closed_at") or "")[:10]


def scenario_adjusted_net(row: dict, cap_rub: int | None, profiles: dict[str, dict]) -> float:
    net = parse_trade_net(row)
    qty = max(1, safe_int(row.get("qty"), 1))
    if cap_rub is None:
        return net
    secid = str(row.get("secid") or "")
    profile = profiles.get(secid, {})
    one_lot_risk = safe_float(row.get("full_stop_1lot_rub")) or safe_float(profile.get("risk_1lot_rub"))
    if one_lot_risk <= 0:
        return net
    adjusted_qty = int(cap_rub // one_lot_risk)
    adjusted_qty = max(0, min(qty, adjusted_qty))
    if adjusted_qty <= 0:
        return 0.0
    scale = adjusted_qty / qty
    return net * scale


def evaluate_scenario(
    name: str,
    rows: list[dict],
    profiles: dict[str, dict],
    predicate=None,
    cap_rub: int | None = None,
    note: str = "",
) -> dict:
    if predicate is None:
        selected = list(rows)
    else:
        selected = [row for row in rows if predicate(row)]
    adjusted_nets = [scenario_adjusted_net(row, cap_rub, profiles) for row in selected]
    wins = [value for value in adjusted_nets if value > 0]
    losses = [value for value in adjusted_nets if value < 0]
    total = sum(adjusted_nets)
    count = len(selected)
    return {
        "scenario": name,
        "note": note,
        "trades": count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round((len(wins) / count * 100.0), 2) if count else 0.0,
        "net_rub": round(total, 2),
        "expectancy_rub": round(total / count, 2) if count else 0.0,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 4) if losses else None,
    }


def evaluate_pause_after_losses(
    name: str,
    rows: list[dict],
    profiles: dict[str, dict],
    max_losses: int,
    scope: str,
    note: str,
    cap_rub: int | None = None,
) -> dict:
    loss_counts: dict[str, int] = defaultdict(int)
    selected: list[dict] = []
    skipped = 0
    for row in sorted(rows, key=trade_sort_key):
        if scope == "family":
            key = family_for_row(row, profiles)
        else:
            key = str(row.get("secid") or "")
        if not key:
            key = "unknown"
        if loss_counts[key] >= max_losses:
            skipped += 1
            continue
        selected.append(row)
        if parse_trade_net(row) < 0:
            loss_counts[key] += 1
    out = evaluate_scenario(name, selected, profiles, cap_rub=cap_rub, note=note)
    out["skipped_trades"] = skipped
    return out


def build_research_scenarios(
    all_rows: list[dict],
    sample_rows: list[dict],
    profiles: dict[str, dict],
    include_combo_scenarios: bool = True,
) -> list[dict]:
    def after_start_predicate(start_at: str):
        hh, mm = map(int, start_at.split(":"))

        def pred(row, hh=hh, mm=mm):
            dt = entry_dt(row)
            if dt is None:
                return True
            return (dt.hour, dt.minute) >= (hh, mm)

        return pred

    def before_cutoff_predicate(cutoff: str):
        hh, mm = map(int, cutoff.split(":"))

        def pred(row, hh=hh, mm=mm):
            dt = entry_dt(row)
            if dt is None:
                return True
            return (dt.hour, dt.minute) <= (hh, mm)

        return pred

    def entry_window_predicate(start_at: str, cutoff: str):
        after_start = after_start_predicate(start_at)
        before_cutoff = before_cutoff_predicate(cutoff)

        def pred(row):
            return after_start(row) and before_cutoff(row)

        return pred

    def outside_blackout_predicate(start_at: str, end_at: str):
        start_hh, start_mm = map(int, start_at.split(":"))
        end_hh, end_mm = map(int, end_at.split(":"))

        def pred(row, start_hh=start_hh, start_mm=start_mm, end_hh=end_hh, end_mm=end_mm):
            dt = entry_dt(row)
            if dt is None:
                return True
            current = (dt.hour, dt.minute)
            return current < (start_hh, start_mm) or current > (end_hh, end_mm)

        return pred

    def portfolio_contour_key(row: dict) -> str:
        return normalize_group_blackout_slice(f"{str(row.get('portfolio_group') or '')}/{str(row.get('contour') or '')}")

    def group_blackout_predicate(group_key: str, start_at: str, end_at: str):
        group_key = normalize_group_blackout_slice(group_key)
        outside_blackout = outside_blackout_predicate(start_at, end_at)

        def pred(row, group_key=group_key, outside_blackout=outside_blackout):
            if portfolio_contour_key(row) != group_key:
                return True
            return outside_blackout(row)

        return pred

    scenarios: list[dict] = []
    scenarios.append(evaluate_scenario("base", sample_rows, profiles, note="current live policy"))

    for cap in [500, 750, 1000, 1250, 1500, 2000]:
        scenarios.append(
            evaluate_scenario(
                f"stop_cap_{cap}",
                sample_rows,
                profiles,
                cap_rub=cap,
                note="linear qty rescale by full stop cap",
            )
        )

    for start_at in ["10:30", "10:45", "11:00"]:
        scenarios.append(
            evaluate_scenario(
                f"no_trade_before_{start_at.replace(':', '')}",
                sample_rows,
                profiles,
                predicate=after_start_predicate(start_at),
                note="delay session start to avoid weaker early entries",
            )
        )

    for cutoff in ["16:30", "17:00", "17:15", "17:30", "17:45"]:
        scenarios.append(
            evaluate_scenario(
                f"no_new_after_{cutoff.replace(':', '')}",
                sample_rows,
                profiles,
                predicate=before_cutoff_predicate(cutoff),
                note="entry time cutoff",
            )
        )

    for start_at, cutoff in [
        ("10:15", "11:59"),
        ("10:15", "12:59"),
        ("10:15", "13:59"),
        ("10:30", "12:59"),
        ("10:30", "13:59"),
        ("10:45", "13:59"),
    ]:
        scenarios.append(
            evaluate_scenario(
                f"entry_window_{start_at.replace(':', '')}_{cutoff.replace(':', '')}",
                sample_rows,
                profiles,
                predicate=entry_window_predicate(start_at, cutoff),
                note=f"only open new entries inside {start_at}-{cutoff}",
            )
        )

    for start_at, end_at in [
        ("12:00", "12:59"),
        ("12:00", "13:59"),
        ("12:00", "15:59"),
        ("13:00", "15:59"),
    ]:
        scenarios.append(
            evaluate_scenario(
                f"blackout_{start_at.replace(':', '')}_{end_at.replace(':', '')}",
                sample_rows,
                profiles,
                predicate=outside_blackout_predicate(start_at, end_at),
                note=f"skip new entries during {start_at}-{end_at}",
            )
        )

    observed_group_slices = sorted(
        {
            str(row.get("group") or "")
            for row in grouped_metrics(all_rows, lambda row: portfolio_contour_key(row))
            if str(row.get("group") or "")
        }
    )
    for group_key in observed_group_slices:
        try:
            portfolio_name, contour_name = group_key.split("/", 1)
        except ValueError:
            continue
        contour_token = contour_name.lower()
        for start_at, end_at in [
            ("12:00", "12:59"),
            ("12:00", "13:59"),
            ("12:00", "15:59"),
            ("13:00", "15:59"),
        ]:
            scenarios.append(
                evaluate_scenario(
                    f"group_blackout_{portfolio_name}__{contour_token}__{start_at.replace(':', '')}_{end_at.replace(':', '')}",
                    sample_rows,
                    profiles,
                    predicate=group_blackout_predicate(group_key, start_at, end_at),
                    note=f"skip new entries during {start_at}-{end_at} only for {group_key}",
                )
            )

    for contour_name in ["strict", "aggressive"]:
        scenarios.append(
            evaluate_scenario(
                f"contour_only_{contour_name}",
                sample_rows,
                profiles,
                predicate=lambda row, contour_name=contour_name: str(row.get("contour") or "") == contour_name,
                note="single signal layer only",
            )
        )

    scenarios.append(
        evaluate_pause_after_losses(
            "pause_ticker_after_1_loss",
            sample_rows,
            profiles,
            max_losses=1,
            scope="ticker",
            note="stop opening same ticker after first losing close",
        )
    )
    scenarios.append(
        evaluate_pause_after_losses(
            "pause_ticker_after_2_losses",
            sample_rows,
            profiles,
            max_losses=2,
            scope="ticker",
            note="stop opening same ticker after second losing close",
        )
    )
    scenarios.append(
        evaluate_pause_after_losses(
            "pause_family_after_2_losses",
            sample_rows,
            profiles,
            max_losses=2,
            scope="family",
            note="stop opening same family after second losing close",
        )
    )

    family_rows = grouped_metrics(
        all_rows,
        lambda row: family_for_row(row, profiles),
    )
    portfolio_rows = grouped_metrics(
        all_rows,
        lambda row: str(row.get("portfolio_group") or ""),
    )
    group_family_rows = grouped_metrics(
        all_rows,
        lambda row: f"{str(row.get('portfolio_group') or '')}/{str(row.get('contour') or '').lower()}::{family_for_row(row, profiles).upper()}",
    )
    weak_families = [row["group"] for row in family_rows if row["trades"] >= 3 and row["net_rub"] < 0]
    weak_portfolios = [row["group"] for row in portfolio_rows if row["group"] and row["trades"] >= 1 and row["net_rub"] < 0]
    weak_group_families = [
        row["group"]
        for row in group_family_rows
        if row["group"] and row["net_rub"] < 0 and (row["trades"] >= 2 or row["net_rub"] <= -1_000)
    ]
    profitable_aggressive_group_families = []
    for row in group_family_rows:
        group_name = str(row.get("group") or "")
        if not group_name or safe_int(row.get("trades")) < 2 or safe_float(row.get("net_rub")) <= 0:
            continue
        portfolio_name, contour_name, family = split_group_family_key(group_name)
        if contour_name != "AGGRESSIVE" or not portfolio_name or not family:
            continue
        profitable_aggressive_group_families.append((portfolio_name, family, safe_float(row.get("net_rub"))))
    profitable_aggressive_group_families.sort(key=lambda item: item[2], reverse=True)
    if weak_families:
        for family in weak_families[:8]:
            scenarios.append(
                evaluate_scenario(
                    f"blacklist_family_{family}",
                    sample_rows,
                    profiles,
                    predicate=lambda row, family=family: family_for_row(row, profiles) != family,
                    note="remove one weak family",
                )
            )
    if weak_portfolios:
        for portfolio in weak_portfolios[:6]:
            scenarios.append(
                evaluate_scenario(
                    f"blacklist_portfolio_{portfolio}",
                    sample_rows,
                    profiles,
                    predicate=lambda row, portfolio=portfolio: str(row.get("portfolio_group") or "") != portfolio,
                    note="remove one weak portfolio group",
                )
            )
    if weak_group_families:
        for item in weak_group_families[:8]:
            try:
                group_key, family = item.split("::", 1)
                portfolio_name, contour_name = group_key.split("/", 1)
            except ValueError:
                continue
            scenarios.append(
                evaluate_scenario(
                    f"blacklist_group_family_{portfolio_name}__{contour_name}__{family}",
                    sample_rows,
                    profiles,
                    predicate=lambda row, group_key=group_key, family=family: (
                        f"{str(row.get('portfolio_group') or '')}/{str(row.get('contour') or '').lower()}" != group_key
                        or family_for_row(row, profiles).upper() != family
                    ),
                    note="remove one weak portfolio/contour/family slice",
                )
            )

    profitable_families = {row["group"] for row in family_rows if row["trades"] >= 3 and row["net_rub"] > 0}
    if profitable_families:
        scenarios.append(
            evaluate_scenario(
                "whitelist_profitable_families",
                sample_rows,
                profiles,
                predicate=lambda row: family_for_row(row, profiles) in profitable_families,
                note="keep only families positive on full sample",
            )
        )

    for portfolio_name, family, _net_rub in profitable_aggressive_group_families[:6]:
        scenarios.append(
            evaluate_scenario(
                f"strict_plus_aggressive_group_family_{portfolio_name}__{family}",
                sample_rows,
                profiles,
                predicate=lambda row, portfolio_name=portfolio_name, family=family: (
                    str(row.get("contour") or "") == "strict"
                    or (
                        str(row.get("contour") or "") == "aggressive"
                        and str(row.get("portfolio_group") or "").upper() == portfolio_name
                        and family_for_row(row, profiles).upper() == family
                    )
                ),
                note=f"strict layer + profitable aggressive slice {portfolio_name}/AGGRESSIVE::{family}",
            )
        )

    if include_combo_scenarios:
        scenarios.append(
            evaluate_scenario(
                "combo_stop_cap_500__contour_only_strict",
                sample_rows,
                profiles,
                predicate=lambda row: str(row.get("contour") or "") == "strict",
                cap_rub=500,
                note="strict layer only + 500 RUB stop cap",
            )
        )
        scenarios.append(
            evaluate_pause_after_losses(
                "combo_stop_cap_500__pause_ticker_after_1_loss",
                sample_rows,
                profiles,
                max_losses=1,
                scope="ticker",
                cap_rub=500,
                note="500 RUB stop cap + ticker pause after first losing close",
            )
        )
        scenarios.append(
            evaluate_pause_after_losses(
                "combo_stop_cap_500__pause_family_after_2_losses",
                sample_rows,
                profiles,
                max_losses=2,
                scope="family",
                cap_rub=500,
                note="500 RUB stop cap + family pause after second losing close",
            )
        )
        for cutoff in ["17:00", "17:15", "17:30"]:
            cutoff_token = cutoff.replace(":", "")
            scenarios.append(
                evaluate_scenario(
                    f"combo_stop_cap_500__no_new_after_{cutoff_token}",
                    sample_rows,
                    profiles,
                    predicate=before_cutoff_predicate(cutoff),
                    cap_rub=500,
                    note=f"500 RUB stop cap + no new entries after {cutoff}",
                )
            )
            scenarios.append(
                evaluate_scenario(
                    f"combo_stop_cap_500__contour_only_strict__no_new_after_{cutoff_token}",
                    sample_rows,
                    profiles,
                    predicate=lambda row, cutoff=cutoff: (
                        str(row.get("contour") or "") == "strict"
                        and before_cutoff_predicate(cutoff)(row)
                    ),
                    cap_rub=500,
                    note=f"strict only + 500 RUB stop cap + no new entries after {cutoff}",
                )
            )
        for start_at, end_at in [("12:00", "13:59"), ("12:00", "15:59")]:
            start_token = start_at.replace(":", "")
            end_token = end_at.replace(":", "")
            scenarios.append(
                evaluate_scenario(
                    f"combo_stop_cap_500__blackout_{start_token}_{end_token}",
                    sample_rows,
                    profiles,
                    predicate=outside_blackout_predicate(start_at, end_at),
                    cap_rub=500,
                    note=f"500 RUB stop cap + skip new entries during {start_at}-{end_at}",
                )
            )
            scenarios.append(
                evaluate_scenario(
                    f"combo_stop_cap_500__contour_only_strict__blackout_{start_token}_{end_token}",
                    sample_rows,
                    profiles,
                    predicate=lambda row, start_at=start_at, end_at=end_at: (
                        str(row.get("contour") or "") == "strict"
                        and outside_blackout_predicate(start_at, end_at)(row)
                    ),
                    cap_rub=500,
                    note=f"strict only + 500 RUB stop cap + skip new entries during {start_at}-{end_at}",
                )
            )
        for family in weak_families[:4]:
            scenarios.append(
                evaluate_scenario(
                    f"combo_stop_cap_500__blacklist_family_{family}",
                    sample_rows,
                    profiles,
                    predicate=lambda row, family=family: family_for_row(row, profiles) != family,
                    cap_rub=500,
                    note="500 RUB stop cap + remove one weak family",
                )
            )
            scenarios.append(
                evaluate_scenario(
                    f"combo_stop_cap_500__contour_only_strict__blacklist_family_{family}",
                    sample_rows,
                    profiles,
                    predicate=lambda row, family=family: (
                        str(row.get("contour") or "") == "strict"
                        and family_for_row(row, profiles) != family
                    ),
                    cap_rub=500,
                    note=f"strict only + 500 RUB stop cap + remove family {family}",
                )
            )
        for portfolio in weak_portfolios[:4]:
            scenarios.append(
                evaluate_scenario(
                    f"combo_stop_cap_500__blacklist_portfolio_{portfolio}",
                    sample_rows,
                    profiles,
                    predicate=lambda row, portfolio=portfolio: str(row.get("portfolio_group") or "") != portfolio,
                    cap_rub=500,
                    note=f"500 RUB stop cap + remove portfolio {portfolio}",
                )
            )
        for item in weak_group_families[:6]:
            try:
                group_key, family = item.split("::", 1)
                portfolio_name, contour_name = group_key.split("/", 1)
            except ValueError:
                continue
            scenarios.append(
                evaluate_scenario(
                    f"combo_stop_cap_500__blacklist_group_family_{portfolio_name}__{contour_name}__{family}",
                    sample_rows,
                    profiles,
                    predicate=lambda row, group_key=group_key, family=family: (
                        f"{str(row.get('portfolio_group') or '')}/{str(row.get('contour') or '').lower()}" != group_key
                        or family_for_row(row, profiles).upper() != family
                    ),
                    cap_rub=500,
                    note=f"500 RUB stop cap + remove slice {group_key}::{family}",
                )
            )
        for portfolio_name, family, _net_rub in profitable_aggressive_group_families[:4]:
            scenarios.append(
                evaluate_scenario(
                    f"combo_stop_cap_500__strict_plus_aggressive_group_family_{portfolio_name}__{family}",
                    sample_rows,
                    profiles,
                    predicate=lambda row, portfolio_name=portfolio_name, family=family: (
                        str(row.get("contour") or "") == "strict"
                        or (
                            str(row.get("contour") or "") == "aggressive"
                            and str(row.get("portfolio_group") or "").upper() == portfolio_name
                            and family_for_row(row, profiles).upper() == family
                        )
                    ),
                    cap_rub=500,
                    note=f"500 RUB stop cap + strict layer + aggressive slice {portfolio_name}/AGGRESSIVE::{family}",
                )
            )

    scenarios.sort(key=lambda row: (row["net_rub"], row["expectancy_rub"], row["trades"]), reverse=True)
    return scenarios


def classify_day(trade_date: str, rows: list[dict], profiles: dict[str, dict]) -> dict:
    overall = metrics(rows)
    nets = [parse_trade_net(row) for row in rows]
    wins = [value for value in nets if value > 0]
    losses = sorted([value for value in nets if value < 0])
    gross_win = round(sum(wins), 2)
    gross_loss = round(abs(sum(losses)), 2)
    top1_loss = round(abs(losses[0]), 2) if losses else 0.0
    top3_loss = round(abs(sum(losses[:3])), 2) if losses else 0.0
    late_rows = []
    for row in rows:
        dt = entry_dt(row)
        if dt and dt.hour >= 17:
            late_rows.append(row)
    late_net = round(sum(parse_trade_net(row) for row in late_rows), 2)
    by_ticker = grouped_metrics(rows, lambda row: str(row.get("secid") or ""))
    by_family = grouped_metrics(rows, lambda row: family_for_row(row, profiles))
    worst_ticker = ranked_tail([row for row in by_ticker if safe_float(row.get("net_rub")) < 0], limit=1, reverse=False)
    worst_family = ranked_tail([row for row in by_family if safe_float(row.get("net_rub")) < 0], limit=1, reverse=False)
    killer_condition = top3_loss >= max(1500.0, gross_win * 0.9) or top1_loss >= max(1000.0, gross_win * 0.6)
    if overall["net_rub"] > 0 and not killer_condition:
        day_class = "good_day"
        class_reason = "день в плюсе без доминирующего хвостового убытка"
    elif killer_condition:
        day_class = "killer_day"
        class_reason = "1-3 крупных убытка съедают дневную структуру"
    else:
        day_class = "bad_day"
        class_reason = "день в минусе без одного явного разрушителя"
    return {
        "trade_date": trade_date,
        "trades": overall["trades"],
        "wins": overall["wins"],
        "losses": overall["losses"],
        "win_rate_pct": overall["win_rate_pct"],
        "net_rub": overall["net_rub"],
        "expectancy_rub": overall["expectancy_rub"],
        "gross_win_rub": gross_win,
        "gross_loss_rub": gross_loss,
        "top1_loss_rub": top1_loss,
        "top3_loss_rub": top3_loss,
        "late_net_rub": late_net,
        "worst_ticker": worst_ticker[0]["group"] if worst_ticker else "",
        "worst_ticker_net_rub": worst_ticker[0]["net_rub"] if worst_ticker else 0.0,
        "worst_family": worst_family[0]["group"] if worst_family else "",
        "worst_family_net_rub": worst_family[0]["net_rub"] if worst_family else 0.0,
        "day_class": day_class,
        "class_reason": class_reason,
    }


def build_day_history(all_rows: list[dict], profiles: dict[str, dict]) -> list[dict]:
    out: list[dict] = []
    dates = sorted({trade_date_value(row) for row in all_rows if trade_date_value(row)})
    for trade_date in dates:
        day_rows = filter_trade_date(all_rows, trade_date)
        if not day_rows:
            continue
        out.append(classify_day(trade_date, day_rows, profiles))
    return out


def build_recurring_killers(day_history: list[dict], field: str) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    net_field = f"{field}_net_rub"
    for row in day_history:
        key = str(row.get(field) or "")
        if key:
            groups[key].append(row)
    out = []
    for key, items in groups.items():
        bucket_nets = [safe_float(item.get(net_field)) for item in items]
        out.append(
            {
                "group": key,
                "days": len(items),
                "killer_days": sum(1 for item in items if item.get("day_class") == "killer_day"),
                "bad_days": sum(1 for item in items if item.get("day_class") == "bad_day"),
                "good_days": sum(1 for item in items if item.get("day_class") == "good_day"),
                "total_bucket_net_rub": round(sum(bucket_nets), 2),
                "avg_bucket_net_rub": round(sum(bucket_nets) / len(bucket_nets), 2) if bucket_nets else 0.0,
                "worst_bucket_rub": round(min(bucket_nets), 2) if bucket_nets else 0.0,
                "latest_trade_date": items[-1].get("trade_date"),
            }
        )
    out.sort(key=lambda row: (safe_int(row.get("killer_days"), 0), safe_float(row.get("total_bucket_net_rub"))), reverse=True)
    return out


def build_scenario_history(all_rows: list[dict], profiles: dict[str, dict]) -> list[dict]:
    dates = sorted({trade_date_value(row) for row in all_rows if trade_date_value(row)})
    out: list[dict] = []
    for trade_date in dates:
        history_rows = [row for row in all_rows if trade_date_value(row) <= trade_date]
        day_rows = filter_trade_date(all_rows, trade_date)
        scenarios = build_research_scenarios(history_rows, day_rows, profiles, include_combo_scenarios=False)
        for idx, scenario in enumerate(scenarios, start=1):
            item = dict(scenario)
            item["trade_date"] = trade_date
            item["rank"] = idx
            out.append(item)
    return out


def summarize_scenario_history(rows: list[dict]) -> list[dict]:
    base_by_date = {
        str(row.get("trade_date") or ""): row
        for row in rows
        if str(row.get("scenario") or "") == "base" and str(row.get("trade_date") or "")
    }
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[str(row.get("scenario") or "")].append(row)
    out: list[dict] = []
    for scenario, items in groups.items():
        nets = [safe_float(item.get("net_rub")) for item in items]
        deltas = []
        beat_base_days = 0
        latest_day_delta = None
        for item in items:
            trade_date = str(item.get("trade_date") or "")
            base = base_by_date.get(trade_date)
            if not base:
                continue
            delta = safe_float(item.get("net_rub")) - safe_float(base.get("net_rub"))
            deltas.append(delta)
            if delta > 0:
                beat_base_days += 1
            latest_day_delta = delta
        note = next((str(item.get("note") or "") for item in items if item.get("note")), "")
        out.append(
            {
                "scenario": scenario,
                "note": note,
                "days": len(items),
                "positive_days": sum(1 for value in nets if value > 0),
                "negative_days": sum(1 for value in nets if value < 0),
                "total_net_rub": round(sum(nets), 2),
                "avg_daily_net_rub": round(sum(nets) / len(nets), 2) if nets else 0.0,
                "median_daily_net_rub": round(median(nets), 2) if nets else 0.0,
                "worst_day_rub": round(min(nets), 2) if nets else 0.0,
                "best_day_rub": round(max(nets), 2) if nets else 0.0,
                "latest_day_rub": round(nets[-1], 2) if nets else 0.0,
                "beat_base_days": beat_base_days,
                "beat_base_pct": round((beat_base_days / len(items) * 100.0), 2) if items else 0.0,
                "delta_total_rub": round(sum(deltas), 2),
                "latest_day_delta_rub": round(latest_day_delta, 2) if latest_day_delta is not None else 0.0,
            }
        )
    out.sort(
        key=lambda row: (
            safe_int(row.get("beat_base_days"), 0),
            safe_float(row.get("delta_total_rub")),
            safe_float(row.get("median_daily_net_rub")),
            safe_float(row.get("worst_day_rub")),
            safe_float(row.get("total_net_rub")),
        ),
        reverse=True,
    )
    return out


def pick_best_consensus_scenario(rows: list[dict]) -> dict:
    if not rows:
        return {}
    base = next((row for row in rows if row.get("scenario") == "base"), rows[0])
    qualified = []
    for row in rows:
        if row.get("scenario") == "base":
            continue
        days = max(1, safe_int(row.get("days"), 0))
        if safe_int(row.get("beat_base_days"), 0) < math.ceil(days * 0.5):
            continue
        if safe_float(row.get("delta_total_rub")) <= 0:
            continue
        qualified.append(row)
    return qualified[0] if qualified else base


def scenario_kind(name: str) -> str:
    if scenario_allow_aggressive_group_family(name):
        return "allow_aggressive_group_family"
    if name.startswith("combo_"):
        return "combo_overlay"
    if name.startswith("stop_cap_"):
        return "stop_cap_rub"
    if name.startswith("entry_window_"):
        return "entry_window"
    if name.startswith("group_blackout_"):
        return "group_blackout"
    if name.startswith("blackout_"):
        return "entry_blackout"
    if name.startswith("no_trade_before_"):
        return "entry_start"
    if name.startswith("no_new_after_"):
        return "entry_cutoff"
    if name.startswith("contour_only_"):
        return "contour_filter"
    if name.startswith("pause_ticker_after_"):
        return "ticker_pause_after_losses"
    if name.startswith("pause_family_after_"):
        return "family_pause_after_losses"
    if name.startswith("blacklist_family_"):
        return "family_blacklist"
    if name.startswith("blacklist_portfolio_"):
        return "portfolio_blacklist"
    if name.startswith("blacklist_group_family_"):
        return "group_family_blacklist"
    if name.startswith("whitelist_"):
        return "family_whitelist"
    return "other"


def recommended_use_for_scenario(kind: str) -> str:
    if kind == "combo_overlay":
        return "candidate_runtime_combo"
    if kind in {"entry_window", "entry_blackout", "entry_start", "entry_cutoff", "stop_cap_rub"}:
        return "candidate_runtime_tune"
    if kind == "allow_aggressive_group_family":
        return "candidate_runtime_exception"
    if kind in {
        "contour_filter",
        "ticker_pause_after_losses",
        "family_pause_after_losses",
        "family_blacklist",
        "portfolio_blacklist",
        "group_family_blacklist",
        "group_blackout",
        "family_whitelist",
    }:
        return "candidate_runtime_restriction"
    return "research_only"


def build_optimizer_candidates(
    research_day: list[dict],
    research_all: list[dict],
    research_consensus: list[dict],
) -> list[dict]:
    rows: list[dict] = []

    def append_rows(source: str, items: list[dict], limit: int) -> None:
        for rank, row in enumerate(items[:limit], start=1):
            scenario = str(row.get("scenario") or "")
            if not scenario or scenario == "base":
                continue
            kind = scenario_kind(scenario)
            item = {
                "source": source,
                "rank": rank,
                "scenario": scenario,
                "candidate_type": kind,
                "recommended_use": recommended_use_for_scenario(kind),
                "note": str(row.get("note") or ""),
            }
            for key in (
                "trades",
                "wins",
                "losses",
                "win_rate_pct",
                "net_rub",
                "expectancy_rub",
                "profit_factor",
                "days",
                "beat_base_days",
                "beat_base_pct",
                "delta_total_rub",
                "median_daily_net_rub",
                "worst_day_rub",
                "latest_day_delta_rub",
            ):
                if key in row:
                    item[key] = row.get(key)
            rows.append(item)

    append_rows("latest_day", research_day, 12)
    append_rows("all_sample", research_all, 12)
    append_rows("consensus", research_consensus, 12)

    priority = {"consensus": 0, "all_sample": 1, "latest_day": 2}
    deduped: dict[str, dict] = {}
    for row in rows:
        scenario = str(row.get("scenario") or "")
        prev = deduped.get(scenario)
        if prev is None:
            deduped[scenario] = row
            continue
        prev_pri = priority.get(str(prev.get("source") or ""), 99)
        curr_pri = priority.get(str(row.get("source") or ""), 99)
        if curr_pri < prev_pri:
            deduped[scenario] = row
            continue
        if curr_pri == prev_pri:
            prev_score = (
                safe_float(prev.get("delta_total_rub")),
                safe_float(prev.get("net_rub")),
                safe_float(prev.get("expectancy_rub")),
            )
            curr_score = (
                safe_float(row.get("delta_total_rub")),
                safe_float(row.get("net_rub")),
                safe_float(row.get("expectancy_rub")),
            )
            if curr_score > prev_score:
                deduped[scenario] = row

    out = list(deduped.values())
    out.sort(
        key=lambda row: (
            priority.get(str(row.get("source") or ""), 99) * -1,
            safe_float(row.get("beat_base_days")),
            safe_float(row.get("delta_total_rub")),
            safe_float(row.get("net_rub")),
            safe_float(row.get("expectancy_rub")),
        ),
        reverse=True,
    )
    return out


def build_restriction_rows(auto_policy: dict) -> list[dict]:
    active = auto_policy.get("active") if isinstance(auto_policy.get("active"), dict) else {}
    proposed = auto_policy.get("proposed") if isinstance(auto_policy.get("proposed"), dict) else {}
    rows: list[dict] = []
    for value in active.get("observe_only_portfolios") or []:
        rows.append(
            {
                "stage": "active",
                "restriction_type": "observe_only_portfolios",
                "value": value,
                "note": "",
            }
        )
    for value in active.get("observe_only_group_families") or []:
        rows.append(
            {
                "stage": "active",
                "restriction_type": "observe_only_group_families",
                "value": value,
                "note": "",
            }
        )
    for value in active.get("allow_aggressive_group_families") or []:
        rows.append(
            {
                "stage": "active",
                "restriction_type": "allow_aggressive_group_families",
                "value": value,
                "note": "",
            }
        )
    for key in ("observe_only_tickers", "observe_only_families", "strict_only_tickers", "strict_only_families"):
        values = active.get(key) or []
        for value in values:
            rows.append(
                {
                    "stage": "active",
                    "restriction_type": key,
                    "value": value,
                    "note": "",
                }
            )
    if active.get("entry_max_full_stop_rub") not in (None, ""):
        rows.append(
            {
                "stage": "active",
                "restriction_type": "entry_max_full_stop_rub",
                "value": active.get("entry_max_full_stop_rub"),
                "note": "",
            }
        )
    if active.get("entry_no_trade_before") not in (None, ""):
        rows.append(
            {
                "stage": "active",
                "restriction_type": "entry_no_trade_before",
                "value": active.get("entry_no_trade_before"),
                "note": "",
            }
        )
    if active.get("entry_no_new_after") not in (None, ""):
        rows.append(
            {
                "stage": "active",
                "restriction_type": "entry_no_new_after",
                "value": active.get("entry_no_new_after"),
                "note": "",
            }
        )
    for value in active.get("entry_blackout_windows") or []:
        rows.append(
            {
                "stage": "active",
                "restriction_type": "entry_blackout_window",
                "value": value,
                "note": "",
            }
        )
    for group_key, windows in policy_group_blackout_windows(active).items():
        for window in windows:
            rows.append(
                {
                    "stage": "active",
                    "restriction_type": "group_blackout_window",
                    "value": f"{group_key}::{window}",
                    "note": "",
                }
            )
    for key in ("pause_ticker_after_losses", "pause_family_after_losses", "pause_after_loss_minutes"):
        if active.get(key) not in (None, ""):
            rows.append(
                {
                    "stage": "active",
                    "restriction_type": key,
                    "value": active.get(key),
                    "note": "",
                }
            )
    for note in active.get("notes") or []:
        rows.append(
            {
                "stage": "active_note",
                "restriction_type": "note",
                "value": "",
                "note": note,
            }
        )
    if proposed.get("candidate_entry_cutoff"):
        rows.append(
            {
                "stage": "proposed",
                "restriction_type": "candidate_entry_cutoff",
                "value": proposed.get("candidate_entry_cutoff"),
                "note": "",
            }
        )
    if proposed.get("candidate_entry_start"):
        rows.append(
            {
                "stage": "proposed",
                "restriction_type": "candidate_entry_start",
                "value": proposed.get("candidate_entry_start"),
                "note": "",
            }
        )
    for value in proposed.get("candidate_entry_blackout_windows") or []:
        rows.append(
            {
                "stage": "proposed",
                "restriction_type": "candidate_entry_blackout_window",
                "value": value,
                "note": "",
            }
        )
    for group_key, windows in normalize_group_blackout_windows(proposed.get("candidate_group_blackout_windows")).items():
        for window in windows:
            rows.append(
                {
                    "stage": "proposed",
                    "restriction_type": "candidate_group_blackout_window",
                    "value": f"{group_key}::{window}",
                    "note": "",
                }
            )
    if proposed.get("candidate_stop_cap_rub") not in (None, ""):
        rows.append(
            {
                "stage": "proposed",
                "restriction_type": "candidate_stop_cap_rub",
                "value": proposed.get("candidate_stop_cap_rub"),
                "note": "",
            }
        )
    best_latest = proposed.get("best_latest_overlay") if isinstance(proposed.get("best_latest_overlay"), dict) else {}
    best_consensus = proposed.get("best_consensus_overlay") if isinstance(proposed.get("best_consensus_overlay"), dict) else {}
    best_latest_group_blackout = proposed.get("best_latest_group_blackout_overlay") if isinstance(proposed.get("best_latest_group_blackout_overlay"), dict) else {}
    best_consensus_group_blackout = proposed.get("best_consensus_group_blackout_overlay") if isinstance(proposed.get("best_consensus_group_blackout_overlay"), dict) else {}
    if best_latest.get("scenario"):
        rows.append(
            {
                "stage": "proposed",
                "restriction_type": "best_latest_overlay",
                "value": best_latest.get("scenario"),
                "note": "",
            }
        )
    if best_consensus.get("scenario"):
        rows.append(
            {
                "stage": "proposed",
                "restriction_type": "best_consensus_overlay",
                "value": best_consensus.get("scenario"),
                "note": "",
            }
        )
    if best_latest_group_blackout.get("scenario"):
        rows.append(
            {
                "stage": "proposed",
                "restriction_type": "best_latest_group_blackout_overlay",
                "value": best_latest_group_blackout.get("scenario"),
                "note": "",
            }
        )
    if best_consensus_group_blackout.get("scenario"):
        rows.append(
            {
                "stage": "proposed",
                "restriction_type": "best_consensus_group_blackout_overlay",
                "value": best_consensus_group_blackout.get("scenario"),
                "note": "",
            }
        )
    for note in proposed.get("notes") or []:
        rows.append(
            {
                "stage": "proposed_note",
                "restriction_type": "note",
                "value": "",
                "note": note,
            }
        )
    return rows


def build_nightly_cycle_status(
    trade_date: str,
    overall: dict,
    research_day: list[dict],
    research_all: list[dict],
    research_consensus: list[dict],
    optimizer_candidates: list[dict],
    strategy_lab: list[dict],
    restriction_rows: list[dict],
    auto_policy: dict,
    email_to: str,
) -> dict:
    active = auto_policy.get("active") if isinstance(auto_policy.get("active"), dict) else {}
    email_settings = smtp_settings(default_recipient=email_to)
    return {
        "trade_date": trade_date,
        "generated_at": now_str(),
        "status": "ok",
        "stages": {
            "analyze": {
                "status": "ok",
                "trades": safe_int(overall.get("trades")),
                "net_rub": safe_float(overall.get("net_rub")),
            },
            "research": {
                "status": "ok",
                "latest_day_scenarios": len(research_day),
                "all_sample_scenarios": len(research_all),
                "consensus_scenarios": len(research_consensus),
            },
            "optimizer": {
                "status": "ok",
                "candidates": len(optimizer_candidates),
                "top_candidate": str(optimizer_candidates[0].get("scenario") or "") if optimizer_candidates else "",
            },
            "strategy_lab": {
                "status": "ok",
                "candidates": len(strategy_lab),
                "top_candidate": str(strategy_lab[0].get("candidate") or "") if strategy_lab else "",
            },
            "restrictions": {
                "status": "ok",
                "active_rule_count": summarize_active_policy(active)["active_rule_count"],
                "rows": len(restriction_rows),
            },
            "summary": {
                "status": "ok",
                "archive_ready": False,
                "archive_path": "",
            },
            "email": {
                "status": "ready" if email_settings["enabled"] else "disabled_missing_smtp",
                "configured": bool(email_settings["enabled"]),
                "recipient": str(email_settings.get("recipient") or email_to),
                "sent": False,
            },
        },
    }


def metrics_map(rows: list[dict], key_fn) -> dict[str, dict]:
    return {str(row.get("group") or ""): row for row in grouped_metrics(rows, key_fn)}


def build_microstructure_summary(rows: list[dict], trade_metrics_by_group: dict[str, dict] | None = None) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        key = str(row.get("group") or "").strip().upper()
        if key:
            groups[key].append(row)
    out: list[dict] = []
    for key, items in groups.items():
        ratios = [safe_float(item.get("spread_to_stop_ratio")) for item in items if item.get("spread_to_stop_ratio") not in (None, "")]
        dominates = sum(1 for item in items if str(item.get("spread_class") or "") == "SPREAD_DOMINATES")
        heavy = sum(1 for item in items if str(item.get("spread_class") or "") == "SPREAD_HEAVY")
        watch = sum(1 for item in items if str(item.get("spread_class") or "") == "SPREAD_WATCH")
        sample = items[0]
        trade_metrics = (trade_metrics_by_group or {}).get(key, {})
        count = len(items)
        out.append(
            {
                "group": key,
                "portfolio_group": str(sample.get("portfolio_group") or "").upper(),
                "contour": "aggressive",
                "family": str(sample.get("family") or "").upper(),
                "spread_events": count,
                "median_spread_ratio": round(median(ratios), 4) if ratios else None,
                "max_spread_ratio": round(max(ratios), 4) if ratios else None,
                "dominates_share_pct": round(dominates / count * 100.0, 2) if count else 0.0,
                "heavy_share_pct": round(heavy / count * 100.0, 2) if count else 0.0,
                "watch_share_pct": round(watch / count * 100.0, 2) if count else 0.0,
                "trades": safe_int(trade_metrics.get("trades")),
                "net_rub": safe_float(trade_metrics.get("net_rub")),
                "expectancy_rub": safe_float(trade_metrics.get("expectancy_rub")),
            }
        )
    out.sort(
        key=lambda row: (
            safe_float(row.get("net_rub")),
            -safe_int(row.get("spread_events")),
            -safe_float(row.get("median_spread_ratio")),
        )
    )
    return out


def scenario_loss_limit(name: str, prefix: str) -> int | None:
    if not name.startswith(prefix):
        return None
    suffix = name.removeprefix(prefix)
    try:
        return int(suffix.split("_", 1)[0])
    except Exception:
        return None


def combo_components(name: str) -> list[str]:
    if not name.startswith("combo_"):
        return []
    return [part for part in name.removeprefix("combo_").split("__") if part]


def scenario_stop_cap(name: str) -> int | None:
    candidates = combo_components(name) if name.startswith("combo_") else [name]
    for candidate in candidates:
        if not candidate.startswith("stop_cap_"):
            continue
        try:
            return int(candidate.removeprefix("stop_cap_").split("_", 1)[0])
        except Exception:
            continue
    return None


def scenario_entry_cutoff(name: str) -> str:
    candidates = combo_components(name) if name.startswith("combo_") else [name]
    for candidate in candidates:
        if candidate.startswith("entry_window_"):
            parts = candidate.removeprefix("entry_window_").split("_", 1)
            if len(parts) == 2:
                return normalize_clock_hhmm(parts[1])
        if not candidate.startswith("no_new_after_"):
            continue
        return normalize_clock_hhmm(candidate.removeprefix("no_new_after_"))
    return ""


def scenario_entry_start(name: str) -> str:
    candidates = combo_components(name) if name.startswith("combo_") else [name]
    for candidate in candidates:
        if candidate.startswith("entry_window_"):
            parts = candidate.removeprefix("entry_window_").split("_", 1)
            if len(parts) == 2:
                return normalize_clock_hhmm(parts[0])
        if not candidate.startswith("no_trade_before_"):
            continue
        return normalize_clock_hhmm(candidate.removeprefix("no_trade_before_"))
    return ""


def scenario_blackout_windows(name: str) -> list[str]:
    candidate_names = combo_components(name) if name.startswith("combo_") else [name]
    out: list[str] = []
    for candidate in candidate_names:
        if not candidate.startswith("blackout_"):
            continue
        parts = candidate.removeprefix("blackout_").split("_", 1)
        if len(parts) != 2:
            continue
        normalized = normalize_blackout_window(f"{parts[0]}-{parts[1]}")
        if normalized:
            out.append(normalized)
    return sorted(set(out))


def scenario_group_blackout_windows(name: str) -> dict[str, list[str]]:
    candidate_names = combo_components(name) if name.startswith("combo_") else [name]
    prefix = "group_blackout_"
    out: dict[str, list[str]] = {}
    for candidate in candidate_names:
        if not candidate.startswith(prefix):
            continue
        payload = candidate.removeprefix(prefix)
        parts = payload.split("__", 2)
        if len(parts) != 3:
            continue
        portfolio_name, contour_name, window_token = parts
        window_parts = window_token.split("_", 1)
        if len(window_parts) != 2:
            continue
        group_key = normalize_group_blackout_slice(f"{portfolio_name}/{contour_name}")
        normalized_window = normalize_blackout_window(f"{window_parts[0]}-{window_parts[1]}")
        if not group_key or not normalized_window:
            continue
        out[group_key] = merge_blackout_windows(out.get(group_key), [normalized_window])
    return {key: out[key] for key in sorted(out)}


def scenario_blacklist_family(name: str) -> str:
    if name.startswith("blacklist_family_"):
        return name.removeprefix("blacklist_family_")
    for candidate in combo_components(name):
        if candidate.startswith("blacklist_family_"):
            return candidate.removeprefix("blacklist_family_")
    return ""


def scenario_blacklist_portfolio(name: str) -> str:
    if name.startswith("blacklist_portfolio_"):
        return name.removeprefix("blacklist_portfolio_")
    for candidate in combo_components(name):
        if candidate.startswith("blacklist_portfolio_"):
            return candidate.removeprefix("blacklist_portfolio_")
    return ""


def scenario_blacklist_group_family(name: str) -> str:
    candidate_names = combo_components(name) if name.startswith("combo_") else [name]
    prefix = "blacklist_group_family_"
    for candidate in candidate_names:
        if not candidate.startswith(prefix):
            continue
        payload = candidate.removeprefix(prefix)
        parts = payload.split("__", 2)
        if len(parts) != 3:
            continue
        portfolio_name, contour_name, family = parts
        return f"{portfolio_name}/{contour_name}::{family}".upper()
    return ""


def scenario_allow_aggressive_group_family(name: str) -> str:
    candidate_names = combo_components(name) if name.startswith("combo_") else [name]
    prefix = "strict_plus_aggressive_group_family_"
    for candidate in candidate_names:
        if not candidate.startswith(prefix):
            continue
        payload = candidate.removeprefix(prefix)
        parts = payload.split("__", 1)
        if len(parts) != 2:
            continue
        portfolio_name, family = parts
        if not portfolio_name or not family:
            continue
        return f"{portfolio_name}/AGGRESSIVE::{family}".upper()
    return ""


def scenario_has_component(name: str, component: str) -> bool:
    if not name or not component:
        return False
    if name == component:
        return True
    return component in combo_components(name)


def concentrated_family_group_key(family: str, by_group_family: dict[str, dict]) -> str:
    family_norm = str(family or "").strip().upper()
    if not family_norm:
        return ""
    family_rows: list[tuple[str, dict]] = []
    for key, row in by_group_family.items():
        if not key.endswith(f"::{family_norm}"):
            continue
        family_rows.append((key, row))
    if len(family_rows) < 2:
        return ""
    negative_rows = [(key, row) for key, row in family_rows if safe_float(row.get("net_rub")) < 0]
    positive_rows = [(key, row) for key, row in family_rows if safe_float(row.get("net_rub")) > 0]
    if not negative_rows or not positive_rows:
        return ""
    total_negative_abs = sum(abs(safe_float(row.get("net_rub"))) for _, row in negative_rows)
    if total_negative_abs < 1_000:
        return ""
    worst_key, worst_row = min(negative_rows, key=lambda item: safe_float(item[1].get("net_rub")))
    worst_abs = abs(safe_float(worst_row.get("net_rub")))
    if worst_abs < max(1_000.0, total_negative_abs * 0.55):
        return ""
    if max(safe_float(row.get("net_rub")) for _, row in positive_rows) <= 0:
        return ""
    return worst_key


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


def split_portfolio_contour_key(value: str) -> tuple[str, str]:
    text = normalize_group_blackout_slice(value)
    if not text:
        return "", ""
    try:
        portfolio_name, contour_name = text.split("/", 1)
    except ValueError:
        return "", ""
    return portfolio_name, contour_name


def group_family_portfolio_contour_key(value: str) -> str:
    portfolio_name, contour_name, _family_name = split_group_family_key(value)
    return normalize_group_blackout_slice(f"{portfolio_name}/{contour_name}")


def build_auto_policy(
    all_rows: list[dict],
    profiles: dict[str, dict],
    trade_date: str,
    day_history: list[dict],
    recurring_tickers: list[dict],
    recurring_families: list[dict],
    microstructure_summary: list[dict],
    research_day: list[dict],
    research_all: list[dict],
    research_consensus: list[dict],
) -> dict:
    history_days = len(day_history)
    by_ticker = metrics_map(all_rows, lambda row: str(row.get("secid") or ""))
    by_family = metrics_map(all_rows, lambda row: family_for_row(row, profiles))
    by_portfolio = metrics_map(all_rows, lambda row: str(row.get("portfolio_group") or ""))
    by_portfolio_contour = metrics_map(
        all_rows,
        lambda row: normalize_group_blackout_slice(f"{str(row.get('portfolio_group') or '')}/{str(row.get('contour') or '')}"),
    )
    by_group_family = metrics_map(
        all_rows,
        lambda row: f"{str(row.get('portfolio_group') or '').upper()}/{str(row.get('contour') or '').upper()}::{family_for_row(row, profiles).upper()}",
    )
    portfolio_contour_families: dict[str, set[str]] = {}
    for group_family_key in by_group_family:
        portfolio_name, contour_name, family_name = split_group_family_key(group_family_key)
        portfolio_contour_key = normalize_group_blackout_slice(f"{portfolio_name}/{contour_name}")
        if portfolio_contour_key and family_name:
            portfolio_contour_families.setdefault(portfolio_contour_key, set()).add(family_name)
    by_ticker_contour = metrics_map(all_rows, lambda row: f"{row.get('secid') or ''}::{row.get('contour') or ''}")
    by_family_contour = metrics_map(all_rows, lambda row: f"{family_for_row(row, profiles)}::{row.get('contour') or ''}")

    active = {
        "observe_only_portfolios": [],
        "observe_only_group_families": [],
        "allow_aggressive_group_families": [],
        "observe_only_tickers": [],
        "observe_only_families": [],
        "strict_only_tickers": [],
        "strict_only_families": [],
        "entry_blackout_windows": [],
        "entry_blackout_group_windows": {},
        "entry_no_trade_before": None,
        "entry_no_new_after": None,
        "entry_max_full_stop_rub": None,
        "pause_ticker_after_losses": None,
        "pause_family_after_losses": None,
        "pause_after_loss_minutes": None,
        "notes": [],
    }

    if history_days >= 2:
        for row in recurring_tickers:
            ticker = str(row.get("group") or "")
            if not ticker:
                continue
            if safe_int(row.get("killer_days")) < 2:
                continue
            if safe_float(row.get("total_bucket_net_rub")) > -2_000:
                continue
            active["observe_only_tickers"].append(ticker)
            active["notes"].append(
                f"{ticker}: повторяющийся killer по дням, переводим в observe-only до накопления новой статистики."
            )

        for row in recurring_families:
            family = str(row.get("group") or "")
            if not family:
                continue
            if safe_int(row.get("killer_days")) < 2:
                continue
            if safe_float(row.get("total_bucket_net_rub")) > -3_000:
                continue
            active["observe_only_families"].append(family)
            active["notes"].append(
                f"{family}: семейство повторно разрушает дни, новые входы временно только в режиме наблюдения."
            )

    for ticker, total_row in by_ticker.items():
        if safe_int(total_row.get("trades")) < 4:
            continue
        aggr = by_ticker_contour.get(f"{ticker}::aggressive")
        strict = by_ticker_contour.get(f"{ticker}::strict")
        if not aggr or safe_int(aggr.get("trades")) < 3:
            continue
        aggr_net = safe_float(aggr.get("net_rub"))
        strict_net = safe_float(strict.get("net_rub")) if strict else 0.0
        if aggr_net <= -1_000 and (not strict or strict_net >= 0 or aggr_net < strict_net - 500):
            active["strict_only_tickers"].append(ticker)
            active["notes"].append(
                f"{ticker}: aggressive слой заметно хуже strict, поэтому aggressive новые входы временно блокируются."
            )

    for family, total_row in by_family.items():
        if safe_int(total_row.get("trades")) < 6:
            continue
        aggr = by_family_contour.get(f"{family}::aggressive")
        strict = by_family_contour.get(f"{family}::strict")
        if not aggr or safe_int(aggr.get("trades")) < 4:
            continue
        aggr_net = safe_float(aggr.get("net_rub"))
        strict_net = safe_float(strict.get("net_rub")) if strict else 0.0
        if aggr_net <= -1_500 and (not strict or strict_net >= 0 or aggr_net < strict_net - 700):
            active["strict_only_families"].append(family)
            active["notes"].append(
                f"{family}: семейство лучше ведёт себя без aggressive-слоя, переводим aggressive в observe-only."
            )

    for row in microstructure_summary:
        group_key = str(row.get("group") or "").upper()
        if not group_key:
            continue
        spread_events = safe_int(row.get("spread_events"))
        median_ratio = safe_float(row.get("median_spread_ratio"))
        dominates_share_pct = safe_float(row.get("dominates_share_pct"))
        trades = safe_int(row.get("trades"))
        net_rub = safe_float(row.get("net_rub"))
        if spread_events < 200:
            continue
        if median_ratio < 0.75 and dominates_share_pct < 50.0:
            continue
        if trades < 1 or net_rub >= 0:
            continue
        active["observe_only_group_families"].append(group_key)
        active["notes"].append(
            f"Микроструктура {group_key}: {spread_events} wide-spread событий, median spread/stop={median_ratio:.2f}, "
            f"dominates={dominates_share_pct:.1f}% и net={net_rub:.2f} ₽. Новые входы переводим в observe-only."
        )

    best_latest_overlay = next((row for row in research_day if row.get("scenario") != "base"), {})
    best_latest_group_family_overlays = [
        row
        for row in research_day
        if scenario_kind(str(row.get("scenario") or "")) == "group_family_blacklist"
    ]
    best_latest_blackout_overlay = next(
        (row for row in research_day if scenario_kind(str(row.get("scenario") or "")) == "entry_blackout"),
        {},
    )
    best_latest_group_blackout_overlay = next(
        (row for row in research_day if scenario_kind(str(row.get("scenario") or "")) == "group_blackout"),
        {},
    )
    best_consensus_overlay = pick_best_consensus_scenario(research_consensus)
    best_consensus_blackout_overlay = next(
        (row for row in research_consensus if scenario_kind(str(row.get("scenario") or "")) == "entry_blackout"),
        {},
    )
    best_consensus_group_blackout_overlay = next(
        (row for row in research_consensus if scenario_kind(str(row.get("scenario") or "")) == "group_blackout"),
        {},
    )
    base_day_overlay = next((row for row in research_day if str(row.get("scenario") or "") == "base"), {})
    base_all_overlay = next((row for row in research_all if str(row.get("scenario") or "") == "base"), {})
    pause_ticker_day_overlay = next((row for row in research_day if str(row.get("scenario") or "") == "pause_ticker_after_1_loss"), {})
    pause_ticker_consensus_overlay = next(
        (row for row in research_consensus if scenario_kind(str(row.get("scenario") or "")) == "ticker_pause_after_losses"),
        {},
    )
    pause_family_consensus_overlay = next(
        (row for row in research_consensus if scenario_kind(str(row.get("scenario") or "")) == "family_pause_after_losses"),
        {},
    )
    best_all_combo_overlay = next(
        (row for row in research_all if scenario_kind(str(row.get("scenario") or "")) == "combo_overlay"),
        {},
    )
    research_all_by_scenario = {str(row.get("scenario") or ""): row for row in research_all}
    research_day_by_scenario = {str(row.get("scenario") or ""): row for row in research_day}

    proposed = {
        "best_latest_overlay": best_latest_overlay,
        "best_consensus_overlay": best_consensus_overlay,
        "best_latest_group_blackout_overlay": best_latest_group_blackout_overlay,
        "best_consensus_group_blackout_overlay": best_consensus_group_blackout_overlay,
        "candidate_entry_start": "",
        "candidate_entry_cutoff": "",
        "candidate_entry_blackout_windows": [],
        "candidate_group_blackout_windows": {},
        "candidate_stop_cap_rub": None,
        "notes": [],
    }
    for candidate in [
        best_consensus_overlay,
        best_latest_overlay,
        best_consensus_blackout_overlay,
        best_latest_blackout_overlay,
        best_consensus_group_blackout_overlay,
        best_latest_group_blackout_overlay,
    ]:
        scenario = str(candidate.get("scenario") or "")
        candidate_entry_start = scenario_entry_start(scenario)
        if candidate_entry_start and not proposed["candidate_entry_start"]:
            proposed["candidate_entry_start"] = candidate_entry_start
            proposed["notes"].append(
                f"Сценарий {scenario} дал лучший результат в исследовательском слое: это кандидат на более поздний старт {candidate_entry_start}, но пока не активируется автоматически."
            )
        candidate_entry_cutoff = scenario_entry_cutoff(scenario)
        if candidate_entry_cutoff and not proposed["candidate_entry_cutoff"]:
            proposed["candidate_entry_cutoff"] = candidate_entry_cutoff
            proposed["notes"].append(
                f"Сценарий {scenario} дал лучший результат в исследовательском слое: это кандидат на ранний cutoff {candidate_entry_cutoff}, но пока не активируется автоматически."
            )
        candidate_blackout_windows = scenario_blackout_windows(scenario)
        if candidate_blackout_windows and not proposed["candidate_entry_blackout_windows"]:
            proposed["candidate_entry_blackout_windows"] = candidate_blackout_windows
            proposed["notes"].append(
                f"Сценарий {scenario} дал лучший результат в исследовательском слое: это кандидат на blackout новых входов {', '.join(candidate_blackout_windows)}."
            )
        candidate_group_blackout_windows = scenario_group_blackout_windows(scenario)
        if candidate_group_blackout_windows:
            merged_group_blackout_windows = merge_group_blackout_windows(
                proposed.get("candidate_group_blackout_windows"),
                candidate_group_blackout_windows,
            )
            if merged_group_blackout_windows != normalize_group_blackout_windows(proposed.get("candidate_group_blackout_windows")):
                proposed["candidate_group_blackout_windows"] = merged_group_blackout_windows
                proposed["notes"].append(
                    f"Сценарий {scenario} дал лучший результат в исследовательском слое: "
                    f"это кандидат на точечный blackout {format_group_blackout_windows(candidate_group_blackout_windows, empty='-')}."
                )
        if scenario.startswith("stop_cap_") and proposed["candidate_stop_cap_rub"] is None:
            try:
                proposed["candidate_stop_cap_rub"] = int(scenario.removeprefix("stop_cap_"))
            except Exception:
                proposed["candidate_stop_cap_rub"] = None
            if proposed["candidate_stop_cap_rub"] is not None:
                proposed["notes"].append(
                    f"Сценарий {scenario} выглядит сильнее base: это кандидат на следующий тест лимита полного стопа."
                )

    consensus_scenario = str(best_consensus_overlay.get("scenario") or "")
    consensus_days = safe_int(best_consensus_overlay.get("days"))
    consensus_beats = safe_int(best_consensus_overlay.get("beat_base_days"))
    consensus_delta = safe_float(best_consensus_overlay.get("delta_total_rub"))
    consensus_latest_delta = safe_float(best_consensus_overlay.get("latest_day_delta_rub"))
    candidate_stop_cap = proposed.get("candidate_stop_cap_rub")
    if (
        consensus_scenario.startswith("stop_cap_")
        and candidate_stop_cap not in (None, "")
        and consensus_days >= 2
        and consensus_beats >= consensus_days
        and consensus_delta >= 1_000
    ):
        active["entry_max_full_stop_rub"] = int(candidate_stop_cap)
        active["notes"].append(
            f"Авто-тюнинг: лимит полного стопа новых входов снижен до {candidate_stop_cap} ₽, "
            f"потому что {consensus_scenario} улучшил все {consensus_days} последних дня."
        )

    consensus_entry_cutoff = scenario_entry_cutoff(consensus_scenario)
    consensus_entry_start = scenario_entry_start(consensus_scenario)
    consensus_blackout_windows = scenario_blackout_windows(consensus_scenario)
    blackout_consensus_scenario = str(best_consensus_blackout_overlay.get("scenario") or "")
    blackout_consensus_days = safe_int(best_consensus_blackout_overlay.get("days"))
    blackout_consensus_beats = safe_int(best_consensus_blackout_overlay.get("beat_base_days"))
    blackout_consensus_delta = safe_float(best_consensus_blackout_overlay.get("delta_total_rub"))
    blackout_consensus_latest_delta = safe_float(best_consensus_blackout_overlay.get("latest_day_delta_rub"))
    blackout_consensus_windows = scenario_blackout_windows(blackout_consensus_scenario)
    group_blackout_consensus_scenario = str(best_consensus_group_blackout_overlay.get("scenario") or "")
    group_blackout_consensus_days = safe_int(best_consensus_group_blackout_overlay.get("days"))
    group_blackout_consensus_beats = safe_int(best_consensus_group_blackout_overlay.get("beat_base_days"))
    group_blackout_consensus_delta = safe_float(best_consensus_group_blackout_overlay.get("delta_total_rub"))
    group_blackout_consensus_latest_delta = safe_float(best_consensus_group_blackout_overlay.get("latest_day_delta_rub"))
    group_blackout_consensus_windows = scenario_group_blackout_windows(group_blackout_consensus_scenario)
    group_blackout_consensus_all_overlay = research_all_by_scenario.get(group_blackout_consensus_scenario) if group_blackout_consensus_scenario else {}
    group_blackout_consensus_all_trades = safe_int(group_blackout_consensus_all_overlay.get("trades")) if isinstance(group_blackout_consensus_all_overlay, dict) else 0
    group_blackout_consensus_all_net = safe_float(group_blackout_consensus_all_overlay.get("net_rub")) if isinstance(group_blackout_consensus_all_overlay, dict) else 0.0
    group_blackout_consensus_decisive = (
        group_blackout_consensus_days >= 1
        and group_blackout_consensus_delta >= 3_000
        and group_blackout_consensus_latest_delta >= 1_500
        and group_blackout_consensus_all_trades >= 4
        and group_blackout_consensus_all_net > 0
    )
    activated_group_blackout_scenarios: list[str] = []
    if (
        consensus_entry_start
        and consensus_days >= 2
        and consensus_beats >= consensus_days
        and consensus_delta >= 1_000
        and consensus_latest_delta >= 500
    ):
        active["entry_no_trade_before"] = later_clock_hhmm(active.get("entry_no_trade_before"), consensus_entry_start)
        active["notes"].append(
            f"Авто-policy: старт новых входов сдвинут до {consensus_entry_start}, "
            f"потому что {consensus_scenario} устойчиво улучшает результат против base."
        )
    if (
        consensus_entry_cutoff
        and consensus_days >= 2
        and consensus_beats >= consensus_days
        and consensus_delta >= 1_000
        and consensus_latest_delta >= 500
    ):
        active["entry_no_new_after"] = earlier_clock_hhmm(active.get("entry_no_new_after"), consensus_entry_cutoff)
        active["notes"].append(
            f"Авто-policy: окно новых входов ужесточено до {consensus_entry_cutoff}, "
            f"потому что {consensus_scenario} устойчиво улучшает результат против base."
        )
    if (
        consensus_blackout_windows
        and consensus_days >= 2
        and consensus_beats >= consensus_days
        and consensus_delta >= 1_000
        and consensus_latest_delta >= 500
    ):
        active["entry_blackout_windows"] = merge_blackout_windows(active.get("entry_blackout_windows"), consensus_blackout_windows)
        active["notes"].append(
            f"Авто-policy: новые входы блокируются в окне {', '.join(consensus_blackout_windows)}, "
            f"потому что {consensus_scenario} устойчиво улучшает результат против base."
        )
    if (
        blackout_consensus_scenario
        and blackout_consensus_scenario != consensus_scenario
        and blackout_consensus_windows
        and blackout_consensus_days >= 2
        and blackout_consensus_beats >= blackout_consensus_days
        and blackout_consensus_delta >= 1_000
        and blackout_consensus_latest_delta >= 500
    ):
        active["entry_blackout_windows"] = merge_blackout_windows(active.get("entry_blackout_windows"), blackout_consensus_windows)
        active["notes"].append(
            f"Авто-policy: новые входы блокируются в окне {', '.join(blackout_consensus_windows)}, "
            f"потому что лучший blackout-сценарий {blackout_consensus_scenario} устойчиво улучшает результат против base."
        )
    if group_blackout_consensus_windows:
        group_blackout_consensus_key = next(iter(group_blackout_consensus_windows))
        group_blackout_consensus_total = by_portfolio_contour.get(group_blackout_consensus_key) or {}
        group_blackout_consensus_trades = safe_int(group_blackout_consensus_total.get("trades"))
        group_blackout_consensus_net = safe_float(group_blackout_consensus_total.get("net_rub"))
        if (
            group_blackout_consensus_days >= 2
            and group_blackout_consensus_beats >= max(1, group_blackout_consensus_days - 1)
            and group_blackout_consensus_delta >= 1_000
            and group_blackout_consensus_latest_delta >= 500
            and group_blackout_consensus_trades >= 2
            and group_blackout_consensus_net < 0
        ) or (
            group_blackout_consensus_decisive
            and group_blackout_consensus_net <= -2_500
        ):
            active["entry_blackout_group_windows"] = merge_group_blackout_windows(
                policy_group_blackout_windows(active),
                group_blackout_consensus_windows,
            )
            activated_group_blackout_scenarios.append(group_blackout_consensus_scenario)
            active["notes"].append(
                f"Авто-policy: для {group_blackout_consensus_key} новые входы блокируются в окне "
                f"{', '.join(group_blackout_consensus_windows[group_blackout_consensus_key])}, "
                f"потому что {group_blackout_consensus_scenario} устойчиво улучшает результат против base."
            )

    latest_scenario = str(best_latest_overlay.get("scenario") or "")
    base_day_net = safe_float(base_day_overlay.get("net_rub"))
    latest_blackout_scenario = str(best_latest_blackout_overlay.get("scenario") or "")
    latest_blackout_windows = scenario_blackout_windows(latest_blackout_scenario)
    latest_blackout_all_overlay = research_all_by_scenario.get(latest_blackout_scenario) if latest_blackout_scenario else {}
    latest_blackout_all_net = safe_float(latest_blackout_all_overlay.get("net_rub")) if isinstance(latest_blackout_all_overlay, dict) else 0.0
    latest_blackout_all_trades = safe_int(latest_blackout_all_overlay.get("trades")) if isinstance(latest_blackout_all_overlay, dict) else 0
    latest_blackout_day_net = safe_float(best_latest_blackout_overlay.get("net_rub"))
    latest_blackout_delta = latest_blackout_day_net - base_day_net
    if (
        latest_blackout_windows
        and base_day_net < 0
        and latest_blackout_day_net > 0
        and latest_blackout_delta >= 2_500
        and latest_blackout_all_net > 0
        and latest_blackout_all_trades >= 5
    ):
        active["entry_blackout_windows"] = merge_blackout_windows(active.get("entry_blackout_windows"), latest_blackout_windows)
        active["notes"].append(
            f"Авто-policy: новые входы блокируются в окне {', '.join(latest_blackout_windows)}, "
            f"потому что {latest_blackout_scenario} переворачивает текущий день из минуса в плюс и остаётся положительным по общей выборке."
        )
    latest_group_blackout_scenario = str(best_latest_group_blackout_overlay.get("scenario") or "")
    latest_group_blackout_windows = scenario_group_blackout_windows(latest_group_blackout_scenario)
    latest_group_blackout_all_overlay = research_all_by_scenario.get(latest_group_blackout_scenario) if latest_group_blackout_scenario else {}
    latest_group_blackout_all_trades = safe_int(latest_group_blackout_all_overlay.get("trades")) if isinstance(latest_group_blackout_all_overlay, dict) else 0
    latest_group_blackout_all_net = safe_float(latest_group_blackout_all_overlay.get("net_rub")) if isinstance(latest_group_blackout_all_overlay, dict) else 0.0
    if latest_group_blackout_windows:
        latest_group_blackout_key = next(iter(latest_group_blackout_windows))
        latest_group_blackout_total = by_portfolio_contour.get(latest_group_blackout_key) or {}
        latest_group_blackout_trades = safe_int(latest_group_blackout_total.get("trades"))
        latest_group_blackout_net = safe_float(latest_group_blackout_total.get("net_rub"))
        latest_group_blackout_delta = safe_float(best_latest_group_blackout_overlay.get("net_rub")) - base_day_net
        if (
            latest_group_blackout_trades >= 2
            and latest_group_blackout_net <= -1_500
            and latest_group_blackout_delta >= 1_500
        ) or (
            latest_group_blackout_trades >= 1
            and latest_group_blackout_net <= -2_500
            and latest_group_blackout_delta >= 2_500
            and latest_group_blackout_all_trades >= 4
            and latest_group_blackout_all_net > 0
        ):
            active["entry_blackout_group_windows"] = merge_group_blackout_windows(
                policy_group_blackout_windows(active),
                latest_group_blackout_windows,
            )
            activated_group_blackout_scenarios.append(latest_group_blackout_scenario)
            active["notes"].append(
                f"Авто-policy: для {latest_group_blackout_key} новые входы блокируются в окне "
                f"{', '.join(latest_group_blackout_windows[latest_group_blackout_key])}, "
                f"потому что {latest_group_blackout_scenario} резко улучшает текущий день против base."
            )
    for row in best_latest_group_family_overlays[:2]:
        group_family_key = scenario_blacklist_group_family(str(row.get("scenario") or ""))
        group_family_total = by_group_family.get(group_family_key) or {}
        latest_group_delta = safe_float(row.get("net_rub")) - base_day_net
        if safe_float(group_family_total.get("net_rub")) <= -1_000 and latest_group_delta >= 1_500:
            active["observe_only_group_families"].append(group_family_key)
            active["notes"].append(
                f"Авто-policy: связка {group_family_key} переведена в observe-only, "
                f"потому что {row.get('scenario')} резко улучшает текущий день против base."
            )
    latest_blacklist_group_family = scenario_blacklist_group_family(latest_scenario)
    if latest_blacklist_group_family:
        group_family_total = by_group_family.get(latest_blacklist_group_family) or {}
        latest_group_delta = safe_float(best_latest_overlay.get("net_rub")) - base_day_net
        if safe_float(group_family_total.get("net_rub")) <= -1_000 and latest_group_delta >= 1_500:
            active["observe_only_group_families"].append(latest_blacklist_group_family)
            active["notes"].append(
                f"Авто-policy: связка {latest_blacklist_group_family} переведена в observe-only, "
                f"потому что {latest_scenario} резко улучшает текущий день против base."
            )
    latest_blacklist_portfolio = scenario_blacklist_portfolio(latest_scenario)
    if latest_blacklist_portfolio:
        portfolio_total = by_portfolio.get(latest_blacklist_portfolio) or {}
        portfolio_trades = safe_int(portfolio_total.get("trades"))
        portfolio_net = safe_float(portfolio_total.get("net_rub"))
        latest_portfolio_delta = safe_float(best_latest_overlay.get("net_rub")) - safe_float(base_day_overlay.get("net_rub"))
        if portfolio_trades >= 1 and portfolio_net <= -2_000 and latest_portfolio_delta >= 2_500:
            active["observe_only_portfolios"].append(latest_blacklist_portfolio)
            active["notes"].append(
                f"Авто-policy: контур {latest_blacklist_portfolio} переведён в observe-only, "
                f"потому что {latest_scenario} резко улучшает текущий день против base."
            )
    pause_ticker_day_delta = safe_float(pause_ticker_day_overlay.get("net_rub")) - safe_float(base_day_overlay.get("net_rub"))
    if (
        pause_ticker_day_overlay
        and safe_int(pause_ticker_day_overlay.get("skipped_trades")) >= 1
        and pause_ticker_day_delta >= 800
    ):
        active["pause_ticker_after_losses"] = 1
        active["pause_after_loss_minutes"] = max(int(active.get("pause_after_loss_minutes") or 0), 120)
        active["notes"].append(
            "Авто-policy: после первого убыточного закрытия тикер ставится на паузу на 120 минут, "
            "потому что это заметно улучшило последний день и убрало повторный вход в тот же убыточный тикер."
        )
    pause_ticker_consensus_scenario = str(pause_ticker_consensus_overlay.get("scenario") or "")
    pause_ticker_consensus_limit = scenario_loss_limit(pause_ticker_consensus_scenario, "pause_ticker_after_")
    pause_ticker_consensus_days = safe_int(pause_ticker_consensus_overlay.get("days"))
    pause_ticker_consensus_beats = safe_int(pause_ticker_consensus_overlay.get("beat_base_days"))
    pause_ticker_consensus_delta = safe_float(pause_ticker_consensus_overlay.get("delta_total_rub"))
    pause_ticker_consensus_latest_delta = safe_float(pause_ticker_consensus_overlay.get("latest_day_delta_rub"))
    current_pause_ticker_limit = active.get("pause_ticker_after_losses")
    if (
        pause_ticker_consensus_limit is not None
        and pause_ticker_consensus_days >= 2
        and pause_ticker_consensus_beats >= pause_ticker_consensus_days
        and pause_ticker_consensus_delta >= 1_500
        and pause_ticker_consensus_latest_delta >= 0
        and (
            current_pause_ticker_limit in (None, "")
            or safe_int(current_pause_ticker_limit, 99) > pause_ticker_consensus_limit
        )
    ):
        active["pause_ticker_after_losses"] = pause_ticker_consensus_limit
        active["pause_after_loss_minutes"] = max(int(active.get("pause_after_loss_minutes") or 0), 120)
        active["notes"].append(
            f"Авто-policy: тикер ставится на паузу после {pause_ticker_consensus_limit} убыточн. закрыт. на 120 минут, "
            f"потому что {pause_ticker_consensus_scenario} устойчиво улучшает все последние дни против base."
        )
    pause_family_consensus_scenario = str(pause_family_consensus_overlay.get("scenario") or "")
    pause_family_consensus_limit = scenario_loss_limit(pause_family_consensus_scenario, "pause_family_after_")
    pause_family_consensus_days = safe_int(pause_family_consensus_overlay.get("days"))
    pause_family_consensus_beats = safe_int(pause_family_consensus_overlay.get("beat_base_days"))
    pause_family_consensus_delta = safe_float(pause_family_consensus_overlay.get("delta_total_rub"))
    pause_family_consensus_latest_delta = safe_float(pause_family_consensus_overlay.get("latest_day_delta_rub"))
    current_pause_family_limit = active.get("pause_family_after_losses")
    if (
        pause_family_consensus_limit is not None
        and pause_family_consensus_days >= 2
        and pause_family_consensus_beats >= pause_family_consensus_days
        and pause_family_consensus_delta >= 1_200
        and pause_family_consensus_latest_delta >= 0
        and (
            current_pause_family_limit in (None, "")
            or safe_int(current_pause_family_limit, 99) > pause_family_consensus_limit
        )
    ):
        active["pause_family_after_losses"] = pause_family_consensus_limit
        active["pause_after_loss_minutes"] = max(int(active.get("pause_after_loss_minutes") or 0), 120)
        active["notes"].append(
            f"Авто-policy: семейство ставится на паузу после {pause_family_consensus_limit} убыточн. закрыт. на 120 минут, "
            f"потому что {pause_family_consensus_scenario} устойчиво улучшает все последние дни против base."
        )

    strict_consensus_overlay = next((row for row in research_consensus if str(row.get("scenario") or "") == "contour_only_strict"), {})
    strict_days = safe_int(strict_consensus_overlay.get("days"))
    strict_total_net = safe_float(strict_consensus_overlay.get("total_net_rub"))
    strict_delta = safe_float(strict_consensus_overlay.get("delta_total_rub"))
    strict_latest_delta = safe_float(strict_consensus_overlay.get("latest_day_delta_rub"))
    strict_latest_net = safe_float(strict_consensus_overlay.get("latest_day_rub"))
    futures_families = sorted(
        family
        for family in by_family
        if family and "PERPA" not in str(family).upper()
    )
    best_active_group_blackout_all_net = 0.0
    best_active_group_blackout_day_net = 0.0
    if activated_group_blackout_scenarios:
        best_active_group_blackout_all_net = max(
            safe_float((research_all_by_scenario.get(scenario) or {}).get("net_rub"))
            for scenario in activated_group_blackout_scenarios
        )
        best_active_group_blackout_day_net = max(
            safe_float((research_day_by_scenario.get(scenario) or {}).get("net_rub"))
            for scenario in activated_group_blackout_scenarios
        )
    group_blackout_beats_blanket_strict = (
        bool(activated_group_blackout_scenarios)
        and best_active_group_blackout_all_net >= strict_total_net
        and best_active_group_blackout_day_net >= strict_latest_net
    )
    if (
        futures_families
        and strict_days >= 2
        and strict_total_net >= 0
        and strict_delta >= 2_000
        and strict_latest_delta >= 1_000
        and latest_scenario == "contour_only_strict"
        and not group_blackout_beats_blanket_strict
    ):
        active["strict_only_families"].extend(futures_families)
        active["notes"].append(
            "Авто-policy: все фьючерсные семьи переведены в strict-only для новых входов, "
            "потому что contour_only_strict дал сильный прирост на последнем дне и не уходит в минус по накопленной серии."
        )
    elif futures_families and strict_days >= 2 and group_blackout_beats_blanket_strict:
        active["notes"].append(
            "Авто-policy: blanket strict-only не активируется, потому что адресный group blackout даёт не хуже результат и сохраняет больше торгового потока."
        )

    consensus_blacklist_family = scenario_blacklist_family(consensus_scenario)
    if consensus_blacklist_family:
        family_total = by_family.get(consensus_blacklist_family) or {}
        family_trades = safe_int(family_total.get("trades"))
        family_net = safe_float(family_total.get("net_rub"))
        concentrated_group_key = concentrated_family_group_key(consensus_blacklist_family, by_group_family)
        latest_delta = safe_float(best_consensus_overlay.get("latest_day_delta_rub"))
        robust_consensus = consensus_days >= 2 and consensus_beats >= consensus_days and consensus_delta >= 1_500
        strong_latest_confirmation = (
            latest_scenario == consensus_scenario
            and latest_delta >= 2_000
        )
        if family_trades >= 3 and family_net < 0 and (robust_consensus or strong_latest_confirmation):
            if concentrated_group_key:
                active["observe_only_group_families"].append(concentrated_group_key)
                active["notes"].append(
                    f"Авто-policy: широкая блокировка семьи {consensus_blacklist_family} заменена на узкий quarantine {concentrated_group_key}, "
                    f"потому что урон семьи сосредоточен в одном срезе."
                )
            else:
                active["observe_only_families"].append(consensus_blacklist_family)
                active["notes"].append(
                    f"Авто-policy: семейство {consensus_blacklist_family} переведено в observe-only, "
                    f"потому что {consensus_scenario} устойчиво улучшает результат против base."
                )

    consensus_blacklist_portfolio = scenario_blacklist_portfolio(consensus_scenario)
    if consensus_blacklist_portfolio:
        portfolio_total = by_portfolio.get(consensus_blacklist_portfolio) or {}
        portfolio_trades = safe_int(portfolio_total.get("trades"))
        portfolio_net = safe_float(portfolio_total.get("net_rub"))
        latest_delta = safe_float(best_consensus_overlay.get("latest_day_delta_rub"))
        robust_portfolio_consensus = (
            consensus_days >= 2
            and consensus_beats >= max(1, consensus_days - 1)
            and consensus_delta >= 2_000
        )
        strong_portfolio_latest = latest_delta >= 1_500
        if portfolio_trades >= 1 and portfolio_net <= -2_000 and (robust_portfolio_consensus or strong_portfolio_latest):
            active["observe_only_portfolios"].append(consensus_blacklist_portfolio)
            active["notes"].append(
                f"Авто-policy: контур {consensus_blacklist_portfolio} переведён в observe-only, "
                f"потому что {consensus_scenario} заметно улучшает результат против base."
            )
    consensus_blacklist_group_family = scenario_blacklist_group_family(consensus_scenario)
    if consensus_blacklist_group_family:
        group_family_total = by_group_family.get(consensus_blacklist_group_family) or {}
        latest_delta = safe_float(best_consensus_overlay.get("latest_day_delta_rub"))
        robust_group_consensus = (
            consensus_days >= 2
            and consensus_beats >= max(1, consensus_days - 1)
            and consensus_delta >= 1_500
            and latest_delta >= 1_000
        )
        if safe_float(group_family_total.get("net_rub")) <= -1_000 and robust_group_consensus:
            active["observe_only_group_families"].append(consensus_blacklist_group_family)
            active["notes"].append(
                f"Авто-policy: связка {consensus_blacklist_group_family} переведена в observe-only, "
                f"потому что {consensus_scenario} устойчиво улучшает результат против base."
            )

    best_combo_scenario = str(best_all_combo_overlay.get("scenario") or "")
    combo_blacklist_family = scenario_blacklist_family(best_combo_scenario)
    combo_blacklist_portfolio = scenario_blacklist_portfolio(best_combo_scenario)
    combo_blacklist_group_family = scenario_blacklist_group_family(best_combo_scenario)
    combo_entry_start = scenario_entry_start(best_combo_scenario)
    combo_entry_cutoff = scenario_entry_cutoff(best_combo_scenario)
    combo_blackout_windows = scenario_blackout_windows(best_combo_scenario)
    combo_stop_cap = scenario_stop_cap(best_combo_scenario)
    combo_has_strict = scenario_has_component(best_combo_scenario, "contour_only_strict")
    best_day_same_combo = next((row for row in research_day if str(row.get("scenario") or "") == best_combo_scenario), {})
    all_sample_combo_delta = safe_float(best_all_combo_overlay.get("net_rub")) - safe_float(base_all_overlay.get("net_rub"))
    latest_combo_delta = safe_float(best_day_same_combo.get("net_rub")) - safe_float(base_day_overlay.get("net_rub"))
    latest_combo_net = safe_float(best_day_same_combo.get("net_rub"))
    combo_trades = safe_int(best_all_combo_overlay.get("trades"))
    combo_net = safe_float(best_all_combo_overlay.get("net_rub"))
    active_stop_cap = int(active.get("entry_max_full_stop_rub") or 0) if active.get("entry_max_full_stop_rub") not in (None, "") else 0
    combo_cap_matches_active = bool(combo_stop_cap and active_stop_cap and combo_stop_cap == active_stop_cap)
    robust_positive_combo = (
        combo_trades >= 5
        and combo_net > 0
        and latest_combo_net > 0
        and all_sample_combo_delta >= 1_500
        and latest_combo_delta >= 700
    )
    group_blackout_beats_strict_combo = (
        bool(activated_group_blackout_scenarios)
        and combo_has_strict
        and best_active_group_blackout_all_net >= combo_net
        and best_active_group_blackout_day_net >= latest_combo_net
    )
    if combo_entry_start and combo_cap_matches_active and robust_positive_combo:
        active["entry_no_trade_before"] = later_clock_hhmm(active.get("entry_no_trade_before"), combo_entry_start)
        active["notes"].append(
            f"Авто-policy: старт новых входов сдвинут до {combo_entry_start}, "
            f"потому что strongest combo {best_combo_scenario} лучше base и по последнему дню, и по короткой серии."
        )
    if combo_entry_cutoff and combo_cap_matches_active and robust_positive_combo:
        active["entry_no_new_after"] = earlier_clock_hhmm(active.get("entry_no_new_after"), combo_entry_cutoff)
        active["notes"].append(
            f"Авто-policy: окно новых входов ужесточено до {combo_entry_cutoff}, "
            f"потому что strongest combo {best_combo_scenario} лучше base и по последнему дню, и по короткой серии."
        )
    if combo_blackout_windows and combo_cap_matches_active and robust_positive_combo:
        active["entry_blackout_windows"] = merge_blackout_windows(active.get("entry_blackout_windows"), combo_blackout_windows)
        active["notes"].append(
            f"Авто-policy: новые входы блокируются в окне {', '.join(combo_blackout_windows)}, "
            f"потому что strongest combo {best_combo_scenario} лучше base и по последнему дню, и по короткой серии."
        )
    if combo_blacklist_family and combo_cap_matches_active:
        family_total = by_family.get(combo_blacklist_family) or {}
        family_trades = safe_int(family_total.get("trades"))
        family_net = safe_float(family_total.get("net_rub"))
        concentrated_group_key = concentrated_family_group_key(combo_blacklist_family, by_group_family)
        robust_combo = (
            combo_trades >= 6
            and all_sample_combo_delta >= 2_500
            and latest_combo_delta >= 1_500
        )
        if family_trades >= 3 and family_net < 0 and robust_combo:
            if concentrated_group_key:
                active["observe_only_group_families"].append(concentrated_group_key)
                active["notes"].append(
                    f"Авто-policy: широкая блокировка семьи {combo_blacklist_family} заменена на узкий quarantine {concentrated_group_key}, "
                    f"потому что strongest combo показывает локальную, а не общесемейную проблему."
                )
            else:
                active["observe_only_families"].append(combo_blacklist_family)
                active["notes"].append(
                    f"Авто-policy: семейство {combo_blacklist_family} переведено в observe-only, "
                    f"потому что strongest combo {best_combo_scenario} резко улучшает и последний день, и всю короткую выборку."
                )
    if combo_blacklist_portfolio and combo_cap_matches_active:
        portfolio_total = by_portfolio.get(combo_blacklist_portfolio) or {}
        portfolio_trades = safe_int(portfolio_total.get("trades"))
        portfolio_net = safe_float(portfolio_total.get("net_rub"))
        robust_portfolio_combo = (
            combo_trades >= 4
            and portfolio_trades >= 1
            and portfolio_net <= -2_000
            and all_sample_combo_delta >= 2_000
            and latest_combo_delta >= 1_500
        )
        if robust_portfolio_combo:
            active["observe_only_portfolios"].append(combo_blacklist_portfolio)
            active["notes"].append(
                f"Авто-policy: контур {combo_blacklist_portfolio} переведён в observe-only, "
                f"потому что strongest combo {best_combo_scenario} убирает разрушительный слой и улучшает короткую серию."
            )
    if combo_blacklist_group_family and combo_cap_matches_active:
        group_family_total = by_group_family.get(combo_blacklist_group_family) or {}
        robust_group_combo = (
            combo_trades >= 4
            and safe_float(group_family_total.get("net_rub")) <= -1_000
            and all_sample_combo_delta >= 1_500
            and latest_combo_delta >= 1_000
        )
        if robust_group_combo:
            active["observe_only_group_families"].append(combo_blacklist_group_family)
            active["notes"].append(
                f"Авто-policy: связка {combo_blacklist_group_family} переведена в observe-only, "
                f"потому что strongest combo {best_combo_scenario} убирает разрушительную под-логику и улучшает короткую серию."
            )
    if combo_has_strict and combo_cap_matches_active:
        robust_strict_combo = (
            combo_trades >= 5
            and combo_net > 0
            and latest_combo_net > 0
            and all_sample_combo_delta >= 2_000
            and latest_combo_delta >= 1_000
        )
        if robust_strict_combo and not group_blackout_beats_strict_combo:
            active["strict_only_families"].extend(futures_families)
            active["notes"].append(
                f"Авто-policy: все фьючерсные семьи переведены в strict-only для новых входов, "
                f"потому что strongest combo {best_combo_scenario} уже даёт положительный результат на общей и последней выборке."
            )
        elif group_blackout_beats_strict_combo or group_blackout_beats_blanket_strict:
            active["notes"].append(
                f"Авто-policy: strongest combo {best_combo_scenario} не переводит все семьи в strict-only, "
                "потому что адресный group blackout уже перекрывает основной ущерб мягче."
            )

    allowed_aggressive_by_group: dict[str, dict] = {}
    for scenario_name, scenario_row in research_all_by_scenario.items():
        group_key = scenario_allow_aggressive_group_family(scenario_name)
        if not group_key:
            continue
        portfolio_name, contour_name, family = split_group_family_key(group_key)
        if contour_name != "AGGRESSIVE" or not portfolio_name or not family:
            continue
        if family not in set(active.get("strict_only_families") or []):
            continue
        day_row = research_day_by_scenario.get(scenario_name)
        if not isinstance(day_row, dict):
            continue
        stop_cap = scenario_stop_cap(scenario_name)
        benchmark_name = f"combo_stop_cap_{stop_cap}__contour_only_strict" if stop_cap is not None else "contour_only_strict"
        benchmark_all = research_all_by_scenario.get(benchmark_name)
        benchmark_day = research_day_by_scenario.get(benchmark_name)
        if not isinstance(benchmark_all, dict) or not isinstance(benchmark_day, dict):
            continue
        group_total = by_group_family.get(group_key) or {}
        group_trades = safe_int(group_total.get("trades"))
        group_net = safe_float(group_total.get("net_rub"))
        candidate_all_net = safe_float(scenario_row.get("net_rub"))
        candidate_day_net = safe_float(day_row.get("net_rub"))
        strict_all_net = safe_float(benchmark_all.get("net_rub"))
        strict_day_net = safe_float(benchmark_day.get("net_rub"))
        delta_all_vs_strict = candidate_all_net - strict_all_net
        delta_day_vs_strict = candidate_day_net - strict_day_net
        if (
            group_trades < 2
            or group_net <= 0
            or candidate_all_net <= 0
            or delta_all_vs_strict < 300
            or delta_day_vs_strict < 0
        ):
            continue
        score = (delta_all_vs_strict, delta_day_vs_strict, group_net, candidate_all_net)
        prev = allowed_aggressive_by_group.get(group_key)
        if prev is None or score > prev["score"]:
            allowed_aggressive_by_group[group_key] = {
                "group_key": group_key,
                "scenario": scenario_name,
                "delta_all_vs_strict": round(delta_all_vs_strict, 2),
                "delta_day_vs_strict": round(delta_day_vs_strict, 2),
                "group_net": round(group_net, 2),
                "score": score,
            }

    for item in sorted(allowed_aggressive_by_group.values(), key=lambda row: row["score"], reverse=True)[:4]:
        active["allow_aggressive_group_families"].append(item["group_key"])
        active["notes"].append(
            f"Авто-policy: разрешаем aggressive для {item['group_key']} поверх broad strict-only, "
            f"потому что {item['scenario']} лучше strict-only на серии (+{item['delta_all_vs_strict']:.2f} ₽) "
            f"и на последнем дне (+{item['delta_day_vs_strict']:.2f} ₽)."
        )

    pause_ticker_limit = scenario_loss_limit(consensus_scenario, "pause_ticker_after_")
    if (
        pause_ticker_limit is not None
        and consensus_days >= 2
        and consensus_delta >= 1_000
        and consensus_beats >= 1
        and safe_float(best_consensus_overlay.get("latest_day_delta_rub")) >= 0
    ):
        active["pause_ticker_after_losses"] = pause_ticker_limit
        active["pause_after_loss_minutes"] = max(int(active.get("pause_after_loss_minutes") or 0), 120)
        active["notes"].append(
            f"Авто-policy: тикер ставится на паузу после {pause_ticker_limit} убыточн. закрыт., "
            f"потому что {consensus_scenario} улучшает выборку против base."
        )

    pause_family_limit = scenario_loss_limit(consensus_scenario, "pause_family_after_")
    if (
        pause_family_limit is not None
        and consensus_days >= 2
        and consensus_delta >= 800
        and consensus_beats >= 1
        and safe_float(best_consensus_overlay.get("latest_day_delta_rub")) >= 0
    ):
        active["pause_family_after_losses"] = pause_family_limit
        active["pause_after_loss_minutes"] = max(int(active.get("pause_after_loss_minutes") or 0), 120)
        active["notes"].append(
            f"Авто-policy: семейство ставится на паузу после {pause_family_limit} убыточн. закрыт., "
            f"потому что {consensus_scenario} улучшает выборку против base."
        )

    covered_portfolios = {str(value).strip().upper() for value in (active.get("observe_only_portfolios") or []) if str(value).strip()}
    covered_families = {str(value).strip().upper() for value in (active.get("observe_only_families") or []) if str(value).strip()}
    active["entry_blackout_group_windows"] = {
        key: windows
        for key, windows in policy_group_blackout_windows(active).items()
        if split_portfolio_contour_key(key)[0] not in covered_portfolios
    }
    single_family_group_blackout_keys = {
        key
        for key in policy_group_blackout_windows(active)
        if len(portfolio_contour_families.get(key) or set()) <= 1
    }
    active["observe_only_group_families"] = [
        value
        for value in (active.get("observe_only_group_families") or [])
        if (
            (lambda portfolio_name, _contour_name, family_name, portfolio_contour_key: (
                portfolio_name not in covered_portfolios
                and family_name not in covered_families
                and portfolio_contour_key not in single_family_group_blackout_keys
            ))(
                *split_group_family_key(str(value)),
                group_family_portfolio_contour_key(str(value)),
            )
        )
    ]
    observe_group_family_values = set(active.get("observe_only_group_families") or [])
    active["allow_aggressive_group_families"] = [
        value
        for value in (active.get("allow_aggressive_group_families") or [])
        if (
            (lambda portfolio_name, _contour_name, family_name: portfolio_name not in covered_portfolios and family_name not in covered_families and value not in observe_group_family_values)(
                *split_group_family_key(str(value))
            )
        )
    ]
    for key in (
        "observe_only_portfolios",
        "observe_only_group_families",
        "allow_aggressive_group_families",
        "observe_only_tickers",
        "observe_only_families",
        "strict_only_tickers",
        "strict_only_families",
    ):
        active[key] = sorted({str(value).upper() for value in active[key] if str(value).strip()})
    active["notes"] = list(dict.fromkeys(active["notes"]))[:12]

    return {
        "generated_at": now_str(),
        "trade_date": trade_date,
        "history_days": history_days,
        "sample_trades": len(all_rows),
        "active": active,
        "active_base": dict(active),
        "watchdog_overrides": {
            "trade_date": trade_date,
            "observe_only_group_families": [],
            "observe_only_tickers": [],
            "observe_only_families": [],
            "entry_blackout_group_windows": {},
            "notes": [],
        },
        "proposed": proposed,
        "summary": {
            **summarize_active_policy(active),
            "best_consensus_scenario": str(best_consensus_overlay.get("scenario") or ""),
        },
    }


def render_auto_policy_markdown(auto_policy: dict) -> str:
    active = auto_policy.get("active") if isinstance(auto_policy.get("active"), dict) else {}
    proposed = auto_policy.get("proposed") if isinstance(auto_policy.get("proposed"), dict) else {}
    lines = [
        "# Runtime auto-policy",
        "",
        f"- trade_date: {auto_policy.get('trade_date') or '-'}",
        f"- generated_at: {auto_policy.get('generated_at') or '-'}",
        f"- history_days: {auto_policy.get('history_days') or 0}",
        f"- sample_trades: {auto_policy.get('sample_trades') or 0}",
        "",
        "## Active entry policy",
        "",
        f"- observe_only_portfolios: {', '.join(active.get('observe_only_portfolios') or []) or 'none'}",
        f"- observe_only_group_families: {', '.join(active.get('observe_only_group_families') or []) or 'none'}",
        f"- allow_aggressive_group_families: {', '.join(active.get('allow_aggressive_group_families') or []) or 'none'}",
        f"- observe_only_tickers: {', '.join(active.get('observe_only_tickers') or []) or 'none'}",
        f"- observe_only_families: {', '.join(active.get('observe_only_families') or []) or 'none'}",
        f"- strict_only_tickers: {', '.join(active.get('strict_only_tickers') or []) or 'none'}",
        f"- strict_only_families: {', '.join(active.get('strict_only_families') or []) or 'none'}",
        f"- entry_blackout_windows: {', '.join(active.get('entry_blackout_windows') or []) or 'none'}",
        f"- entry_blackout_group_windows: {format_group_blackout_windows(policy_group_blackout_windows(active), empty='none')}",
        f"- entry_no_trade_before: {active.get('entry_no_trade_before') or '-'}",
        f"- entry_no_new_after: {active.get('entry_no_new_after') or '-'}",
        f"- entry_max_full_stop_rub: {active.get('entry_max_full_stop_rub') or '-'}",
        f"- pause_ticker_after_losses: {active.get('pause_ticker_after_losses') or '-'}",
        f"- pause_family_after_losses: {active.get('pause_family_after_losses') or '-'}",
        f"- pause_after_loss_minutes: {active.get('pause_after_loss_minutes') or '-'}",
        "",
    ]
    notes = active.get("notes") or []
    if notes:
        lines.append("### Why active")
        lines.append("")
        for note in notes:
            lines.append(f"- {note}")
        lines.append("")
    lines.append("## Proposed next overlays")
    lines.append("")
    best_latest = proposed.get("best_latest_overlay") if isinstance(proposed.get("best_latest_overlay"), dict) else {}
    best_consensus = proposed.get("best_consensus_overlay") if isinstance(proposed.get("best_consensus_overlay"), dict) else {}
    best_latest_group_blackout = proposed.get("best_latest_group_blackout_overlay") if isinstance(proposed.get("best_latest_group_blackout_overlay"), dict) else {}
    best_consensus_group_blackout = proposed.get("best_consensus_group_blackout_overlay") if isinstance(proposed.get("best_consensus_group_blackout_overlay"), dict) else {}
    lines.append(f"- best_latest_overlay: {best_latest.get('scenario') or '-'}")
    lines.append(f"- best_consensus_overlay: {best_consensus.get('scenario') or '-'}")
    lines.append(f"- best_latest_group_blackout_overlay: {best_latest_group_blackout.get('scenario') or '-'}")
    lines.append(f"- best_consensus_group_blackout_overlay: {best_consensus_group_blackout.get('scenario') or '-'}")
    lines.append(f"- candidate_entry_start: {proposed.get('candidate_entry_start') or '-'}")
    lines.append(f"- candidate_entry_cutoff: {proposed.get('candidate_entry_cutoff') or '-'}")
    lines.append(f"- candidate_entry_blackout_windows: {', '.join(proposed.get('candidate_entry_blackout_windows') or []) or '-'}")
    lines.append(f"- candidate_group_blackout_windows: {format_group_blackout_windows(proposed.get('candidate_group_blackout_windows'), empty='-')}")
    lines.append(f"- candidate_stop_cap_rub: {proposed.get('candidate_stop_cap_rub') or '-'}")
    lines.append("")
    for note in proposed.get("notes") or []:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def markdown_top(title: str, rows: list[dict], columns: list[str], limit: int = 10) -> str:
    if not rows:
        return f"## {title}\n\nНет данных.\n"
    head = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    body = []
    for row in rows[:limit]:
        values = []
        for col in columns:
            value = row.get(col, "")
            values.append("" if value is None else str(value))
        body.append("| " + " | ".join(values) + " |")
    return "\n".join([f"## {title}", "", head, sep, *body, ""])


def load_open_position_snapshot(run_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(run_dir.glob("*_paper_open_positions.json")):
        group = path.stem.removesuffix("_paper_open_positions")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, list):
            continue
        for item in payload:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row.setdefault("portfolio_group", group)
            rows.append(row)
    return rows


def summarize_open_positions(rows: list[dict]) -> dict:
    total_net = 0.0
    for row in rows:
        total_net += safe_float(row.get("unrealized_net_rub") or row.get("unrealized_rub") or 0.0)
    return {
        "count": len(rows),
        "net_rub": round(total_net, 2),
    }


def parse_key_values(text: str) -> dict[str, str]:
    return {key: value for key, value in re.findall(r"([A-Za-z_]+)=([^\s]+)", text)}


def load_margin_timeline(run_dir: Path, trade_date: str) -> list[dict]:
    capitals = load_portfolio_capitals(run_dir)
    rows: list[dict] = []
    for path in sorted(run_dir.glob("*_multi_paper.log")):
        portfolio = path.stem.removesuffix("_multi_paper")
        capital = capitals.get(portfolio, 0.0)
        max_total_margin_pct = 0.80
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if "multi_paper start" in line:
                    fields = parse_key_values(line)
                    start_capital = safe_float(fields.get("paper_capital"))
                    if start_capital > 0:
                        capital = start_capital
                    start_max_margin = safe_float(fields.get("max_total_margin_pct"))
                    if start_max_margin > 0:
                        max_total_margin_pct = start_max_margin
                if not line.startswith(trade_date) or "PORTFOLIO " not in line:
                    continue
                fields = parse_key_values(line)
                dt = parse_dt(line[:19])
                equity = safe_float(fields.get("equity"), capital)
                used_margin = safe_float(fields.get("used_margin"))
                closed_net = safe_float(fields.get("closed_net"))
                open_count = safe_int(fields.get("open"))
                max_total_margin_rub = equity * max_total_margin_pct if equity > 0 and max_total_margin_pct > 0 else 0.0
                free_headroom = max_total_margin_rub - used_margin if max_total_margin_rub > 0 else 0.0
                rows.append(
                    {
                        "portfolio": portfolio,
                        "timestamp": dt.isoformat(sep=" ") if dt else "",
                        "capital_rub": round(capital, 2),
                        "equity_rub": round(equity, 2),
                        "closed_net_rub": round(closed_net, 2),
                        "used_margin_rub": round(used_margin, 2),
                        "open_positions": open_count,
                        "max_total_margin_pct": round(max_total_margin_pct * 100.0, 2),
                        "max_total_margin_rub": round(max_total_margin_rub, 2),
                        "free_margin_headroom_rub": round(free_headroom, 2),
                        "used_margin_pct_of_capital": round(used_margin / capital * 100.0, 2) if capital > 0 else None,
                        "used_margin_pct_of_limit": round(used_margin / max_total_margin_rub * 100.0, 2) if max_total_margin_rub > 0 else None,
                    }
                )
    return rows


def load_margin_snapshot_fallback(run_dir: Path) -> list[dict]:
    capitals = load_portfolio_capitals(run_dir)
    rows: list[dict] = []
    for path in sorted(run_dir.glob("*_health.json")):
        portfolio = path.stem.removesuffix("_health")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        capital = capitals.get(portfolio, 0.0)
        closed_net = safe_float(payload.get("closed_net"))
        used_margin = safe_float(payload.get("used_margin"))
        max_total_margin_pct = 0.80
        equity = capital + closed_net if capital > 0 else 0.0
        max_total_margin_rub = equity * max_total_margin_pct if equity > 0 else 0.0
        free_headroom = max_total_margin_rub - used_margin if max_total_margin_rub > 0 else 0.0
        rows.append(
            {
                "portfolio": portfolio,
                "timestamp": str(payload.get("timestamp") or ""),
                "capital_rub": round(capital, 2) if capital > 0 else None,
                "equity_rub": round(equity, 2) if equity > 0 else None,
                "closed_net_rub": round(closed_net, 2),
                "used_margin_rub": round(used_margin, 2),
                "open_positions": safe_int(payload.get("open_positions")),
                "max_total_margin_pct": round(max_total_margin_pct * 100.0, 2),
                "max_total_margin_rub": round(max_total_margin_rub, 2) if max_total_margin_rub > 0 else None,
                "free_margin_headroom_rub": round(free_headroom, 2) if max_total_margin_rub > 0 else None,
                "used_margin_pct_of_capital": round(used_margin / capital * 100.0, 2) if capital > 0 else None,
                "used_margin_pct_of_limit": round(used_margin / max_total_margin_rub * 100.0, 2) if max_total_margin_rub > 0 else None,
                "source": "health_snapshot_fallback",
            }
        )
    return rows


def summarize_margin_day(day_rows: list[dict], timeline_rows: list[dict], run_dir: Path) -> list[dict]:
    capitals = load_portfolio_capitals(run_dir)
    by_portfolio = grouped_metrics(day_rows, lambda row: str(row.get("portfolio_group") or ""))
    trade_map = {str(row.get("group") or ""): row for row in by_portfolio}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in timeline_rows:
        grouped[str(row.get("portfolio") or "")].append(row)
    portfolio_names = sorted(set(capitals) | set(trade_map) | set(grouped))
    out: list[dict] = []
    for portfolio in portfolio_names:
        points = sorted(grouped.get(portfolio, []), key=lambda row: str(row.get("timestamp") or ""))
        trade_row = trade_map.get(portfolio, {})
        capital = safe_float(capitals.get(portfolio), 0.0)
        used_values = [safe_float(row.get("used_margin_rub")) for row in points]
        limit_pcts = [safe_float(row.get("used_margin_pct_of_limit")) for row in points if row.get("used_margin_pct_of_limit") not in (None, "")]
        headroom_values = [safe_float(row.get("free_margin_headroom_rub")) for row in points]
        equity_values = [safe_float(row.get("equity_rub")) for row in points]
        peak_equity = equity_values[0] if equity_values else 0.0
        worst_drawdown = 0.0
        for value in equity_values:
            peak_equity = max(peak_equity, value)
            worst_drawdown = min(worst_drawdown, value - peak_equity)
        peak_used = max(used_values) if used_values else 0.0
        avg_used = sum(used_values) / len(used_values) if used_values else 0.0
        day_net = safe_float(trade_row.get("net_rub"))
        out.append(
            {
                "portfolio": portfolio,
                "capital_rub": round(capital, 2) if capital > 0 else None,
                "trades": safe_int(trade_row.get("trades")),
                "net_rub": round(day_net, 2),
                "peak_used_margin_rub": round(peak_used, 2),
                "avg_used_margin_rub": round(avg_used, 2),
                "peak_used_margin_pct_of_capital": round(peak_used / capital * 100.0, 2) if capital > 0 and peak_used > 0 else None,
                "avg_used_margin_pct_of_capital": round(avg_used / capital * 100.0, 2) if capital > 0 and avg_used > 0 else None,
                "peak_used_margin_pct_of_limit": round(max(limit_pcts), 2) if limit_pcts else None,
                "avg_used_margin_pct_of_limit": round(sum(limit_pcts) / len(limit_pcts), 2) if limit_pcts else None,
                "min_free_margin_headroom_rub": round(min(headroom_values), 2) if headroom_values else None,
                "realized_intraday_drawdown_rub": round(abs(worst_drawdown), 2) if equity_values else None,
                "return_on_peak_margin_pct": round(day_net / peak_used * 100.0, 3) if peak_used > 0 else None,
                "return_on_avg_margin_pct": round(day_net / avg_used * 100.0, 3) if avg_used > 0 else None,
                "samples": len(points),
                "source": str(points[0].get("source") or "portfolio_log") if points else "missing",
            }
        )
    out.sort(key=lambda row: (safe_float(row.get("return_on_peak_margin_pct")), safe_float(row.get("net_rub"))), reverse=True)
    return out


def load_roll_watch(run_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(run_dir.glob("*_roll_state.json")):
        group = path.stem.removesuffix("_roll_state")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        observe_days = safe_float(payload.get("roll_observe_days"))
        for event in payload.get("roll_events") or []:
            if not isinstance(event, dict):
                continue
            dte = safe_float(event.get("days_to_expiration"))
            selected = str(event.get("selected") or "")
            status = str(event.get("status") or "")
            if not (
                selected
                or status not in {"not_near_roll", "perpetual_or_far_expiration", "no_expiration"}
                or (observe_days > 0 and dte <= observe_days)
            ):
                continue
            rows.append(
                {
                    "portfolio_group": group,
                    "family": str(event.get("family") or ""),
                    "ticker": str(event.get("ticker") or ""),
                    "days_to_expiration": round(dte, 3) if math.isfinite(dte) else None,
                    "selected": selected,
                    "status": status,
                    "reason": str(event.get("reason") or ""),
                }
            )
    rows.sort(key=lambda row: (safe_float(row.get("days_to_expiration"), 10**9), row.get("portfolio_group", ""), row.get("ticker", "")))
    return rows


def build_recommendations(
    overall: dict,
    by_ticker: list[dict],
    by_family: list[dict],
    by_hour: list[dict],
    microstructure_summary: list[dict],
    research_day: list[dict],
    research_all: list[dict],
    research_consensus: list[dict],
    day_history: list[dict],
    margin_summary: list[dict],
) -> list[str]:
    notes: list[str] = []
    if overall.get("trades", 0) <= 0:
        return ["Сделок за день нет, менять логику рано."]
    avg_win = safe_float(overall.get("avg_win_rub"))
    avg_loss = abs(safe_float(overall.get("avg_loss_rub")))
    if avg_win > 0 and avg_loss > avg_win * 1.8:
        notes.append(f"Средний убыток {round(avg_loss,2)} ₽ заметно крупнее среднего плюса {round(avg_win,2)} ₽: проблема в хвостовых стопах, а не в частоте входов.")
    worst_tickers = [row for row in by_ticker if safe_float(row.get("net_rub")) < 0]
    if worst_tickers:
        top = ranked_tail(worst_tickers, limit=3, reverse=False)
        ticker_line = ", ".join(f"{row['group']} {row['net_rub']} ₽" for row in top)
        notes.append(f"Главные разрушители дня по тикерам: {ticker_line}.")
    worst_families = [row for row in by_family if safe_float(row.get("net_rub")) < 0]
    if worst_families:
        top = ranked_tail(worst_families, limit=2, reverse=False)
        fam_line = ", ".join(f"{row['group']} {row['net_rub']} ₽" for row in top)
        notes.append(f"Семейства под давлением: {fam_line}.")
    toxic_micro = [
        row
        for row in microstructure_summary
        if safe_int(row.get("spread_events")) >= 200
        and (safe_float(row.get("median_spread_ratio")) >= 0.75 or safe_float(row.get("dominates_share_pct")) >= 50.0)
        and safe_float(row.get("net_rub")) < 0
    ]
    if toxic_micro:
        top = toxic_micro[0]
        notes.append(
            f"Микроструктурный токсичный срез: {top['group']} "
            f"(wide-spread={top['spread_events']}, median spread/stop={top.get('median_spread_ratio')}, dominates={top.get('dominates_share_pct')}%, net={top.get('net_rub')} ₽)."
        )
    late_hours = []
    for row in by_hour:
        hour = str(row.get("group") or "")
        try:
            hh = int(hour.split(":", 1)[0])
        except Exception:
            continue
        if hh >= 17 and safe_float(row.get("net_rub")) < 0:
            late_hours.append(row)
    if late_hours:
        worst_late = ranked_tail(late_hours, limit=1, reverse=False)[0]
        notes.append(f"Поздний час {worst_late['group']} дал {worst_late['net_rub']} ₽: cutoff по новым входам остаётся важным.")
    if research_day:
        base = next((row for row in research_day if row.get("scenario") == "base"), research_day[0])
        best = research_day[0]
        if best.get("scenario") != "base" and safe_float(best.get("net_rub")) > safe_float(base.get("net_rub")) + 300:
            notes.append(
                f"Лучший быстрый overlay дня: {best['scenario']} ({best['net_rub']} ₽ против {base['net_rub']} ₽ у base). "
                f"Это кандидат на следующий тестовый слой, а не мгновенный перевод боевой логики."
            )
        else:
            notes.append("На дневном срезе нет overlay, который убедительно лучше base без натяжки.")
    if research_all:
        base_all = next((row for row in research_all if row.get("scenario") == "base"), research_all[0])
        best_combo = next((row for row in research_all if str(row.get("scenario") or "").startswith("combo_")), {})
        if best_combo and safe_float(best_combo.get("net_rub")) > safe_float(base_all.get("net_rub")) + 500:
            notes.append(
                f"Сильнейшая связка на всей выборке сейчас: {best_combo['scenario']} "
                f"({best_combo['net_rub']} ₽ против {base_all['net_rub']} ₽ у base). "
                f"Пока это исследовательский кандидат, а не автоматический боевой перевод."
            )
    if day_history:
        killer_days = sum(1 for row in day_history if row.get("day_class") == "killer_day")
        if killer_days:
            notes.append(f"По накопленной серии killer-дней: {killer_days} из {len(day_history)}. Значит, хвостовая проблема повторяется, а не случайна.")
    if research_consensus:
        best_consensus = pick_best_consensus_scenario(research_consensus)
        if best_consensus and best_consensus.get("scenario") != "base":
            notes.append(
                f"Самый устойчивый overlay по всей серии: {best_consensus['scenario']} "
                f"(beat_base_days={best_consensus['beat_base_days']}/{best_consensus['days']}, delta_total={best_consensus['delta_total_rub']} ₽)."
            )
    if margin_summary:
        stressed = sorted(
            [row for row in margin_summary if safe_float(row.get("peak_used_margin_pct_of_limit")) > 50.0],
            key=lambda row: safe_float(row.get("peak_used_margin_pct_of_limit")),
            reverse=True,
        )
        if stressed:
            top = stressed[0]
            notes.append(
                f"Контур {top['portfolio']} нагружал до {top['peak_used_margin_pct_of_limit']}% допустимого лимита ГО; "
                f"дневная отдача на пик ГО {top.get('return_on_peak_margin_pct')}%."
            )
        weak_margin = sorted(
            [row for row in margin_summary if safe_float(row.get("peak_used_margin_rub")) > 0],
            key=lambda row: safe_float(row.get("return_on_peak_margin_pct")),
        )
        if weak_margin and safe_float(weak_margin[0].get("return_on_peak_margin_pct")) < 0:
            worst = weak_margin[0]
            notes.append(
                f"Контур {worst['portfolio']} использовал ГО неэффективно: return_on_peak_margin={worst.get('return_on_peak_margin_pct')}%."
            )
    return notes[:6]


def build_strategy_lab(
    all_rows: list[dict],
    day_rows: list[dict],
    by_group: list[dict],
    by_family: list[dict],
    by_hour: list[dict],
    microstructure_summary: list[dict],
    day_history: list[dict],
    research_day: list[dict],
    research_all: list[dict],
    research_consensus: list[dict],
    auto_policy: dict,
) -> list[dict]:
    active = auto_policy.get("active") if isinstance(auto_policy.get("active"), dict) else {}
    all_overall = metrics(all_rows)
    day_overall = metrics(day_rows)
    all_group_families = grouped_metrics(
        all_rows,
        lambda row: f"{str(row.get('portfolio_group') or '').upper()}/{str(row.get('contour') or '').upper()}::{str(row.get('family') or '').upper()}",
    )
    all_group_families.sort(key=lambda row: (safe_float(row.get("net_rub")), safe_float(row.get("expectancy_rub"))), reverse=True)

    def hour_net(rows: list[dict], start_hh: int, end_hh: int | None = None) -> float:
        total = 0.0
        for row in rows:
            bucket = str(row.get("group") or "")
            try:
                hh = int(bucket.split(":", 1)[0])
            except Exception:
                continue
            if hh < start_hh:
                continue
            if end_hh is not None and hh >= end_hh:
                continue
            total += safe_float(row.get("net_rub"))
        return round(total, 2)

    killer_days = sum(1 for row in day_history if row.get("day_class") == "killer_day")
    killer_share = round(killer_days / len(day_history) * 100.0, 2) if day_history else 0.0
    avg_win = safe_float(all_overall.get("avg_win_rub"))
    avg_loss = abs(safe_float(all_overall.get("avg_loss_rub")))
    morning_net = hour_net(by_hour, 10, 13)
    late_net = hour_net(by_hour, 17, None)
    strict_consensus = next((row for row in research_consensus if str(row.get("scenario") or "") == "contour_only_strict"), {})
    best_consensus = pick_best_consensus_scenario(research_consensus)
    aggressive_positive_slices = []
    for row in all_group_families:
        key = str(row.get("group") or "")
        portfolio_name, contour_name, family_name = split_group_family_key(key)
        if contour_name != "AGGRESSIVE":
            continue
        if safe_int(row.get("trades")) < 2 or safe_float(row.get("net_rub")) <= 0:
            continue
        aggressive_positive_slices.append((key, portfolio_name, family_name, row))

    hypotheses: list[dict] = []

    def add_hypothesis(
        hypothesis_id: str,
        priority: int,
        category: str,
        candidate: str,
        scope: str,
        action_type: str,
        safe_mode: str,
        autopromote_ready: bool,
        evidence: str,
        next_step: str,
        required_features: str,
        scenario_anchor: str = "",
    ) -> None:
        hypotheses.append(
            {
                "hypothesis_id": hypothesis_id,
                "priority": priority,
                "category": category,
                "candidate": candidate,
                "scope": scope,
                "action_type": action_type,
                "safe_mode": safe_mode,
                "autopromote_ready": autopromote_ready,
                "evidence": evidence,
                "recommended_next_step": next_step,
                "required_features": required_features,
                "scenario_anchor": scenario_anchor,
            }
        )

    if strict_consensus and str(strict_consensus.get("scenario") or "") == "contour_only_strict":
        add_hypothesis(
            hypothesis_id="runtime_strict_primary",
            priority=98,
            category="runtime_policy",
            candidate="strict primary baseline",
            scope="all classic futures",
            action_type="runtime_policy",
            safe_mode="paper_autopolicy",
            autopromote_ready=bool(active.get("strict_only_families")),
            evidence=(
                f"strict consensus delta_total={safe_float(strict_consensus.get('delta_total_rub'))} ₽, "
                f"beat_base_days={safe_int(strict_consensus.get('beat_base_days'))}/{safe_int(strict_consensus.get('days'))}"
            ),
            next_step="держать strict как базовую ось и пускать aggressive только точечно, где он переживает серию.",
            required_features="trade csv, day history, research consensus",
            scenario_anchor=str(strict_consensus.get("scenario") or ""),
        )

    if aggressive_positive_slices:
        best_slice = aggressive_positive_slices[0]
        add_hypothesis(
            hypothesis_id="runtime_selective_aggressive_exceptions",
            priority=94,
            category="runtime_hybrid",
            candidate="strict baseline + selective aggressive slices",
            scope="portfolio/family slice",
            action_type="runtime_policy",
            safe_mode="paper_autopolicy",
            autopromote_ready=bool(active.get("allow_aggressive_group_families")),
            evidence=(
                f"positive aggressive slices={len(aggressive_positive_slices)}, best={best_slice[0]} "
                f"net={safe_float(best_slice[3].get('net_rub'))} ₽ trades={safe_int(best_slice[3].get('trades'))}"
            ),
            next_step="продолжать искать profitable aggressive-срезы и выпускать их из broad strict-only только после подтверждения на серии.",
            required_features="trade csv, grouped contour/family metrics, strict baseline comparison",
            scenario_anchor=str(best_consensus.get("scenario") or ""),
        )

    if morning_net > 0 and late_net < 0:
        add_hypothesis(
            hypothesis_id="shadow_opening_range_continuation",
            priority=88,
            category="shadow_strategy",
            candidate="opening-range continuation only",
            scope="10:15-13:00 Moscow session",
            action_type="shadow_backtest",
            safe_mode="research_only",
            autopromote_ready=False,
            evidence=f"morning_net={morning_net} ₽ while late_net={late_net} ₽ on latest day",
            next_step="собрать shadow/backtest слой: разрешать новые входы только в сильные утренние часы и отдельно считать expectancy.",
            required_features="1m candles, hour bucket, family, contour, session clock",
            scenario_anchor=str(best_consensus.get("scenario") or ""),
        )

    if avg_win > 0 and avg_loss > avg_win * 1.8:
        add_hypothesis(
            hypothesis_id="shadow_tail_risk_normalized_exit",
            priority=91,
            category="exit_model",
            candidate="tail-risk normalized exits",
            scope="all futures layers",
            action_type="shadow_backtest",
            safe_mode="research_only",
            autopromote_ready=False,
            evidence=f"avg_loss={round(avg_loss,2)} ₽ vs avg_win={round(avg_win,2)} ₽ on accumulated sample",
            next_step="считать альтернативный выход: более ранний трейл / tighter time-stop / volatility-normalized exit и сравнить tail damage.",
            required_features="trade ledger, opened_at/closed_at, 1m candles, current stop/exit path",
            scenario_anchor=str(best_consensus.get("scenario") or ""),
        )

    if killer_days >= 1:
        add_hypothesis(
            hypothesis_id="runtime_family_regime_routing",
            priority=86,
            category="regime_routing",
            candidate="family-specific routing by regime",
            scope="destructive families only",
            action_type="research_then_runtime",
            safe_mode="paper_only",
            autopromote_ready=False,
            evidence=f"killer_days={killer_days}/{len(day_history)} ({killer_share}%) in accumulated history",
            next_step="разделить семьи на stable / mixed / destructive и для mixed запускать новые контракты сначала в micro/observe до накопления своей статистики.",
            required_features="day history, family PnL, rollover state, contract lineage",
            scenario_anchor="day_history",
        )

    worst_family = next((row for row in by_family if safe_float(row.get("net_rub")) < 0), {})
    if worst_family:
        add_hypothesis(
            hypothesis_id="shadow_vwap_reversion_family_probe",
            priority=80,
            category="new_strategy",
            candidate="VWAP reversion probe on weak families",
            scope=str(worst_family.get("group") or "weak families"),
            action_type="shadow_backtest",
            safe_mode="research_only",
            autopromote_ready=False,
            evidence=f"worst_family={worst_family.get('group')} net={safe_float(worst_family.get('net_rub'))} ₽ on latest day",
            next_step="подготовить отдельный research-слой mean-reversion вокруг VWAP для семейств, где текущий momentum-tail даёт плохой payout.",
            required_features="1m candles, rolling/session VWAP, deviation z-score, spread filter, family label",
            scenario_anchor=str(best_consensus.get("scenario") or ""),
        )

    wide_spread_family = next(
        (
            row
            for row in microstructure_summary
            if safe_int(row.get("spread_events")) >= 200
            and (safe_float(row.get("median_spread_ratio")) >= 0.75 or safe_float(row.get("dominates_share_pct")) >= 50.0)
            and safe_float(row.get("net_rub")) < 0
        ),
        {},
    )
    if wide_spread_family:
        add_hypothesis(
            hypothesis_id="microstructure_spread_adaptive_gate",
            priority=78,
            category="microstructure",
            candidate="spread-adaptive entry gate",
            scope=str(wide_spread_family.get("group") or "wide-spread slices"),
            action_type="shadow_backtest",
            safe_mode="research_only",
            autopromote_ready=False,
            evidence=(
                f"weak slice={wide_spread_family.get('group')} net={safe_float(wide_spread_family.get('net_rub'))} ₽, "
                f"spread_events={safe_int(wide_spread_family.get('spread_events'))}, "
                f"median_ratio={safe_float(wide_spread_family.get('median_spread_ratio'))}"
            ),
            next_step="отдельно моделировать входы с динамическим spread/stop ratio вместо грубого статического запрета.",
            required_features="orderbook snapshots, spread ticks, stop ticks, fill path, family slice",
            scenario_anchor=str(best_consensus.get("scenario") or ""),
        )

    hypotheses.sort(key=lambda row: (safe_int(row.get("priority")), str(row.get("candidate") or "")), reverse=True)
    for idx, row in enumerate(hypotheses, start=1):
        row["rank"] = idx
    return hypotheses


def build_summary_markdown(
    trade_date: str,
    overall: dict,
    open_summary: dict,
    by_group: list[dict],
    by_portfolio: list[dict],
    by_ticker: list[dict],
    by_family: list[dict],
    by_hour: list[dict],
    worst_trades: list[dict],
    best_tickers: list[dict],
    worst_tickers: list[dict],
    worst_families: list[dict],
    roll_watch: list[dict],
    day_history: list[dict],
    recurring_tickers: list[dict],
    recurring_families: list[dict],
    margin_summary: list[dict],
    microstructure_summary: list[dict],
    recommendations: list[str],
    best_research_day: list[dict],
    best_research_all: list[dict],
    best_research_consensus: list[dict],
    strategy_lab: list[dict],
    runtime_trade_model: dict,
) -> str:
    fee_model = runtime_trade_model.get("fee_model") if isinstance(runtime_trade_model.get("fee_model"), dict) else dict(DEFAULT_FEE_MODEL)
    fee_note = str(fee_model.get("note") or PREMIUM_FEE_NOTE)
    fee_pct = safe_float(fee_model.get("futures_rate_per_side_pct"))
    fee_summary = f"{fee_pct:.3f}% per side" if fee_pct > 0 else str(runtime_trade_model.get("tariff") or DEFAULT_TARIFF)
    lines = [
        f"# 3pips daily autonomy summary: {trade_date}",
        "",
        f"- generated_at: {now_str()}",
        f"- trades: {overall['trades']}",
        f"- win_rate_pct: {overall['win_rate_pct']}",
        f"- net_rub: {overall['net_rub']}",
        f"- expectancy_rub: {overall['expectancy_rub']}",
        f"- median_trade_rub: {overall['median_trade_rub']}",
        f"- avg_win_rub: {overall['avg_win_rub']}",
        f"- avg_loss_rub: {overall['avg_loss_rub']}",
        f"- profit_factor: {overall['profit_factor']}",
        f"- broker: {runtime_trade_model.get('broker')}",
        f"- tariff: {runtime_trade_model.get('tariff')}",
        f"- margin_mode: {runtime_trade_model.get('margin_mode')}",
        f"- fee_model: {fee_summary}",
        f"- open_positions_count: {open_summary.get('count')}",
        f"- open_positions_net_rub: {open_summary.get('net_rub')}",
        "",
    ]
    if recommendations:
        lines.append("## Что делать дальше\n")
        for note in recommendations:
            lines.append(f"- {note}")
        lines.append("")
    lines.append("## Комиссия и ГО\n")
    lines.append(f"- {fee_note}")
    lines.append("- GO analysis uses actual paper portfolio logs: equity / used_margin / free headroom from runtime `PORTFOLIO` snapshots.")
    lines.append("")
    lines.append(markdown_top("By Portfolio", by_portfolio, ["group", "trades", "win_rate_pct", "net_rub", "expectancy_rub"], limit=10))
    lines.append(markdown_top("Margin / GO by Portfolio", margin_summary, ["portfolio", "trades", "net_rub", "peak_used_margin_rub", "peak_used_margin_pct_of_limit", "min_free_margin_headroom_rub", "return_on_peak_margin_pct", "realized_intraday_drawdown_rub", "source"], limit=10))
    lines.append(markdown_top("By Portfolio + Layer", by_group, ["group", "trades", "win_rate_pct", "net_rub", "expectancy_rub", "profit_factor"]))
    lines.append(markdown_top("Best Tickers", best_tickers, ["group", "trades", "win_rate_pct", "net_rub", "expectancy_rub"], limit=10))
    lines.append(markdown_top("Worst Tickers", worst_tickers, ["group", "trades", "win_rate_pct", "net_rub", "expectancy_rub"], limit=10))
    lines.append(markdown_top("By Ticker", by_ticker, ["group", "trades", "win_rate_pct", "net_rub", "expectancy_rub"], limit=15))
    lines.append(markdown_top("Worst Families", worst_families, ["group", "trades", "win_rate_pct", "net_rub", "expectancy_rub"], limit=10))
    lines.append(markdown_top("By Family", by_family, ["group", "trades", "win_rate_pct", "net_rub", "expectancy_rub"], limit=15))
    lines.append(markdown_top("By Hour", by_hour, ["group", "trades", "win_rate_pct", "net_rub", "expectancy_rub"], limit=12))
    lines.append(markdown_top("Rollover Watch", roll_watch, ["portfolio_group", "family", "ticker", "days_to_expiration", "selected", "status"], limit=12))
    lines.append(markdown_top("Day History", day_history, ["trade_date", "day_class", "trades", "net_rub", "top1_loss_rub", "top3_loss_rub", "late_net_rub", "worst_ticker"], limit=15))
    lines.append(markdown_top("Recurring Killer Tickers", recurring_tickers, ["group", "days", "killer_days", "total_bucket_net_rub", "worst_bucket_rub"], limit=10))
    lines.append(markdown_top("Recurring Killer Families", recurring_families, ["group", "days", "killer_days", "total_bucket_net_rub", "worst_bucket_rub"], limit=10))
    lines.append(markdown_top("Microstructure Toxic Slices", microstructure_summary, ["group", "spread_events", "median_spread_ratio", "dominates_share_pct", "trades", "net_rub", "expectancy_rub"], limit=10))
    lines.append(markdown_top("Worst Trades", worst_trades, ["closed_at", "portfolio_group", "contour", "secid", "direction", "qty", "net_rub", "ticks"], limit=10))
    lines.append(markdown_top("Research Top: Latest Day", best_research_day, ["scenario", "trades", "win_rate_pct", "net_rub", "expectancy_rub", "note"], limit=10))
    lines.append(markdown_top("Research Top: All Sample", best_research_all, ["scenario", "trades", "win_rate_pct", "net_rub", "expectancy_rub", "note"], limit=10))
    lines.append(markdown_top("Research Top: Consensus", best_research_consensus, ["scenario", "days", "beat_base_days", "delta_total_rub", "median_daily_net_rub", "worst_day_rub", "note"], limit=10))
    lines.append(markdown_top("Strategy Lab", strategy_lab, ["rank", "candidate", "category", "action_type", "safe_mode", "autopromote_ready", "evidence"], limit=10))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--run-name", default="v7_live_20260525")
    parser.add_argument("--profiles", default="")
    parser.add_argument("--date", default="latest")
    parser.add_argument("--email-to", default="etc00051@yandex.ru")
    parser.add_argument("--state-path", default="")
    parser.add_argument("--force-email", action="store_true")
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    run_dir = project_root / "reports" / "paper_runs" / args.run_name
    profiles_path = Path(args.profiles) if args.profiles else project_root / "reports" / "futures_scalp_profiles_v7_paper_20260525.csv"
    autonomy_root = project_root / "reports" / "autonomy"
    analysis_root = autonomy_root / "analysis"
    research_root = autonomy_root / "research"
    archive_root = autonomy_root / "archives"
    manifest_root = autonomy_root / "latest"
    state_path = Path(args.state_path) if args.state_path else (project_root / "reports" / "runtime" / "daily_autonomy_state.json")
    ensure_dir(analysis_root)
    ensure_dir(research_root)
    ensure_dir(archive_root)
    ensure_dir(manifest_root)

    all_rows = load_primary_trades(run_dir)
    if not all_rows:
        write_text(manifest_root / "latest_daily_summary.md", f"# 3pips daily autonomy summary\n\nNo trade rows found.\n")
        return 0

    trade_date = args.date
    if trade_date == "latest":
        trade_date = latest_trade_date(all_rows) or ""
    if not trade_date:
        write_text(manifest_root / "latest_daily_summary.md", f"# 3pips daily autonomy summary\n\nNo trade date found.\n")
        return 0

    day_rows = filter_trade_date(all_rows, trade_date)
    profiles = load_profiles(profiles_path)
    all_wide_spread_rows = load_wide_spread_reviews(run_dir)

    annotate_trade_rows(all_rows, profiles)
    annotate_trade_rows(day_rows, profiles)

    day_history = build_day_history(all_rows, profiles)
    recurring_tickers = build_recurring_killers(day_history, "worst_ticker")
    recurring_families = build_recurring_killers(day_history, "worst_family")
    scenario_history = build_scenario_history(all_rows, profiles)
    scenario_consensus = summarize_scenario_history(scenario_history)

    analysis_dir = analysis_root / trade_date
    research_dir = research_root / trade_date
    bundle_dir = archive_root / f"bundle_{trade_date}"
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    ensure_dir(bundle_dir)
    ensure_dir(analysis_dir)
    ensure_dir(research_dir)

    overall = metrics(day_rows)
    by_group = grouped_metrics(day_rows, lambda row: row["group_key"])
    by_portfolio = grouped_metrics(day_rows, lambda row: str(row.get("portfolio_group") or ""))
    by_ticker = grouped_metrics(day_rows, lambda row: str(row.get("secid") or ""))
    by_family = grouped_metrics(day_rows, lambda row: row["family"])
    by_hour = grouped_metrics(day_rows, hour_bucket)
    all_group_family_metrics = metrics_map(
        all_rows,
        lambda row: f"{str(row.get('portfolio_group') or '').upper()}/{str(row.get('contour') or '').upper()}::{str(row.get('family') or '').upper()}",
    )
    microstructure_summary = build_microstructure_summary(all_wide_spread_rows, all_group_family_metrics)
    worst_trades = sorted(day_rows, key=parse_trade_net)[:10]
    best_tickers = ranked_tail([row for row in by_ticker if safe_float(row.get("net_rub")) > 0], limit=10, reverse=True)
    worst_tickers = ranked_tail([row for row in by_ticker if safe_float(row.get("net_rub")) < 0], limit=10, reverse=False)
    worst_families = ranked_tail([row for row in by_family if safe_float(row.get("net_rub")) < 0], limit=10, reverse=False)
    open_positions = load_open_position_snapshot(run_dir)
    open_summary = summarize_open_positions(open_positions)
    roll_watch = load_roll_watch(run_dir)
    margin_timeline = load_margin_timeline(run_dir, trade_date)
    fallback_margin_timeline = load_margin_snapshot_fallback(run_dir)
    existing_margin_portfolios = {str(row.get("portfolio") or "") for row in margin_timeline}
    for row in fallback_margin_timeline:
        portfolio = str(row.get("portfolio") or "")
        if portfolio and portfolio not in existing_margin_portfolios:
            margin_timeline.append(row)
    margin_summary = summarize_margin_day(day_rows, margin_timeline, run_dir)
    runtime_trade_model = load_runtime_trade_model(run_dir)

    research_day = build_research_scenarios(all_rows, day_rows, profiles)
    research_all = build_research_scenarios(all_rows, all_rows, profiles)
    recommendations = build_recommendations(overall, by_ticker, by_family, by_hour, microstructure_summary, research_day, research_all, scenario_consensus, day_history, margin_summary)
    auto_policy = build_auto_policy(
        all_rows=all_rows,
        profiles=profiles,
        trade_date=trade_date,
        day_history=day_history,
        recurring_tickers=recurring_tickers,
        recurring_families=recurring_families,
        microstructure_summary=microstructure_summary,
        research_day=research_day,
        research_all=research_all,
        research_consensus=scenario_consensus,
    )
    auto_policy = merge_watchdog_overrides(auto_policy, load_json(manifest_root / "latest_auto_policy.json"))
    optimizer_candidates = build_optimizer_candidates(research_day, research_all, scenario_consensus)
    strategy_lab = build_strategy_lab(
        all_rows=all_rows,
        day_rows=day_rows,
        by_group=by_group,
        by_family=by_family,
        by_hour=by_hour,
        microstructure_summary=microstructure_summary,
        day_history=day_history,
        research_day=research_day,
        research_all=research_all,
        research_consensus=scenario_consensus,
        auto_policy=auto_policy,
    )
    restriction_rows = build_restriction_rows(auto_policy)

    summary_md = build_summary_markdown(
        trade_date,
        overall,
        open_summary,
        by_group,
        by_portfolio,
        by_ticker,
        by_family,
        by_hour,
        worst_trades,
        best_tickers,
        worst_tickers,
        worst_families,
        roll_watch,
        day_history,
        recurring_tickers,
        recurring_families,
        margin_summary,
        microstructure_summary,
        recommendations,
        research_day,
        research_all,
        scenario_consensus,
        strategy_lab,
        runtime_trade_model,
    )
    best_consensus_scenario = pick_best_consensus_scenario(scenario_consensus)
    write_analysis_outputs(
        analysis_dir,
        trade_date=trade_date,
        overall=overall,
        open_summary=open_summary,
        recommendations=recommendations,
        best_consensus_scenario=best_consensus_scenario,
        strategy_lab=strategy_lab,
        microstructure_summary=microstructure_summary,
        runtime_trade_model=runtime_trade_model,
        summary_md=summary_md,
        by_portfolio=by_portfolio,
        by_group=by_group,
        by_ticker=by_ticker,
        by_family=by_family,
        by_hour=by_hour,
        worst_trades=worst_trades,
        best_tickers=best_tickers,
        worst_tickers=worst_tickers,
        worst_families=worst_families,
        roll_watch=roll_watch,
        day_history=day_history,
        recurring_tickers=recurring_tickers,
        recurring_families=recurring_families,
        margin_timeline=margin_timeline,
        margin_summary=margin_summary,
        auto_policy=auto_policy,
        restriction_rows=restriction_rows,
        render_auto_policy_markdown=render_auto_policy_markdown,
    )
    strategy_lab_counts = write_research_outputs(
        research_dir,
        research_day=research_day,
        research_all=research_all,
        scenario_history=scenario_history,
        scenario_consensus=scenario_consensus,
        optimizer_candidates=optimizer_candidates,
        strategy_lab=strategy_lab,
        markdown_top=markdown_top,
    )

    runtime_dir = project_root / "reports" / "runtime"
    shadow_rows = filter_trade_date(load_shadow_trades(run_dir), trade_date)
    copy_bundle_outputs(
        bundle_dir,
        day_rows=day_rows,
        shadow_rows=shadow_rows,
        run_dir=run_dir,
        runtime_dir=runtime_dir,
        analysis_dir=analysis_dir,
        research_dir=research_dir,
    )
    manifest_payload = build_manifest_payload(
        trade_date=trade_date,
        overall=overall,
        open_summary=open_summary,
        runtime_trade_model=runtime_trade_model,
        margin_summary=margin_summary,
        recommendations=recommendations,
        research_day=research_day,
        research_all=research_all,
        best_consensus_scenario=best_consensus_scenario,
        day_history=day_history,
        worst_tickers=worst_tickers,
        worst_families=worst_families,
        recurring_tickers=recurring_tickers,
        recurring_families=recurring_families,
        microstructure_summary=microstructure_summary,
        scenario_consensus=scenario_consensus,
        optimizer_candidates=optimizer_candidates,
        strategy_lab=strategy_lab,
        strategy_lab_counts=strategy_lab_counts,
        restriction_rows=restriction_rows,
        roll_watch=roll_watch,
        auto_policy=auto_policy,
    )
    write_json(bundle_dir / "manifest.json", manifest_payload)

    nightly_cycle_status = build_nightly_cycle_status(
        trade_date=trade_date,
        overall=overall,
        research_day=research_day,
        research_all=research_all,
        research_consensus=scenario_consensus,
        optimizer_candidates=optimizer_candidates,
        strategy_lab=strategy_lab,
        restriction_rows=restriction_rows,
        auto_policy=auto_policy,
        email_to=args.email_to,
    )
    persist_nightly_cycle_status(
        nightly_cycle_status,
        analysis_dir / "nightly_cycle_status.json",
        bundle_dir / "nightly_cycle_status.json",
    )

    zip_path = archive_root / f"3pips_daily_{trade_date}.zip"
    if zip_path.exists():
        zip_path.unlink()
    build_zip(zip_path, bundle_dir)
    nightly_cycle_status["stages"]["summary"]["archive_ready"] = True
    nightly_cycle_status["stages"]["summary"]["archive_path"] = str(zip_path)
    persist_nightly_cycle_status(
        nightly_cycle_status,
        analysis_dir / "nightly_cycle_status.json",
        bundle_dir / "nightly_cycle_status.json",
    )

    subject = f"[3pips] daily {trade_date} net={overall['net_rub']} trades={overall['trades']}"
    body_lines = [
        f"Trade date: {trade_date}",
        f"Net RUB: {overall['net_rub']}",
        f"Trades: {overall['trades']}",
        f"Win rate %: {overall['win_rate_pct']}",
        f"Expectancy RUB: {overall['expectancy_rub']}",
    ]
    if research_day:
        body_lines.extend(
            [
                "",
                "Best latest-day research overlay:",
                f"{research_day[0]['scenario']} net={research_day[0]['net_rub']} trades={research_day[0]['trades']} exp={research_day[0]['expectancy_rub']}",
            ]
        )
    last_state = {}
    if state_path.exists():
        try:
            last_state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            last_state = {}
    already_sent = last_state.get("last_trade_date_sent") == trade_date
    if already_sent and not args.force_email:
        ok, email_status = False, "skipped_already_sent"
    else:
        ok, email_status = send_email(subject, "\n".join(body_lines), recipient=args.email_to, attachments=[zip_path])
    email_stage = nightly_cycle_status["stages"].setdefault("email", {})
    email_stage["status"] = email_status
    email_stage["sent"] = bool(ok)
    email_stage["archive"] = str(zip_path)
    persist_nightly_cycle_status(
        nightly_cycle_status,
        analysis_dir / "nightly_cycle_status.json",
        bundle_dir / "nightly_cycle_status.json",
        manifest_root / "latest_nightly_cycle_status.json",
    )
    latest_summary = manifest_root / "latest_daily_summary.md"
    write_text(latest_summary, summary_md)
    write_json(manifest_root / "latest_auto_policy.json", auto_policy)
    latest_manifest_payload = {
        **manifest_payload,
        "archive": str(zip_path),
        "nightly_cycle_status": nightly_cycle_status,
    }
    write_json(manifest_root / "latest_daily_manifest.json", latest_manifest_payload)
    write_json(manifest_root / "latest_manifest.json", latest_manifest_payload)
    write_json(
        manifest_root / "latest_email_status.json",
        {
            "sent": ok,
            "status": email_status,
            "trade_date": trade_date,
            "archive": str(zip_path),
        },
    )
    write_json(
        state_path,
        {
            "last_trade_date_sent": trade_date if ok or email_status == "skipped_already_sent" else last_state.get("last_trade_date_sent", ""),
            "last_email_status": email_status,
            "updated_at": now_str(),
            "archive": str(zip_path),
        },
    )
    print(f"[{now_str()}] daily_autonomy_done trade_date={trade_date} archive={zip_path} email_status={email_status}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
