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
    tail_text,
    write_csv_rows,
    write_json,
    write_text,
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


def latest_trade_date(rows: list[dict]) -> str | None:
    dates = sorted({str(row.get("closed_at") or "")[:10] for row in rows if row.get("closed_at")})
    return dates[-1] if dates else None


def filter_trade_date(rows: list[dict], trade_date: str) -> list[dict]:
    return [row for row in rows if str(row.get("closed_at") or "").startswith(trade_date)]


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
    out = evaluate_scenario(name, selected, profiles, note=note)
    out["skipped_trades"] = skipped
    return out


def build_research_scenarios(all_rows: list[dict], sample_rows: list[dict], profiles: dict[str, dict]) -> list[dict]:
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

    for cutoff in ["16:30", "17:00", "17:15", "17:30", "17:45"]:
        hh, mm = map(int, cutoff.split(":"))

        def pred(row, hh=hh, mm=mm):
            dt = entry_dt(row)
            if dt is None:
                return True
            return (dt.hour, dt.minute) <= (hh, mm)

        scenarios.append(
            evaluate_scenario(
                f"no_new_after_{cutoff.replace(':', '')}",
                sample_rows,
                profiles,
                predicate=pred,
                note="entry time cutoff",
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
    weak_families = [row["group"] for row in family_rows if row["trades"] >= 3 and row["net_rub"] < 0]
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
        scenarios = build_research_scenarios(history_rows, day_rows, profiles)
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
    if name.startswith("stop_cap_"):
        return "stop_cap_rub"
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
    if name.startswith("whitelist_"):
        return "family_whitelist"
    return "other"


def recommended_use_for_scenario(kind: str) -> str:
    if kind in {"entry_cutoff", "stop_cap_rub"}:
        return "candidate_runtime_tune"
    if kind in {
        "contour_filter",
        "ticker_pause_after_losses",
        "family_pause_after_losses",
        "family_blacklist",
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
    restriction_rows: list[dict],
    auto_policy: dict,
) -> dict:
    active = auto_policy.get("active") if isinstance(auto_policy.get("active"), dict) else {}
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
            "restrictions": {
                "status": "ok",
                "active_rule_count": sum(
                    len(active.get(key) or [])
                    for key in ("observe_only_tickers", "observe_only_families", "strict_only_tickers", "strict_only_families")
                )
                + sum(
                    1
                    for key in ("entry_max_full_stop_rub", "pause_ticker_after_losses", "pause_family_after_losses", "pause_after_loss_minutes")
                    if active.get(key) not in (None, "")
                ),
                "rows": len(restriction_rows),
            },
            "summary": {
                "status": "ok",
                "archive_ready": True,
                "archive_path": "",
            },
        },
    }


def metrics_map(rows: list[dict], key_fn) -> dict[str, dict]:
    return {str(row.get("group") or ""): row for row in grouped_metrics(rows, key_fn)}


def scenario_loss_limit(name: str, prefix: str) -> int | None:
    if not name.startswith(prefix):
        return None
    suffix = name.removeprefix(prefix)
    try:
        return int(suffix.split("_", 1)[0])
    except Exception:
        return None


