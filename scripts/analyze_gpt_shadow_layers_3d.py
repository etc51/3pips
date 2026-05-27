from __future__ import annotations

import csv
import math
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "reports" / "paper_runs" / "v7_live_20260525"
OUT_DIR = RUN_DIR / "analysis"
PROFILE_PATH = ROOT / "reports" / "futures_scalp_profiles_v7_paper_20260525_gpt_shadow_params.csv"
BASE_PROFILE_PATH = ROOT / "reports" / "futures_scalp_profiles_v7_paper_20260525.csv"


@dataclass(frozen=True)
class Layer:
    name: str
    threshold_mult: float
    volume_mult: float
    trend_tolerance_ticks: float
    trend_tolerance_mult: float
    vwap_buffer_mult: float


LAYERS = [
    Layer("full", 1.0, 1.0, 0.0, 0.0, 1.0),
    Layer("relaxed", 0.75, 0.75, 1.0, 0.20, 0.50),
    Layer("loose", 0.55, 0.50, 2.0, 0.35, 0.0),
]

OPEN_RE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?\bOPEN\s+"
    r"(?P<contour>\S+)\s+(?P<secid>\S+)\s+(?P<direction>long|short)\s+"
    r"qty=(?P<qty>\d+)\s+entry=(?P<entry>[-+0-9.]+).*?"
    r"(?P<signal>entry_signal\s+(?:long|short)\s+.*)$"
)
METRIC_RE = re.compile(
    r"p=(?P<p>[-+0-9.]+).*?"
    r"vwap=(?P<vwap>[-+0-9.]+).*?"
    r"mom=(?P<mom>[-+0-9.]+).*?"
    r"trend=(?P<trend>[-+0-9.]+).*?"
    r"vol=(?P<vol>[-+0-9.]+)/(?P<avgv>[-+0-9.]+).*?"
    r"book=(?P<bid>[-+0-9.]+)/(?P<ask>[-+0-9.]+)"
)


def parse_time(value: object) -> pd.Timestamp | None:
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    if getattr(ts, "tzinfo", None) is not None:
        ts = ts.tz_convert(None)
    return ts


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.read_csv(path, engine="python", on_bad_lines="skip")


def analysis_dirs() -> list[Path]:
    dirs = [
        ROOT / "reports" / "paper_runs" / "v7_live_20260525_reset_before_20260526_125502",
        RUN_DIR / "backups" / "pre_open_reset_20260527_085405",
        RUN_DIR,
    ]
    return [p for p in dirs if p.exists()]


def portfolio_from_trade_file(path: Path) -> str | None:
    suffix = "_multi_futures_paper_trades.csv"
    if not path.name.endswith(suffix):
        return None
    return path.name[: -len(suffix)]


def portfolio_from_log_file(path: Path) -> str | None:
    name = path.name
    for marker in ("_multi_paper", "_supervisor_"):
        if marker in name:
            return name.split(marker, 1)[0]
    return None


def load_trades() -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for base in analysis_dirs():
        for path in base.glob("*_multi_futures_paper_trades.csv"):
            portfolio = portfolio_from_trade_file(path)
            if portfolio in {None, "stock_watch"}:
                continue
            df = read_csv(path)
            if df.empty:
                continue
            df["portfolio"] = portfolio
            df["source_dir"] = str(base)
            parts.append(df)
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True, sort=False)
    df["closed_at_ts"] = pd.to_datetime(df.get("closed_at"), errors="coerce")
    df = df.dropna(subset=["closed_at_ts"])
    df = df[df["closed_at_ts"] >= pd.Timestamp(datetime.now().date() - timedelta(days=2))]
    df["net_rub"] = pd.to_numeric(df.get("net_rub"), errors="coerce")
    df["qty"] = pd.to_numeric(df.get("qty"), errors="coerce").fillna(0).astype(int)
    df["entry_price"] = pd.to_numeric(df.get("entry_price"), errors="coerce")
    return df.sort_values("closed_at_ts").reset_index(drop=True)


def parse_open_line(line: str, portfolio: str, source: Path) -> dict | None:
    m = OPEN_RE.search(line)
    if not m:
        return None
    row = m.groupdict()
    metrics = METRIC_RE.search(row.get("signal") or "")
    if metrics:
        row.update(metrics.groupdict())
    row["portfolio"] = portfolio
    row["source_log"] = str(source)
    row["open_time_ts"] = parse_time(row["time"])
    row["qty"] = int(row["qty"])
    row["entry"] = float(row["entry"])
    for col in ("p", "vwap", "mom", "trend", "vol", "avgv", "bid", "ask"):
        try:
            row[col] = float(row.get(col))
        except Exception:
            row[col] = math.nan
    return row


def load_open_events() -> list[dict]:
    events: list[dict] = []
    for base in analysis_dirs():
        for path in list(base.glob("*_multi_paper*.log")) + list(base.glob("*_supervisor_*.stdout.log")):
            portfolio = portfolio_from_log_file(path)
            if portfolio in {None, "stock_watch"}:
                continue
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        row = parse_open_line(line, portfolio, path)
                        if row:
                            events.append(row)
            except Exception:
                continue
    events = [e for e in events if e.get("open_time_ts") is not None]
    events.sort(key=lambda x: x["open_time_ts"])
    return events


