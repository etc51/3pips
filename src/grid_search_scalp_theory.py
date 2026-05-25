from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "raw" / "tbank_1m" / "selected_1m.parquet"
REPORTS = ROOT / "reports"


@dataclass(frozen=True)
class Params:
    stop: int
    trail: int
    arm: int
    signal: int
    vol_mult: float
    cooldown: int
    confirm: bool
    max_hold: int


def prepare(df: pd.DataFrame, tick: float) -> pd.DataFrame:
    out = df.rename(columns={"time": "begin"}).copy()
    out["begin"] = pd.to_datetime(out["begin"], utc=True)
    out = out.sort_values(["secid", "begin"]).reset_index(drop=True)
    out["date"] = out["begin"].dt.date
    typical = (out["high"] + out["low"] + out["close"]) / 3
    out["vwap"] = (typical * out["volume"]).groupby([out["secid"], out["date"]]).cumsum() / out["volume"].groupby([out["secid"], out["date"]]).cumsum().replace(0, pd.NA)
    g = out.groupby("secid", group_keys=False)
    out["mom"] = g["close"].diff() / tick
    out["trend"] = (g["close"].rolling(3).mean().reset_index(level=0, drop=True) - g["close"].rolling(8).mean().reset_index(level=0, drop=True)) / tick
    out["avg_vol"] = g["volume"].shift(1).rolling(30).mean().reset_index(level=0, drop=True)
    out["range"] = (out["high"] - out["low"]) / tick
    out["avg_range"] = g["range"].shift(1).rolling(20).mean().reset_index(level=0, drop=True)
    out["prev_high"] = g["high"].shift(1).rolling(5).max().reset_index(level=0, drop=True)
    out["prev_low"] = g["low"].shift(1).rolling(5).min().reset_index(level=0, drop=True)
    return out.dropna().reset_index(drop=True)


def run_one(df: pd.DataFrame, p: Params, tick: float, tick_rub: float, fee_ticks: float) -> dict:
    trades = 0
    wins = 0
    net = 0.0
    max_dd = 0.0
    equity = 0.0
    peak = 0.0
    pending = None
    pos = None
    cooldown_until = -1
    gross_ticks_sum = 0.0
    for i, row in enumerate(df.itertuples(index=False)):
        if pos is not None:
            hold = i - pos["i"]
            exit_price = None
            direction = pos["dir"]
            if direction == 1:
                move = (row.high - pos["entry"]) / tick
                if move >= max(p.arm, fee_ticks + 1):
                    pos["stop"] = max(pos["stop"], row.high - p.trail * tick, pos["entry"] + (fee_ticks + 0.5) * tick)
                if row.low <= pos["stop"]:
                    exit_price = pos["stop"]
                elif hold >= p.max_hold:
                    exit_price = row.close
                if exit_price is not None:
                    ticks = (exit_price - pos["entry"]) / tick
                    pnl = (ticks - fee_ticks) * tick_rub
                    trades += 1
                    wins += pnl > 0
                    net += pnl
                    gross_ticks_sum += ticks
                    equity += pnl
                    peak = max(peak, equity)
                    max_dd = min(max_dd, equity - peak)
                    pos = None
                    cooldown_until = i + p.cooldown
            else:
                move = (pos["entry"] - row.low) / tick
                if move >= max(p.arm, fee_ticks + 1):
                    pos["stop"] = min(pos["stop"], row.low + p.trail * tick, pos["entry"] - (fee_ticks + 0.5) * tick)
                if row.high >= pos["stop"]:
                    exit_price = pos["stop"]
                elif hold >= p.max_hold:
                    exit_price = row.close
                if exit_price is not None:
                    ticks = (pos["entry"] - exit_price) / tick
                    pnl = (ticks - fee_ticks) * tick_rub
                    trades += 1
                    wins += pnl > 0
                    net += pnl
                    gross_ticks_sum += ticks
                    equity += pnl
                    peak = max(peak, equity)
                    max_dd = min(max_dd, equity - peak)
                    pos = None
                    cooldown_until = i + p.cooldown
            continue

        if i <= cooldown_until:
            pending = None
            continue

        long_signal = row.close >= row.vwap and row.mom >= p.signal and row.trend >= 1 and row.volume >= row.avg_vol * p.vol_mult and row.avg_range >= 3 and row.close >= row.prev_high
        short_signal = row.close <= row.vwap and row.mom <= -p.signal and row.trend <= -1 and row.volume >= row.avg_vol * p.vol_mult and row.avg_range >= 3 and row.close <= row.prev_low

        if p.confirm:
            if pending is not None:
                if pending == 1 and row.close >= row.open and row.close >= row.vwap:
                    pos = {"dir": 1, "entry": row.close, "stop": row.close - p.stop * tick, "i": i}
                    pending = None
                    continue
                if pending == -1 and row.close <= row.open and row.close <= row.vwap:
                    pos = {"dir": -1, "entry": row.close, "stop": row.close + p.stop * tick, "i": i}
                    pending = None
                    continue
                pending = None
            if long_signal:
                pending = 1
            elif short_signal:
                pending = -1
        else:
            if long_signal:
                pos = {"dir": 1, "entry": row.close, "stop": row.close - p.stop * tick, "i": i}
            elif short_signal:
                pos = {"dir": -1, "entry": row.close, "stop": row.close + p.stop * tick, "i": i}

    return {
        "trades": trades,
        "net": round(net, 2),
        "avg": round(net / trades, 2) if trades else 0.0,
        "win_rate": round(wins / trades * 100, 1) if trades else 0.0,
        "avg_gross_ticks": round(gross_ticks_sum / trades, 2) if trades else 0.0,
        "max_dd": round(max_dd, 2),
    }


def main() -> None:
    raw = pd.read_parquet(DATA)
    df = prepare(raw, 0.001)
    grid: list[Params] = []
    families = [
        (4, 4, 8),
        (5, 4, 10),
        (5, 5, 10),
        (6, 5, 12),
        (6, 6, 12),
        (8, 6, 15),
    ]
    for stop, trail, arm in families:
        for signal in [3, 4, 5]:
            for vol in [1.0, 1.5, 2.0]:
                for cooldown in [3, 5, 10]:
                    for confirm in [False, True]:
                        for max_hold in [10, 15]:
                            grid.append(Params(stop, trail, arm, signal, vol, cooldown, confirm, max_hold))
    rows = []
    for secid, one in df.groupby("secid"):
        one = one.reset_index(drop=True)
        for idx, params in enumerate(grid, start=1):
            result = run_one(one, params, 0.001, 7.1209, 1.5)
            if result["trades"] < 20:
                continue
            rows.append({"secid": secid, **params.__dict__, **result})
        print(f"{secid} done rows={len(rows)}", flush=True)
    out = pd.DataFrame(rows)
    out["score"] = out["net"] + out["max_dd"] * 0.5
    out = out.sort_values(["secid", "score"], ascending=[True, False])
    REPORTS.mkdir(exist_ok=True)
    out.to_csv(REPORTS / "scalp_grid_search_1m.csv", index=False)
    top = out.groupby("secid").head(20)
    top.to_csv(REPORTS / "scalp_grid_search_1m_top.csv", index=False)
    print(top.to_string(index=False))


if __name__ == "__main__":
    main()
