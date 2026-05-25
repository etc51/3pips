from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from leadlag_ng_moex import REPORTS, ROOT, ensure_dirs


TRADES_PATH = REPORTS / "paper_execution_trades.csv"
SUMMARY_PATH = REPORTS / "paper_execution_summary.csv"
BY_DAY_PATH = REPORTS / "paper_execution_by_day.csv"
DAILY_MD_PATH = REPORTS / "paper_execution_daily_summary.md"
OPEN_POSITIONS_PATH = REPORTS / "paper_open_positions.json"
HEARTBEAT_PATH = REPORTS / "paper_monitor_heartbeat.csv"
SNAPSHOTS_PATH = REPORTS / "live_orderbook_snapshots.csv"
PAUSE_FLAG_PATH = REPORTS / "paper_pause_new_entries.flag"
RECONCILIATION_PATH = REPORTS / "paper_position_reconciliation.csv"
RECONCILIATION_MD_PATH = REPORTS / "paper_position_reconciliation_summary.md"
BACKUP_ROOT = REPORTS / "paper_state_backups"

MIN_STEP = 0.001
TICK_VALUE_USD = 0.1


def read_csv(path: Path, date_cols: list[str] | None = None) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    df = pd.read_csv(path)
    for col in date_cols or []:
        if col in df:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def backup_state() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = BACKUP_ROOT / f"paper_state_backup_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    for path in [OPEN_POSITIONS_PATH, TRADES_PATH, SUMMARY_PATH, BY_DAY_PATH, SNAPSHOTS_PATH, HEARTBEAT_PATH]:
        if path.exists():
            shutil.copy2(path, out / path.name)
    return out


def load_open_positions() -> list[dict]:
    if not OPEN_POSITIONS_PATH.exists():
        return []
    raw = json.loads(OPEN_POSITIONS_PATH.read_text(encoding="utf-8") or "[]")
    if raw is None:
        return []
    return raw if isinstance(raw, list) else [raw]


def market_signal_id(row: pd.Series | dict) -> str:
    return str(row.get("signal_id") or "")


def execution_decision_id(row: pd.Series | dict) -> str:
    sid = market_signal_id(row)
    policy = str(row.get("execution_policy") or "none")
    return str(row.get("execution_decision_id") or f"{sid}|{policy}")


def latest_book() -> dict:
    hb = read_csv(HEARTBEAT_PATH)
    if not hb.empty:
        row = hb.tail(1).iloc[0].to_dict()
        return {
            "bid": pd.to_numeric(row.get("target_bid"), errors="coerce"),
            "ask": pd.to_numeric(row.get("target_ask"), errors="coerce"),
            "age": pd.to_numeric(row.get("target_age_seconds"), errors="coerce"),
            "source": "heartbeat",
        }
    sn = read_csv(SNAPSHOTS_PATH)
    if not sn.empty:
        row = sn.tail(1).iloc[0].to_dict()
        return {
            "bid": pd.to_numeric(row.get("bid_target"), errors="coerce"),
            "ask": pd.to_numeric(row.get("ask_target"), errors="coerce"),
            "age": pd.to_numeric(row.get("target_age_seconds"), errors="coerce"),
            "source": "snapshot",
        }
    return {"bid": np.nan, "ask": np.nan, "age": np.nan, "source": "missing"}


