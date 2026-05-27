from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
DATA_DIR = Path(r"D:\3_pips_x2_project\historical_data\C_documents_men_i_trend_data_raw\tbank_1m")
OUT_DIR = REPORTS / "paper_runs" / "v7_live_20260525" / "analysis" / "four_layer_backtest"
BASE_PROFILE_PATH = REPORTS / "futures_scalp_profiles_v7_paper_20260525.csv"
GPT_PROFILE_PATH = REPORTS / "futures_scalp_profiles_v7_paper_20260525_gpt_shadow_params.csv"


@dataclass
class Profile:
    ticker: str
    family: str
    stop_ticks: int
    trail_ticks: int
    trail_arm_ticks: int
    allowed_direction: str
    tick: float
    tick_rub: float
    signal_family: str = "online_current"
    entry_timing: str = "next_bar_open"
    session_filter: str = "all_available_data"
    momentum_pct: float = 0.0
    momentum_ticks: float = 0.0
    breakout_lookback: int = 6
    trend_fast: int = 3
    trend_slow: int = 8
    volume_multiplier: float = 1.0
    volume_window: int = 20
    vwap_mode: str = "disabled"
    vwap_buffer_pct: float = 0.0
    cooldown_minutes: int = 0
    max_hold_minutes: int = 0


@dataclass(frozen=True)
class Layer:
    name: str
    kind: str
    threshold_mult: float = 1.0
    volume_mult: float = 1.0
    trend_tolerance_ticks: float = 0.0
    trend_tolerance_mult: float = 0.0
    breakout_tolerance_ticks: float = 0.0
    vwap_buffer_mult: float = 1.0
    range_cost_mult: float = 1.0


LAYERS = [
    Layer("current_actual", "current"),
    Layer("gpt_full", "gpt", 1.0, 1.0, 0.0, 0.0, 0.0, 1.0, 1.0),
    Layer("gpt_relaxed", "gpt", 0.75, 0.75, 1.0, 0.20, 1.0, 0.50, 0.85),
    Layer("gpt_loose", "gpt", 0.55, 0.50, 2.0, 0.35, 2.0, 0.0, 0.70),
]


def as_int(value: object, default: int = 0) -> int:
    try:
        if pd.isna(value):
            return default
        return int(float(value))
    except Exception:
        return default


