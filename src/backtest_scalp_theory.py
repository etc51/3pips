from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
DATA = ROOT / "data" / "raw" / "leadlag_ng_10m" / "moex_ng_10m_candles.parquet"
TBANK_1M = ROOT / "data" / "raw" / "tbank_1m" / "selected_1m.parquet"


@dataclass
class Profile:
    name: str
    stop_ticks: int
    trail_ticks: int
    trail_arm_ticks: int
    signal_ticks: int
    volume_mult: float
    vwap_buffer_ticks: int
    fee_ticks: float


PROFILES = [
    Profile("strict", 3, 3, 6, 3, 1.4, 1, 1.5),
    Profile("aggressive", 3, 3, 6, 2, 0.9, 0, 1.5),
]


def vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].clip(lower=0)
    grouped_value = (typical * vol).groupby(df["date"]).cumsum()
    grouped_volume = vol.groupby(df["date"]).cumsum()
    return grouped_value / grouped_volume.replace(0, pd.NA)


def prepare(df: pd.DataFrame, tick: float) -> pd.DataFrame:
    out = df.copy()
    out["begin"] = pd.to_datetime(out["begin"])
    out = out.sort_values(["secid", "begin"]).reset_index(drop=True)
    out["date"] = out["begin"].dt.date
    out["vwap"] = out.groupby("secid", group_keys=False).apply(vwap, include_groups=False)
    g = out.groupby("secid", group_keys=False)
    out["mom_ticks"] = g["close"].diff() / tick
    out["trend_ticks"] = (g["close"].rolling(2).mean().reset_index(level=0, drop=True) - g["close"].rolling(5).mean().reset_index(level=0, drop=True)) / tick
    out["avg_vol"] = g["volume"].shift(1).rolling(20).mean().reset_index(level=0, drop=True)
    out["prev_high"] = g["high"].shift(1).rolling(4).max().reset_index(level=0, drop=True)
    out["prev_low"] = g["low"].shift(1).rolling(4).min().reset_index(level=0, drop=True)
    return out.dropna(subset=["vwap", "mom_ticks", "trend_ticks", "avg_vol", "prev_high", "prev_low"])


def simulate_one(df: pd.DataFrame, profile: Profile, tick: float, tick_rub: float, cooldown_bars: int) -> list[dict]:
    trades: list[dict] = []
    cooldown_until = -1
    position = None
    for i, row in enumerate(df.itertuples(index=False)):
        if position is not None:
            direction = position["direction"]
            if direction == "long":
                move_ticks = (row.high - position["entry"]) / tick
                if move_ticks >= max(profile.trail_arm_ticks, profile.fee_ticks + 1):
                    new_stop = row.high - profile.trail_ticks * tick
                    breakeven_stop = position["entry"] + (profile.fee_ticks + 0.5) * tick
                    position["stop"] = max(position["stop"], new_stop, breakeven_stop)
                if row.low <= position["stop"]:
                    exit_price = position["stop"]
                    ticks = (exit_price - position["entry"]) / tick
                    trades.append({**position, "exit_time": row.begin, "exit": exit_price, "ticks": ticks, "net_rub": (ticks - profile.fee_ticks) * tick_rub})
                    position = None
                    cooldown_until = i + cooldown_bars
            else:
                move_ticks = (position["entry"] - row.low) / tick
                if move_ticks >= max(profile.trail_arm_ticks, profile.fee_ticks + 1):
                    new_stop = row.low + profile.trail_ticks * tick
                    breakeven_stop = position["entry"] - (profile.fee_ticks + 0.5) * tick
                    position["stop"] = min(position["stop"], new_stop, breakeven_stop)
                if row.high >= position["stop"]:
                    exit_price = position["stop"]
                    ticks = (position["entry"] - exit_price) / tick
                    trades.append({**position, "exit_time": row.begin, "exit": exit_price, "ticks": ticks, "net_rub": (ticks - profile.fee_ticks) * tick_rub})
                    position = None
                    cooldown_until = i + cooldown_bars
            continue

        if i <= cooldown_until:
            continue
        long_ok = (
            row.close >= row.vwap + profile.vwap_buffer_ticks * tick
            and row.mom_ticks >= profile.signal_ticks
            and row.trend_ticks >= 1
            and row.volume >= row.avg_vol * profile.volume_mult
            and row.close >= row.prev_high
        )
        short_ok = (
            row.close <= row.vwap - profile.vwap_buffer_ticks * tick
            and row.mom_ticks <= -profile.signal_ticks
            and row.trend_ticks <= -1
            and row.volume >= row.avg_vol * profile.volume_mult
            and row.close <= row.prev_low
        )
        if long_ok:
            position = {
                "profile": profile.name,
                "secid": row.secid,
                "entry_time": row.begin,
                "direction": "long",
                "entry": row.close,
                "stop": row.close - profile.stop_ticks * tick,
            }
        elif short_ok:
            position = {
                "profile": profile.name,
                "secid": row.secid,
                "entry_time": row.begin,
                "direction": "short",
                "entry": row.close,
                "stop": row.close + profile.stop_ticks * tick,
            }
    return trades


