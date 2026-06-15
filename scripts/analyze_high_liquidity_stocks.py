from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


DEFAULT_RESULTS_DIR = Path(
    r"C:\Users\-\Documents\мен и тренд\reports\stock_moex_scalp_results_review"
)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def adjusted_net(old_net: float, old_commission: float, old_rate: float, new_rate: float) -> float:
    if old_rate <= 0:
        return old_net
    new_commission = old_commission * (new_rate / old_rate)
    return old_net - (new_commission - old_commission)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def report_status(profile: dict[str, Any] | None) -> str:
    if not profile:
        return "NO_PROFILE"
    return str(profile.get("status") or "UNKNOWN")


def build_top_liquidity_rows(
    profiles: list[dict[str, Any]],
    stage0_rows: list[dict[str, str]],
    micro_rows: list[dict[str, str]],
    rejected_rows: list[dict[str, str]],
    top_n: int,
    old_rate: float,
    premium_rate: float,
) -> list[dict[str, Any]]:
    profile_by_ticker = {str(row.get("ticker")): row for row in profiles}
    micro_by_ticker = {str(row.get("ticker")): row for row in micro_rows}
    rejected_by_ticker = {str(row.get("ticker")): row for row in rejected_rows}

    ordered = sorted(
        stage0_rows,
        key=lambda row: int(float(row.get("liquidity_rank") or "999999")),
    )[:top_n]

    rows: list[dict[str, Any]] = []
    for stage0 in ordered:
        ticker = str(stage0["ticker"])
        profile = profile_by_ticker.get(ticker)
        rejected = rejected_by_ticker.get(ticker, {})
        micro = micro_by_ticker.get(ticker, {})
        hard = (profile or {}).get("hard_2T_metrics", {})
        status = report_status(profile)
        if status == "NO_PROFILE" and rejected:
            status = "REJECTED"
        reason = (profile or {}).get("reason") or rejected.get("reason", "")
        commission_old = float(hard.get("commission_rub_1lot", 0.0) or 0.0)
        net_old = float(hard.get("net_pnl_rub_1lot", 0.0) or 0.0)
        net_premium = adjusted_net(net_old, commission_old, old_rate, premium_rate)
        rows.append(
            {
                "liquidity_rank": int(float(stage0.get("liquidity_rank") or "999999")),
                "ticker": ticker,
                "name": stage0.get("name"),
                "status": status,
                "family": (profile or {}).get("family", ""),
                "direction": (profile or {}).get("direction", ""),
                "reason": reason,
                "test_trades_2t": int(hard.get("n_trades", 0) or 0),
                "win_rate_2t": float(hard.get("win_rate", 0.0) or 0.0),
                "profit_factor_2t": float(hard.get("profit_factor", 0.0) or 0.0),
                "net_2t_rub_1lot_old_fee": net_old,
                "net_2t_rub_1lot_premium": net_premium,
                "remove_best_3_net_2t_rub_1lot": float(
                    hard.get("remove_best_3_net_pnl_rub_1lot", 0.0) or 0.0
                ),
                "trades_per_day": float(hard.get("trades_per_day", 0.0) or 0.0),
                "short_enabled": stage0.get("short_enabled"),
                "policy_spread_ticks": micro.get("policy_spread_ticks", ""),
                "policy_top_liq_rub": micro.get("policy_top_liq_rub", ""),
                "microstructure_class": micro.get("microstructure_class", ""),
                "microstructure_action": micro.get("microstructure_action", ""),
            }
        )
    return rows