def build_auto_policy(
    all_rows: list[dict],
    profiles: dict[str, dict],
    trade_date: str,
    day_history: list[dict],
    recurring_tickers: list[dict],
    recurring_families: list[dict],
    research_day: list[dict],
    research_consensus: list[dict],
) -> dict:
    history_days = len(day_history)
    by_ticker = metrics_map(all_rows, lambda row: str(row.get("secid") or ""))
    by_family = metrics_map(all_rows, lambda row: family_for_row(row, profiles))
    by_ticker_contour = metrics_map(all_rows, lambda row: f"{row.get('secid') or ''}::{row.get('contour') or ''}")
    by_family_contour = metrics_map(all_rows, lambda row: f"{family_for_row(row, profiles)}::{row.get('contour') or ''}")

    active = {
        "observe_only_tickers": [],
        "observe_only_families": [],
        "strict_only_tickers": [],
        "strict_only_families": [],
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

    best_latest_overlay = next((row for row in research_day if row.get("scenario") != "base"), {})
    best_consensus_overlay = pick_best_consensus_scenario(research_consensus)

    proposed = {
        "best_latest_overlay": best_latest_overlay,
        "best_consensus_overlay": best_consensus_overlay,
        "candidate_entry_cutoff": "",
        "candidate_stop_cap_rub": None,
        "notes": [],
    }
    for candidate in [best_consensus_overlay, best_latest_overlay]:
        scenario = str(candidate.get("scenario") or "")
        if scenario.startswith("no_new_after_") and not proposed["candidate_entry_cutoff"]:
            proposed["candidate_entry_cutoff"] = scenario.removeprefix("no_new_after_")
            proposed["notes"].append(
                f"Сценарий {scenario} дал лучший результат в исследовательском слое: это кандидат на ранний cutoff, но пока не активируется автоматически."
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

    latest_scenario = str(best_latest_overlay.get("scenario") or "")
    strict_consensus_overlay = next((row for row in research_consensus if str(row.get("scenario") or "") == "contour_only_strict"), {})
    strict_days = safe_int(strict_consensus_overlay.get("days"))
    strict_total_net = safe_float(strict_consensus_overlay.get("total_net_rub"))
    strict_delta = safe_float(strict_consensus_overlay.get("delta_total_rub"))
    strict_latest_delta = safe_float(strict_consensus_overlay.get("latest_day_delta_rub"))
    futures_families = sorted(
        family
        for family in by_family
        if family and "PERPA" not in str(family).upper()
    )
    if (
        futures_families
        and strict_days >= 2
        and strict_total_net >= 0
        and strict_delta >= 2_000
        and strict_latest_delta >= 1_000
        and latest_scenario == "contour_only_strict"
    ):
        active["strict_only_families"].extend(futures_families)
        active["notes"].append(
            "Авто-policy: все фьючерсные семьи переведены в strict-only для новых входов, "
            "потому что contour_only_strict дал сильный прирост на последнем дне и не уходит в минус по накопленной серии."
        )

    consensus_blacklist_family = consensus_scenario.removeprefix("blacklist_family_") if consensus_scenario.startswith("blacklist_family_") else ""
    if consensus_blacklist_family:
        family_total = by_family.get(consensus_blacklist_family) or {}
        family_trades = safe_int(family_total.get("trades"))
        family_net = safe_float(family_total.get("net_rub"))
        latest_delta = safe_float(best_consensus_overlay.get("latest_day_delta_rub"))
        latest_overlay_net = safe_float(best_latest_overlay.get("net_rub"))
        robust_consensus = consensus_days >= 2 and consensus_beats >= consensus_days and consensus_delta >= 1_500
        strong_latest_confirmation = (
            latest_scenario == consensus_scenario
            and latest_delta >= 2_000
            and latest_overlay_net > 0
        )
        if family_trades >= 3 and family_net < 0 and (robust_consensus or strong_latest_confirmation):
            active["observe_only_families"].append(consensus_blacklist_family)
            active["notes"].append(
                f"Авто-policy: семейство {consensus_blacklist_family} переведено в observe-only, "
                f"потому что {consensus_scenario} устойчиво улучшает результат против base."
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

    for key in ("observe_only_tickers", "observe_only_families", "strict_only_tickers", "strict_only_families"):
        active[key] = sorted({str(value).upper() for value in active[key] if str(value).strip()})
    active["notes"] = active["notes"][:12]

    return {
        "generated_at": now_str(),
        "trade_date": trade_date,
        "history_days": history_days,
        "sample_trades": len(all_rows),
        "active": active,
        "active_base": dict(active),
        "watchdog_overrides": {
            "trade_date": trade_date,
            "observe_only_tickers": [],
            "observe_only_families": [],
            "notes": [],
        },
        "proposed": proposed,
        "summary": {
            "active_rule_count": sum(
                len(active[key])
                for key in ("observe_only_tickers", "observe_only_families", "strict_only_tickers", "strict_only_families")
            )
            + sum(
                1
                for key in ("entry_max_full_stop_rub", "pause_ticker_after_losses", "pause_family_after_losses", "pause_after_loss_minutes")
                if active.get(key) not in (None, "")
            ),
            "active_notes_count": len(active["notes"]),
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
        f"- observe_only_tickers: {', '.join(active.get('observe_only_tickers') or []) or 'none'}",
        f"- observe_only_families: {', '.join(active.get('observe_only_families') or []) or 'none'}",
        f"- strict_only_tickers: {', '.join(active.get('strict_only_tickers') or []) or 'none'}",
        f"- strict_only_families: {', '.join(active.get('strict_only_families') or []) or 'none'}",
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
    lines.append(f"- best_latest_overlay: {best_latest.get('scenario') or '-'}")
    lines.append(f"- best_consensus_overlay: {best_consensus.get('scenario') or '-'}")
    lines.append(f"- candidate_entry_cutoff: {proposed.get('candidate_entry_cutoff') or '-'}")
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
    research_day: list[dict],
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
    recommendations: list[str],
    best_research_day: list[dict],
    best_research_all: list[dict],
    best_research_consensus: list[dict],
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
    lines.append(markdown_top("Worst Trades", worst_trades, ["closed_at", "portfolio_group", "contour", "secid", "direction", "qty", "net_rub", "ticks"], limit=10))
    lines.append(markdown_top("Research Top: Latest Day", best_research_day, ["scenario", "trades", "win_rate_pct", "net_rub", "expectancy_rub", "note"], limit=10))
    lines.append(markdown_top("Research Top: All Sample", best_research_all, ["scenario", "trades", "win_rate_pct", "net_rub", "expectancy_rub", "note"], limit=10))
    lines.append(markdown_top("Research Top: Consensus", best_research_consensus, ["scenario", "days", "beat_base_days", "delta_total_rub", "median_daily_net_rub", "worst_day_rub", "note"], limit=10))
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

    for row in all_rows:
        row["family"] = family_for_row(row, profiles)
        row["group_key"] = f"{row.get('portfolio_group','')}/{row.get('contour','')}"

    for row in day_rows:
        row["family"] = family_for_row(row, profiles)
        row["group_key"] = f"{row.get('portfolio_group','')}/{row.get('contour','')}"

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
    recommendations = build_recommendations(overall, by_ticker, by_family, by_hour, research_day, scenario_consensus, day_history, margin_summary)
    auto_policy = build_auto_policy(
        all_rows=all_rows,
        profiles=profiles,
        trade_date=trade_date,
        day_history=day_history,
        recurring_tickers=recurring_tickers,
        recurring_families=recurring_families,
        research_day=research_day,
        research_consensus=scenario_consensus,
    )
    optimizer_candidates = build_optimizer_candidates(research_day, research_all, scenario_consensus)
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
        recommendations,
        research_day,
        research_all,
        scenario_consensus,
        runtime_trade_model,
    )
    write_text(analysis_dir / "daily_summary.md", summary_md)
    write_json(
        analysis_dir / "daily_summary.json",
        {
            "trade_date": trade_date,
            "generated_at": now_str(),
            "overall": overall,
            "open_positions": open_summary,
            "recommendations": recommendations,
            "best_consensus_scenario": pick_best_consensus_scenario(scenario_consensus),
            "margin_mode": runtime_trade_model.get("margin_mode"),
            "fee_model": runtime_trade_model.get("fee_model"),
        },
    )
    write_csv_rows(analysis_dir / "by_portfolio.csv", by_portfolio)
    write_csv_rows(analysis_dir / "by_group.csv", by_group)
    write_csv_rows(analysis_dir / "by_ticker.csv", by_ticker)
    write_csv_rows(analysis_dir / "by_family.csv", by_family)
    write_csv_rows(analysis_dir / "by_hour.csv", by_hour)
    write_csv_rows(analysis_dir / "worst_trades.csv", worst_trades)
    write_csv_rows(analysis_dir / "best_tickers.csv", best_tickers)
    write_csv_rows(analysis_dir / "worst_tickers.csv", worst_tickers)
    write_csv_rows(analysis_dir / "worst_families.csv", worst_families)
    write_csv_rows(analysis_dir / "roll_watch.csv", roll_watch)
    write_csv_rows(analysis_dir / "day_history.csv", day_history)
    write_csv_rows(analysis_dir / "recurring_killer_tickers.csv", recurring_tickers)
    write_csv_rows(analysis_dir / "recurring_killer_families.csv", recurring_families)
    write_csv_rows(analysis_dir / "margin_timeline.csv", margin_timeline)
    write_csv_rows(analysis_dir / "margin_summary.csv", margin_summary)
    write_text(analysis_dir / "recommendations.md", "\n".join(f"- {line}" for line in recommendations) + ("\n" if recommendations else ""))
    write_json(analysis_dir / "auto_policy.json", auto_policy)
    write_text(analysis_dir / "auto_policy.md", render_auto_policy_markdown(auto_policy))
    write_csv_rows(analysis_dir / "restrictions_runtime.csv", restriction_rows)

    for row in research_day:
        row["sample"] = "latest_day"
    for row in research_all:
        row["sample"] = "all_sample"
    write_csv_rows(research_dir / "policy_sweep_latest_day.csv", research_day)
    write_csv_rows(research_dir / "policy_sweep_all_sample.csv", research_all)
    write_csv_rows(research_dir / "policy_sweep_daily_history.csv", scenario_history)
    write_csv_rows(research_dir / "policy_sweep_consensus.csv", scenario_consensus)
    write_text(
        research_dir / "research_summary.md",
        markdown_top("Research Top: Latest Day", research_day, ["scenario", "trades", "win_rate_pct", "net_rub", "expectancy_rub", "note"], limit=15)
        + "\n"
        + markdown_top("Research Top: All Sample", research_all, ["scenario", "trades", "win_rate_pct", "net_rub", "expectancy_rub", "note"], limit=15)
        + "\n"
        + markdown_top("Research Top: Consensus", scenario_consensus, ["scenario", "days", "beat_base_days", "delta_total_rub", "median_daily_net_rub", "worst_day_rub", "note"], limit=15),
    )
    write_csv_rows(research_dir / "optimizer_candidates.csv", optimizer_candidates)
    write_text(
        research_dir / "optimizer_summary.md",
        markdown_top(
            "Optimizer Candidates",
            optimizer_candidates,
            ["source", "scenario", "candidate_type", "recommended_use", "net_rub", "expectancy_rub", "delta_total_rub", "beat_base_days", "note"],
            limit=20,
        ),
    )

    raw_dir = bundle_dir / "raw"
    ensure_dir(raw_dir)
    write_csv_rows(raw_dir / "day_primary_trades.csv", day_rows)
    shadow_rows = filter_trade_date(load_shadow_trades(run_dir), trade_date)
    if shadow_rows:
        write_csv_rows(raw_dir / "day_shadow_trades.csv", shadow_rows)

    for pattern in ["*_health.json", "*_paper_open_positions.json", "*_instrument_specs.csv", "*_startup_status.csv", "*_roll_state.json"]:
        for path in run_dir.glob(pattern):
            shutil.copy2(path, raw_dir / path.name)
    for pattern in ["*_wide_spread_review.csv", "*_shadow_exit_models.csv"]:
        for path in run_dir.glob(pattern):
            shutil.copy2(path, raw_dir / path.name)

    runtime_dir = project_root / "reports" / "runtime"
    write_text(raw_dir / "v7_paper_supervisor_20260525.tail.log", tail_text(runtime_dir / "v7_paper_supervisor_20260525.log", lines=500))
    write_text(raw_dir / "server_watchdog.tail.log", tail_text(runtime_dir / "server_watchdog.log", lines=500))

    shutil.copy2(analysis_dir / "daily_summary.md", bundle_dir / "daily_summary.md")
    shutil.copy2(analysis_dir / "auto_policy.md", bundle_dir / "auto_policy.md")
    shutil.copy2(research_dir / "research_summary.md", bundle_dir / "research_summary.md")
    shutil.copy2(research_dir / "optimizer_summary.md", bundle_dir / "optimizer_summary.md")
    for path in [
        analysis_dir / "day_history.csv",
        analysis_dir / "by_portfolio.csv",
        analysis_dir / "margin_timeline.csv",
        analysis_dir / "margin_summary.csv",
        analysis_dir / "restrictions_runtime.csv",
        analysis_dir / "recurring_killer_tickers.csv",
        analysis_dir / "recurring_killer_families.csv",
        analysis_dir / "worst_tickers.csv",
        analysis_dir / "worst_families.csv",
        research_dir / "policy_sweep_latest_day.csv",
        research_dir / "policy_sweep_all_sample.csv",
        research_dir / "policy_sweep_daily_history.csv",
        research_dir / "policy_sweep_consensus.csv",
        research_dir / "optimizer_candidates.csv",
    ]:
        if path.exists():
            shutil.copy2(path, bundle_dir / path.name)
    manifest_payload = {
        "trade_date": trade_date,
        "generated_at": now_str(),
        "overall": overall,
        "open_positions": open_summary,
        "margin_mode": runtime_trade_model.get("margin_mode"),
        "fee_model": runtime_trade_model.get("fee_model"),
        "portfolio_margin_day": margin_summary,
        "recommendations": recommendations,
        "best_latest_day_scenario": research_day[0] if research_day else {},
        "best_all_sample_scenario": research_all[0] if research_all else {},
        "best_consensus_scenario": pick_best_consensus_scenario(scenario_consensus),
        "day_history_tail": day_history[-10:],
        "day_class_counts": {
            "good_day": sum(1 for row in day_history if row.get("day_class") == "good_day"),
            "bad_day": sum(1 for row in day_history if row.get("day_class") == "bad_day"),
            "killer_day": sum(1 for row in day_history if row.get("day_class") == "killer_day"),
        },
        "top_killer_tickers": worst_tickers[:3],
        "top_killer_families": worst_families[:3],
        "recurring_killer_tickers": recurring_tickers[:5],
        "recurring_killer_families": recurring_families[:5],
        "research_consensus_top": scenario_consensus[:10],
        "optimizer_top": optimizer_candidates[:10],
        "restrictions_runtime": restriction_rows,
        "roll_watch": roll_watch[:12],
        "auto_policy": auto_policy,
    }
    write_json(bundle_dir / "manifest.json", manifest_payload)

    nightly_cycle_status = build_nightly_cycle_status(
        trade_date=trade_date,
        overall=overall,
        research_day=research_day,
        research_all=research_all,
        research_consensus=scenario_consensus,
        optimizer_candidates=optimizer_candidates,
        restriction_rows=restriction_rows,
        auto_policy=auto_policy,
    )
    write_json(analysis_dir / "nightly_cycle_status.json", nightly_cycle_status)
    write_json(bundle_dir / "nightly_cycle_status.json", nightly_cycle_status)

    zip_path = archive_root / f"3pips_daily_{trade_date}.zip"
    if zip_path.exists():
        zip_path.unlink()
    build_zip(zip_path, bundle_dir)
    nightly_cycle_status["stages"]["summary"]["archive_path"] = str(zip_path)
    write_json(analysis_dir / "nightly_cycle_status.json", nightly_cycle_status)
    write_json(bundle_dir / "nightly_cycle_status.json", nightly_cycle_status)

    latest_summary = manifest_root / "latest_daily_summary.md"
    write_text(latest_summary, summary_md)
    write_json(manifest_root / "latest_auto_policy.json", auto_policy)
    write_json(manifest_root / "latest_nightly_cycle_status.json", nightly_cycle_status)
    latest_manifest_payload = {
        **manifest_payload,
        "archive": str(zip_path),
        "nightly_cycle_status": nightly_cycle_status,
    }
    write_json(manifest_root / "latest_daily_manifest.json", latest_manifest_payload)
    write_json(manifest_root / "latest_manifest.json", latest_manifest_payload)

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
