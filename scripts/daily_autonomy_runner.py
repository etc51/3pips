from __future__ import annotations

import argparse
import json
import math
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


def load_primary_trades(run_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(run_dir.glob("*_multi_futures_paper_trades.csv")):
        group = trade_file_group(path)
        for row in read_csv_rows(path):
            item = dict(row)
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


def family_for_row(row: dict, profiles: dict[str, dict]) -> str:
    secid = str(row.get("secid") or "")
    profile = profiles.get(secid, {})
    return str(row.get("family") or profile.get("v7_family") or secid)


def entry_dt(row: dict):
    return parse_dt(str(row.get("opened_at") or "")) or parse_dt(str(row.get("closed_at") or ""))


def hour_bucket(row: dict) -> str:
    dt = entry_dt(row)
    return f"{dt.hour:02d}:00" if dt else "unknown"


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


def markdown_top(title: str, rows: list[dict], columns: list[str], limit: int = 10) -> str:
    if not rows:
        return f"## {title}\n\nНет данных.\n"
    head = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    body = []
    for row in rows[:limit]:
        body.append("| " + " | ".join(str(row.get(col, "")) for col in columns) + " |")
    return "\n".join([f"## {title}", "", head, sep, *body, ""])


def build_summary_markdown(
    trade_date: str,
    overall: dict,
    by_group: list[dict],
    by_ticker: list[dict],
    by_family: list[dict],
    by_hour: list[dict],
    worst_trades: list[dict],
    best_research_day: list[dict],
    best_research_all: list[dict],
) -> str:
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
        "",
    ]
    lines.append(markdown_top("By Portfolio + Layer", by_group, ["group", "trades", "win_rate_pct", "net_rub", "expectancy_rub", "profit_factor"]))
    lines.append(markdown_top("By Ticker", by_ticker, ["group", "trades", "win_rate_pct", "net_rub", "expectancy_rub"], limit=15))
    lines.append(markdown_top("By Family", by_family, ["group", "trades", "win_rate_pct", "net_rub", "expectancy_rub"], limit=15))
    lines.append(markdown_top("By Hour", by_hour, ["group", "trades", "win_rate_pct", "net_rub", "expectancy_rub"], limit=12))
    lines.append(markdown_top("Worst Trades", worst_trades, ["closed_at", "portfolio_group", "contour", "secid", "direction", "qty", "net_rub", "ticks"], limit=10))
    lines.append(markdown_top("Research Top: Latest Day", best_research_day, ["scenario", "trades", "win_rate_pct", "net_rub", "expectancy_rub", "note"], limit=10))
    lines.append(markdown_top("Research Top: All Sample", best_research_all, ["scenario", "trades", "win_rate_pct", "net_rub", "expectancy_rub", "note"], limit=10))
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
    by_ticker = grouped_metrics(day_rows, lambda row: str(row.get("secid") or ""))
    by_family = grouped_metrics(day_rows, lambda row: row["family"])
    by_hour = grouped_metrics(day_rows, hour_bucket)
    worst_trades = sorted(day_rows, key=parse_trade_net)[:10]

    research_day = build_research_scenarios(all_rows, day_rows, profiles)
    research_all = build_research_scenarios(all_rows, all_rows, profiles)

    summary_md = build_summary_markdown(
        trade_date,
        overall,
        by_group,
        by_ticker,
        by_family,
        by_hour,
        worst_trades,
        research_day,
        research_all,
    )
    write_text(analysis_dir / "daily_summary.md", summary_md)
    write_json(
        analysis_dir / "daily_summary.json",
        {
            "trade_date": trade_date,
            "generated_at": now_str(),
            "overall": overall,
        },
    )
    write_csv_rows(analysis_dir / "by_group.csv", by_group)
    write_csv_rows(analysis_dir / "by_ticker.csv", by_ticker)
    write_csv_rows(analysis_dir / "by_family.csv", by_family)
    write_csv_rows(analysis_dir / "by_hour.csv", by_hour)
    write_csv_rows(analysis_dir / "worst_trades.csv", worst_trades)

    for row in research_day:
        row["sample"] = "latest_day"
    for row in research_all:
        row["sample"] = "all_sample"
    write_csv_rows(research_dir / "policy_sweep_latest_day.csv", research_day)
    write_csv_rows(research_dir / "policy_sweep_all_sample.csv", research_all)
    write_text(
        research_dir / "research_summary.md",
        markdown_top("Research Top: Latest Day", research_day, ["scenario", "trades", "win_rate_pct", "net_rub", "expectancy_rub", "note"], limit=15)
        + "\n"
        + markdown_top("Research Top: All Sample", research_all, ["scenario", "trades", "win_rate_pct", "net_rub", "expectancy_rub", "note"], limit=15),
    )

    raw_dir = bundle_dir / "raw"
    ensure_dir(raw_dir)
    write_csv_rows(raw_dir / "day_primary_trades.csv", day_rows)
    shadow_rows = filter_trade_date(load_shadow_trades(run_dir), trade_date)
    if shadow_rows:
        write_csv_rows(raw_dir / "day_shadow_trades.csv", shadow_rows)

    for pattern in ["*_health.json", "*_paper_open_positions.json", "*_instrument_specs.csv", "*_startup_status.csv"]:
        for path in run_dir.glob(pattern):
            shutil.copy2(path, raw_dir / path.name)
    for pattern in ["*_wide_spread_review.csv", "*_shadow_exit_models.csv"]:
        for path in run_dir.glob(pattern):
            shutil.copy2(path, raw_dir / path.name)

    runtime_dir = project_root / "reports" / "runtime"
    write_text(raw_dir / "v7_paper_supervisor_20260525.tail.log", tail_text(runtime_dir / "v7_paper_supervisor_20260525.log", lines=500))
    write_text(raw_dir / "server_watchdog.tail.log", tail_text(runtime_dir / "server_watchdog.log", lines=500))

    shutil.copy2(analysis_dir / "daily_summary.md", bundle_dir / "daily_summary.md")
    shutil.copy2(research_dir / "research_summary.md", bundle_dir / "research_summary.md")
    write_json(
        bundle_dir / "manifest.json",
        {
            "trade_date": trade_date,
            "generated_at": now_str(),
            "overall": overall,
            "best_latest_day_scenario": research_day[0] if research_day else {},
            "best_all_sample_scenario": research_all[0] if research_all else {},
        },
    )

    zip_path = archive_root / f"3pips_daily_{trade_date}.zip"
    if zip_path.exists():
        zip_path.unlink()
    build_zip(zip_path, bundle_dir)

    latest_summary = manifest_root / "latest_daily_summary.md"
    write_text(latest_summary, summary_md)
    write_json(
        manifest_root / "latest_daily_manifest.json",
        {
            "trade_date": trade_date,
            "generated_at": now_str(),
            "archive": str(zip_path),
            "overall": overall,
        },
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
