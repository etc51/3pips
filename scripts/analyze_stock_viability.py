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


def recommendation_for_watch(
    adj_net_2t: float,
    remove_best_3_net_2t: float,
    trade_count: int,
    reason: str,
) -> str:
    if adj_net_2t <= 0:
        return "REJECT_AFTER_PREMIUM_FEES"
    if remove_best_3_net_2t <= 0:
        return "REJECT_OUTLIER_DEPENDENT"
    if trade_count < 20:
        return "LOW_SAMPLE_RESEARCH_ONLY"
    if "execution_watchlist" in (reason or ""):
        return "PAPER_ONLY_EXECUTION_WATCHLIST"
    return "RESEARCH_ONLY"


def build_watchlist_summary(
    profiles: list[dict[str, Any]],
    micro_rows: list[dict[str, str]],
    old_rate: float,
    new_rate: float,
) -> list[dict[str, Any]]:
    micro_by_ticker = {row["ticker"]: row for row in micro_rows}
    watchlist = []
    for profile in profiles:
        if profile.get("status") != "WATCHLIST":
            continue
        hard = profile.get("hard_2T_metrics", {})
        ticker = profile["ticker"]
        micro = micro_by_ticker.get(ticker, {})
        commission_old = float(hard.get("commission_rub_1lot", 0.0) or 0.0)
        net_old = float(hard.get("net_pnl_rub_1lot", 0.0) or 0.0)
        trade_count = int(hard.get("n_trades", 0) or 0)
        remove_best_3 = float(hard.get("remove_best_3_net_pnl_rub_1lot", 0.0) or 0.0)
        commission_new = commission_old * (new_rate / old_rate) if old_rate > 0 else commission_old
        net_new = adjusted_net(net_old, commission_old, old_rate, new_rate)

        entry = {
            "ticker": ticker,
            "name": profile.get("name"),
            "family": profile.get("family"),
            "direction": profile.get("direction"),
            "reason": profile.get("reason"),
            "exit_model": profile.get("exit_model_preferred"),
            "session_filter": profile.get("params", {}).get("session_filter"),
            "max_hold_minutes": profile.get("params", {}).get("max_hold_minutes"),
            "test_trades_2t": trade_count,
            "win_rate_2t": float(hard.get("win_rate", 0.0) or 0.0),
            "profit_factor_2t": float(hard.get("profit_factor", 0.0) or 0.0),
            "max_drawdown_2t_rub_1lot": float(hard.get("max_drawdown_rub_1lot", 0.0) or 0.0),
            "avg_trade_2t_rub_1lot_old_fee": float(hard.get("avg_net_trade_rub_1lot", 0.0) or 0.0),
            "net_2t_rub_1lot_old_fee": net_old,
            "commission_2t_rub_1lot_old_fee": commission_old,
            "commission_2t_rub_1lot_premium": commission_new,
            "net_2t_rub_1lot_premium": net_new,
            "remove_best_3_net_2t_rub_1lot": remove_best_3,
            "trades_per_day": float(hard.get("trades_per_day", 0.0) or 0.0),
            "microstructure_class": micro.get("microstructure_class"),
            "microstructure_action": micro.get("microstructure_action"),
            "policy_spread_ticks": micro.get("policy_spread_ticks"),
            "policy_top_liq_rub": micro.get("policy_top_liq_rub"),
            "liquidity_rank": micro.get("liquidity_rank"),
        }
        entry["recommendation"] = recommendation_for_watch(
            adj_net_2t=net_new,
            remove_best_3_net_2t=remove_best_3,
            trade_count=trade_count,
            reason=str(profile.get("reason", "")),
        )
        watchlist.append(entry)

    watchlist.sort(key=lambda row: row["net_2t_rub_1lot_premium"], reverse=True)
    return watchlist