def as_float(value: object, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def round_step(value: float, tick: float) -> float:
    if tick <= 0:
        return value
    return round(round(value / tick) * tick, 10)


def family_of(ticker: str) -> str:
    ticker = str(ticker)
    if ticker.endswith("perpA"):
        return ticker.replace("perpA", "")
    letters = "".join(ch for ch in ticker if ch.isalpha())
    return letters[:2] if len(letters) > 1 else ticker[:2]


def load_profiles() -> dict[str, Profile]:
    base = pd.read_csv(BASE_PROFILE_PATH)
    gpt = pd.read_csv(GPT_PROFILE_PATH)
    gpt_by_ticker = {str(r["ticker"]).upper(): r for _, r in gpt.iterrows()}
    profiles: dict[str, Profile] = {}
    for _, row in base.iterrows():
        ticker = str(row["ticker"]).upper()
        overlay = gpt_by_ticker.get(ticker)
        p = Profile(
            ticker=ticker,
            family=str(row.get("v7_family") or family_of(ticker)),
            stop_ticks=max(1, as_int(row.get("stop_ticks"), 1)),
            trail_ticks=max(1, as_int(row.get("trail_ticks"), 1)),
            trail_arm_ticks=max(1, as_int(row.get("trail_arm_ticks"), 1)),
            allowed_direction=str(row.get("v7_direction") or row.get("allowed_direction") or "both").lower(),
            tick=0.0,
            tick_rub=max(0.0, as_float(row.get("tick_rub"), 0.0)),
        )
        if overlay is not None:
            p.signal_family = str(overlay.get("signal_family") or p.signal_family)
            p.entry_timing = str(overlay.get("entry_timing") or p.entry_timing)
            p.session_filter = str(overlay.get("session_filter") or p.session_filter)
            p.momentum_pct = as_float(overlay.get("momentum_pct"), 0.0)
            p.momentum_ticks = as_float(overlay.get("momentum_ticks"), 0.0)
            p.breakout_lookback = max(2, as_int(overlay.get("breakout_lookback"), p.breakout_lookback))
            p.trend_fast = max(1, as_int(overlay.get("trend_fast"), p.trend_fast))
            p.trend_slow = max(p.trend_fast + 1, as_int(overlay.get("trend_slow"), p.trend_slow))
            p.volume_multiplier = as_float(overlay.get("volume_multiplier"), p.volume_multiplier)
            p.volume_window = max(2, as_int(overlay.get("volume_window"), p.volume_window))
            p.vwap_mode = str(overlay.get("vwap_mode") or p.vwap_mode)
            p.vwap_buffer_pct = as_float(overlay.get("vwap_buffer_pct"), 0.0)
            p.cooldown_minutes = max(0, as_int(overlay.get("cooldown_minutes"), 0))
            p.max_hold_minutes = max(0, as_int(overlay.get("max_hold_minutes"), 0))
        profiles[ticker] = p

    for spec_path in (REPORTS / "paper_runs" / "v7_live_20260525").glob("*_instrument_specs.csv"):
        df = pd.read_csv(spec_path)
        for _, row in df.iterrows():
            ticker = str(row.get("ticker") or "").upper()
            if ticker in profiles:
                profiles[ticker].tick = as_float(row.get("tick"), profiles[ticker].tick)
                profiles[ticker].tick_rub = as_float(row.get("tick_rub"), profiles[ticker].tick_rub)
    specs = pd.read_csv(REPORTS / "all_futures_instrument_specs.csv")
    for _, row in specs.iterrows():
        ticker = str(row.get("ticker") or "").upper()
        if ticker in profiles and profiles[ticker].tick <= 0:
            profiles[ticker].tick = as_float(row.get("tick"), 0.0)
            profiles[ticker].tick_rub = as_float(row.get("tick_rub"), profiles[ticker].tick_rub)
    return {k: v for k, v in profiles.items() if v.tick > 0 and v.tick_rub > 0}


def load_candles(ticker: str) -> pd.DataFrame:
    path = DATA_DIR / f"{ticker}_1m.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    if df.empty:
        return df
    df["time"] = pd.to_datetime(df["time"], errors="coerce", utc=True).dt.tz_convert("Europe/Moscow").dt.tz_localize(None)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["time", "open", "high", "low", "close"]).sort_values("time").reset_index(drop=True)
    return df


def add_features(df: pd.DataFrame, p: Profile) -> pd.DataFrame:
    out = df.copy()
    out["prev_close"] = out["close"].shift(1)
    out["mom"] = (out["close"] - out["prev_close"]) / p.tick
    out["avgv20"] = out["volume"].shift(1).rolling(20, min_periods=2).mean()
    out["fast2"] = out["close"].rolling(2, min_periods=2).mean()
    out["slow5"] = out["close"].rolling(5, min_periods=5).mean()
    out["trend_current"] = (out["fast2"] - out["slow5"]) / p.tick
    for n in sorted({3, 4, 5, 6, 8, 13, 21, 34, 55, 89, 120, 144, p.breakout_lookback, p.volume_window, p.trend_fast, p.trend_slow}):
        if n <= 0:
            continue
        out[f"high_{n}"] = out["high"].shift(1).rolling(n, min_periods=2).max()
        out[f"low_{n}"] = out["low"].shift(1).rolling(n, min_periods=2).min()
        out[f"avgv_{n}"] = out["volume"].shift(1).rolling(n, min_periods=2).mean()
        out[f"ma_{n}"] = out["close"].rolling(n, min_periods=2).mean()
        pv = (out["close"] * out["volume"]).shift(1).rolling(n, min_periods=2).sum()
        vv = out["volume"].shift(1).rolling(n, min_periods=2).sum()
        out[f"vwap_{n}"] = pv / vv.replace(0, math.nan)
    pv_all = (out["close"] * out["volume"]).groupby(out["time"].dt.date).cumsum()
    vv_all = out["volume"].groupby(out["time"].dt.date).cumsum()
    out["vwap_session"] = pv_all / vv_all.replace(0, math.nan)
    return out