def load_profiles() -> tuple[dict[str, dict], dict[str, float]]:
    profiles: dict[str, dict] = {}
    gpt = read_csv(PROFILE_PATH)
    for _, row in gpt.iterrows():
        ticker = str(row.get("ticker") or "").upper()
        if ticker:
            profiles[ticker] = row.to_dict()
    ticks: dict[str, float] = {}
    base = read_csv(BASE_PROFILE_PATH)
    for _, row in base.iterrows():
        ticker = str(row.get("ticker") or "").upper()
        tick_rub = pd.to_numeric(pd.Series([row.get("tick_rub")]), errors="coerce").iloc[0]
        if ticker and pd.notna(tick_rub):
            ticks[ticker] = float(tick_rub)
    for spec_path in RUN_DIR.glob("*_instrument_specs.csv"):
        specs = read_csv(spec_path)
        for _, row in specs.iterrows():
            ticker = str(row.get("ticker") or "").upper()
            tick = pd.to_numeric(pd.Series([row.get("tick")]), errors="coerce").iloc[0]
            if ticker and pd.notna(tick):
                ticks[ticker] = float(tick)
    return profiles, ticks


def match_events(trades: pd.DataFrame, events: list[dict]) -> pd.DataFrame:
    buckets: dict[tuple, deque] = defaultdict(deque)
    for e in events:
        key = (e["portfolio"], e["contour"], e["secid"], e["direction"], int(e["qty"]))
        buckets[key].append(e)

    matched: list[dict] = []
    for idx, trade in trades.iterrows():
        key = (
            str(trade.get("portfolio")),
            str(trade.get("contour")),
            str(trade.get("secid")),
            str(trade.get("direction")),
            int(trade.get("qty") or 0),
        )
        entry = float(trade.get("entry_price") or math.nan)
        close_ts = trade.get("closed_at_ts")
        selected = None
        queue = buckets.get(key, deque())
        keep = deque()
        while queue:
            e = queue.popleft()
            if e["open_time_ts"] > close_ts:
                keep.appendleft(e)
                break
            if math.isfinite(entry) and abs(float(e["entry"]) - entry) > max(1e-9, abs(entry) * 0.002):
                keep.append(e)
                continue
            selected = e
            break
        while queue:
            keep.append(queue.popleft())
        buckets[key] = keep

        rec = trade.to_dict()
        rec["trade_id"] = idx
        if selected:
            for k, v in selected.items():
                rec[f"open_{k}"] = v
            rec["has_open_metrics"] = True
        else:
            rec["has_open_metrics"] = False
        matched.append(rec)
    return pd.DataFrame(matched)


def session_allowed(mode: str, ts: pd.Timestamp) -> bool:
    mode = (mode or "all_available_data").lower()
    if mode in {"", "all_available_data", "all"}:
        return True
    seconds = ts.hour * 3600 + ts.minute * 60 + ts.second
    start = 10 * 3600
    end = 18 * 3600 + 45 * 60
    if mode == "exclude_first_last_10_minutes":
        start += 10 * 60
        end -= 10 * 60
    return start <= seconds < end


