from __future__ import annotations

import argparse
from pathlib import Path
from collections import Counter

import pandas as pd

from leadlag_ng_moex import REPORTS, ensure_dirs


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path)


def agg(df: pd.DataFrame) -> dict:
    if df.empty:
        return {
            "count": 0,
            "gross_ticks_sum": 0.0,
            "net_ticks_sum": 0.0,
            "net_pnl_rub_sum": 0.0,
            "mean_net_ticks": None,
            "mean_net_pnl_rub": None,
            "hit_rate": None,
        }
    gross = pd.to_numeric(df.get("gross_ticks", pd.Series(dtype=float)), errors="coerce")
    net = pd.to_numeric(df.get("net_ticks", pd.Series(dtype=float)), errors="coerce")
    pnl = pd.to_numeric(df.get("net_pnl_rub", pd.Series(dtype=float)), errors="coerce")
    return {
        "count": int(len(df)),
        "gross_ticks_sum": float(gross.sum()),
        "net_ticks_sum": float(net.sum()),
        "net_pnl_rub_sum": float(pnl.sum()),
        "mean_net_ticks": float(net.mean()) if net.notna().any() else None,
        "mean_net_pnl_rub": float(pnl.mean()) if pnl.notna().any() else None,
        "hit_rate": float((pnl > 0).mean()) if pnl.notna().any() else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--target", default="NGM6")
    parser.add_argument("--plus1", default="NGN6")
    args = parser.parse_args()
    ensure_dirs()
    trades = read_csv(REPORTS / "paper_execution_trades.csv")
    heartbeat = read_csv(REPORTS / "paper_monitor_heartbeat.csv")
    if not trades.empty:
        trades = trades[
            (trades.get("run_id", pd.Series("", index=trades.index)).astype(str) == args.run_id)
            & (trades.get("target_contract", pd.Series("", index=trades.index)).astype(str) == args.target)
            & (trades.get("plus1_contract", pd.Series("", index=trades.index)).astype(str) == args.plus1)
        ].copy()
    if not heartbeat.empty:
        heartbeat = heartbeat[
            (heartbeat.get("run_id", pd.Series("", index=heartbeat.index)).astype(str) == args.run_id)
            & (heartbeat.get("target_contract", pd.Series("", index=heartbeat.index)).astype(str) == args.target)
            & (heartbeat.get("plus1_contract", pd.Series("", index=heartbeat.index)).astype(str) == args.plus1)
        ].copy()

    fill = trades.get("fill_status", pd.Series("", index=trades.index)).fillna("").astype(str) if not trades.empty else pd.Series(dtype=str)
    is_shadow = trades.get("is_shadow", pd.Series(False, index=trades.index)).fillna(False).astype(bool) if not trades.empty else pd.Series(dtype=bool)
    closed_shadow = trades[is_shadow & fill.eq("CLOSED")].copy() if not trades.empty else pd.DataFrame()
    total = agg(closed_shadow)
    by_policy = {}
    for policy in ["market_now", "passive_mid_or_better", "wait_5s_market", "wait_30s_market"]:
        subset = closed_shadow[closed_shadow.get("execution_policy", pd.Series("", index=closed_shadow.index)).astype(str) == policy] if not closed_shadow.empty else closed_shadow
        by_policy[policy] = agg(subset)

    passive = trades[trades.get("execution_policy", pd.Series("", index=trades.index)).astype(str) == "passive_mid_or_better"] if not trades.empty else pd.DataFrame()
    passive_fill_rate = None
    if not passive.empty:
        passive_fill_rate = float(passive.get("fill_status", pd.Series("", index=passive.index)).astype(str).eq("OPEN").sum() + passive.get("fill_status", pd.Series("", index=passive.index)).astype(str).eq("CLOSED").sum()) / float(len(passive))

    reasons = trades.get("skip_reason", pd.Series("", index=trades.index)).fillna("").astype(str).tolist() if not trades.empty else []
    reason_counts = Counter(x for x in reasons if x and x not in {"nan", "None"})
    target_spread = pd.to_numeric(heartbeat.get("target_spread_ticks", pd.Series(dtype=float)), errors="coerce") if not heartbeat.empty else pd.Series(dtype=float)
    plus1_spread = pd.to_numeric(heartbeat.get("plus1_spread_ticks", pd.Series(dtype=float)), errors="coerce") if not heartbeat.empty else pd.Series(dtype=float)
    exit_spread_ge4 = 0
    if not closed_shadow.empty and {"exit_bid", "exit_ask"}.issubset(closed_shadow.columns):
        exit_spread_ge4 = int(((pd.to_numeric(closed_shadow["exit_ask"], errors="coerce") - pd.to_numeric(closed_shadow["exit_bid"], errors="coerce")) / 0.001 >= 4).sum())

    market_now = by_policy["market_now"]
    execution_edge_alive = bool(
        total["count"] >= 10
        and total["net_ticks_sum"] > 0
        and (passive_fill_rate is not None and passive_fill_rate >= 0.5)
        and market_now["net_ticks_sum"] > 0
        and exit_spread_ge4 < max(1, total["count"] // 3)
    )

    rows = []
    rows.append({"metric": "closed_shadow_trades_count", "value": total["count"]})
    rows.append({"metric": "net_ticks_sum", "value": total["net_ticks_sum"]})
    rows.append({"metric": "net_pnl_rub_sum", "value": total["net_pnl_rub_sum"]})
    rows.append({"metric": "mean_net_ticks", "value": total["mean_net_ticks"]})
    rows.append({"metric": "hit_rate", "value": total["hit_rate"]})
    rows.append({"metric": "passive_fill_rate", "value": passive_fill_rate})
    rows.append({"metric": "missing_bid_ask_after_wait_count", "value": reason_counts.get("missing_bid_ask_after_wait", 0)})
    rows.append({"metric": "stale_orderbook_count", "value": reason_counts.get("stale_orderbook", 0)})
    rows.append({"metric": "spread_too_wide_count", "value": reason_counts.get("spread_too_wide", 0)})
    rows.append({"metric": "avg_target_spread_ticks", "value": float(target_spread.mean()) if target_spread.notna().any() else None})
    rows.append({"metric": "median_target_spread_ticks", "value": float(target_spread.median()) if target_spread.notna().any() else None})
    rows.append({"metric": "avg_plus1_spread_ticks", "value": float(plus1_spread.mean()) if plus1_spread.notna().any() else None})
    rows.append({"metric": "median_plus1_spread_ticks", "value": float(plus1_spread.median()) if plus1_spread.notna().any() else None})
    rows.append({"metric": "exit_spread_ge_4_ticks_count", "value": exit_spread_ge4})
    rows.append({"metric": "execution_edge_alive", "value": execution_edge_alive})
    for policy, values in by_policy.items():
        for key, value in values.items():
            rows.append({"metric": f"{policy}_{key}", "value": value})
    out_csv = REPORTS / "ng_paper_final_execution_report.csv"
    pd.DataFrame(rows).to_csv(out_csv, index=False)
    lines = [
        "# NG paper final execution report",
        "",
        f"- run_id: {args.run_id}",
        f"- pair: {args.target}/{args.plus1}",
        f"- closed shadow trades count: {total['count']}",
        f"- net_ticks_sum: {total['net_ticks_sum']}",
        f"- net_pnl_rub_sum: {total['net_pnl_rub_sum']}",
        f"- mean_net_ticks: {total['mean_net_ticks']}",
        f"- hit_rate: {total['hit_rate']}",
        f"- passive_fill_rate: {passive_fill_rate}",
        f"- missing_bid_ask_after_wait count: {reason_counts.get('missing_bid_ask_after_wait', 0)}",
        f"- stale_orderbook count: {reason_counts.get('stale_orderbook', 0)}",
        f"- spread_too_wide count: {reason_counts.get('spread_too_wide', 0)}",
        f"- avg/median target spread ticks: {rows[9]['value']} / {rows[10]['value']}",
        f"- avg/median plus1 spread ticks: {rows[11]['value']} / {rows[12]['value']}",
        f"- exit spread >= 4 ticks count: {exit_spread_ge4}",
        f"- execution_edge_alive: {execution_edge_alive}",
        "",
        "## By execution_policy",
    ]
    for policy, values in by_policy.items():
        lines.append(f"- {policy}: {values}")
    lines.append("")
    lines.append("Rule: do not call the edge alive unless closed_shadow_trades >= 10, net_ticks_sum > 0, passive fill-rate is acceptable, market_now is positive, and wide spreads are not frequent.")
    (REPORTS / "ng_paper_final_execution_report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