def build_watchlist(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [row for row in rows if row["status"] == "WATCHLIST"]
    out.sort(
        key=lambda row: (
            row["net_2t_rub_1lot_premium"],
            row["profit_factor_2t"],
            row["test_trades_2t"],
        ),
        reverse=True,
    )
    return out


def build_rejections(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [row for row in rows if row["status"] != "WATCHLIST"]
    out.sort(key=lambda row: row["liquidity_rank"])
    return out


def calc_watchlist_portfolio(watchlist: list[dict[str, Any]], risk_cap_rub: int) -> dict[str, Any]:
    if not watchlist:
        return {
            "risk_cap_rub_per_ticker": risk_cap_rub,
            "tickers": 0,
            "trades_per_day": 0.0,
            "net_premium_rub_per_day_1lot": 0.0,
        }
    total_trades_per_day = sum(float(row["trades_per_day"]) for row in watchlist)
    total_net_per_day = sum(
        float(row["net_2t_rub_1lot_premium"]) * float(row["trades_per_day"]) / max(int(row["test_trades_2t"]) or 1, 1)
        for row in watchlist
    )
    return {
        "risk_cap_rub_per_ticker": risk_cap_rub,
        "tickers": len(watchlist),
        "trades_per_day": round(total_trades_per_day, 4),
        "net_premium_rub_per_day_1lot": round(total_net_per_day, 2),
    }


def write_report(
    path: Path,
    top_n: int,
    old_rate: float,
    premium_rate: float,
    rows: list[dict[str, Any]],
    watchlist: list[dict[str, Any]],
) -> None:
    lines: list[str] = []
    lines.append("# High-liquidity MOEX stock review")
    lines.append("")
    lines.append("This report isolates only the most liquid stock names for the current 3pips stock strategy package.")
    lines.append("")
    lines.append(f"- Top liquidity basket size: {top_n}")
    lines.append(f"- Backtest side commission in package: {old_rate:.4%}")
    lines.append(f"- Review side commission for T-Bank Premium: {premium_rate:.4%}")
    lines.append("- Goal: decide whether high-liquidity stocks deserve a separate paper contour under 10:15-17:45 Moscow time.")
    lines.append("")
    lines.append("## Basket status")
    lines.append("")
    lines.append("| rank | ticker | status | family | dir | trades | PF | net premium | reason |")
    lines.append("|--:|:--|:--|:--|:--|--:|--:|--:|:--|")
    for row in rows:
        lines.append(
            f"| {row['liquidity_rank']} | {row['ticker']} | {row['status']} | {row['family']} | {row['direction']} | "
            f"{row['test_trades_2t']} | {row['profit_factor_2t']:.2f} | {row['net_2t_rub_1lot_premium']:.2f} | {row['reason']} |"
        )
    lines.append("")
    lines.append("## Surviving watchlist")
    lines.append("")
    if not watchlist:
        lines.append("No high-liquidity names survived even as watchlist candidates.")
    else:
        lines.append("| ticker | net premium | trades | win rate | PF | micro action |")
        lines.append("|:--|--:|--:|--:|--:|:--|")
        for row in watchlist:
            lines.append(
                f"| {row['ticker']} | {row['net_2t_rub_1lot_premium']:.2f} | {row['test_trades_2t']} | "
                f"{row['win_rate_2t']:.2%} | {row['profit_factor_2t']:.2f} | {row['microstructure_action']} |"
            )
    lines.append("")
    lines.append("## Conclusion")
    lines.append("")
    if watchlist:
        lines.append(
            "High-liquidity stocks are worth keeping only as a narrow paper research basket. "
            "They do not currently justify promotion into the main futures runtime."
        )
    else:
        lines.append(
            "High-liquidity stocks do not currently justify even a narrow paper basket under this strategy package."
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--top-liquidity-count", type=int, default=20)
    parser.add_argument("--old-side-rate", type=float, default=0.00025)
    parser.add_argument("--premium-side-rate", type=float, default=0.0004)
    args = parser.parse_args()

    results_dir = args.results_dir
    profiles = load_json(results_dir / "stock_final_live_paper_profiles.json")
    stage0_rows = load_csv_rows(results_dir / "stock_stage0_audit.csv")
    micro_rows = load_csv_rows(results_dir / "stock_microstructure_policy.csv")
    rejected_rows = load_csv_rows(results_dir / "stock_rejected_tickers.csv")

    top_rows = build_top_liquidity_rows(
        profiles=profiles,
        stage0_rows=stage0_rows,
        micro_rows=micro_rows,
        rejected_rows=rejected_rows,
        top_n=args.top_liquidity_count,
        old_rate=args.old_side_rate,
        premium_rate=args.premium_side_rate,
    )
    watchlist_rows = build_watchlist(top_rows)
    rejection_rows = build_rejections(top_rows)
    day_estimate = calc_watchlist_portfolio(watchlist_rows, risk_cap_rub=1000)

    watchlist_csv = results_dir / "stock_high_liquidity_watchlist.csv"
    rejected_csv = results_dir / "stock_high_liquidity_rejections.csv"
    report_md = results_dir / "stock_high_liquidity_report.md"
    summary_json = results_dir / "stock_high_liquidity_summary.json"

    write_csv(watchlist_csv, watchlist_rows)
    write_csv(rejected_csv, rejection_rows)
    write_report(
        report_md,
        top_n=args.top_liquidity_count,
        old_rate=args.old_side_rate,
        premium_rate=args.premium_side_rate,
        rows=top_rows,
        watchlist=watchlist_rows,
    )

    summary = {
        "top_liquidity_count": args.top_liquidity_count,
        "watchlist_count": len(watchlist_rows),
        "watchlist_tickers": [row["ticker"] for row in watchlist_rows],
        "rejected_count": len(rejection_rows),
        "paper_day_estimate_1lot": day_estimate,
        "output_files": {
            "watchlist_csv": str(watchlist_csv),
            "rejected_csv": str(rejected_csv),
            "report_md": str(report_md),
        },
    }
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Wrote {watchlist_csv}")
    print(f"Wrote {rejected_csv}")
    print(f"Wrote {report_md}")
    print(f"Wrote {summary_json}")


if __name__ == "__main__":
    main()