def fee_side(price: float, p: Profile, qty: int = 1) -> float:
    if price <= 0 or p.tick <= 0:
        return 0.0
    notional = price / p.tick * p.tick_rub
    return notional * qty * 0.00025


def session_allowed(mode: str, ts: pd.Timestamp) -> bool:
    mode = (mode or "all_available_data").lower()
    if mode in {"", "all", "all_available_data"}:
        return True
    seconds = ts.hour * 3600 + ts.minute * 60 + ts.second
    start = 10 * 3600
    end = 18 * 3600 + 45 * 60
    if mode == "exclude_first_last_10_minutes":
        start += 10 * 60
        end -= 10 * 60
    return start <= seconds < end


def allowed_direction(p: Profile, direction: str) -> bool:
    return p.allowed_direction in {"", "both", direction}


def current_signal(row: pd.Series, p: Profile, aggressive: bool) -> str | None:
    lookback = 4 if aggressive else 6
    vol_mult = 0.7 if aggressive else 1.15
    vwap_buffer = 0 if aggressive else 1
    trend_need = 0.5 if aggressive else 1.0
    signal_need = 1 if aggressive else 2
    max_fee_to_stop = 0.55 if aggressive else 0.40
    high = row.get(f"high_{lookback}")
    low = row.get(f"low_{lookback}")
    avgv = row.get("avgv20")
    if pd.isna(high) or pd.isna(low) or pd.isna(avgv) or avgv <= 0:
        return None
    price = float(row["close"])
    vwap = row.get("vwap_session")
    if pd.isna(vwap):
        vwap = price
    mom = float(row.get("mom") or 0.0)
    trend = float(row.get("trend_current") or 0.0)
    fee_t = (2 * fee_side(price, p) / p.tick_rub) if p.tick_rub else 999.0
    recent_range = (float(row["high"]) - float(row["low"])) / p.tick
    if fee_t > p.stop_ticks * max_fee_to_stop:
        return None
    if recent_range < fee_t + 2:
        return None
    vol_ok = float(row.get("volume") or 0.0) >= float(avgv) * vol_mult
    long_ok = price >= float(vwap) + vwap_buffer * p.tick and trend >= trend_need and mom >= signal_need and price >= float(high) and vol_ok
    short_ok = price <= float(vwap) - vwap_buffer * p.tick and trend <= -trend_need and mom <= -signal_need and price <= float(low) and vol_ok
    if long_ok and allowed_direction(p, "long"):
        return "long"
    if short_ok and allowed_direction(p, "short"):
        return "short"
    return None