def pnl_fields(row: pd.Series | dict, exit_price: float) -> dict:
    direction = int(row.get("signal_direction", 0) or 0)
    entry_price = pd.to_numeric(row.get("entry_price"), errors="coerce")
    usd_rub = pd.to_numeric(row.get("usd_rub_rate"), errors="coerce")
    margin = pd.to_numeric(row.get("initial_margin_rub"), errors="coerce")
    spread_paid = pd.to_numeric(row.get("spread_paid_ticks", 0.0), errors="coerce")
    slippage = pd.to_numeric(row.get("slippage_ticks", 0.0), errors="coerce")
    raw_ticks = (exit_price - entry_price) / MIN_STEP
    signed_ticks = direction * raw_ticks
    tick_value_rub = TICK_VALUE_USD * usd_rub
    gross = signed_ticks * tick_value_rub
    net = gross - (spread_paid + slippage) * tick_value_rub - 2.0
    return {
        "exit_time": pd.Timestamp.now(),
        "exit_price": exit_price,
        "gross_ticks": signed_ticks,
        "net_ticks": net / tick_value_rub if tick_value_rub else np.nan,
        "gross_pnl_rub": gross,
        "net_pnl_rub": net,
        "return_on_go": net / margin if margin else np.nan,
        "fill_status": "CLOSED",
        "exit_reason": "overdue_paper_repair",
        "include_in_primary_stats": False,
        "include_in_diagnostics": True,
        "real_order_sent": False,
    }