def summarize(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    out = (
        trades.groupby(["profile", "secid"])
        .agg(
            trades=("net_rub", "size"),
            net_rub=("net_rub", "sum"),
            avg_net=("net_rub", "mean"),
            win_rate=("net_rub", lambda s: (s > 0).mean()),
            avg_ticks=("ticks", "mean"),
            worst=("net_rub", "min"),
            best=("net_rub", "max"),
        )
        .reset_index()
    )
    out["net_rub"] = out["net_rub"].round(2)
    out["avg_net"] = out["avg_net"].round(2)
    out["win_rate"] = (out["win_rate"] * 100).round(1)
    out["avg_ticks"] = out["avg_ticks"].round(2)
    out["worst"] = out["worst"].round(2)
    out["best"] = out["best"].round(2)
    return out.sort_values(["profile", "net_rub"], ascending=[True, False])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["moex-ng-10m", "tbank-1m"], default="moex-ng-10m")
    parser.add_argument("--tick", type=float, default=0.001)
    parser.add_argument("--tick-rub", type=float, default=7.1209)
    parser.add_argument("--cooldown-bars", type=int, default=1)
    parser.add_argument("--from-date", default="2024-05-23")
    parser.add_argument("--to-date", default="2026-05-23")
    args = parser.parse_args()

    if args.source == "tbank-1m":
        raw = pd.read_parquet(TBANK_1M)
        raw = raw.rename(columns={"time": "begin"})
    else:
        raw = pd.read_parquet(DATA)
        raw = raw.rename(columns={"SECID": "secid"})
    raw["begin"] = pd.to_datetime(raw["begin"])
    raw = raw[(raw["begin"] >= args.from_date) & (raw["begin"] <= args.to_date)]
    raw = raw[raw["volume"] > 0]
    df = prepare(raw, args.tick)
    all_trades: list[dict] = []
    for _, one in df.groupby("secid"):
        one = one.sort_values("begin").reset_index(drop=True)
        if len(one) < 100:
            continue
        for profile in PROFILES:
            all_trades.extend(simulate_one(one, profile, args.tick, args.tick_rub, args.cooldown_bars))
    trades = pd.DataFrame(all_trades)
    REPORTS.mkdir(exist_ok=True)
    suffix = "tbank_1m" if args.source == "tbank-1m" else "moex_ng_10m"
    trades.to_csv(REPORTS / f"scalp_theory_{suffix}_trades.csv", index=False)
    summary = summarize(trades)
    summary.to_csv(REPORTS / f"scalp_theory_{suffix}_summary.csv", index=False)
    print(summary.to_string(index=False))
    if not trades.empty:
        total = trades.groupby("profile")["net_rub"].agg(["size", "sum", "mean"])
        print()
        print(total.round(2).to_string())


if __name__ == "__main__":
    main()