def gpt_signal(row: pd.Series, p: Profile, layer: Layer) -> str | None:
    if not session_allowed(p.session_filter, row["time"]):
        return None
    lookback = max(2, p.breakout_lookback)
    high = row.get(f"high_{lookback}")
    low = row.get(f"low_{lookback}")
    if pd.isna(high) or pd.isna(low):
        return None
    price = float(row["close"])
    threshold = max(float(p.momentum_ticks or 0.0), abs(float(p.momentum_pct or 0.0) * price / p.tick))
    if threshold <= 0:
        threshold = 2.0
    threshold *= max(0.05, layer.threshold_mult)
    mom = float(row.get("mom") or 0.0)
    fast = row.get(f"ma_{max(1, p.trend_fast)}")
    slow = row.get(f"ma_{max(2, p.trend_slow)}")
    if pd.isna(fast) or pd.isna(slow):
        return None
    trend = (float(fast) - float(slow)) / p.tick
    avgv = row.get(f"avgv_{max(2, p.volume_window)}")
    if pd.isna(avgv):
        return None
    vol_mult = max(0.0, float(p.volume_multiplier) * layer.volume_mult)
    if vol_mult > 0 and not (avgv > 0 and float(row.get("volume") or 0.0) >= float(avgv) * vol_mult):
        return None
    mode = (p.vwap_mode or "disabled").lower()
    if mode == "rolling20":
        vwap = row.get("vwap_20")
    elif mode == "rolling60":
        vwap = row.get("vwap_60")
    elif mode == "session":
        vwap = row.get("vwap_session")
    else:
        vwap = math.nan
    vwap_buffer_ticks = abs(float(p.vwap_buffer_pct or 0.0) * price / p.tick) * max(0.0, layer.vwap_buffer_mult)
    vwap_long_ok = mode == "disabled" or (not pd.isna(vwap) and (price - float(vwap)) / p.tick >= vwap_buffer_ticks)
    vwap_short_ok = mode == "disabled" or (not pd.isna(vwap) and (float(vwap) - price) / p.tick >= vwap_buffer_ticks)
    break_tol = max(0.0, layer.breakout_tolerance_ticks) * p.tick
    long_break = price >= float(high) - break_tol
    short_break = price <= float(low) + break_tol
    long_mom = mom >= threshold
    short_mom = mom <= -threshold
    trend_tol = max(layer.trend_tolerance_ticks, abs(threshold) * layer.trend_tolerance_mult)
    long_trend = trend >= -trend_tol
    short_trend = trend <= trend_tol
    family = (p.signal_family or "pure_trailing_after_impulse").lower()
    recent_range_ticks = (float(row["high"]) - float(row["low"])) / p.tick
    fee_t = (2 * fee_side(price, p) / p.tick_rub) if p.tick_rub else 999.0
    if family == "momentum_breakout":
        long_ok = long_mom and long_break and long_trend and vwap_long_ok
        short_ok = short_mom and short_break and short_trend and vwap_short_ok
    elif family == "vwap_impulse":
        long_ok = long_mom and long_trend and vwap_long_ok
        short_ok = short_mom and short_trend and vwap_short_ok
    elif family == "range_expansion":
        min_range = max(threshold, (fee_t + 1.0 + 2.0) * max(0.1, layer.range_cost_mult))
        long_ok = recent_range_ticks >= min_range and long_break and mom > 0 and vwap_long_ok
        short_ok = recent_range_ticks >= min_range and short_break and mom < 0 and vwap_short_ok
    elif family == "trend_pullback":
        long_ok = long_trend and mom >= max(1.0, threshold * 0.5) and vwap_long_ok
        short_ok = short_trend and mom <= -max(1.0, threshold * 0.5) and vwap_short_ok
    else:
        long_ok = long_mom and long_trend and vwap_long_ok
        short_ok = short_mom and short_trend and vwap_short_ok
    allowed = (p.allowed_direction or "both").lower()
    if long_ok and allowed in {"long", "both"}:
        return "long"
    if short_ok and allowed in {"short", "both"}:
        return "short"
    return None


def pnl(entry: float, exit_price: float, direction: str, p: Profile, qty: int, slippage_ticks: int) -> tuple[float, float, float]:
    sign = 1 if direction == "long" else -1
    ticks = sign * (exit_price - entry) / p.tick
    gross_ticks = ticks - float(slippage_ticks)
    gross = gross_ticks * p.tick_rub * qty
    fees = (fee_side(entry, p, qty) + fee_side(exit_price, p, qty))
    return gross_ticks, gross, gross - fees


def stop_price(entry: float, direction: str, stop_ticks: int, p: Profile) -> float:
    raw = entry - stop_ticks * p.tick if direction == "long" else entry + stop_ticks * p.tick
    return round_step(raw, p.tick)