def summarize_after_reconcile(trades: pd.DataFrame, open_positions: list[dict], recon: pd.DataFrame) -> None:
    if trades.empty:
        pd.DataFrame().to_csv(SUMMARY_PATH, index=False)
        pd.DataFrame().to_csv(BY_DAY_PATH, index=False)
        return
    include_primary = trades.get("include_in_primary_stats", pd.Series(True, index=trades.index)).fillna(True).astype(bool)
    closed = trades[trades.get("fill_status", pd.Series("", index=trades.index)).astype(str).eq("CLOSED")].copy()
    primary_closed = closed[include_primary.loc[closed.index]] if not closed.empty else closed
    diagnostic_closed = closed[~include_primary.loc[closed.index]] if not closed.empty else closed
    is_shadow = trades.get("is_shadow", pd.Series(False, index=trades.index)).fillna(False).astype(bool)
    reason = trades.get("skip_reason", pd.Series("", index=trades.index)).fillna("")
    overdue_count = 0
    now = pd.Timestamp.now()
    for pos in open_positions:
        planned = pd.to_datetime(pos.get("planned_exit_time"), errors="coerce")
        if pd.notna(planned) and planned < now:
            overdue_count += 1
    summary = {
        "execution_policy": "__overall__",
        "paper_new_entries_paused": PAUSE_FLAG_PATH.exists(),
        "open_positions_count": len(open_positions),
        "overdue_positions_count": overdue_count,
        "repaired_positions_count": int((recon["action"] == "paper_closed_repaired").sum()) if not recon.empty else 0,
        "state_cleanup_positions_count": int((recon["action"] == "removed_from_open_positions").sum()) if not recon.empty else 0,
        "include_in_primary_stats_closed_trades": len(primary_closed),
        "include_in_diagnostics_closed_trades": len(diagnostic_closed),
        "closed_trades": len(closed),
        "strategy_opened_trades": int((~is_shadow & trades["fill_status"].astype(str).eq("OPEN")).sum()),
        "strategy_closed_trades": int((~is_shadow.loc[closed.index]).sum()) if not closed.empty else 0,
        "shadow_opened_trades": int((is_shadow & trades["fill_status"].astype(str).eq("OPEN")).sum()),
        "shadow_closed_trades": int((is_shadow.loc[closed.index]).sum()) if not closed.empty else 0,
        "net_pnl_rub": float(primary_closed.get("net_pnl_rub", pd.Series(dtype=float)).sum()) if not primary_closed.empty else 0.0,
        "diagnostic_repair_net_pnl_rub": float(diagnostic_closed.get("net_pnl_rub", pd.Series(dtype=float)).sum()) if not diagnostic_closed.empty else 0.0,
        "below_threshold_signals": int((reason == "below_threshold").sum()),
    }
    pd.DataFrame([summary]).to_csv(SUMMARY_PATH, index=False)
    if not closed.empty and "exit_time" in closed:
        closed["day"] = pd.to_datetime(closed["exit_time"], errors="coerce").dt.date.astype(str)
        by_day = closed.groupby("day", as_index=False).agg(
            closed_trades=("fill_status", "size"),
            net_pnl_rub=("net_pnl_rub", "sum"),
            diagnostic_closed=("include_in_primary_stats", lambda x: int((~x.fillna(True).astype(bool)).sum())),
        )
    else:
        by_day = pd.DataFrame(columns=["day", "closed_trades", "net_pnl_rub", "diagnostic_closed"])
    by_day.to_csv(BY_DAY_PATH, index=False)
    lines = [
        "# Paper execution daily summary",
        "",
        f"Updated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"- paper_new_entries_paused: {PAUSE_FLAG_PATH.exists()}",
        f"- open_positions_count: {len(open_positions)}",
        f"- overdue_positions_count: {overdue_count}",
        f"- repaired_positions_count: {summary['repaired_positions_count']}",
        f"- state_cleanup_positions_count: {summary['state_cleanup_positions_count']}",
        f"- include_in_primary_stats_closed_trades: {summary['include_in_primary_stats_closed_trades']}",
        f"- include_in_diagnostics_closed_trades: {summary['include_in_diagnostics_closed_trades']}",
        f"- primary net PnL RUB: {summary['net_pnl_rub']:.2f}",
        f"- diagnostic repair net PnL RUB: {summary['diagnostic_repair_net_pnl_rub']:.2f}",
        "",
        "Paper only. No real orders were sent.",
    ]
    DAILY_MD_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper-only", action="store_true", required=True)
    parser.add_argument("--rollover-close-all", action="store_true")
    parser.add_argument("--rollover-exit-reason", default="overdue_paper_repair")
    args = parser.parse_args()
    if not args.paper_only:
        raise SystemExit("This reconciliation script is paper-only.")
    ensure_dirs()
    PAUSE_FLAG_PATH.write_text(f"paused {datetime.now().isoformat()} paper-state-reconciliation\n", encoding="utf-8")
    backup_dir = backup_state()
    open_positions = load_open_positions()
    trades = read_csv(TRADES_PATH, ["timestamp_signal", "entry_time", "planned_exit_time", "exit_time"])
    if trades.empty:
        trades = pd.DataFrame()
    if "execution_decision_id" not in trades.columns and not trades.empty:
        trades["execution_decision_id"] = trades.apply(execution_decision_id, axis=1)
    if "include_in_primary_stats" not in trades.columns and not trades.empty:
        trades["include_in_primary_stats"] = True
    if "include_in_diagnostics" not in trades.columns and not trades.empty:
        trades["include_in_diagnostics"] = True
    if "real_order_sent" not in trades.columns and not trades.empty:
        trades["real_order_sent"] = False
    latest = latest_book()
    now = pd.Timestamp.now()
    remaining: list[dict] = []
    actions: list[dict] = []
    for pos in open_positions:
        sid = market_signal_id(pos)
        policy = str(pos.get("execution_policy") or "none")
        decision_id = execution_decision_id(pos)
        planned = pd.to_datetime(pos.get("planned_exit_time"), errors="coerce")
        is_overdue = bool(pd.notna(planned) and planned < now)
        matching = trades[trades["execution_decision_id"].astype(str).eq(decision_id)] if not trades.empty else pd.DataFrame()
        closed_match = matching[matching.get("fill_status", pd.Series(dtype=str)).astype(str).eq("CLOSED")] if not matching.empty else pd.DataFrame()
        open_match = matching[matching.get("fill_status", pd.Series(dtype=str)).astype(str).eq("OPEN")] if not matching.empty else pd.DataFrame()
        unfilled_match = matching[matching.get("fill_status", pd.Series(dtype=str)).astype(str).isin(["UNFILLED", "missing_bid_ask_after_wait"])] if not matching.empty else pd.DataFrame()
        action = "kept_open"
        reason = "not_overdue"
        old_status = str(pos.get("fill_status", "OPEN"))
        new_status = old_status
        exit_price = np.nan
        if not closed_match.empty:
            action = "removed_from_open_positions"
            reason = "already_closed_state_cleanup"
            new_status = "CLOSED"
        elif not unfilled_match.empty and open_match.empty:
            action = "removed_from_open_positions"
            reason = "unfilled_should_not_be_open"
            new_status = str(unfilled_match.iloc[-1].get("fill_status"))
        elif is_overdue or args.rollover_close_all:
            direction = int(pos.get("signal_direction", 0) or 0)
            can_close = pd.notna(latest["bid"]) and pd.notna(latest["ask"]) and pd.notna(latest["age"]) and float(latest["age"]) <= 30
            if can_close:
                exit_price = float(latest["bid"] if direction > 0 else latest["ask"])
                fields = pnl_fields(pos, exit_price)
                fields["exit_reason"] = args.rollover_exit_reason if args.rollover_close_all else "overdue_paper_repair"
                if not open_match.empty:
                    idx = open_match.index[-1]
                    for key, value in fields.items():
                        trades.loc[idx, key] = value
                else:
                    new_row = {**pos, **fields, "execution_decision_id": decision_id}
                    trades = pd.concat([trades, pd.DataFrame([new_row])], ignore_index=True, sort=False)
                action = "paper_closed_repaired"
                reason = fields["exit_reason"]
                new_status = "CLOSED"
            else:
                remaining.append(pos)
                action = "kept_open"
                reason = "cannot_repair_no_fresh_orderbook"
        else:
            remaining.append(pos)
        actions.append(
            {
                "run_ts": datetime.now().isoformat(timespec="seconds"),
                "market_signal_id": sid,
                "execution_policy": policy,
                "execution_decision_id": decision_id,
                "target_contract": pos.get("target_contract"),
                "plus1_contract": pos.get("plus1_contract"),
                "entry_time": pos.get("entry_time"),
                "planned_exit_time": pos.get("planned_exit_time"),
                "is_overdue": is_overdue,
                "action": action,
                "reason": reason,
                "old_status": old_status,
                "new_status": new_status,
                "exit_price_used": exit_price,
                "exit_bid": latest["bid"],
                "exit_ask": latest["ask"],
                "exit_orderbook_age_seconds": latest["age"],
                "real_order_sent": False,
            }
        )
    trades.to_csv(TRADES_PATH, index=False)
    OPEN_POSITIONS_PATH.write_text(json.dumps(remaining, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    recon = pd.DataFrame(actions)
    recon.to_csv(RECONCILIATION_PATH, index=False)
    summarize_after_reconcile(trades, remaining, recon)
    overdue_after = 0
    for pos in remaining:
        planned = pd.to_datetime(pos.get("planned_exit_time"), errors="coerce")
        if pd.notna(planned) and planned < pd.Timestamp.now():
            overdue_after += 1
    md = [
        "# Paper position reconciliation",
        "",
        f"- backup_dir: `{backup_dir}`",
        f"- positions before: {len(open_positions)}",
        f"- overdue before: {int(recon['is_overdue'].sum()) if not recon.empty else 0}",
        f"- already_closed_state_cleanup count: {int((recon['reason'] == 'already_closed_state_cleanup').sum()) if not recon.empty else 0}",
        f"- unfilled_removed count: {int((recon['reason'] == 'unfilled_should_not_be_open').sum()) if not recon.empty else 0}",
        f"- overdue_paper_repair count: {int((recon['reason'] == 'overdue_paper_repair').sum()) if not recon.empty else 0}",
        f"- rollover_paper_close count: {int(recon['reason'].astype(str).str.startswith('rollover_paper_close').sum()) if not recon.empty else 0}",
        f"- positions after: {len(remaining)}",
        f"- overdue after: {overdue_after}",
        f"- new entries paused: {PAUSE_FLAG_PATH.exists()}",
        "- real_order_sent count: 0",
        "",
        "Recommendation: keep paper monitor in close-only mode until open paper positions are closed or reviewed.",
    ]
    RECONCILIATION_MD_PATH.write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