def build_portfolio_summary(
    portfolio_rows: list[dict[str, str]],
    old_rate: float,
    new_rate: float,
) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for row in portfolio_rows:
        net_old = float(row["net_pnl_rub"])
        commission_old = float(row["commission_rub"])
        commission_new = commission_old * (new_rate / old_rate) if old_rate > 0 else commission_old
        net_new = adjusted_net(net_old, commission_old, old_rate, new_rate)
        summary.append(
            {
                "portfolio_group": row["portfolio_group"],
                "risk_cap_rub_per_ticker": int(float(row["risk_cap_rub_per_ticker"])),
                "profiles": int(float(row["profiles"])),
                "n_trades": int(float(row["n_trades"])),
                "net_pnl_rub_old_fee": net_old,
                "commission_rub_old_fee": commission_old,
                "commission_rub_premium": commission_new,
                "net_pnl_rub_premium": net_new,
                "max_drawdown_rub": float(row["max_drawdown_rub"]),
                "trades_per_day": float(row["trades_per_day"]),
                "avg_qty": float(row["avg_qty"]),
                "max_qty": int(float(row["max_qty"])),
            }
        )
    return summary


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_report(
    path: Path,
    watchlist_rows: list[dict[str, Any]],
    portfolio_rows: list[dict[str, Any]],
    old_rate: float,
    new_rate: float,
) -> None:
    lines: list[str] = []
    lines.append("# Stock viability review")
    lines.append("")
    lines.append("This is a research-only summary for MOEX stocks under the current 3pips logic.")
    lines.append("")
    lines.append(f"- Backtest commission model used in the uploaded stock package: {old_rate:.4%} per side")
    lines.append(f"- Adjusted review commission model: {new_rate:.4%} per side")
    lines.append("- Runtime interpretation: do not mix this with the futures runtime until a separate paper-only stock contour is justified.")
    lines.append("- Operational window recommendation for any future stock paper contour: Moscow 10:15-17:45, no overnight carry.")
    lines.append("")
    lines.append("## Watchlist")
    lines.append("")
    if not watchlist_rows:
        lines.append("No watchlist names survived after premium-fee adjustment.")
    else:
        lines.append("| ticker | family | dir | trades | net 2T old | net 2T premium | PF 2T | remove best 3 | rec |")
        lines.append("|:--|:--|:--|--:|--:|--:|--:|--:|:--|")
        for row in watchlist_rows:
            lines.append(
                f"| {row['ticker']} | {row['family']} | {row['direction']} | {row['test_trades_2t']} | "
                f"{row['net_2t_rub_1lot_old_fee']:.2f} | {row['net_2t_rub_1lot_premium']:.2f} | "
                f"{row['profit_factor_2t']:.2f} | {row['remove_best_3_net_2t_rub_1lot']:.2f} | {row['recommendation']} |"
            )
    lines.append("")
    lines.append("## Portfolio scaling view")
    lines.append("")
    lines.append("| group | risk cap | trades | net old | net premium | max DD |")
    lines.append("|:--|--:|--:|--:|--:|--:|")
    for row in portfolio_rows:
        if row["portfolio_group"] != "LIVE_PLUS_WATCHLIST":
            continue
        lines.append(
            f"| {row['portfolio_group']} | {row['risk_cap_rub_per_ticker']} | {row['n_trades']} | "
            f"{row['net_pnl_rub_old_fee']:.2f} | {row['net_pnl_rub_premium']:.2f} | {row['max_drawdown_rub']:.2f} |"
        )
    lines.append("")
    lines.append("## Conclusion")
    lines.append("")
    if watchlist_rows:
        lines.append(
            "Stocks are viable only as a separate paper research contour. "
            "They are not strong enough to replace the futures core and they do not currently justify being merged into the main runtime."
        )
    else:
        lines.append(
            "Stocks are not currently viable even as a research watchlist after the premium-fee adjustment."
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--old-side-rate", type=float, default=0.00025)
    parser.add_argument("--premium-side-rate", type=float, default=0.0004)
    args = parser.parse_args()

    results_dir = args.results_dir
    profiles = load_json(results_dir / "stock_final_live_paper_profiles.json")
    micro_rows = load_csv_rows(results_dir / "stock_microstructure_policy.csv")
    portfolio_rows = load_csv_rows(results_dir / "stock_portfolio_simulation.csv")

    watchlist_rows = build_watchlist_summary(
        profiles=profiles,
        micro_rows=micro_rows,
        old_rate=args.old_side_rate,
        new_rate=args.premium_side_rate,
    )
    portfolio_summary = build_portfolio_summary(
        portfolio_rows=portfolio_rows,
        old_rate=args.old_side_rate,
        new_rate=args.premium_side_rate,
    )

    watchlist_csv = results_dir / "stock_viability_watchlist.csv"
    portfolio_csv = results_dir / "stock_viability_portfolio_summary.csv"
    report_md = results_dir / "stock_viability_report.md"
    summary_json = results_dir / "stock_viability_summary.json"

    write_csv(watchlist_csv, watchlist_rows)
    write_csv(portfolio_csv, portfolio_summary)
    write_report(
        report_md,
        watchlist_rows=watchlist_rows,
        portfolio_rows=portfolio_summary,
        old_rate=args.old_side_rate,
        new_rate=args.premium_side_rate,
    )

    summary = {
        "results_dir": str(results_dir),
        "old_side_rate": args.old_side_rate,
        "premium_side_rate": args.premium_side_rate,
        "watchlist_count": len(watchlist_rows),
        "paper_only_execution_watchlist_count": sum(
            1 for row in watchlist_rows if row["recommendation"] == "PAPER_ONLY_EXECUTION_WATCHLIST"
        ),
        "top_candidates": [row["ticker"] for row in watchlist_rows[:3]],
        "output_files": {
            "watchlist_csv": str(watchlist_csv),
            "portfolio_csv": str(portfolio_csv),
            "report_md": str(report_md),
        },
    }
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Wrote {watchlist_csv}")
    print(f"Wrote {portfolio_csv}")
    print(f"Wrote {report_md}")
    print(f"Wrote {summary_json}")


if __name__ == "__main__":
    main()