def update_trailing(pos: dict, favorable_price: float, p: Profile, fee_t: float) -> None:
    direction = pos["direction"]
    entry = pos["entry_price"]
    if direction == "long":
        pos["best_price"] = max(pos["best_price"], favorable_price)
        move = (pos["best_price"] - entry) / p.tick
        if move >= max(p.trail_arm_ticks, fee_t + 1.0):
            trailed = round_step(pos["best_price"] - p.trail_ticks * p.tick, p.tick)
            min_net = round_step(entry + (fee_t + 0.5) * p.tick, p.tick)
            pos["stop_price"] = max(pos["stop_price"], trailed, min_net)
    else:
        pos["best_price"] = min(pos["best_price"], favorable_price)
        move = (entry - pos["best_price"]) / p.tick
        if move >= max(p.trail_arm_ticks, fee_t + 1.0):
            trailed = round_step(pos["best_price"] + p.trail_ticks * p.tick, p.tick)
            min_net = round_step(entry - (fee_t + 0.5) * p.tick, p.tick)
            pos["stop_price"] = min(pos["stop_price"], trailed, min_net)


def exit_on_bar(pos: dict, row: pd.Series, p: Profile, layer: Layer) -> tuple[float | None, str]:
    direction = pos["direction"]
    fee_t = (2 * fee_side(pos["entry_price"], p) / p.tick_rub) if p.tick_rub else 999.0
    opened = pos["entry_time"]
    if layer.kind == "gpt" and p.max_hold_minutes > 0:
        if row["time"] >= opened + timedelta(minutes=p.max_hold_minutes):
            price = row["open"]
            return round_step(float(price), p.tick), "max_hold"
    stop = pos["stop_price"]
    if direction == "long":
        if float(row["open"]) <= stop:
            return round_step(float(row["open"]), p.tick), "gap_stop"
        if float(row["low"]) <= stop:
            return stop, "stop"
        update_trailing(pos, float(row["high"]), p, fee_t)
        if float(row["low"]) <= pos["stop_price"]:
            return pos["stop_price"], "trail_stop"
    else:
        if float(row["open"]) >= stop:
            return round_step(float(row["open"]), p.tick), "gap_stop"
        if float(row["high"]) >= stop:
            return stop, "stop"
        update_trailing(pos, float(row["low"]), p, fee_t)
        if float(row["high"]) >= pos["stop_price"]:
            return pos["stop_price"], "trail_stop"
    return None, ""


def run_layer_ticker(df: pd.DataFrame, p: Profile, layer: Layer) -> list[dict]:
    trades: list[dict] = []
    pos: dict | None = None
    cooldown_until = pd.Timestamp.min
    if len(df) < 30:
        return trades
    records = df.to_dict("records")
    for i in range(20, len(records) - 1):
        row = records[i]
        next_row = records[i + 1]
        if pos is not None:
            fill, reason = exit_on_bar(pos, row, p, layer)
            if fill is not None:
                ticks, gross, net = pnl(pos["entry_price"], fill, pos["direction"], p, 1, 0)
                trades.append(
                    {
                        "layer": layer.name,
                        "ticker": p.ticker,
                        "family": p.family,
                        "direction": pos["direction"],
                        "entry_time": pos["entry_time"],
                        "exit_time": row["time"],
                        "entry_price": pos["entry_price"],
                        "exit_price": fill,
                        "exit_reason": reason,
                        "ticks": round(ticks, 3),
                        "gross_rub": round(gross, 2),
                        "fees_rub": round(gross - net, 2),
                        "net_rub": round(net, 2),
                    }
                )
                cooldown = p.cooldown_minutes if layer.kind == "gpt" else 1
                cooldown_until = row["time"] + timedelta(minutes=max(1, cooldown))
                pos = None
            continue
        if row["time"] < cooldown_until:
            continue
        if layer.kind == "current":
            direction = current_signal(row, p, aggressive=False)
            contour = "strict"
            if direction is None:
                direction = current_signal(row, p, aggressive=True)
                contour = "aggressive" if direction else ""
        else:
            direction = gpt_signal(row, p, layer)
            contour = "gpt"
        if direction is None:
            continue
        entry = float(next_row["open"])
        if layer.kind == "gpt" and (p.entry_timing or "").lower() == "adverse_1tick":
            entry += p.tick if direction == "long" else -p.tick
        entry = round_step(entry, p.tick)
        pos = {
            "direction": direction,
            "entry_price": entry,
            "best_price": entry,
            "stop_price": stop_price(entry, direction, p.stop_ticks, p),
            "entry_time": next_row["time"],
            "contour": contour,
        }
    if pos is not None:
        last = df.iloc[-1]
        fill = round_step(float(last["close"]), p.tick)
        ticks, gross, net = pnl(pos["entry_price"], fill, pos["direction"], p, 1, 0)
        trades.append(
            {
                "layer": layer.name,
                "ticker": p.ticker,
                "family": p.family,
                "direction": pos["direction"],
                "entry_time": pos["entry_time"],
                "exit_time": last["time"],
                "entry_price": pos["entry_price"],
                "exit_price": fill,
                "exit_reason": "end_of_data",
                "ticks": round(ticks, 3),
                "gross_rub": round(gross, 2),
                "fees_rub": round(gross - net, 2),
                "net_rub": round(net, 2),
            }
        )
    return trades