def classify(row: pd.Series, layer: Layer, profiles: dict[str, dict], tick_by_ticker: dict[str, float]) -> tuple[bool, str]:
    if str(row.get("contour")) != "strict":
        return False, "aggressive"
    ticker = str(row.get("secid") or "").upper()
    profile = profiles.get(ticker)
    if not profile:
        return False, "no_profile"
    if not bool(row.get("has_open_metrics")):
        return False, "no_metrics"
    ts = row.get("open_open_time_ts")
    if ts is None or pd.isna(ts):
        return False, "no_open_time"
    session_filter = str(profile.get("session_filter") or "all_available_data")
    if not session_allowed(session_filter, ts):
        return False, "session"

    direction = str(row.get("direction") or "")
    allowed = str(profile.get("direction") or "both").lower()
    if allowed not in {"", "both", direction}:
        return False, "direction"

    try:
        bid = float(row.get("open_bid"))
        ask = float(row.get("open_ask"))
    except Exception:
        return False, "book"
    if not (bid > 0 and ask > 0):
        return False, "book"

    tick = tick_by_ticker.get(ticker)
    price = float(row.get("open_p") or row.get("entry_price") or 0.0)
    if not tick or tick <= 0 or price <= 0:
        return False, "tick"

    mom = float(row.get("open_mom"))
    trend = float(row.get("open_trend"))
    vol = float(row.get("open_vol"))
    avgv = float(row.get("open_avgv"))
    vwap = float(row.get("open_vwap"))
    if any(not math.isfinite(x) for x in (mom, trend, vol, avgv, vwap)):
        return False, "metrics"

    try:
        momentum_ticks = float(profile.get("momentum_ticks") or 0.0)
    except Exception:
        momentum_ticks = 0.0
    try:
        momentum_pct = float(profile.get("momentum_pct") or 0.0)
    except Exception:
        momentum_pct = 0.0
    threshold = max(momentum_ticks, abs(momentum_pct * price / tick))
    if threshold <= 0:
        threshold = 2.0
    threshold *= layer.threshold_mult

    trend_tol = max(layer.trend_tolerance_ticks, abs(threshold) * layer.trend_tolerance_mult)
    try:
        vol_mult = float(profile.get("volume_multiplier") or 1.0) * layer.volume_mult
    except Exception:
        vol_mult = layer.volume_mult
    if vol_mult > 0 and not (avgv > 0 and vol >= avgv * vol_mult):
        return False, "volume"

    try:
        vwap_buffer_pct = float(profile.get("vwap_buffer_pct") or 0.0) * layer.vwap_buffer_mult
    except Exception:
        vwap_buffer_pct = 0.0
    vwap_buffer_ticks = abs(vwap_buffer_pct * price / tick)
    vwap_mode = str(profile.get("vwap_mode") or "disabled").lower()
    if vwap_mode != "disabled":
        directional_vwap = (price - vwap) / tick if direction == "long" else (vwap - price) / tick
        if directional_vwap < vwap_buffer_ticks:
            return False, "vwap"

    family = str(profile.get("signal_family") or "pure_trailing_after_impulse").lower()
    if direction == "long":
        mom_ok = mom >= threshold
        trend_ok = trend >= -trend_tol
        pullback_ok = trend_ok and mom >= max(1.0, threshold * 0.5)
    else:
        mom_ok = mom <= -threshold
        trend_ok = trend <= trend_tol
        pullback_ok = trend_ok and mom <= -max(1.0, threshold * 0.5)

    if family == "trend_pullback":
        ok = pullback_ok
    elif family == "range_expansion":
        ok = abs(mom) >= threshold and ((direction == "long" and mom > 0) or (direction == "short" and mom < 0))
    else:
        ok = mom_ok and trend_ok
    if not ok:
        return False, "momentum"
    return True, "ok"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = load_trades()
    events = load_open_events()
    profiles, ticks = load_profiles()
    matched = match_events(trades, events)

    rows = []
    for _, trade in matched.iterrows():
        for layer in LAYERS:
            ok, reason = classify(trade, layer, profiles, ticks)
            rows.append(
                {
                    "layer": layer.name,
                    "accepted": ok,
                    "reject_reason": reason,
                    "portfolio": trade.get("portfolio"),
                    "contour": trade.get("contour"),
                    "ticker": trade.get("secid"),
                    "direction": trade.get("direction"),
                    "qty": trade.get("qty"),
                    "closed_at": trade.get("closed_at"),
                    "entry_price": trade.get("entry_price"),
                    "exit_price": trade.get("exit_price"),
                    "ticks": trade.get("ticks"),
                    "net_rub": trade.get("net_rub"),
                    "has_open_metrics": trade.get("has_open_metrics"),
                    "open_time": trade.get("open_time"),
                    "open_mom": trade.get("open_mom"),
                    "open_trend": trade.get("open_trend"),
                    "open_vol": trade.get("open_vol"),
                    "open_avgv": trade.get("open_avgv"),
                    "open_bid": trade.get("open_bid"),
                    "open_ask": trade.get("open_ask"),
                }
            )
    result = pd.DataFrame(rows)
    detail_path = OUT_DIR / "gpt_shadow_layers_3d_trade_filter.csv"
    result.to_csv(detail_path, index=False, encoding="utf-8-sig")

    summary_rows = []
    for layer, g in result.groupby("layer"):
        accepted = g[g["accepted"]].copy()
        pnl = pd.to_numeric(accepted.get("net_rub"), errors="coerce").dropna()
        summary_rows.append(
            {
                "layer": layer,
                "accepted_trades": int(len(accepted)),
                "wins": int((pnl > 0).sum()),
                "losses": int((pnl < 0).sum()),
                "net_rub": round(float(pnl.sum()), 2) if not pnl.empty else 0.0,
                "avg_trade_rub": round(float(pnl.mean()), 2) if not pnl.empty else 0.0,
                "actual_total_trades": int(len(trades)),
                "actual_total_net_rub": round(float(pd.to_numeric(trades.get("net_rub"), errors="coerce").sum()), 2),
                "matched_with_open_metrics": int(bool(len(matched)) and matched["has_open_metrics"].sum()),
            }
        )
    summary = pd.DataFrame(summary_rows)
    summary_path = OUT_DIR / "gpt_shadow_layers_3d_summary.csv"
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")

    reason = (
        result.groupby(["layer", "reject_reason"], dropna=False)
        .agg(trades=("accepted", "size"), net_rub=("net_rub", lambda s: round(float(pd.to_numeric(s, errors="coerce").sum()), 2)))
        .reset_index()
    )
    reason_path = OUT_DIR / "gpt_shadow_layers_3d_reject_reasons.csv"
    reason.to_csv(reason_path, index=False, encoding="utf-8-sig")

    print(summary.to_string(index=False))
    print(f"detail={detail_path}")
    print(f"summary={summary_path}")
    print(f"reasons={reason_path}")


if __name__ == "__main__":
    main()