def expand_slippage(trades: pd.DataFrame, profiles: dict[str, Profile]) -> pd.DataFrame:
    if trades.empty:
        return trades
    rows = []
    for _, row in trades.iterrows():
        p = profiles[str(row["ticker"])]
        entry = float(row["entry_price"])
        exit_price = float(row["exit_price"])
        direction = str(row["direction"])
        fees = fee_side(entry, p, 1) + fee_side(exit_price, p, 1)
        raw_ticks = (exit_price - entry) / p.tick if direction == "long" else (entry - exit_price) / p.tick
        for slip in [0, 1, 2]:
            rec = row.to_dict()
            ticks = raw_ticks - slip
            gross = ticks * p.tick_rub
            rec["slippage_ticks"] = slip
            rec["ticks"] = round(ticks, 3)
            rec["gross_rub"] = round(gross, 2)
            rec["fees_rub"] = round(fees, 2)
            rec["net_rub"] = round(gross - fees, 2)
            rows.append(rec)
    return pd.DataFrame(rows)


def summarize(trades: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(columns=group_cols + ["trades", "wins", "losses", "net_rub", "avg_trade", "median_trade", "profit_factor", "max_drawdown"])
    rows = []
    for keys, g in trades.groupby(group_cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        pnl_s = pd.to_numeric(g["net_rub"], errors="coerce").fillna(0.0)
        equity = pnl_s.cumsum()
        dd = equity - equity.cummax()
        gains = pnl_s[pnl_s > 0].sum()
        losses = -pnl_s[pnl_s < 0].sum()
        row = dict(zip(group_cols, keys))
        row.update(
            {
                "trades": int(len(g)),
                "wins": int((pnl_s > 0).sum()),
                "losses": int((pnl_s < 0).sum()),
                "net_rub": round(float(pnl_s.sum()), 2),
                "avg_trade": round(float(pnl_s.mean()), 2) if len(pnl_s) else 0.0,
                "median_trade": round(float(pnl_s.median()), 2) if len(pnl_s) else 0.0,
                "profit_factor": round(float(gains / losses), 3) if losses > 0 else None,
                "max_drawdown": round(float(abs(dd.min())), 2) if len(dd) else 0.0,
            }
        )
        rows.append(row)
    return pd.DataFrame(rows).sort_values(group_cols)


def assign_splits(trades: pd.DataFrame, candle_ranges: dict[str, tuple[pd.Timestamp, pd.Timestamp]]) -> pd.DataFrame:
    out = trades.copy()
    split = []
    for _, row in out.iterrows():
        start, end = candle_ranges[str(row["ticker"])]
        cut = start + (end - start) * 0.7
        split.append("train" if row["entry_time"] < cut else "test")
    out["split"] = split
    return out


def walkforward(trades: pd.DataFrame, candle_ranges: dict[str, tuple[pd.Timestamp, pd.Timestamp]]) -> pd.DataFrame:
    rows = []
    if trades.empty:
        return pd.DataFrame()
    global_start = min(v[0] for v in candle_ranges.values())
    global_end = max(v[1] for v in candle_ranges.values())
    start = global_start
    idx = 1
    while start + timedelta(days=80) <= global_end:
        train_start = start
        train_end = start + timedelta(days=60)
        test_start = train_end
        test_end = test_start + timedelta(days=20)
        part = trades[(trades["entry_time"] >= test_start) & (trades["entry_time"] < test_end)]
        if not part.empty:
            s = summarize(part, ["layer", "slippage_ticks"])
            for _, row in s.iterrows():
                rec = row.to_dict()
                rec.update(
                    {
                        "window": idx,
                        "train_start": train_start,
                        "train_end": train_end,
                        "test_start": test_start,
                        "test_end": test_end,
                    }
                )
                rows.append(rec)
        start = start + timedelta(days=20)
        idx += 1
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    profiles = load_profiles()
    all_trades: list[dict] = []
    coverage_rows = []
    candle_ranges: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {}
    for ticker, profile in sorted(profiles.items()):
        df = load_candles(ticker)
        if df.empty or len(df) < 60:
            coverage_rows.append({"ticker": ticker, "rows": len(df), "status": "no_data"})
            continue
        df = add_features(df, profile)
        candle_ranges[ticker] = (df["time"].iloc[0], df["time"].iloc[-1])
        coverage_rows.append(
            {
                "ticker": ticker,
                "rows": len(df),
                "start": df["time"].iloc[0],
                "end": df["time"].iloc[-1],
                "status": "ok",
                "stop_ticks": profile.stop_ticks,
                "trail_ticks": profile.trail_ticks,
                "trail_arm_ticks": profile.trail_arm_ticks,
                "gpt_signal_family": profile.signal_family,
            }
        )
        for layer in LAYERS:
            all_trades.extend(run_layer_ticker(df, profile, layer))

    trades = pd.DataFrame(all_trades)
    trades = expand_slippage(trades, profiles)
    if not trades.empty:
        trades["entry_time"] = pd.to_datetime(trades["entry_time"], errors="coerce")
        trades["exit_time"] = pd.to_datetime(trades["exit_time"], errors="coerce")
        trades = assign_splits(trades, candle_ranges)
    pd.DataFrame(coverage_rows).to_csv(OUT_DIR / "four_layer_backtest_coverage.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUT_DIR / "four_layer_backtest_trades.csv", index=False, encoding="utf-8-sig")
    summarize(trades, ["layer", "slippage_ticks"]).to_csv(OUT_DIR / "four_layer_backtest_summary.csv", index=False, encoding="utf-8-sig")
    summarize(trades, ["layer", "slippage_ticks", "split"]).to_csv(OUT_DIR / "four_layer_backtest_train_test.csv", index=False, encoding="utf-8-sig")
    summarize(trades, ["layer", "slippage_ticks", "ticker"]).to_csv(OUT_DIR / "four_layer_backtest_by_ticker.csv", index=False, encoding="utf-8-sig")
    wf = walkforward(trades, candle_ranges)
    wf.to_csv(OUT_DIR / "four_layer_walkforward.csv", index=False, encoding="utf-8-sig")
    if not wf.empty:
        wf2 = wf[wf["slippage_ticks"] == 2].copy()
        wf_summary = (
            wf2.groupby("layer")
            .agg(
                windows=("window", "count"),
                profitable_windows=("net_rub", lambda s: int((s > 0).sum())),
                net_rub=("net_rub", "sum"),
                median_window_net=("net_rub", "median"),
                trades=("trades", "sum"),
            )
            .reset_index()
        )
        wf_summary["profitable_window_pct"] = (wf_summary["profitable_windows"] / wf_summary["windows"] * 100).round(1)
        wf_summary.to_csv(OUT_DIR / "four_layer_walkforward_summary_2t.csv", index=False, encoding="utf-8-sig")
    print("summary 2T")
    s = summarize(trades[trades["slippage_ticks"] == 2], ["layer", "slippage_ticks"])
    print(s.to_string(index=False))
    print(f"out_dir={OUT_DIR}")


if __name__ == "__main__":
    main()
