from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from ng_scalper_bot import (
    Direction,
    Position,
    append_trade,
    commission_side_rub,
    find_paper_tbank_token,
    is_stop_hit,
    now_str,
    open_position,
    pnl_rub,
    quotation_to_float,
    require_paper_only,
    round_to_step,
    tbank_find_future,
    update_stop,
    volume_vwap,
)


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
BR_POLICY_MIN_STOP_TICKS = 15
BR_POLICY_MAX_SPREAD_TO_STOP = 0.40
BR_POLICY_LOSS_PAUSE_COUNT = 3
BR_POLICY_LOSS_PAUSE_SECONDS = 2 * 60 * 60
SPREAD_WATCH_RATIO = 0.25
SPREAD_HEAVY_RATIO = 0.40
SPREAD_DOMINATES_RATIO = 1.00
DEFAULT_MAX_FULL_STOP_RUB = 1_000.0
AUTO_POLICY_RELOAD_SEC = 30.0
EXTERNAL_OPEN_POSITIONS_RELOAD_SEC = 2.0
DEFAULT_SHARED_FILE_MODE = 0o644


def atomic_write_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    newline: str | None = None,
    mode: int = DEFAULT_SHARED_FILE_MODE,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.tmp.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(tmp_path, mode)
        tmp_path.replace(path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


@dataclass
class Spec:
    secid: str
    figi: str
    uid: str
    min_step: float
    step_price: float
    last_rub: float
    last_price: float = 0.0
    expiration_date: datetime | None = None
    margin_buy: float = 0.0
    margin_sell: float = 0.0


@dataclass
class Profile:
    secid: str
    stop_ticks: int
    trail_ticks: int
    trail_arm_ticks: int
    target_min_ticks: int
    max_attempts: int
    allowed_direction: str = "both"
    family: str = ""
    source_secid: str = ""


@dataclass
class State:
    spec: Spec
    profile: Profile
    contour: str
    side_fee: float
    candles: deque[dict] = field(default_factory=lambda: deque(maxlen=180))
    position: Position | None = None
    attempts: int = 0
    closed: int = 0
    closed_net: float = 0.0
    last_price: float = 0.0
    last_order_book: object | None = None
    last_reason: str = ""
    last_entry_candle_count: int = -1
    cooldown_until: float = 0.0
    consecutive_losses: int = 0
    ticker_loss_streak: int = 0
    family_loss_streak: int = 0
    shadow_positions: dict[str, Position] = field(default_factory=dict)
    shadow_closed: dict[str, bool] = field(default_factory=dict)
    shadow_close_details: dict[str, dict] = field(default_factory=dict)
    shadow_entry_mode: str = ""
    shadow_entry_anchor_model: str = ""
    entry_shadow_decisions: list[dict] = field(default_factory=list)


@dataclass
class Portfolio:
    initial_capital: float
    max_total_margin_pct: float
    max_position_margin_pct: float
    closed_net: float = 0.0

    @property
    def equity(self) -> float:
        return self.initial_capital + self.closed_net


@dataclass
class SizingDecision:
    qty: int
    margin_qty: int
    risk_qty: int | None
    gross_stop_per_contract_rub: float
    round_turn_fee_per_contract_rub: float
    full_stop_per_contract_rub: float
    full_stop_rub: float
    reason: str


def empty_auto_policy() -> dict:
    return {
        "trade_date": "",
        "generated_at": "",
        "observe_only_portfolios": [],
        "observe_only_group_families": [],
        "allow_aggressive_group_families": [],
        "observe_only_tickers": [],
        "observe_only_families": [],
        "strict_only_tickers": [],
        "strict_only_families": [],
        "entry_blackout_windows": [],
        "entry_blackout_group_windows": {},
        "entry_shadow_gate_group_models": {},
        "entry_no_trade_before": None,
        "entry_no_new_after": None,
        "entry_max_full_stop_rub": None,
        "pause_ticker_after_losses": None,
        "pause_family_after_losses": None,
        "pause_after_loss_minutes": None,
        "notes": [],
    }


def normalize_policy_names(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        text = str(value or "").strip().upper()
        if text:
            out.append(text)
    return sorted(set(out))


def normalize_rub_cap(value: object) -> int | None:
    try:
        cap = int(float(value))
    except Exception:
        return None
    return cap if cap > 0 else None


def normalize_positive_int(value: object) -> int | None:
    try:
        parsed = int(float(value))
    except Exception:
        return None
    return parsed if parsed > 0 else None


def normalize_clock_hhmm(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if ":" in text:
        parts = text.split(":", 1)
    elif len(text) == 4 and text.isdigit():
        parts = [text[:2], text[2:]]
    else:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except Exception:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def normalize_blackout_window(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or "-" not in text:
        return None
    start_raw, end_raw = text.split("-", 1)
    start_norm = normalize_clock_hhmm(start_raw)
    end_norm = normalize_clock_hhmm(end_raw)
    if not start_norm or not end_norm or start_norm > end_norm:
        return None
    return f"{start_norm}-{end_norm}"


def normalize_blackout_windows(values: object) -> list[str]:
    if isinstance(values, str):
        values = [part.strip() for part in values.split(",")]
    if not isinstance(values, (list, tuple, set)):
        return []
    out: list[str] = []
    for value in values:
        normalized = normalize_blackout_window(value)
        if normalized:
            out.append(normalized)
    return sorted(set(out))


def normalize_group_blackout_key(portfolio_group: object, contour: object) -> str:
    portfolio = str(portfolio_group or "").strip().upper()
    contour_name = str(contour or "").strip().upper()
    if not portfolio or not contour_name:
        return ""
    return f"{portfolio}/{contour_name}"


def normalize_group_blackout_windows(values: object) -> dict[str, list[str]]:
    if not isinstance(values, dict):
        return {}
    out: dict[str, list[str]] = {}
    for raw_key, raw_windows in values.items():
        key_text = str(raw_key or "").strip().upper()
        if "/" not in key_text:
            continue
        portfolio, contour = key_text.split("/", 1)
        key = normalize_group_blackout_key(portfolio, contour)
        if not key:
            continue
        windows = normalize_blackout_windows(raw_windows)
        if windows:
            out[key] = windows
    return out


def normalize_shadow_model_name(value: object) -> str:
    return str(value or "").strip().lower()


def normalize_entry_shadow_gate_group_models(values: object) -> dict[str, str]:
    if not isinstance(values, dict):
        return {}
    out: dict[str, str] = {}
    for raw_key, raw_model in values.items():
        key_text = str(raw_key or "").strip().upper()
        if "/" not in key_text:
            continue
        portfolio, contour = key_text.split("/", 1)
        key = normalize_group_blackout_key(portfolio, contour)
        model_name = normalize_shadow_model_name(raw_model)
        if key and model_name:
            out[key] = model_name
    return {key: out[key] for key in sorted(out)}


def parse_auto_policy_payload(payload: object) -> dict:
    if not isinstance(payload, dict):
        return empty_auto_policy()
    active = payload.get("active") if isinstance(payload.get("active"), dict) else {}
    return {
        "trade_date": str(payload.get("trade_date") or ""),
        "generated_at": str(payload.get("generated_at") or ""),
        "observe_only_portfolios": normalize_policy_names(active.get("observe_only_portfolios")),
        "observe_only_group_families": normalize_policy_names(active.get("observe_only_group_families")),
        "allow_aggressive_group_families": normalize_policy_names(active.get("allow_aggressive_group_families")),
        "observe_only_tickers": normalize_policy_names(active.get("observe_only_tickers")),
        "observe_only_families": normalize_policy_names(active.get("observe_only_families")),
        "strict_only_tickers": normalize_policy_names(active.get("strict_only_tickers")),
        "strict_only_families": normalize_policy_names(active.get("strict_only_families")),
        "entry_blackout_windows": normalize_blackout_windows(active.get("entry_blackout_windows")),
        "entry_blackout_group_windows": normalize_group_blackout_windows(active.get("entry_blackout_group_windows")),
        "entry_shadow_gate_group_models": normalize_entry_shadow_gate_group_models(active.get("entry_shadow_gate_group_models")),
        "entry_no_trade_before": normalize_clock_hhmm(active.get("entry_no_trade_before")),
        "entry_no_new_after": normalize_clock_hhmm(active.get("entry_no_new_after")),
        "entry_max_full_stop_rub": normalize_rub_cap(active.get("entry_max_full_stop_rub")),
        "pause_ticker_after_losses": normalize_positive_int(active.get("pause_ticker_after_losses")),
        "pause_family_after_losses": normalize_positive_int(active.get("pause_family_after_losses")),
        "pause_after_loss_minutes": normalize_positive_int(active.get("pause_after_loss_minutes")),
        "notes": [str(item) for item in (active.get("notes") or []) if str(item).strip()],
    }


def refresh_auto_policy(cache: dict, force: bool = False) -> dict:
    now = time.monotonic()
    reload_sec = float(cache.get("reload_sec") or AUTO_POLICY_RELOAD_SEC)
    next_check = float(cache.get("next_check_at") or 0.0)
    if not force and now < next_check:
        return cache.get("payload") if isinstance(cache.get("payload"), dict) else empty_auto_policy()
    cache["next_check_at"] = now + reload_sec
    path = cache.get("path")
    if not isinstance(path, Path):
        cache["payload"] = empty_auto_policy()
        cache["status"] = "disabled"
        return cache["payload"]
    if not path.exists():
        cache["mtime"] = None
        cache["status"] = "missing"
        cache["last_error"] = ""
        cache["payload"] = empty_auto_policy()
        return cache["payload"]
    try:
        mtime = path.stat().st_mtime
    except Exception as exc:
        cache["status"] = "stat_error"
        cache["last_error"] = f"{type(exc).__name__}: {exc}"
        return cache.get("payload") if isinstance(cache.get("payload"), dict) else empty_auto_policy()
    if not force and cache.get("mtime") == mtime and isinstance(cache.get("payload"), dict):
        return cache["payload"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        payload = parse_auto_policy_payload(raw)
    except Exception as exc:
        cache["status"] = "load_error"
        cache["last_error"] = f"{type(exc).__name__}: {exc}"
        return cache.get("payload") if isinstance(cache.get("payload"), dict) else empty_auto_policy()
    cache["mtime"] = mtime
    cache["status"] = "loaded"
    cache["last_error"] = ""
    cache["payload"] = payload
    observe_count = len(payload["observe_only_portfolios"]) + len(payload["observe_only_group_families"]) + len(payload["observe_only_tickers"]) + len(payload["observe_only_families"])
    allow_aggressive_count = len(payload["allow_aggressive_group_families"])
    strict_count = len(payload["strict_only_tickers"]) + len(payload["strict_only_families"])
    blackout_windows = payload.get("entry_blackout_windows") or []
    blackout_group_windows = payload.get("entry_blackout_group_windows") or {}
    entry_shadow_gate_count = len(payload.get("entry_shadow_gate_group_models") or {})
    entry_start = payload.get("entry_no_trade_before")
    entry_cutoff = payload.get("entry_no_new_after")
    stop_cap = payload.get("entry_max_full_stop_rub")
    pause_ticker = payload.get("pause_ticker_after_losses")
    pause_family = payload.get("pause_family_after_losses")
    print(
        f"{now_str()} AUTO_POLICY loaded trade_date={payload.get('trade_date') or '-'} "
        f"observe={observe_count} allow_aggressive={allow_aggressive_count} strict_only={strict_count} "
        f"entry_blackout_windows={','.join(blackout_windows) if blackout_windows else '-'} "
        f"entry_blackout_groups={len(blackout_group_windows)} "
        f"entry_shadow_gates={entry_shadow_gate_count} "
        f"entry_no_trade_before={entry_start or '-'} "
        f"entry_no_new_after={entry_cutoff or '-'} "
        f"stop_cap_rub={stop_cap if stop_cap is not None else '-'} "
        f"pause_ticker={pause_ticker if pause_ticker is not None else '-'} "
        f"pause_family={pause_family if pause_family is not None else '-'} path={path}",
        flush=True,
    )
    return payload


def auto_policy_block_reason(st: State, contour: str, policy: dict, portfolio_group: str) -> str | None:
    secid = st.spec.secid.upper()
    family = state_family(st).upper()
    if portfolio_group and portfolio_group.upper() in set(policy.get("observe_only_portfolios") or []):
        return "auto_policy observe_only_portfolio"
    group_family_key = f"{portfolio_group.upper()}/{contour.upper()}::{family}" if portfolio_group else ""
    if group_family_key and group_family_key in set(policy.get("observe_only_group_families") or []):
        return "auto_policy observe_only_group_family"
    if secid in set(policy.get("observe_only_tickers") or []):
        return "auto_policy observe_only_ticker"
    if family in set(policy.get("observe_only_families") or []):
        return "auto_policy observe_only_family"
    if contour == "aggressive":
        if secid in set(policy.get("strict_only_tickers") or []):
            return "auto_policy strict_only_ticker"
        allow_aggressive = set(policy.get("allow_aggressive_group_families") or [])
        if family in set(policy.get("strict_only_families") or []) and group_family_key not in allow_aggressive:
            return "auto_policy strict_only_family"
    return None


def effective_max_full_stop_rub(base_cap_rub: float, policy: dict) -> float:
    cap = float(base_cap_rub or 0.0)
    policy_cap = normalize_rub_cap(policy.get("entry_max_full_stop_rub") if isinstance(policy, dict) else None)
    if policy_cap is None:
        return cap
    if cap <= 0:
        return float(policy_cap)
    return min(cap, float(policy_cap))


def effective_no_trade_before(base_no_trade_before: int | None, policy: dict) -> int | None:
    policy_start = normalize_clock_hhmm(policy.get("entry_no_trade_before") if isinstance(policy, dict) else None)
    policy_no_trade_before = parse_clock_time(policy_start) if policy_start else None
    if base_no_trade_before is None:
        return policy_no_trade_before
    if policy_no_trade_before is None:
        return base_no_trade_before
    return max(base_no_trade_before, policy_no_trade_before)


def effective_no_new_after(base_no_new_after: int | None, policy: dict) -> int | None:
    policy_cutoff = normalize_clock_hhmm(policy.get("entry_no_new_after") if isinstance(policy, dict) else None)
    policy_no_new_after = parse_clock_time(policy_cutoff) if policy_cutoff else None
    if base_no_new_after is None:
        return policy_no_new_after
    if policy_no_new_after is None:
        return base_no_new_after
    return min(base_no_new_after, policy_no_new_after)


def effective_entry_blackout_windows(
    base_windows: object,
    policy: dict,
    portfolio_group: str = "",
    contour: str = "",
) -> list[str]:
    windows = set(normalize_blackout_windows(base_windows))
    if isinstance(policy, dict):
        windows |= set(normalize_blackout_windows(policy.get("entry_blackout_windows")))
        group_windows = normalize_group_blackout_windows(policy.get("entry_blackout_group_windows"))
        group_key = normalize_group_blackout_key(portfolio_group, contour)
        if group_key:
            windows |= set(group_windows.get(group_key) or [])
    return sorted(windows)


def entry_shadow_gate_block_reason(
    decisions: list[dict],
    policy: dict,
    portfolio_group: str = "",
    contour: str = "",
) -> str | None:
    if not isinstance(policy, dict):
        return None
    group_models = normalize_entry_shadow_gate_group_models(policy.get("entry_shadow_gate_group_models"))
    group_key = normalize_group_blackout_key(portfolio_group, contour)
    if not group_key:
        return None
    required_model = group_models.get(group_key)
    if not required_model:
        return None
    for row in decisions:
        model_name = normalize_shadow_model_name(row.get("model"))
        if model_name != required_model:
            continue
        if bool(row.get("allow")):
            return None
        reason = str(row.get("decision_reason") or "blocked")
        return f"auto_policy entry_shadow_gate {group_key}::{required_model} {reason}"
    return f"auto_policy entry_shadow_gate_missing {group_key}::{required_model}"


def apply_auto_loss_pause(st: State, states: list[State], net: float, policy: dict) -> None:
    ticker_limit = normalize_positive_int(policy.get("pause_ticker_after_losses") if isinstance(policy, dict) else None)
    family_limit = normalize_positive_int(policy.get("pause_family_after_losses") if isinstance(policy, dict) else None)
    pause_minutes = normalize_positive_int(policy.get("pause_after_loss_minutes") if isinstance(policy, dict) else None) or 120
    if ticker_limit is None and family_limit is None:
        return

    secid = st.spec.secid.upper()
    family = state_family(st).upper()
    ticker_related = [other for other in states if other.spec.secid.upper() == secid]
    family_related = [other for other in states if state_family(other).upper() == family]

    if net >= 0:
        if ticker_limit is not None:
            for other in ticker_related:
                other.ticker_loss_streak = 0
        if family_limit is not None:
            for other in family_related:
                other.family_loss_streak = 0
        return

    if ticker_limit is not None:
        st.ticker_loss_streak += 1
        ticker_losses = sum(other.ticker_loss_streak for other in ticker_related)
        if ticker_losses >= ticker_limit:
            pause_until = time.monotonic() + pause_minutes * 60
            for other in ticker_related:
                other.cooldown_until = max(other.cooldown_until, pause_until)
                other.last_reason = f"auto_policy pause_ticker_after_losses losses={ticker_losses} minutes={pause_minutes}"

    if family_limit is not None:
        st.family_loss_streak += 1
        family_losses = sum(other.family_loss_streak for other in family_related)
        if family_losses >= family_limit:
            pause_until = time.monotonic() + pause_minutes * 60
            for other in family_related:
                other.cooldown_until = max(other.cooldown_until, pause_until)
                other.last_reason = f"auto_policy pause_family_after_losses losses={family_losses} minutes={pause_minutes}"


def position_margin(spec: Spec, direction: Direction | str, qty: int) -> float:
    per_contract = spec.margin_buy if direction == "long" else spec.margin_sell
    if per_contract <= 0:
        per_contract = max(spec.margin_buy, spec.margin_sell, 0.0)
    return per_contract * qty


def used_margin(states: list[State]) -> float:
    total = 0.0
    for st in states:
        if st.position is not None:
            total += position_margin(st.spec, st.position.direction, st.position.qty)
    return total


def has_open_ticker(states: list[State], secid: str) -> bool:
    return any(st.spec.secid == secid and st.position is not None for st in states)


def empty_external_open_positions() -> dict:
    return {"tickers": [], "next_check_at": 0.0, "mtimes": {}, "payload": {"tickers": []}}


def refresh_external_open_positions(cache: dict) -> dict:
    now = time.monotonic()
    reload_sec = float(cache.get("reload_sec") or EXTERNAL_OPEN_POSITIONS_RELOAD_SEC)
    next_check = float(cache.get("next_check_at") or 0.0)
    if now < next_check and isinstance(cache.get("payload"), dict):
        return cache["payload"]
    cache["next_check_at"] = now + reload_sec

    own_path = cache.get("own_path")
    if not isinstance(own_path, Path):
        payload = {"tickers": []}
        cache["payload"] = payload
        return payload
    base_dir = own_path.parent
    tickers: set[str] = set()
    mtimes: dict[str, float] = {}
    for path in sorted(base_dir.glob("*_paper_open_positions.json")):
        if path == own_path:
            continue
        try:
            mtimes[str(path)] = path.stat().st_mtime
        except Exception:
            continue
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or row.get("secid") or "").strip().upper()
            if ticker:
                tickers.add(ticker)
    payload = {"tickers": sorted(tickers)}
    cache["mtimes"] = mtimes
    cache["payload"] = payload
    return payload


def state_family(st: State) -> str:
    return st.profile.family or contract_family(st.spec.secid)


def is_resource_exhausted_error(exc: Exception) -> bool:
    text = str(exc)
    return "RESOURCE_EXHAUSTED" in text or "resource exhausted" in text.lower()


def rate_limit_backoff_sec(exc: Exception, default_sec: float = 5.0) -> float:
    match = re.search(r"ratelimit_reset=(\d+)", str(exc))
    if not match:
        return max(2.0, float(default_sec))
    try:
        return max(2.0, float(match.group(1)) + 1.0)
    except Exception:
        return max(2.0, float(default_sec))


def has_roll_family_conflict(states: list[State], spec: Spec, family: str, roll_window_days: float) -> str | None:
    if roll_window_days <= 0:
        return None
    current_dte = days_to_expiration(spec)
    current_in_roll = current_dte is not None and current_dte <= roll_window_days
    for st in states:
        if st.position is None:
            continue
        if st.spec.secid == spec.secid:
            continue
        if state_family(st) != family:
            continue
        other_dte = days_to_expiration(st.spec)
        other_in_roll = other_dte is not None and other_dte <= roll_window_days
        if current_in_roll or other_in_roll:
            return st.spec.secid
    return None


def paper_sizing(
    portfolio: Portfolio,
    states: list[State],
    spec: Spec,
    profile: Profile,
    direction: Direction | str,
    side_fee: float,
    max_full_stop_rub: float,
) -> SizingDecision:
    per_contract = spec.margin_buy if direction == "long" else spec.margin_sell
    if per_contract <= 0:
        per_contract = max(spec.margin_buy, spec.margin_sell, 0.0)
    if per_contract <= 0:
        margin_qty = 1
    else:
        max_total_margin = portfolio.equity * portfolio.max_total_margin_pct
        free_margin = max(0.0, max_total_margin - used_margin(states))
        per_position_limit = portfolio.equity * portfolio.max_position_margin_pct
        budget = min(free_margin, per_position_limit)
        margin_qty = max(0, int(budget // per_contract))

    gross_stop_per_contract = max(0.0, profile.stop_ticks * spec.step_price)
    round_turn_fee_per_contract = max(0.0, 2 * side_fee)
    full_stop_per_contract = gross_stop_per_contract + round_turn_fee_per_contract
    risk_qty = None
    if max_full_stop_rub > 0 and full_stop_per_contract > 0:
        risk_qty = int(max_full_stop_rub // full_stop_per_contract)

    qty = margin_qty
    if risk_qty is not None:
        qty = min(qty, risk_qty)
    qty = max(0, qty)
    full_stop_rub = qty * full_stop_per_contract
    reason = (
        f"sizing margin_qty={margin_qty} risk_qty={risk_qty if risk_qty is not None else '-'} "
        f"full_stop_1lot={full_stop_per_contract:.2f} full_stop={full_stop_rub:.2f} "
        f"max_full_stop={max_full_stop_rub:.0f}"
    )
    return SizingDecision(
        qty=qty,
        margin_qty=margin_qty,
        risk_qty=risk_qty,
        gross_stop_per_contract_rub=gross_stop_per_contract,
        round_turn_fee_per_contract_rub=round_turn_fee_per_contract,
        full_stop_per_contract_rub=full_stop_per_contract,
        full_stop_rub=full_stop_rub,
        reason=reason,
    )


def full_stop_risk_rub(profile: Profile, spec: Spec, side_fee: float, qty: int) -> float:
    return max(0.0, (profile.stop_ticks * spec.step_price + 2 * side_fee) * qty)


def fee_ticks(side_fee: float, spec: Spec) -> float:
    return (2 * side_fee / spec.step_price) if spec.step_price else 999.0


def spread_to_stop_metrics(spread_ticks: float | None, stop_ticks: int) -> tuple[float | None, str, bool]:
    if spread_ticks is None or stop_ticks <= 0:
        return None, "NO_BOOK", False
    ratio = float(spread_ticks) / float(stop_ticks)
    if ratio > SPREAD_DOMINATES_RATIO:
        return ratio, "SPREAD_DOMINATES", True
    if ratio > SPREAD_HEAVY_RATIO:
        return ratio, "SPREAD_HEAVY", True
    if ratio > SPREAD_WATCH_RATIO:
        return ratio, "SPREAD_WATCH", True
    return ratio, "SPREAD_OK", False


def profile_can_trade(profile: Profile, side_fee: float, spec: Spec, max_fee_to_stop: float = 0.55) -> tuple[bool, str]:
    round_fee_ticks = fee_ticks(side_fee, spec)
    max_allowed = profile.stop_ticks * max_fee_to_stop
    if round_fee_ticks > max_allowed:
        return False, f"startup_fee_filter fee={round_fee_ticks:.1f}t stop={profile.stop_ticks}t max={max_allowed:.1f}t"
    return True, f"fee_ok fee={round_fee_ticks:.1f}t stop={profile.stop_ticks}t"


def apply_br_loss_pause(st: State, states: list[State], net: float) -> None:
    if not br_small_stop_policy_applies(st.profile):
        return
    related = [
        other
        for other in states
        if br_small_stop_policy_applies(other.profile) and state_family(other) == state_family(st)
    ]
    if net >= 0:
        for other in related:
            other.consecutive_losses = 0
        return

    st.consecutive_losses += 1
    loss_count = sum(other.consecutive_losses for other in related)
    if loss_count < BR_POLICY_LOSS_PAUSE_COUNT:
        return

    pause_until = time.monotonic() + BR_POLICY_LOSS_PAUSE_SECONDS
    minutes = int(BR_POLICY_LOSS_PAUSE_SECONDS // 60)
    for other in related:
        other.cooldown_until = max(other.cooldown_until, pause_until)
        other.last_reason = f"brq6_loss_pause losses={loss_count} minutes={minutes}"


def days_to_expiration(spec: Spec) -> float | None:
    if spec.expiration_date is None:
        return None
    exp = spec.expiration_date
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return (exp.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds() / 86400


def expiry_new_entry_block_reason(spec: Spec, threshold_days: float) -> str | None:
    if threshold_days <= 0:
        return None
    dte = days_to_expiration(spec)
    if dte is None or dte > threshold_days:
        return None
    exp = spec.expiration_date.isoformat() if spec.expiration_date else ""
    return f"expiry_filter no_new_entries dte={dte:.1f}d threshold={threshold_days:.1f}d exp={exp}"


def profile_from_row(row: dict, secid: str | None = None, source_secid: str | None = None) -> Profile:
    ticker = secid or row["ticker"]
    profile = Profile(
        secid=ticker,
        stop_ticks=int(row["stop_ticks"]),
        trail_ticks=int(row["trail_ticks"]),
        trail_arm_ticks=int(row["trail_arm_ticks"]),
        target_min_ticks=int(row["target_min_ticks"]),
        max_attempts=int(row["max_attempts"]),
        allowed_direction=str(row.get("v7_direction") or "both").lower(),
        family=str(row.get("v7_family") or contract_family(ticker)),
        source_secid=source_secid or row["ticker"],
    )
    apply_profile_policy_overrides(profile)
    return profile


def contract_family(secid: str) -> str:
    if secid.endswith("perpA"):
        return secid
    head = secid.rstrip("0123456789")
    month_codes = set("FGHJKMNQUVXZ")
    if len(head) > 1 and head[-1].upper() in month_codes:
        head = head[:-1]
    return head or secid


def br_small_stop_policy_applies(profile: Profile) -> bool:
    source = (profile.source_secid or profile.secid).upper()
    family = (profile.family or contract_family(profile.secid)).upper()
    return source == "BRQ6" or (family == "BR" and profile.stop_ticks <= BR_POLICY_MIN_STOP_TICKS)


def apply_profile_policy_overrides(profile: Profile) -> None:
    if br_small_stop_policy_applies(profile) and profile.stop_ticks < BR_POLICY_MIN_STOP_TICKS:
        profile.stop_ticks = BR_POLICY_MIN_STOP_TICKS


def clone_profile_for_contract(profile: Profile, secid: str) -> Profile:
    cloned = Profile(
        secid=secid,
        stop_ticks=profile.stop_ticks,
        trail_ticks=profile.trail_ticks,
        trail_arm_ticks=profile.trail_arm_ticks,
        target_min_ticks=profile.target_min_ticks,
        max_attempts=profile.max_attempts,
        allowed_direction=profile.allowed_direction,
        family=profile.family or contract_family(secid),
        source_secid=profile.source_secid or profile.secid,
    )
    apply_profile_policy_overrides(cloned)
    return cloned


def load_profiles(path: Path, secids: list[str] | None = None) -> dict[str, Profile]:
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    wanted = set(secids or [])
    out: dict[str, Profile] = {}
    for row in rows:
        if wanted and row["ticker"] not in wanted:
            continue
        if row["quality"] == "skip":
            continue
        out[row["ticker"]] = profile_from_row(row)
    return out


def seed_candles(client: object, figi: str, minutes: int) -> deque[dict]:
    from t_tech.invest import CandleInterval

    to = datetime.now(timezone.utc)
    from_ = to - timedelta(minutes=minutes)
    resp = client.market_data.get_candles(
        figi=figi,
        from_=from_,
        to=to,
        interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
    )
    rows: deque[dict] = deque(maxlen=max(minutes, 180))
    for candle in resp.candles:
        rows.append(
            {
                "open": quotation_to_float(candle.open),
                "high": quotation_to_float(candle.high),
                "low": quotation_to_float(candle.low),
                "close": quotation_to_float(candle.close),
                "volume": int(candle.volume),
            }
        )
    return rows


def spec_from_tbank(client: object, secid: str, future: object, info: object) -> Spec:
    try:
        lp = client.market_data.get_last_prices(figi=[future.figi]).last_prices
        last = quotation_to_float(lp[0].price) if lp else 0.0
    except Exception:
        last = 0.0
    spec = Spec(
        secid=secid,
        figi=future.figi,
        uid=future.uid,
        min_step=quotation_to_float(info.min_price_increment),
        step_price=quotation_to_float(info.min_price_increment_amount),
        last_rub=0.0,
        last_price=last,
        expiration_date=getattr(info, "expiration_date", None),
        margin_buy=quotation_to_float(getattr(info, "initial_margin_on_buy", None)),
        margin_sell=quotation_to_float(getattr(info, "initial_margin_on_sell", None)),
    )
    spec.last_rub = last / spec.min_step * spec.step_price if last and spec.min_step else 0.0
    return spec


def future_candidates_by_family(client: object, family: str) -> list[object]:
    from t_tech.invest import InstrumentStatus

    now = datetime.now(timezone.utc)
    family_upper = family.upper()
    instruments = client.instruments.futures(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE).instruments
    candidates = []
    for item in instruments:
        ticker = str(getattr(item, "ticker", ""))
        exp = getattr(item, "expiration_date", None)
        if not ticker.upper().startswith(family_upper):
            continue
        if exp is None or exp.astimezone(timezone.utc) <= now:
            continue
        candidates.append(item)
    return sorted(candidates, key=lambda item: getattr(item, "expiration_date", datetime.max.replace(tzinfo=timezone.utc)))


def select_roll_contract(
    client: object,
    current_spec: Spec,
    current_profile: Profile,
    all_profiles: dict[str, Profile],
    args: argparse.Namespace,
) -> tuple[str | None, Profile | None, dict]:
    from t_tech.invest import InstrumentIdType

    family = current_profile.family or contract_family(current_spec.secid)
    current_dte = days_to_expiration(current_spec)
    event = {
        "ticker": current_spec.secid,
        "family": family,
        "expiration": current_spec.expiration_date.isoformat() if current_spec.expiration_date else "",
        "days_to_expiration": round(current_dte, 3) if current_dte is not None else None,
        "status": "not_near_roll",
        "selected": "",
        "selected_profile_source": "",
        "reason": "",
        "candidates": [],
    }
    if current_spec.expiration_date is None or current_dte is None:
        event["status"] = "no_expiration"
        return None, None, event
    if current_spec.expiration_date.year >= 2099 or family.endswith("perpA"):
        event["status"] = "perpetual_or_far_expiration"
        return None, None, event
    if current_dte > float(args.roll_observe_days):
        event["reason"] = f"dte_above_observe_window {current_dte:.1f}>{float(args.roll_observe_days):.1f}"
        return None, None, event

    event["status"] = "searching_next"
    for item in future_candidates_by_family(client, family):
        ticker = str(getattr(item, "ticker", ""))
        exp = getattr(item, "expiration_date", None)
        if ticker == current_spec.secid or exp is None:
            continue
        if exp.astimezone(timezone.utc) <= current_spec.expiration_date.astimezone(timezone.utc):
            continue
        dte = (exp.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds() / 86400
        candidate_event = {
            "ticker": ticker,
            "expiration": exp.isoformat(),
            "days_to_expiration": round(dte, 3),
            "status": "checking",
            "profile_source": "",
            "reason": "",
        }
        event["candidates"].append(candidate_event)
        if dte <= float(args.no_new_expiry_days):
            candidate_event["status"] = "blocked"
            candidate_event["reason"] = "candidate_too_close_to_expiry"
            continue
        try:
            info = client.instruments.future_by(
                id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
                id=getattr(item, "uid"),
            ).instrument
            candidate_spec = spec_from_tbank(client, ticker, item, info)
        except Exception as exc:
            candidate_event["status"] = "blocked"
            candidate_event["reason"] = f"instrument_error {type(exc).__name__}: {exc}"
            continue

        profile = all_profiles.get(ticker)
        if profile is not None:
            candidate_event["profile_source"] = ticker
        else:
            profile = clone_profile_for_contract(current_profile, ticker)
            candidate_event["profile_source"] = current_profile.secid
        can_trade, fee_reason = profile_can_trade(profile, commission_side_rub(candidate_spec, 1, 0.00025, None), candidate_spec)
        candidate_event["reason"] = fee_reason
        if not can_trade:
            candidate_event["status"] = "blocked"
            continue
        live_book_ok = False
        try:
            book = client.market_data.get_order_book(figi=candidate_spec.figi, depth=int(args.orderbook_depth))
            live_book_ok = bool(getattr(book, "bids", None) and getattr(book, "asks", None))
        except Exception as exc:
            candidate_event["reason"] = f"{fee_reason}; book_error {type(exc).__name__}: {exc}"
        if live_book_ok:
            candidate_event["status"] = "selected"
            candidate_event["reason"] = fee_reason
        else:
            candidate_event["status"] = "selected_no_live_book"
            candidate_event["reason"] = f"{fee_reason}; no_live_book_startup"
        event["status"] = "roll_ready"
        event["selected"] = ticker
        event["selected_profile_source"] = candidate_event["profile_source"]
        event["reason"] = (
            f"current_dte={current_dte:.1f}d "
            f"{'candidate_ok' if live_book_ok else 'candidate_ok_no_live_book'} "
            f"{candidate_event['reason']}"
        )
        return ticker, profile, event

    event["status"] = "blocked_no_candidate"
    event["reason"] = "no viable next contract found"
    return None, None, event


def make_stream_requests(specs: list[Spec], depth: int):
    from t_tech.invest import (
        CandleInstrument,
        LastPriceInstrument,
        MarketDataRequest,
        OrderBookInstrument,
        SubscribeCandlesRequest,
        SubscribeLastPriceRequest,
        SubscribeOrderBookRequest,
        SubscribeTradesRequest,
        SubscriptionAction,
        SubscriptionInterval,
        TradeInstrument,
    )

    yield MarketDataRequest(
        subscribe_last_price_request=SubscribeLastPriceRequest(
            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[LastPriceInstrument(figi=s.figi, instrument_id=s.uid) for s in specs],
        )
    )
    yield MarketDataRequest(
        subscribe_trades_request=SubscribeTradesRequest(
            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[TradeInstrument(figi=s.figi, instrument_id=s.uid) for s in specs],
        )
    )
    yield MarketDataRequest(
        subscribe_order_book_request=SubscribeOrderBookRequest(
            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[OrderBookInstrument(figi=s.figi, instrument_id=s.uid, depth=depth) for s in specs],
        )
    )
    yield MarketDataRequest(
        subscribe_candles_request=SubscribeCandlesRequest(
            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[
                CandleInstrument(
                    figi=s.figi,
                    instrument_id=s.uid,
                    interval=SubscriptionInterval.SUBSCRIPTION_INTERVAL_ONE_MINUTE,
                )
                for s in specs
            ],
            waiting_close=True,
        )
    )
    while True:
        time.sleep(60)


def avg_volume(rows: list[dict], n: int) -> float:
    vols = [float(r.get("volume") or 0) for r in rows[-n:] if float(r.get("volume") or 0) > 0]
    return sum(vols) / len(vols) if vols else 0.0


def close_values(rows: list[dict], n: int | None = None) -> list[float]:
    subset = rows[-n:] if n else rows
    return [float(r.get("close") or 0.0) for r in subset]


def ema_value(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    ema = sum(values[:period]) / period
    alpha = 2.0 / (period + 1.0)
    for value in values[period:]:
        ema = alpha * value + (1.0 - alpha) * ema
    return ema


def stddev_value(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return math.sqrt(max(variance, 0.0))


def rsi_value(rows: list[dict], period: int = 14) -> float | None:
    values = close_values(rows)
    if len(values) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for idx in range(1, period + 1):
        change = values[idx] - values[idx - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for idx in range(period + 1, len(values)):
        change = values[idx] - values[idx - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
    if avg_loss <= 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def true_range(current: dict, prev_close: float | None = None) -> float:
    high = float(current.get("high") or 0.0)
    low = float(current.get("low") or 0.0)
    if prev_close is None:
        return max(high - low, 0.0)
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr_value(rows: list[dict], period: int = 14) -> float | None:
    if len(rows) < period + 1:
        return None
    trs: list[float] = []
    for idx in range(1, len(rows)):
        prev_close = float(rows[idx - 1].get("close") or 0.0)
        trs.append(true_range(rows[idx], prev_close))
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for value in trs[period:]:
        atr = ((atr * (period - 1)) + value) / period
    return atr


def adx_value(rows: list[dict], period: int = 14) -> float | None:
    if len(rows) < (period * 2) + 1:
        return None
    trs: list[float] = []
    pos_dm: list[float] = []
    neg_dm: list[float] = []
    for idx in range(1, len(rows)):
        current = rows[idx]
        prev = rows[idx - 1]
        current_high = float(current.get("high") or 0.0)
        current_low = float(current.get("low") or 0.0)
        prev_high = float(prev.get("high") or 0.0)
        prev_low = float(prev.get("low") or 0.0)
        up_move = current_high - prev_high
        down_move = prev_low - current_low
        trs.append(true_range(current, float(prev.get("close") or 0.0)))
        pos_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        neg_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
    if len(trs) < period:
        return None
    atr = sum(trs[:period])
    pos = sum(pos_dm[:period])
    neg = sum(neg_dm[:period])
    dxs: list[float] = []
    for idx in range(period, len(trs)):
        if idx > period:
            atr = atr - (atr / period) + trs[idx]
            pos = pos - (pos / period) + pos_dm[idx]
            neg = neg - (neg / period) + neg_dm[idx]
        if atr <= 0:
            continue
        pos_di = 100.0 * (pos / atr)
        neg_di = 100.0 * (neg / atr)
        denom = pos_di + neg_di
        if denom <= 0:
            continue
        dxs.append(100.0 * abs(pos_di - neg_di) / denom)
    if len(dxs) < period:
        return None
    adx = sum(dxs[:period]) / period
    for value in dxs[period:]:
        adx = ((adx * (period - 1)) + value) / period
    return adx


def bollinger_context(rows: list[dict], period: int = 20, std_mult: float = 2.0) -> dict:
    closes = close_values(rows)
    if len(closes) < period:
        return {
            "middle": None,
            "upper": None,
            "lower": None,
            "width_pct": None,
            "prev_width_pct": None,
            "median_width_pct": None,
        }
    window = closes[-period:]
    middle = sum(window) / period
    sd = stddev_value(window)
    if sd is None or middle == 0:
        return {
            "middle": middle,
            "upper": None,
            "lower": None,
            "width_pct": None,
            "prev_width_pct": None,
            "median_width_pct": None,
        }
    upper = middle + (std_mult * sd)
    lower = middle - (std_mult * sd)
    width_pct = (upper - lower) / middle if middle else None
    widths: list[float] = []
    for end in range(period, len(closes)):
        past_window = closes[end - period : end]
        past_middle = sum(past_window) / period
        past_sd = stddev_value(past_window)
        if past_sd is None or past_middle == 0:
            continue
        widths.append(((past_middle + std_mult * past_sd) - (past_middle - std_mult * past_sd)) / past_middle)
    prev_width_pct = widths[-1] if widths else None
    median_width_pct = median(widths[-10:]) if widths else None
    return {
        "middle": middle,
        "upper": upper,
        "lower": lower,
        "width_pct": width_pct,
        "prev_width_pct": prev_width_pct,
        "median_width_pct": median_width_pct,
    }


def choppiness_index(rows: list[dict], period: int = 14) -> float | None:
    if len(rows) < period + 1:
        return None
    window = rows[-period:]
    previous = rows[-period - 1 : -1]
    tr_sum = 0.0
    for current, prev in zip(window, previous):
        tr_sum += true_range(current, float(prev.get("close") or 0.0))
    highest = max(float(r.get("high") or 0.0) for r in window)
    lowest = min(float(r.get("low") or 0.0) for r in window)
    denominator = highest - lowest
    if tr_sum <= 0 or denominator <= 0:
        return None
    return 100.0 * math.log10(tr_sum / denominator) / math.log10(period)


def candle_close_location(row: dict) -> float | None:
    high = float(row.get("high") or 0.0)
    low = float(row.get("low") or 0.0)
    close = float(row.get("close") or 0.0)
    if high <= low:
        return None
    return (close - low) / (high - low)


def current_clock_hhmm() -> str:
    now_sec = clock_seconds_now()
    hour = now_sec // 3600
    minute = (now_sec % 3600) // 60
    return f"{hour:02d}:{minute:02d}"


def build_entry_shadow_context(state: State, direction: Direction, aggressive: bool) -> dict:
    rows = list(state.candles)
    spec = state.spec
    lookback = 4 if aggressive else 6
    recent = rows[-lookback - 1 : -1] if len(rows) > lookback else rows[:-1]
    last = rows[-1]
    prev = rows[-2]
    last_close = float(last.get("close") or 0.0)
    prev_close = float(prev.get("close") or 0.0)
    last_open = float(last.get("open") or last_close)
    avgv = avg_volume(rows[:-1], 20)
    last_vol = float(last.get("volume") or 0.0)
    volume_ratio = (last_vol / avgv) if avgv > 0 else 0.0
    vwap, _ = volume_vwap(rows, state.last_price)
    fast = sum(float(r.get("close") or 0.0) for r in rows[-2:]) / 2.0
    slow = sum(float(r.get("close") or 0.0) for r in rows[-5:]) / 5.0
    trend_ticks = (fast - slow) / spec.min_step
    momentum_ticks = (last_close - prev_close) / spec.min_step
    high = max(float(r.get("high") or 0.0) for r in recent) if recent else last_close
    low = min(float(r.get("low") or 0.0) for r in recent) if recent else last_close
    breakout_margin_ticks = (
        (last_close - high) / spec.min_step
        if direction == "long"
        else (low - last_close) / spec.min_step
    )
    recent_range_ticks = (
        max((float(r.get("high") or 0.0) - float(r.get("low") or 0.0)) / spec.min_step for r in rows[-5:])
        if len(rows) >= 5
        else 0.0
    )
    levels = best_levels(state.last_order_book, spec)
    spread_ticks = float(levels.get("spread_ticks") or 0.0)
    spread_to_stop_ratio = (spread_ticks / state.profile.stop_ticks) if state.profile.stop_ticks > 0 else None
    directional_vwap_ticks = (
        (state.last_price - vwap) / spec.min_step if direction == "long" else (vwap - state.last_price) / spec.min_step
    )
    directional_vwap_to_stop = (
        directional_vwap_ticks / state.profile.stop_ticks if state.profile.stop_ticks > 0 else None
    )
    closes = close_values(rows)
    ema9 = ema_value(closes, 9)
    ema21 = ema_value(closes, 21)
    rsi14 = rsi_value(rows, 14)
    adx14 = adx_value(rows, 14)
    atr14 = atr_value(rows, 14)
    atr14_ticks = (atr14 / spec.min_step) if atr14 is not None and spec.min_step > 0 else None
    bb = bollinger_context(rows, 20, 2.0)
    chop14 = choppiness_index(rows, 14)
    close_location = candle_close_location(last)
    donchian_window = rows[-21:-1] if len(rows) >= 21 else rows[:-1]
    donchian_breakout_ticks = None
    if donchian_window:
        donchian_high = max(float(r.get("high") or 0.0) for r in donchian_window)
        donchian_low = min(float(r.get("low") or 0.0) for r in donchian_window)
        donchian_breakout_ticks = (
            (last_close - donchian_high) / spec.min_step
            if direction == "long"
            else (donchian_low - last_close) / spec.min_step
        )
    macd_fast = ema_value(closes, 12)
    macd_slow = ema_value(closes, 26)
    macd_signal = None
    if len(closes) >= 35:
        macd_line_values: list[float] = []
        for end in range(26, len(closes) + 1):
            fast_val = ema_value(closes[:end], 12)
            slow_val = ema_value(closes[:end], 26)
            if fast_val is not None and slow_val is not None:
                macd_line_values.append(fast_val - slow_val)
        macd_signal = ema_value(macd_line_values, 9) if len(macd_line_values) >= 9 else None
    macd_line = (macd_fast - macd_slow) if macd_fast is not None and macd_slow is not None else None
    macd_hist = (macd_line - macd_signal) if macd_line is not None and macd_signal is not None else None
    return {
        "event_time": now_str(),
        "clock_hhmm": current_clock_hhmm(),
        "direction": direction,
        "aggressive": aggressive,
        "entry_price_reference": state.last_price,
        "last_open": last_open,
        "last_close": last_close,
        "vwap": vwap,
        "volume_ratio": volume_ratio,
        "momentum_ticks": momentum_ticks,
        "trend_ticks": trend_ticks,
        "breakout_margin_ticks": breakout_margin_ticks,
        "recent_range_ticks": recent_range_ticks,
        "spread_ticks": spread_ticks,
        "spread_to_stop_ratio": spread_to_stop_ratio,
        "directional_vwap_ticks": directional_vwap_ticks,
        "directional_vwap_to_stop": directional_vwap_to_stop,
        "ema9": ema9,
        "ema21": ema21,
        "rsi14": rsi14,
        "adx14": adx14,
        "atr14_ticks": atr14_ticks,
        "bb_upper": bb["upper"],
        "bb_lower": bb["lower"],
        "bb_width_pct": bb["width_pct"],
        "bb_prev_width_pct": bb["prev_width_pct"],
        "bb_median_width_pct": bb["median_width_pct"],
        "chop14": chop14,
        "close_location": close_location,
        "donchian20_breakout_ticks": donchian_breakout_ticks,
        "macd_hist": macd_hist,
    }


def evaluate_entry_shadow_models(
    state: State,
    portfolio_group: str,
    direction: Direction,
    entry_price: float,
    qty: int,
    sizing: SizingDecision,
    aggressive: bool,
) -> list[dict]:
    ctx = build_entry_shadow_context(state, direction, aggressive)
    family = state.profile.family or contract_family(state.spec.secid)
    entry_id = f"{ctx['event_time']}|{portfolio_group}|{state.contour}|{state.spec.secid}|{direction}|{entry_price}"
    base_row = {
        "entry_id": entry_id,
        "opened_at": ctx["event_time"],
        "closed_at": "",
        "portfolio_group": portfolio_group,
        "contour": state.contour,
        "family": family,
        "secid": state.spec.secid,
        "direction": direction,
        "qty": qty,
        "entry_price": entry_price,
        "exit_price": "",
        "exit_source": "",
        "net_rub": "",
        "ticks": "",
        "minutes_held": "",
        "clock_hhmm": ctx["clock_hhmm"],
        "model": "",
        "model_source": "",
        "allow": "",
        "decision_reason": "",
        "volume_ratio": round(ctx["volume_ratio"], 4),
        "spread_to_stop_ratio": round(ctx["spread_to_stop_ratio"], 4) if ctx["spread_to_stop_ratio"] is not None else "",
        "spread_ticks": round(ctx["spread_ticks"], 3),
        "momentum_ticks": round(ctx["momentum_ticks"], 3),
        "trend_ticks": round(ctx["trend_ticks"], 3),
        "breakout_margin_ticks": round(ctx["breakout_margin_ticks"], 3),
        "recent_range_ticks": round(ctx["recent_range_ticks"], 3),
        "directional_vwap_ticks": round(ctx["directional_vwap_ticks"], 3),
        "directional_vwap_to_stop": round(ctx["directional_vwap_to_stop"], 4) if ctx["directional_vwap_to_stop"] is not None else "",
        "ema9": round(ctx["ema9"], 6) if ctx["ema9"] is not None else "",
        "ema21": round(ctx["ema21"], 6) if ctx["ema21"] is not None else "",
        "rsi14": round(ctx["rsi14"], 3) if ctx["rsi14"] is not None else "",
        "adx14": round(ctx["adx14"], 3) if ctx["adx14"] is not None else "",
        "atr14_ticks": round(ctx["atr14_ticks"], 3) if ctx["atr14_ticks"] is not None else "",
        "bb_width_pct": round(ctx["bb_width_pct"], 6) if ctx["bb_width_pct"] is not None else "",
        "bb_prev_width_pct": round(ctx["bb_prev_width_pct"], 6) if ctx["bb_prev_width_pct"] is not None else "",
        "bb_median_width_pct": round(ctx["bb_median_width_pct"], 6) if ctx["bb_median_width_pct"] is not None else "",
        "chop14": round(ctx["chop14"], 3) if ctx["chop14"] is not None else "",
        "close_location": round(ctx["close_location"], 4) if ctx["close_location"] is not None else "",
        "donchian20_breakout_ticks": round(ctx["donchian20_breakout_ticks"], 3) if ctx["donchian20_breakout_ticks"] is not None else "",
        "macd_hist": round(ctx["macd_hist"], 6) if ctx["macd_hist"] is not None else "",
        "full_stop_risk_rub": round(sizing.full_stop_rub, 2),
        "stop_ticks": state.profile.stop_ticks,
        "trail_ticks": state.profile.trail_ticks,
        "trail_arm_ticks": state.profile.trail_arm_ticks,
    }

    def finalize(model: str, source: str, checks: list[tuple[bool, str]]) -> dict:
        failed = [reason for ok, reason in checks if not ok]
        row = dict(base_row)
        row["model"] = model
        row["model_source"] = source
        row["allow"] = not failed
        row["decision_reason"] = "ok" if not failed else ";".join(failed)
        return row

    is_long = direction == "long"
    clock_sec = clock_seconds_now()
    early_window_ok = parse_clock_time("10:15") <= clock_sec <= parse_clock_time("11:59")
    close_loc = ctx["close_location"]
    spread_ratio = ctx["spread_to_stop_ratio"] if ctx["spread_to_stop_ratio"] is not None else 99.0
    directional_vwap_ticks = ctx["directional_vwap_ticks"] if ctx["directional_vwap_ticks"] is not None else -999.0
    donchian_breakout_ticks = ctx["donchian20_breakout_ticks"] if ctx["donchian20_breakout_ticks"] is not None else -999.0
    atr_ticks = ctx["atr14_ticks"] if ctx["atr14_ticks"] is not None else 0.0
    price_above_ema = ctx["ema9"] is not None and ctx["ema21"] is not None and (
        (ctx["last_close"] >= ctx["ema9"] >= ctx["ema21"]) if is_long else (ctx["last_close"] <= ctx["ema9"] <= ctx["ema21"])
    )
    bb_breakout = ctx["bb_upper"] is not None and ctx["bb_lower"] is not None and (
        ctx["last_close"] >= ctx["bb_upper"] if is_long else ctx["last_close"] <= ctx["bb_lower"]
    )
    directional_close_ok = close_loc is not None and ((close_loc >= 0.65) if is_long else (close_loc <= 0.35))

    return [
        finalize(
            "tv_early_vwap_volume_breakout",
            "TradingView ORB/VWAP/Volume inspired",
            [
                (early_window_ok, "outside_1015_1159"),
                (ctx["volume_ratio"] >= 1.5, "volume_ratio_lt_1_5"),
                (spread_ratio <= 0.20, "spread_to_stop_gt_0_20"),
                (ctx["breakout_margin_ticks"] >= 2.0, "breakout_margin_lt_2"),
                (directional_vwap_ticks >= 2.0, "vwap_distance_lt_2t"),
                (directional_close_ok, "weak_close_location"),
            ],
        ),
        finalize(
            "tv_ema_rsi_adx_trend",
            "TradingView/Investing EMA+RSI+ADX trend confirmation",
            [
                (price_above_ema, "ema_alignment_fail"),
                ((ctx["rsi14"] or 0.0) >= (55.0 if is_long else 0.0), "rsi_lt_long_threshold") if is_long else (((ctx["rsi14"] or 100.0) <= 45.0), "rsi_gt_short_threshold"),
                ((ctx["adx14"] or 0.0) >= 20.0, "adx_lt_20"),
                (spread_ratio <= 0.25, "spread_to_stop_gt_0_25"),
                (directional_vwap_ticks >= 1.0, "wrong_side_of_vwap"),
            ],
        ),
        finalize(
            "tv_bb_squeeze_release",
            "TradingView Bollinger squeeze breakout inspired",
            [
                (ctx["bb_prev_width_pct"] is not None and ctx["bb_median_width_pct"] is not None and ctx["bb_prev_width_pct"] <= ctx["bb_median_width_pct"] * 0.85, "no_prior_squeeze"),
                (bb_breakout, "no_band_breakout"),
                (ctx["volume_ratio"] >= 1.25, "volume_ratio_lt_1_25"),
                (ctx["breakout_margin_ticks"] >= 2.0, "breakout_margin_lt_2"),
                (spread_ratio <= 0.25, "spread_to_stop_gt_0_25"),
            ],
        ),
        finalize(
            "forum_chop_donchian_guard",
            "Forum/Reddit Donchian breakout with anti-chop guard",
            [
                ((ctx["chop14"] or 100.0) <= 45.0, "chop_gt_45"),
                (donchian_breakout_ticks >= 2.0, "donchian_breakout_lt_2"),
                ((ctx["adx14"] or 0.0) >= 18.0, "adx_lt_18"),
                (spread_ratio <= 0.20, "spread_to_stop_gt_0_20"),
                (atr_ticks >= max(4.0, state.profile.stop_ticks * 0.08), "atr_too_small"),
            ],
        ),
    ]


def signal(state: State, aggressive: bool) -> tuple[Direction | None, str]:
    rows = list(state.candles)
    if len(rows) < 12:
        return None, f"warmup={len(rows)}"
    spec = state.spec
    lookback = 4 if aggressive else 6
    vol_mult = 0.7 if aggressive else 1.15
    vwap_buffer = 0 if aggressive else 1
    trend_need = 0.5 if aggressive else 1.0
    signal_need = 1 if aggressive else 2
    book_imbalance = 1.2 if aggressive else 1.35
    max_fee_to_stop = 0.55 if aggressive else 0.40

    last = rows[-1]
    prev = rows[-2]
    recent = rows[-lookback - 1 : -1]
    last_close = float(last["close"])
    prev_close = float(prev["close"])
    last_vol = float(last.get("volume") or 0)
    avgv = avg_volume(rows[:-1], 20)
    vwap, _ = volume_vwap(rows, state.last_price)
    mom = (last_close - prev_close) / spec.min_step
    fast = sum(float(r["close"]) for r in rows[-2:]) / 2
    slow = sum(float(r["close"]) for r in rows[-5:]) / 5
    trend = (fast - slow) / spec.min_step
    high = max(float(r["high"]) for r in recent)
    low = min(float(r["low"]) for r in recent)

    bid_qty = ask_qty = 0
    if state.last_order_book is not None:
        bid_qty = sum(int(getattr(x, "quantity", 0) or 0) for x in getattr(state.last_order_book, "bids", [])[:3])
        ask_qty = sum(int(getattr(x, "quantity", 0) or 0) for x in getattr(state.last_order_book, "asks", [])[:3])
    if bid_qty <= 0 or ask_qty <= 0:
        return None, f"book_filter empty_book bid={bid_qty} ask={ask_qty}"
    if br_small_stop_policy_applies(state.profile):
        levels = best_levels(state.last_order_book, spec)
        spread_ticks = levels.get("spread_ticks")
        max_spread = state.profile.stop_ticks * BR_POLICY_MAX_SPREAD_TO_STOP
        if spread_ticks is None or float(spread_ticks) > max_spread:
            spread_txt = "-" if spread_ticks is None else f"{float(spread_ticks):.1f}t"
            return None, f"brq6_spread_filter spread={spread_txt} stop={state.profile.stop_ticks}t max={max_spread:.1f}t"
    vol_ok = avgv > 0 and last_vol >= avgv * vol_mult
    fee_ticks = (2 * state.side_fee / spec.step_price) if spec.step_price else 999
    if fee_ticks > state.profile.stop_ticks * max_fee_to_stop:
        return None, f"fee_filter fee={fee_ticks:.1f}t stop={state.profile.stop_ticks}t"
    if max(float(r["high"]) - float(r["low"]) for r in rows[-5:]) / spec.min_step < fee_ticks + 2:
        return None, f"range_filter fee={fee_ticks:.1f}t"
    long_book = bid_qty >= ask_qty * book_imbalance if ask_qty > 0 else True
    short_book = ask_qty >= bid_qty * book_imbalance if bid_qty > 0 else True

    long_ok = (
        state.last_price >= vwap + vwap_buffer * spec.min_step
        and trend >= trend_need
        and mom >= signal_need
        and last_close >= high
        and vol_ok
        and long_book
    )
    short_ok = (
        state.last_price <= vwap - vwap_buffer * spec.min_step
        and trend <= -trend_need
        and mom <= -signal_need
        and last_close <= low
        and vol_ok
        and short_book
    )
    reason = f"p={state.last_price:g} vwap={vwap:g} mom={mom:.1f} trend={trend:.1f} vol={last_vol:.0f}/{avgv:.0f} book={bid_qty}/{ask_qty}"
    if long_ok:
        return "long", f"entry_signal long {reason}"
    if short_ok:
        return "short", f"entry_signal short {reason}"
    return None, f"watch_conditions {reason}"


def print_report(states: list[State], started: float) -> None:
    elapsed = int((time.monotonic() - started) // 60)
    print(f"{now_str()} REPORT minute={elapsed}")
    for contour in ["strict", "aggressive"]:
        subset = [s for s in states if s.contour == contour]
        closed = sum(s.closed for s in subset)
        attempts = sum(s.attempts for s in subset)
        net = sum(s.closed_net for s in subset)
        open_count = sum(1 for s in subset if s.position is not None)
        leaders = sorted(subset, key=lambda s: s.closed_net, reverse=True)[:5]
        leader_txt = " ".join(f"{s.spec.secid}:{s.closed_net:.0f}/{s.attempts}" for s in leaders)
        print(f"{now_str()} {contour} attempts={attempts} closed={closed} open={open_count} net={net:.2f} leaders={leader_txt}")


def print_portfolio_report(states: list[State], portfolio: Portfolio) -> None:
    margin = used_margin(states)
    open_count = sum(1 for s in states if s.position is not None)
    print(
        f"{now_str()} PORTFOLIO capital={portfolio.initial_capital:.0f} "
        f"equity={portfolio.equity:.2f} closed_net={portfolio.closed_net:.2f} "
        f"used_margin={margin:.2f} open={open_count}"
    )


def parse_clock_time(value: str | None) -> int | None:
    if not value:
        return None
    target = datetime.strptime(value, "%H:%M")
    return target.hour * 3600 + target.minute * 60


def clock_seconds_now() -> int:
    current = datetime.now()
    return current.hour * 3600 + current.minute * 60 + current.second


def blackout_window_active(now_sec: int, window: str) -> bool:
    normalized = normalize_blackout_window(window)
    if not normalized:
        return False
    start_raw, end_raw = normalized.split("-", 1)
    start_sec = parse_clock_time(start_raw)
    end_sec = parse_clock_time(end_raw)
    if start_sec is None or end_sec is None:
        return False
    return start_sec <= now_sec < (end_sec + 60)


def daily_trading_block_reason(no_trade_before: int | None, no_new_after: int | None, blackout_windows: list[str] | None = None) -> str | None:
    now_sec = clock_seconds_now()
    if no_trade_before is not None and now_sec < no_trade_before:
        return "before_start"
    if no_new_after is not None and now_sec >= no_new_after:
        return "after_cutoff"
    for window in blackout_windows or []:
        if blackout_window_active(now_sec, window):
            return f"blackout_window {window}"
    return None


def daily_trading_enabled(no_trade_before: int | None, no_new_after: int | None, blackout_windows: list[str] | None = None) -> bool:
    return daily_trading_block_reason(no_trade_before, no_new_after, blackout_windows) is None


def daily_force_close_due(force_close_at: int | None) -> bool:
    return force_close_at is not None and clock_seconds_now() >= force_close_at


def best_levels(orderbook: object | None, spec: Spec) -> dict:
    if orderbook is None:
        return {
            "bid": None,
            "ask": None,
            "bid_size": None,
            "ask_size": None,
            "spread_ticks": None,
        }
    bids = getattr(orderbook, "bids", []) or []
    asks = getattr(orderbook, "asks", []) or []
    bid = quotation_to_float(bids[0].price) if bids else None
    ask = quotation_to_float(asks[0].price) if asks else None
    bid_size = int(getattr(bids[0], "quantity", 0) or 0) if bids else None
    ask_size = int(getattr(asks[0], "quantity", 0) or 0) if asks else None
    spread_ticks = round((ask - bid) / spec.min_step, 3) if bid is not None and ask is not None else None
    return {
        "bid": bid,
        "ask": ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "spread_ticks": spread_ticks,
    }


def executable_price(state: State, direction: Direction | str, action: str) -> tuple[float | None, str]:
    levels = best_levels(state.last_order_book, state.spec)
    bid = levels["bid"]
    ask = levels["ask"]
    if action == "entry":
        price = ask if direction == "long" else bid
        source = "ask_entry" if direction == "long" else "bid_entry"
    else:
        price = bid if direction == "long" else ask
        source = "bid_exit" if direction == "long" else "ask_exit"
    if price is None:
        return None, "no_book"
    return round_to_step(float(price), state.spec.min_step), source


def stop_limit_available_qty(state: State, pos: Position) -> int:
    if state.last_order_book is None:
        return 0
    levels = (
        getattr(state.last_order_book, "bids", []) or []
        if pos.direction == "long"
        else getattr(state.last_order_book, "asks", []) or []
    )
    qty = 0
    for level in levels:
        price = quotation_to_float(level.price)
        if pos.direction == "long":
            if price < pos.stop_price:
                continue
        else:
            if price > pos.stop_price:
                continue
        qty += int(getattr(level, "quantity", 0) or 0)
    return qty


def stop_adverse_overrun_ticks(pos: Position, trigger_price: float, spec: Spec) -> float:
    if pos.direction == "long":
        return max(0.0, (pos.stop_price - trigger_price) / spec.min_step)
    return max(0.0, (trigger_price - pos.stop_price) / spec.min_step)


def stop_limit_fill_price(
    state: State,
    pos: Position,
    trigger_price: float,
    emergency_ticks: float,
) -> tuple[float | None, str, int, float]:
    spec = state.spec
    if not is_stop_hit(pos, trigger_price):
        return None, "stop_limit_waiting", 0, 0.0
    available_qty = stop_limit_available_qty(state, pos)
    if available_qty >= pos.qty:
        return round_to_step(pos.stop_price, spec.min_step), "broker_stop_limit_fill", available_qty, 0.0
    overrun = stop_adverse_overrun_ticks(pos, trigger_price, spec)
    if overrun >= emergency_ticks:
        return round_to_step(trigger_price, spec.min_step), "emergency_market_after_missed_limit", available_qty, overrun
    return None, "stop_limit_touched_waiting_fill", available_qty, overrun


def clone_position(pos: Position) -> Position:
    return Position(
        direction=pos.direction,
        entry_price=pos.entry_price,
        qty=pos.qty,
        best_price=pos.best_price,
        stop_price=pos.stop_price,
        opened_at=pos.opened_at,
    )


def shadow_entry_anchor_model(actual_exit_model: str) -> str:
    return "candle_like" if str(actual_exit_model or "").strip() == "candle_like" else "stream_stoplimit"


def activate_blocked_entry_shadow_tracking(
    state: State,
    *,
    direction: Direction,
    entry_price: float,
    qty: int,
    spec: Spec,
    actual_exit_model: str,
    decisions: list[dict],
) -> None:
    synthetic = open_position(direction, entry_price, qty, state.profile.stop_ticks, state.profile.trail_ticks, spec)
    state.shadow_positions = {
        "stream_stoplimit": clone_position(synthetic),
        "candle_like": clone_position(synthetic),
    }
    state.shadow_closed = {}
    state.shadow_close_details = {}
    state.shadow_entry_mode = "blocked_entry"
    state.shadow_entry_anchor_model = shadow_entry_anchor_model(actual_exit_model)
    state.entry_shadow_decisions = list(decisions)


def has_active_shadow(state: State) -> bool:
    return any(not state.shadow_closed.get(model) for model in state.shadow_positions)


def all_shadow_models_closed(state: State) -> bool:
    return bool(state.shadow_positions) and all(state.shadow_closed.get(model) for model in state.shadow_positions)


def close_shadow(
    path: Path,
    state: State,
    model: str,
    pos: Position,
    exit_price: float,
    exit_source: str,
    trigger_price: float | None,
    trigger_source: str,
    stop_limit_qty: int | None = None,
    stop_overrun_ticks: float | None = None,
) -> None:
    ticks, gross, net = pnl_rub(pos, exit_price, state.spec, state.side_fee)
    closed_at = now_str()
    row = {
        "closed_at": closed_at,
        "opened_at": pos.opened_at,
        "minutes_held": position_minutes_held(pos, closed_at),
        "model": model,
        "contour": state.contour,
        "family": state.profile.family or contract_family(state.spec.secid),
        "profile_source": state.profile.source_secid or state.profile.secid,
        "secid": state.spec.secid,
        "direction": pos.direction,
        "qty": pos.qty,
        "entry_price": pos.entry_price,
        "exit_price": exit_price,
        "exit_source": exit_source,
        "trigger_price": trigger_price,
        "trigger_source": trigger_source,
        "stop_limit_qty": stop_limit_qty,
        "stop_overrun_ticks": stop_overrun_ticks,
        "ticks": round(ticks, 3),
        "gross_rub": round(gross, 2),
        "fees_rub": round(2 * state.side_fee * pos.qty, 2),
        "net_rub": round(net, 2),
        "stop_ticks": state.profile.stop_ticks,
        "trail_ticks": state.profile.trail_ticks,
        "trail_arm_ticks": state.profile.trail_arm_ticks,
        "target_min_ticks": state.profile.target_min_ticks,
        "full_stop_1lot_rub": round(full_stop_risk_rub(state.profile, state.spec, state.side_fee, 1), 2),
        "full_stop_risk_rub": round(full_stop_risk_rub(state.profile, state.spec, state.side_fee, pos.qty), 2),
    }
    append_trade(path, row)
    state.shadow_close_details[model] = {
        "closed_at": closed_at,
        "minutes_held": row["minutes_held"],
        "exit_price": exit_price,
        "exit_source": exit_source,
        "net_rub": row["net_rub"],
        "ticks": row["ticks"],
    }
    state.shadow_closed[model] = True


def parse_position_time(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def position_minutes_held(pos: Position, closed_at: str | None = None) -> int | None:
    opened = parse_position_time(pos.opened_at)
    closed = parse_position_time(closed_at) if closed_at else datetime.now()
    if opened is None or closed is None:
        return None
    return max(0, int((closed - opened).total_seconds() // 60))


def update_shadow_models(
    state: State,
    path: Path,
    exit_price: float,
    exit_source: str,
    candle_closed: bool,
    emergency_ticks: float,
) -> None:
    if not state.shadow_positions:
        return
    stream_pos = state.shadow_positions.get("stream_stoplimit")
    if stream_pos is not None and not state.shadow_closed.get("stream_stoplimit"):
        move_ticks = (
            (exit_price - stream_pos.entry_price) / state.spec.min_step
            if stream_pos.direction == "long"
            else (stream_pos.entry_price - exit_price) / state.spec.min_step
        )
        fee_ticks = (2 * state.side_fee / state.spec.step_price) if state.spec.step_price else 999
        if move_ticks >= max(state.profile.trail_arm_ticks, fee_ticks + 1):
            update_stop(stream_pos, exit_price, state.profile.trail_ticks, state.spec)
            min_net_stop = stream_pos.entry_price + (fee_ticks + 0.5) * state.spec.min_step if stream_pos.direction == "long" else stream_pos.entry_price - (fee_ticks + 0.5) * state.spec.min_step
            if stream_pos.direction == "long":
                stream_pos.stop_price = max(stream_pos.stop_price, round_to_step(min_net_stop, state.spec.min_step))
            else:
                stream_pos.stop_price = min(stream_pos.stop_price, round_to_step(min_net_stop, state.spec.min_step))
        fill_price, fill_source, stop_qty, overrun = stop_limit_fill_price(state, stream_pos, exit_price, emergency_ticks)
        if fill_price is not None:
            close_shadow(path, state, "stream_stoplimit", stream_pos, fill_price, fill_source, exit_price, exit_source, stop_qty, overrun)

    candle_pos = state.shadow_positions.get("candle_like")
    if candle_pos is not None and not state.shadow_closed.get("candle_like") and candle_closed:
        candle_exit = state.last_price
        move_ticks = (
            (candle_exit - candle_pos.entry_price) / state.spec.min_step
            if candle_pos.direction == "long"
            else (candle_pos.entry_price - candle_exit) / state.spec.min_step
        )
        fee_ticks = (2 * state.side_fee / state.spec.step_price) if state.spec.step_price else 999
        if move_ticks >= max(state.profile.trail_arm_ticks, fee_ticks + 1):
            update_stop(candle_pos, candle_exit, state.profile.trail_ticks, state.spec)
            min_net_stop = candle_pos.entry_price + (fee_ticks + 0.5) * state.spec.min_step if candle_pos.direction == "long" else candle_pos.entry_price - (fee_ticks + 0.5) * state.spec.min_step
            if candle_pos.direction == "long":
                candle_pos.stop_price = max(candle_pos.stop_price, round_to_step(min_net_stop, state.spec.min_step))
            else:
                candle_pos.stop_price = min(candle_pos.stop_price, round_to_step(min_net_stop, state.spec.min_step))
        if is_stop_hit(candle_pos, candle_exit):
            close_shadow(path, state, "candle_like", candle_pos, candle_pos.stop_price, "candle_like_stop_fill", candle_exit, "closed_1m_candle")


def close_all_shadow_positions(
    state: State,
    path: Path,
    exit_price: float,
    exit_source: str,
) -> None:
    fill_price = round_to_step(exit_price, state.spec.min_step)
    for model, pos in list(state.shadow_positions.items()):
        if state.shadow_closed.get(model):
            continue
        close_shadow(path, state, model, pos, fill_price, exit_source, exit_price, exit_source)


def write_entry_shadow_decisions(
    path: Path,
    decisions: list[dict],
    *,
    closed_at: str,
    minutes_held: int | None,
    exit_price: float,
    exit_source: str,
    net_rub: float,
    ticks: float,
) -> None:
    for row in decisions:
        out = dict(row)
        out["closed_at"] = closed_at
        out["minutes_held"] = minutes_held
        out["exit_price"] = exit_price
        out["exit_source"] = exit_source
        out["net_rub"] = round(net_rub, 2)
        out["ticks"] = round(ticks, 3)
        append_schema_stable_csv(path, out)


def finalize_shadow_only_entry_tracking(state: State, args: argparse.Namespace) -> None:
    if state.shadow_entry_mode != "blocked_entry" or not state.entry_shadow_decisions:
        return
    anchor_model = state.shadow_entry_anchor_model or shadow_entry_anchor_model(str(getattr(args, "actual_exit_model", "")))
    details = state.shadow_close_details.get(anchor_model)
    if not isinstance(details, dict):
        details = next(iter(state.shadow_close_details.values()), {})
    if not isinstance(details, dict) or not details:
        state.entry_shadow_decisions = []
        state.shadow_entry_mode = ""
        state.shadow_entry_anchor_model = ""
        state.shadow_close_details = {}
        return
    write_entry_shadow_decisions(
        entry_shadow_log_path(args),
        state.entry_shadow_decisions,
        closed_at=str(details.get("closed_at") or ""),
        minutes_held=position_minutes_held(
            next(iter(state.shadow_positions.values())),
            str(details.get("closed_at") or ""),
        ) if state.shadow_positions else details.get("minutes_held"),
        exit_price=float(details.get("exit_price") or 0.0),
        exit_source=f"shadow_only::{details.get('exit_source') or anchor_model}",
        net_rub=float(details.get("net_rub") or 0.0),
        ticks=float(details.get("ticks") or 0.0),
    )
    state.entry_shadow_decisions = []
    state.shadow_entry_mode = ""
    state.shadow_entry_anchor_model = ""
    state.shadow_close_details = {}


def process_open_state_exit(
    st: State,
    args: argparse.Namespace,
    portfolio: Portfolio,
    states: list[State],
    candle_closed: bool,
    auto_policy: dict | None = None,
    actual_trigger_override: float | None = None,
    actual_trigger_source_override: str | None = None,
    force_exit_reason: str | None = None,
) -> None:
    if st.position is None:
        if has_active_shadow(st):
            anchor = st.shadow_entry_anchor_model or shadow_entry_anchor_model(str(getattr(args, "actual_exit_model", "")))
            shadow_pos = st.shadow_positions.get(anchor) or next(iter(st.shadow_positions.values()))
            shadow_exit_price, shadow_exit_source = executable_price(st, shadow_pos.direction, "exit")
            if shadow_exit_price is not None:
                if force_exit_reason:
                    close_all_shadow_positions(
                        st,
                        Path(args.shadow_log),
                        shadow_exit_price,
                        force_exit_reason,
                    )
                else:
                    update_shadow_models(
                        st,
                        Path(args.shadow_log),
                        shadow_exit_price,
                        shadow_exit_source,
                        candle_closed,
                        float(args.stop_limit_emergency_ticks),
                    )
            st.last_reason = "blocked_entry_shadow_active" if st.shadow_entry_mode == "blocked_entry" else "shadow_compare_active"
            if all_shadow_models_closed(st):
                finalize_shadow_only_entry_tracking(st, args)
                st.shadow_positions = {}
                st.shadow_closed = {}
                st.shadow_close_details = {}
                st.shadow_entry_mode = ""
                st.shadow_entry_anchor_model = ""
                write_open_positions(Path(args.open_positions_log), states)
        return

    pos = st.position
    exit_price, exit_source = executable_price(st, pos.direction, "exit")
    if exit_price is None:
        st.last_reason = "book_filter no_executable_exit"
        return
    update_shadow_models(
        st,
        Path(args.shadow_log),
        exit_price,
        exit_source,
        candle_closed,
        float(args.stop_limit_emergency_ticks),
    )
    fill_price = None
    fill_source = "actual_waiting"
    stop_limit_qty = None
    stop_overrun_ticks = None
    actual_trigger_price = actual_trigger_override if actual_trigger_override is not None else exit_price
    actual_trigger_source = actual_trigger_source_override or exit_source
    fee_ticks = (2 * st.side_fee / st.spec.step_price) if st.spec.step_price else 999
    expiry_close_days = float(getattr(args, "expiry_force_close_days", 0.0))
    dte = days_to_expiration(st.spec)
    if force_exit_reason:
        fill_price = round_to_step(exit_price, st.spec.min_step)
        fill_source = force_exit_reason
        actual_trigger_price = fill_price
        actual_trigger_source = force_exit_reason
    elif expiry_close_days > 0 and dte is not None and dte <= expiry_close_days:
        fill_price = round_to_step(exit_price, st.spec.min_step)
        fill_source = "expiry_force_close"
        actual_trigger_price = fill_price
        actual_trigger_source = f"expiry_dte_{dte:.1f}d"
    elif args.actual_exit_model == "candle_like":
        if candle_closed:
            actual_trigger_price = round_to_step(actual_trigger_price, st.spec.min_step)
            move_ticks = (
                (actual_trigger_price - pos.entry_price) / st.spec.min_step
                if pos.direction == "long"
                else (pos.entry_price - actual_trigger_price) / st.spec.min_step
            )
            if move_ticks >= max(st.profile.trail_arm_ticks, fee_ticks + 1):
                update_stop(pos, actual_trigger_price, st.profile.trail_ticks, st.spec)
                min_net_stop = pos.entry_price + (fee_ticks + 0.5) * st.spec.min_step if pos.direction == "long" else pos.entry_price - (fee_ticks + 0.5) * st.spec.min_step
                if pos.direction == "long":
                    pos.stop_price = max(pos.stop_price, round_to_step(min_net_stop, st.spec.min_step))
                else:
                    pos.stop_price = min(pos.stop_price, round_to_step(min_net_stop, st.spec.min_step))
            if is_stop_hit(pos, actual_trigger_price):
                fill_price = round_to_step(pos.stop_price, st.spec.min_step)
                fill_source = "actual_candle_like_stop_fill"
    else:
        move_ticks = (
            (exit_price - pos.entry_price) / st.spec.min_step
            if pos.direction == "long"
            else (pos.entry_price - exit_price) / st.spec.min_step
        )
        if move_ticks >= max(st.profile.trail_arm_ticks, fee_ticks + 1):
            update_stop(pos, exit_price, st.profile.trail_ticks, st.spec)
            min_net_stop = pos.entry_price + (fee_ticks + 0.5) * st.spec.min_step if pos.direction == "long" else pos.entry_price - (fee_ticks + 0.5) * st.spec.min_step
            if pos.direction == "long":
                pos.stop_price = max(pos.stop_price, round_to_step(min_net_stop, st.spec.min_step))
            else:
                pos.stop_price = min(pos.stop_price, round_to_step(min_net_stop, st.spec.min_step))
        fill_price, fill_source, stop_limit_qty, stop_overrun_ticks = stop_limit_fill_price(
            st,
            st.position,
            exit_price,
            float(args.stop_limit_emergency_ticks),
        )
        if fill_price is None and fill_source != "stop_limit_waiting":
            st.last_reason = (
                f"{fill_source} stop={st.position.stop_price:g} "
                f"trigger={exit_price:g} book_qty={stop_limit_qty} "
                f"overrun={stop_overrun_ticks:.1f}t"
            )
    if fill_price is None:
        return
    if not st.shadow_closed.get("actual"):
        close_shadow(
            Path(args.shadow_log),
            st,
            "actual",
            st.position,
            fill_price,
            fill_source,
            actual_trigger_price,
            actual_trigger_source,
            stop_limit_qty,
            stop_overrun_ticks,
        )
    closed_at = now_str()
    ticks, gross, net = pnl_rub(st.position, fill_price, st.spec, st.side_fee)
    st.closed += 1
    st.closed_net += net
    portfolio.closed_net += net
    if st.entry_shadow_decisions:
        write_entry_shadow_decisions(
            entry_shadow_log_path(args),
            st.entry_shadow_decisions,
            closed_at=closed_at,
            minutes_held=position_minutes_held(st.position, closed_at),
            exit_price=fill_price,
            exit_source=fill_source,
            net_rub=net,
            ticks=ticks,
        )
        st.entry_shadow_decisions = []
    append_trade(
        Path(args.log),
        {
            "closed_at": closed_at,
            "opened_at": st.position.opened_at,
            "minutes_held": position_minutes_held(st.position),
            "portfolio_group": trade_log_group(Path(args.log)),
            "contour": st.contour,
            "family": st.profile.family or contract_family(st.spec.secid),
            "profile_source": st.profile.source_secid or st.profile.secid,
            "secid": st.spec.secid,
            "direction": st.position.direction,
            "qty": st.position.qty,
            "entry_price": st.position.entry_price,
            "exit_price": fill_price,
            "exit_source": fill_source,
            "trigger_price": actual_trigger_price,
            "trigger_source": actual_trigger_source,
            "stop_limit_qty": stop_limit_qty,
            "stop_overrun_ticks": round(stop_overrun_ticks, 3) if stop_overrun_ticks is not None else None,
            "ticks": round(ticks, 3),
            "gross_rub": round(gross, 2),
            "fees_rub": round(2 * st.side_fee * st.position.qty, 2),
            "net_rub": round(net, 2),
            "closed_net_rub": round(st.closed_net, 2),
            "stop_ticks": st.profile.stop_ticks,
            "trail_ticks": st.profile.trail_ticks,
            "trail_arm_ticks": st.profile.trail_arm_ticks,
            "target_min_ticks": st.profile.target_min_ticks,
            "full_stop_1lot_rub": round(full_stop_risk_rub(st.profile, st.spec, st.side_fee, 1), 2),
            "full_stop_risk_rub": round(full_stop_risk_rub(st.profile, st.spec, st.side_fee, st.position.qty), 2),
        },
    )
    print(
        f"{now_str()} CLOSE {st.contour} {st.spec.secid} exit={fill_price:g} "
        f"source={fill_source} trigger={actual_trigger_price:g}/{actual_trigger_source} "
        f"ticks={ticks:.1f} net={net:.2f} total={st.closed_net:.2f}"
    )
    apply_br_loss_pause(st, states, net)
    apply_auto_loss_pause(st, states, net, auto_policy if isinstance(auto_policy, dict) else empty_auto_policy())
    st.position = None
    if all_shadow_models_closed(st):
        st.shadow_positions = {}
        st.shadow_closed = {}
    st.cooldown_until = max(st.cooldown_until, time.monotonic() + 90)
    write_open_positions(Path(args.open_positions_log), states)


def poll_market_fallback(
    token: str,
    specs: list[Spec],
    state_by_uid: dict[tuple[str, str], State],
    states: list[State],
    args: argparse.Namespace,
    portfolio: Portfolio,
    last_stream_event: list[float],
    started: float,
    runtime_state: dict[str, object],
    auto_policy_state: dict[str, object],
    stop_event: threading.Event,
    lock: threading.RLock,
) -> None:
    from t_tech.invest import Client

    rate_limit_pause_until = 0.0
    last_backoff_notice = 0.0
    while not stop_event.is_set():
        try:
            with Client(token) as poll_client:
                while not stop_event.is_set():
                    time.sleep(max(0.5, float(args.fallback_poll_sec)))
                    now_mono = time.monotonic()
                    if now_mono < rate_limit_pause_until:
                        if now_mono - last_backoff_notice >= 15:
                            print(
                                f"{now_str()} POLL backoff active wait_sec={rate_limit_pause_until - now_mono:.1f}",
                                flush=True,
                            )
                            last_backoff_notice = now_mono
                        continue
                    if time.monotonic() - last_stream_event[0] < float(args.stream_stale_sec):
                        continue
                    try:
                        prices = poll_client.market_data.get_last_prices(figi=[s.figi for s in specs]).last_prices
                        price_by_uid = {p.instrument_uid: quotation_to_float(p.price) for p in prices}
                    except Exception as exc:
                        print(f"{now_str()} POLL last_price_error={exc}", flush=True)
                        price_by_uid = {}
                        if is_resource_exhausted_error(exc):
                            backoff_sec = rate_limit_backoff_sec(exc)
                            rate_limit_pause_until = max(rate_limit_pause_until, time.monotonic() + backoff_sec)
                            print(
                                f"{now_str()} POLL backoff resource_exhausted stage=last_prices wait_sec={backoff_sec:.1f}",
                                flush=True,
                            )
                            last_backoff_notice = 0.0
                            continue
                    with lock:
                        rate_limited = False
                        for spec in specs:
                            try:
                                orderbook = poll_client.market_data.get_order_book(figi=spec.figi, depth=int(args.orderbook_depth))
                            except Exception as exc:
                                orderbook = None
                                print(f"{now_str()} POLL orderbook_error {spec.secid} {exc}", flush=True)
                                if is_resource_exhausted_error(exc):
                                    backoff_sec = rate_limit_backoff_sec(exc)
                                    rate_limit_pause_until = max(rate_limit_pause_until, time.monotonic() + backoff_sec)
                                    print(
                                        f"{now_str()} POLL backoff resource_exhausted stage=order_book secid={spec.secid} wait_sec={backoff_sec:.1f}",
                                        flush=True,
                                    )
                                    last_backoff_notice = 0.0
                                    rate_limited = True
                            for contour in ["strict", "aggressive"]:
                                st = state_by_uid.get((spec.uid, contour))
                                if st is None:
                                    continue
                                if spec.uid in price_by_uid:
                                    st.last_price = round_to_step(price_by_uid[spec.uid], spec.min_step)
                                if orderbook is not None:
                                    st.last_order_book = orderbook
                                force_close_due = daily_force_close_due(parse_clock_time(getattr(args, "force_close_at", "")))
                                force_reason = "scheduled_force_close" if force_close_due and (st.position is not None or has_active_shadow(st)) else None
                                process_open_state_exit(
                                    st,
                                    args,
                                    portfolio,
                                    states,
                                    candle_closed=False,
                                    auto_policy=refresh_auto_policy(auto_policy_state),
                                    actual_trigger_source_override="polling_fallback",
                                    force_exit_reason=force_reason,
                                )
                            if rate_limited:
                                break
                        active_auto_policy_poll = refresh_auto_policy(auto_policy_state)
                        poll_trading_enabled = daily_trading_enabled(
                            effective_no_trade_before(parse_clock_time(getattr(args, "no_trade_before", "")), active_auto_policy_poll),
                            effective_no_new_after(parse_clock_time(getattr(args, "no_new_after", "")), active_auto_policy_poll),
                            effective_entry_blackout_windows(normalize_blackout_windows(getattr(args, "entry_blackout_window", [])), active_auto_policy_poll),
                        )
                        write_open_positions(Path(args.open_positions_log), states)
                        write_microstructure_snapshot(
                            Path(args.snapshot_log),
                            states,
                            trading_enabled=poll_trading_enabled,
                            no_trade_before=effective_no_trade_before(parse_clock_time(getattr(args, "no_trade_before", "")), active_auto_policy_poll),
                            no_new_after=effective_no_new_after(parse_clock_time(getattr(args, "no_new_after", "")), active_auto_policy_poll),
                            base_blackout_windows=normalize_blackout_windows(getattr(args, "entry_blackout_window", [])),
                            auto_policy=active_auto_policy_poll,
                            portfolio_group_name=trade_log_group(Path(getattr(args, "log", ""))).upper(),
                        )
                        write_bot_health(
                            Path(args.health_log),
                            states,
                            portfolio,
                            started,
                            last_stream_event,
                            int(runtime_state.get("reconnect_count", 0)),
                            str(runtime_state.get("last_stream_error", "")),
                            "polling_fallback",
                            active_auto_policy_poll,
                        )
                    print(f"{now_str()} POLL fallback active stale_sec={time.monotonic() - last_stream_event[0]:.1f}", flush=True)
        except Exception as exc:
            if not stop_event.is_set():
                print(f"{now_str()} POLL reconnect_after_error {type(exc).__name__}: {exc}", flush=True)
                time.sleep(3)


def write_microstructure_snapshot(
    path: Path,
    states: list[State],
    trading_enabled: bool,
    no_trade_before: int | None = None,
    no_new_after: int | None = None,
    base_blackout_windows: object = None,
    auto_policy: dict | None = None,
    portfolio_group_name: str = "",
) -> None:
    rows = []
    spread_review_rows = []
    snapshot_time = now_str()
    for st in states:
        if st.contour != "aggressive":
            continue
        state_blackout_windows = effective_entry_blackout_windows(
            base_blackout_windows,
            auto_policy if isinstance(auto_policy, dict) else {},
            portfolio_group_name,
            st.contour,
        ) if any(value is not None and value != [] and value != "" for value in [base_blackout_windows, auto_policy]) else []
        state_gate_reason = daily_trading_block_reason(no_trade_before, no_new_after, state_blackout_windows)
        state_can_open = state_gate_reason is None if (no_trade_before is not None or no_new_after is not None or state_blackout_windows) else trading_enabled
        levels = best_levels(st.last_order_book, st.spec)
        spread_ratio, spread_class, spread_review = spread_to_stop_metrics(
            levels["spread_ticks"],
            st.profile.stop_ticks,
        )
        fee_to_stop = fee_ticks(st.side_fee, st.spec) / st.profile.stop_ticks if st.profile.stop_ticks > 0 else None
        rows.append(
            {
                "snapshot_time": snapshot_time,
                "target_contract": st.spec.secid,
                "last_price_target": st.last_price,
                "bid_target": levels["bid"],
                "ask_target": levels["ask"],
                "bid_size_target": levels["bid_size"],
                "ask_size_target": levels["ask_size"],
                "spread_ticks_target": levels["spread_ticks"],
                "stop_ticks": st.profile.stop_ticks,
                "trail_ticks": st.profile.trail_ticks,
                "trail_arm_ticks": st.profile.trail_arm_ticks,
                "spread_to_stop_ratio": round(spread_ratio, 4) if spread_ratio is not None else None,
                "spread_class": spread_class,
                "spread_review_flag": spread_review,
                "fee_to_stop_ratio": round(fee_to_stop, 4) if fee_to_stop is not None else None,
                "signal_status": "listening" if state_can_open else "warmup_or_no_new_entries",
                "skip_reason": "" if state_can_open else (state_gate_reason or "time_gate"),
                "orderbook_source": "tbank_stream",
                "execution_validation_possible": levels["bid"] is not None and levels["ask"] is not None,
                "session_phase": "stream",
                "can_open_new_paper_trade": state_can_open,
                "last_reason": st.last_reason,
            }
        )
        if spread_review:
            spread_review_rows.append(
                {
                    "snapshot_time": snapshot_time,
                    "ticker": st.spec.secid,
                    "family": state_family(st),
                    "last_price": st.last_price,
                    "bid": levels["bid"],
                    "ask": levels["ask"],
                    "bid_size": levels["bid_size"],
                    "ask_size": levels["ask_size"],
                    "spread_ticks": levels["spread_ticks"],
                    "stop_ticks": st.profile.stop_ticks,
                    "spread_to_stop_ratio": round(spread_ratio, 4) if spread_ratio is not None else None,
                    "spread_class": spread_class,
                    "last_reason": st.last_reason,
                }
            )
    for row in rows:
        append_schema_stable_csv(path, row)
    if spread_review_rows:
        review_name = path.name.replace("live_orderbook_snapshots", "wide_spread_review")
        if review_name == path.name:
            review_name = f"{path.stem}_wide_spread_review{path.suffix}"
        review_path = path.with_name(review_name)
        for row in spread_review_rows:
            append_schema_stable_csv(review_path, row)


def write_instrument_specs(path: Path, specs: list[Spec]) -> None:
    rows = []
    for spec in specs:
        dte = days_to_expiration(spec)
        rows.append(
            {
                "ticker": spec.secid,
                "figi": spec.figi,
                "uid": spec.uid,
                "expiration": spec.expiration_date.isoformat() if spec.expiration_date else "",
                "days_to_expiration": round(dte, 3) if dte is not None else "",
                "tick": spec.min_step,
                "tick_rub": spec.step_price,
                "last_price": round(spec.last_price, 6),
                "last_rub": round(spec.last_rub, 6),
                "go_buy": round(spec.margin_buy, 2),
                "go_sell": round(spec.margin_sell, 2),
                "source": "tbank_api",
                "updated_at": now_str(),
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["ticker"])
        writer.writeheader()
        writer.writerows(rows)


def write_startup_status(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "timestamp",
        "ticker",
        "status",
        "reason",
        "load_reason",
        "profile_source",
        "family",
        "expiration",
        "days_to_expiration",
        "tick",
        "tick_rub",
        "last_price",
        "go_buy",
        "go_sell",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_roll_state(path: Path, roll_events: list[dict], specs: list[Spec], args: argparse.Namespace) -> None:
    payload = {
        "updated_at": now_str(),
        "auto_roll_enabled": not bool(getattr(args, "disable_auto_roll", False)),
        "roll_observe_days": float(getattr(args, "roll_observe_days", 0.0)),
        "no_new_expiry_days": float(getattr(args, "no_new_expiry_days", 0.0)),
        "expiry_force_close_days": float(getattr(args, "expiry_force_close_days", 0.0)),
        "loaded_contracts": [
            {
                "ticker": spec.secid,
                "family": contract_family(spec.secid),
                "expiration": spec.expiration_date.isoformat() if spec.expiration_date else "",
                "days_to_expiration": round(days_to_expiration(spec), 3) if days_to_expiration(spec) is not None else None,
            }
            for spec in specs
        ],
        "roll_events": roll_events,
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_open_positions(path: Path, states: list[State]) -> None:
    def serialize_position_payload(pos: Position) -> dict:
        return {
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "qty": pos.qty,
            "best_price": pos.best_price,
            "stop_price": pos.stop_price,
            "opened_at": pos.opened_at,
        }

    rows = []
    for st in states:
        if st.position is None:
            continue
        mark_price, mark_source = executable_price(st, st.position.direction, "exit")
        rows.append(
            {
                "contour": st.contour,
                "ticker": st.spec.secid,
                "direction": st.position.direction,
                "qty": st.position.qty,
                "entry_price": st.position.entry_price,
                "best_price": st.position.best_price,
                "last_price": st.last_price,
                "mark_price": mark_price,
                "mark_source": mark_source,
                "stop_price": st.position.stop_price,
                "full_stop_risk_rub": round(full_stop_risk_rub(st.profile, st.spec, st.side_fee, st.position.qty), 2),
                "full_stop_gross_rub": round(st.profile.stop_ticks * st.spec.step_price * st.position.qty, 2),
                "margin_rub": round(position_margin(st.spec, st.position.direction, st.position.qty), 2),
                "opened_at": st.position.opened_at,
            }
        )
    atomic_write_text(path, json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    shadow_rows = []
    for st in states:
        if (
            not st.shadow_positions
            and not st.entry_shadow_decisions
            and not st.shadow_close_details
            and not st.shadow_entry_mode
            and not st.shadow_entry_anchor_model
        ):
            continue
        shadow_rows.append(
            {
                "contour": st.contour,
                "ticker": st.spec.secid,
                "shadow_entry_mode": st.shadow_entry_mode,
                "shadow_entry_anchor_model": st.shadow_entry_anchor_model,
                "entry_shadow_decisions": list(st.entry_shadow_decisions),
                "shadow_positions": {
                    model: serialize_position_payload(pos)
                    for model, pos in st.shadow_positions.items()
                },
                "shadow_closed": dict(st.shadow_closed),
                "shadow_close_details": dict(st.shadow_close_details),
            }
        )
    if shadow_rows:
        rows = [{"_kind": "shadow_state", "states": shadow_rows}] + rows
    atomic_write_text(path, json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def restore_open_positions(path: Path, states: list[State]) -> int:
    def deserialize_position_payload(payload: object) -> Position | None:
        if not isinstance(payload, dict):
            return None
        try:
            return Position(
                direction=str(payload["direction"]),
                entry_price=float(payload["entry_price"]),
                qty=int(float(payload["qty"])),
                best_price=float(payload["best_price"]),
                stop_price=float(payload["stop_price"]),
                opened_at=str(payload.get("opened_at") or now_str()),
            )
        except Exception:
            return None

    def shadow_state_path(base_path: Path) -> Path:
        return base_path.with_name(f"{base_path.stem}_shadow_state{base_path.suffix}")

    if not path.exists() or path.stat().st_size == 0:
        return 0
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"{now_str()} RESTORE open_positions_read_error={exc}", flush=True)
        return 0
    if not isinstance(rows, list):
        return 0
    by_key = {(st.contour, st.spec.secid): st for st in states}
    restored = 0
    embedded_shadow_rows: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("_kind") or "") == "shadow_state":
            payload = row.get("states")
            if isinstance(payload, list):
                embedded_shadow_rows.extend(item for item in payload if isinstance(item, dict))
            continue
        key = (str(row.get("contour") or ""), str(row.get("ticker") or ""))
        st = by_key.get(key)
        if st is None or st.position is not None:
            continue
        try:
            direction = str(row["direction"])
            entry = float(row["entry_price"])
            qty = int(float(row["qty"]))
            stop = float(row["stop_price"])
            best_raw = row.get("best_price")
            best = float(best_raw) if best_raw not in (None, "") else entry
            opened_at = str(row.get("opened_at") or now_str())
        except Exception as exc:
            print(f"{now_str()} RESTORE skip_bad_position {key} error={exc}", flush=True)
            continue
        st.position = Position(
            direction=direction,
            entry_price=entry,
            qty=qty,
            best_price=best,
            stop_price=stop,
            opened_at=opened_at,
        )
        st.attempts = max(st.attempts, 1)
        st.last_reason = "restored_open_position"
        restored += 1
    if restored:
        print(f"{now_str()} RESTORE open_positions count={restored} source={path}", flush=True)
    shadow_rows_to_restore = embedded_shadow_rows
    shadow_source = path
    shadow_path = shadow_state_path(path)
    shadow_restored = 0
    if not shadow_rows_to_restore and shadow_path.exists() and shadow_path.stat().st_size > 0:
        try:
            shadow_rows = json.loads(shadow_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"{now_str()} RESTORE shadow_state_read_error={exc}", flush=True)
            shadow_rows = []
        if isinstance(shadow_rows, list):
            shadow_rows_to_restore = [item for item in shadow_rows if isinstance(item, dict)]
            shadow_source = shadow_path
    for row in shadow_rows_to_restore:
        key = (str(row.get("contour") or ""), str(row.get("ticker") or ""))
        st = by_key.get(key)
        if st is None:
            continue
        shadow_positions = {}
        for model, payload in (row.get("shadow_positions") or {}).items():
            pos = deserialize_position_payload(payload)
            if pos is not None:
                shadow_positions[str(model)] = pos
        shadow_closed = {}
        for model, value in (row.get("shadow_closed") or {}).items():
            shadow_closed[str(model)] = bool(value)
        shadow_close_details = {}
        for model, payload in (row.get("shadow_close_details") or {}).items():
            if isinstance(payload, dict):
                shadow_close_details[str(model)] = dict(payload)
        decisions = row.get("entry_shadow_decisions")
        st.shadow_positions = shadow_positions
        st.shadow_closed = shadow_closed
        st.shadow_close_details = shadow_close_details
        st.shadow_entry_mode = str(row.get("shadow_entry_mode") or "")
        st.shadow_entry_anchor_model = str(row.get("shadow_entry_anchor_model") or "")
        st.entry_shadow_decisions = [dict(item) for item in decisions if isinstance(item, dict)] if isinstance(decisions, list) else []
        if (
            st.shadow_positions
            or st.entry_shadow_decisions
            or st.shadow_close_details
            or st.shadow_entry_mode
            or st.shadow_entry_anchor_model
        ):
            shadow_restored += 1
    if shadow_restored:
        print(f"{now_str()} RESTORE shadow_state count={shadow_restored} source={shadow_source}", flush=True)
    return restored


def restore_closed_totals(path: Path, states: list[State], portfolio: Portfolio) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    by_key = {(st.contour, st.spec.secid): st for st in states}
    restored = 0
    restored_net = 0.0
    try:
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                contour = str(row.get("contour") or "")
                secid = str(row.get("secid") or row.get("ticker") or "")
                st = by_key.get((contour, secid))
                if st is None:
                    continue
                try:
                    net = float(row.get("net_rub") or 0.0)
                except Exception:
                    net = 0.0
                st.closed += 1
                st.closed_net += net
                st.attempts += 1
                portfolio.closed_net += net
                restored += 1
                restored_net += net
    except Exception as exc:
        print(f"{now_str()} RESTORE closed_trades_read_error={exc}", flush=True)
        return 0
    if restored:
        print(f"{now_str()} RESTORE closed_trades count={restored} net={restored_net:.2f} source={path}", flush=True)
    return restored


def write_bot_health(
    path: Path,
    states: list[State],
    portfolio: Portfolio,
    started: float,
    last_stream_event: list[float],
    reconnect_count: int,
    last_stream_error: str,
    status: str,
    auto_policy: dict | None = None,
) -> None:
    now = time.monotonic()
    payload = {
        "timestamp": now_str(),
        "pid": os.getpid(),
        "status": status,
        "uptime_sec": round(now - started, 1),
        "last_stream_age_sec": round(now - last_stream_event[0], 1),
        "reconnect_count": reconnect_count,
        "last_stream_error": last_stream_error,
        "open_positions": sum(1 for st in states if st.position is not None),
        "closed_trades": sum(st.closed for st in states),
        "closed_net": round(portfolio.closed_net, 2),
        "used_margin": round(used_margin(states), 2),
        "contours": {
            contour: {
                "open_positions": sum(1 for st in states if st.contour == contour and st.position is not None),
                "attempts": sum(st.attempts for st in states if st.contour == contour),
                "closed": sum(st.closed for st in states if st.contour == contour),
                "closed_net": round(sum(st.closed_net for st in states if st.contour == contour), 2),
            }
            for contour in ["strict", "aggressive"]
        },
        "auto_policy": auto_policy if isinstance(auto_policy, dict) else empty_auto_policy(),
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def append_schema_stable_csv(path: Path, row: dict) -> None:
    expected = list(row)
    if path.exists() and path.stat().st_size > 0:
        try:
            with path.open(newline="", encoding="utf-8") as f:
                header = next(csv.reader(f), [])
        except Exception:
            header = []
        if header != expected:
            backup = path.with_name(f"{path.stem}_schema_backup_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
            path.replace(backup)
            print(f"{now_str()} SNAPSHOT schema_changed backup={backup}", flush=True)
    append_trade(path, row)


def trade_log_group(path: Path) -> str:
    name = path.stem
    suffix = "_multi_futures_paper_trades"
    if name.endswith(suffix):
        return name[: -len(suffix)]
    return name


def entry_shadow_log_path(args: argparse.Namespace) -> Path:
    configured = str(getattr(args, "entry_shadow_log", "") or "").strip()
    if configured:
        return Path(configured)
    trade_path = Path(getattr(args, "log", REPORTS / "multi_futures_paper_trades.csv"))
    return trade_path.with_name(f"{trade_log_group(trade_path)}_entry_shadow_models.csv")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper-only", action="store_true", required=True)
    parser.add_argument("--secids", nargs="+", default=["NGK6", "NGM6", "BMM6", "VBM6", "GZM6", "S1M6"])
    parser.add_argument("--runtime-sec", type=int, default=3600)
    parser.add_argument("--report-sec", type=int, default=600)
    parser.add_argument("--seed-minutes", type=int, default=180)
    parser.add_argument("--orderbook-depth", type=int, default=10)
    parser.add_argument("--profiles", default=str(REPORTS / "futures_scalp_profiles.csv"))
    parser.add_argument("--log", default=str(REPORTS / "multi_futures_paper_trades.csv"))
    parser.add_argument("--snapshot-log", default=str(REPORTS / "live_orderbook_snapshots.csv"))
    parser.add_argument("--open-positions-log", default=str(REPORTS / "paper_open_positions.json"))
    parser.add_argument("--instrument-specs-log", default=str(REPORTS / "paper_instrument_specs.csv"))
    parser.add_argument("--startup-status-log", default=str(REPORTS / "paper_startup_status.csv"))
    parser.add_argument("--shadow-log", default=str(REPORTS / "paper_shadow_exit_models.csv"))
    parser.add_argument("--entry-shadow-log", default="")
    parser.add_argument("--health-log", default=str(REPORTS / "paper_bot_health.json"))
    parser.add_argument("--snapshot-sec", type=int, default=10)
    parser.add_argument("--no-trade-before", default="")
    parser.add_argument("--no-new-after", default="")
    parser.add_argument("--entry-blackout-window", action="append", default=[])
    parser.add_argument("--force-close-at", default="")
    parser.add_argument("--paper-capital", type=float, default=200_000.0)
    parser.add_argument("--max-total-margin-pct", type=float, default=0.80)
    parser.add_argument("--max-position-margin-pct", type=float, default=0.20)
    parser.add_argument("--max-full-stop-rub", type=float, default=DEFAULT_MAX_FULL_STOP_RUB)
    parser.add_argument("--stop-limit-emergency-ticks", type=float, default=2.0)
    parser.add_argument("--actual-exit-model", choices=["stream_stoplimit", "candle_like"], default="stream_stoplimit")
    parser.add_argument("--stream-stale-sec", type=float, default=15.0)
    parser.add_argument("--fallback-poll-sec", type=float, default=2.0)
    parser.add_argument("--no-new-expiry-days", type=float, default=10.0)
    parser.add_argument("--expiry-force-close-days", type=float, default=3.0)
    parser.add_argument("--roll-observe-days", type=float, default=21.0)
    parser.add_argument("--roll-state-log", default=str(REPORTS / "paper_roll_state.json"))
    parser.add_argument("--disable-auto-roll", action="store_true")
    parser.add_argument("--auto-policy-path", default=str(REPORTS / "autonomy" / "latest" / "latest_auto_policy.json"))
    parser.add_argument("--auto-policy-reload-sec", type=float, default=AUTO_POLICY_RELOAD_SEC)
    args = parser.parse_args(argv)
    require_paper_only(bool(args.paper_only), "multi_futures_paper.py")
    return args


def main() -> None:
    args = parse_args()

    from t_tech.invest import Client, InstrumentIdType

    all_profiles = load_profiles(Path(args.profiles))
    profiles = {secid: all_profiles[secid] for secid in args.secids if secid in all_profiles}
    portfolio = Portfolio(
        initial_capital=float(args.paper_capital),
        max_total_margin_pct=float(args.max_total_margin_pct),
        max_position_margin_pct=float(args.max_position_margin_pct),
    )
    token = find_paper_tbank_token()
    states: list[State] = []
    specs: list[Spec] = []
    with Client(token) as client:
        roll_events: list[dict] = []
        loaded_secids: set[str] = set()
        processed_secids: set[str] = set()
        configured_secids = set(args.secids)
        queued_roll_profiles: dict[str, Profile] = {}
        startup_status: list[dict] = []

        def load_contract(secid: str, profile: Profile, load_reason: str) -> Spec | None:
            if secid in loaded_secids:
                return None
            try:
                future = tbank_find_future(client, secid)
                info = client.instruments.future_by(
                    id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
                    id=future.uid,
                ).instrument
            except Exception as exc:
                startup_status.append(
                    {
                        "timestamp": now_str(),
                        "ticker": secid,
                        "status": "skipped",
                        "reason": f"instrument_error={exc}",
                        "load_reason": load_reason,
                        "profile_source": profile.source_secid or profile.secid,
                        "family": profile.family or contract_family(secid),
                    }
                )
                print(f"{now_str()} SKIP {secid} instrument_error={exc}", flush=True)
                return None
            spec = spec_from_tbank(client, secid, future, info)
            side_fee = commission_side_rub(spec, 1, 0.00025, None)
            can_trade, fee_reason = profile_can_trade(profile, side_fee, spec)
            if not can_trade:
                startup_status.append(
                    {
                        "timestamp": now_str(),
                        "ticker": secid,
                        "status": "skipped",
                        "reason": fee_reason,
                        "load_reason": load_reason,
                        "profile_source": profile.source_secid or profile.secid,
                        "family": profile.family or contract_family(secid),
                        "expiration": spec.expiration_date.isoformat() if spec.expiration_date else "",
                        "days_to_expiration": round(days_to_expiration(spec), 3) if days_to_expiration(spec) is not None else "",
                        "tick": spec.min_step,
                        "tick_rub": spec.step_price,
                        "last_price": round(spec.last_price, 6),
                        "go_buy": round(spec.margin_buy, 2),
                        "go_sell": round(spec.margin_sell, 2),
                    }
                )
                print(f"{now_str()} SKIP {secid} {fee_reason} load_reason={load_reason}", flush=True)
                return None
            specs.append(spec)
            loaded_secids.add(secid)
            dte = days_to_expiration(spec)
            startup_status.append(
                {
                    "timestamp": now_str(),
                    "ticker": secid,
                    "status": "loaded",
                    "reason": fee_reason,
                    "load_reason": load_reason,
                    "profile_source": profile.source_secid or profile.secid,
                    "family": profile.family or contract_family(secid),
                    "expiration": spec.expiration_date.isoformat() if spec.expiration_date else "",
                    "days_to_expiration": round(dte, 3) if dte is not None else "",
                    "tick": spec.min_step,
                    "tick_rub": spec.step_price,
                    "last_price": round(spec.last_price, 6),
                    "go_buy": round(spec.margin_buy, 2),
                    "go_sell": round(spec.margin_sell, 2),
                }
            )
            dte_text = "" if dte is None else f" dte={dte:.1f}d"
            print(
                f"{now_str()} LOAD {secid} {fee_reason} load_reason={load_reason} "
                f"profile_source={profile.source_secid or profile.secid}{dte_text}",
                flush=True,
            )
            try:
                seeded = seed_candles(client, spec.figi, args.seed_minutes)
            except Exception as exc:
                seeded = deque(maxlen=180)
                if is_resource_exhausted_error(exc):
                    backoff_sec = rate_limit_backoff_sec(exc, 15.0)
                    print(
                        f"{now_str()} SEED {secid} resource_exhausted wait_sec={backoff_sec:.1f} fallback=empty_candles",
                        flush=True,
                    )
                else:
                    print(f"{now_str()} SEED {secid} error={type(exc).__name__}: {exc} fallback=empty_candles", flush=True)
            for contour in ["strict", "aggressive"]:
                states.append(
                    State(
                        spec=spec,
                        profile=profile,
                        contour=contour,
                        side_fee=side_fee,
                        candles=deque(seeded, maxlen=180),
                        last_price=spec.last_price,
                    )
                )
            return spec

        for secid in args.secids:
            processed_secids.add(secid)
            if secid not in profiles:
                startup_status.append(
                    {
                        "timestamp": now_str(),
                        "ticker": secid,
                        "status": "skipped",
                        "reason": "no_profile",
                        "load_reason": "configured",
                        "profile_source": "",
                        "family": contract_family(secid),
                    }
                )
                print(f"{now_str()} SKIP {secid} no_profile", flush=True)
                continue
            profile = profiles[secid]
            spec = load_contract(secid, profile, "configured")
            if spec is None or args.disable_auto_roll:
                continue
            roll_secid, roll_profile, roll_event = select_roll_contract(client, spec, profile, all_profiles, args)
            if roll_secid and roll_profile:
                if roll_secid in loaded_secids:
                    roll_event["status"] = "selected_already_loaded"
                    roll_event["reason"] = f"{roll_event.get('reason', '')}; selected already loaded"
                elif roll_secid in configured_secids and roll_secid not in processed_secids:
                    roll_event["status"] = "selected_will_load_later"
                    roll_event["reason"] = f"{roll_event.get('reason', '')}; selected is configured later in this run"
                else:
                    queued_roll_profiles[roll_secid] = roll_profile
                    roll_event["status"] = "queued_roll_contract"
                    roll_event["reason"] = f"{roll_event.get('reason', '')}; queued for auto load"
            roll_events.append(roll_event)

        for secid, profile in queued_roll_profiles.items():
            load_contract(secid, profile, "auto_roll")

        write_startup_status(Path(args.startup_status_log), startup_status)
        if not specs:
            print(f"{now_str()} multi_paper no tradable instruments after startup filters")
            return
        write_instrument_specs(Path(args.instrument_specs_log), specs)
        write_roll_state(Path(args.roll_state_log), roll_events, specs, args)
        by_figi = {s.figi: s for s in specs}
        by_uid = {s.uid: s for s in specs}
        state_by_uid = {(s.spec.uid, s.contour): s for s in states}
        base_blackout_windows = normalize_blackout_windows(getattr(args, "entry_blackout_window", []))
        print(
            f"{now_str()} multi_paper start instruments={len(specs)} contours=2 "
            f"runtime_sec={args.runtime_sec} paper_capital={portfolio.initial_capital:.0f} "
            f"max_total_margin_pct={portfolio.max_total_margin_pct:.2f} "
            f"max_position_margin_pct={portfolio.max_position_margin_pct:.2f} "
            f"max_full_stop_rub={float(args.max_full_stop_rub):.0f} "
            f"auto_policy_path={args.auto_policy_path or '-'} "
            f"no_trade_before={args.no_trade_before or '-'} "
            f"no_new_after={args.no_new_after or '-'} "
            f"entry_blackout_windows={','.join(base_blackout_windows) if base_blackout_windows else '-'} "
            f"force_close_at={args.force_close_at or '-'}"
        )
        started = time.monotonic()
        next_report = started + args.report_sec
        next_snapshot = started
        no_trade_before = parse_clock_time(args.no_trade_before)
        no_new_after = parse_clock_time(args.no_new_after)
        force_close_at = parse_clock_time(args.force_close_at)
        portfolio_group_name = trade_log_group(Path(args.log)).upper()
        last_stream_event = [time.monotonic()]
        runtime_state: dict[str, object] = {"reconnect_count": 0, "last_stream_error": ""}
        auto_policy_state: dict[str, object] = {
            "path": Path(args.auto_policy_path) if args.auto_policy_path else None,
            "reload_sec": float(args.auto_policy_reload_sec),
            "next_check_at": 0.0,
            "mtime": None,
            "status": "init",
            "last_error": "",
            "payload": empty_auto_policy(),
        }
        external_positions_state: dict[str, object] = {
            "own_path": Path(args.open_positions_log),
            "reload_sec": EXTERNAL_OPEN_POSITIONS_RELOAD_SEC,
            **empty_external_open_positions(),
        }
        active_auto_policy = refresh_auto_policy(auto_policy_state, force=True)
        stop_event = threading.Event()
        lock = threading.RLock()
        restore_closed_totals(Path(args.log), states, portfolio)
        restore_open_positions(Path(args.open_positions_log), states)
        write_open_positions(Path(args.open_positions_log), states)
        write_bot_health(
            Path(args.health_log),
            states,
            portfolio,
            started,
            last_stream_event,
            int(runtime_state["reconnect_count"]),
            str(runtime_state["last_stream_error"]),
            "starting",
            active_auto_policy,
        )
        poll_thread = threading.Thread(
            target=poll_market_fallback,
            args=(token, specs, state_by_uid, states, args, portfolio, last_stream_event, started, runtime_state, auto_policy_state, stop_event, lock),
            daemon=True,
        )
        poll_thread.start()

        def handle_stream_response(response) -> None:
            nonlocal next_report, next_snapshot
            now = time.monotonic()
            active_auto_policy_local = refresh_auto_policy(auto_policy_state)
            effective_entry_start = effective_no_trade_before(no_trade_before, active_auto_policy_local)
            effective_entry_cutoff = effective_no_new_after(no_new_after, active_auto_policy_local)
            trading_enabled = daily_trading_enabled(
                effective_entry_start,
                effective_entry_cutoff,
                effective_entry_blackout_windows(base_blackout_windows, active_auto_policy_local),
            )
            force_close_due = daily_force_close_due(force_close_at)
            uid = ""
            price = None
            candle = None
            orderbook = None
            if response.last_price is not None:
                uid = response.last_price.instrument_uid
                price = quotation_to_float(response.last_price.price)
            elif response.trade is not None:
                uid = response.trade.instrument_uid
                price = quotation_to_float(response.trade.price)
            elif response.orderbook is not None:
                uid = response.orderbook.instrument_uid
                orderbook = response.orderbook
            elif response.candle is not None:
                uid = response.candle.instrument_uid
                candle = response.candle
            if uid not in by_uid:
                return
            last_stream_event[0] = time.monotonic()
            spec = by_uid[uid]
            with lock:
                for contour in ["strict", "aggressive"]:
                    st = state_by_uid[(uid, contour)]
                    state_blackout_windows = effective_entry_blackout_windows(
                        base_blackout_windows,
                        active_auto_policy_local,
                        portfolio_group_name,
                        contour,
                    )
                    state_trading_gate_reason = daily_trading_block_reason(
                        effective_entry_start,
                        effective_entry_cutoff,
                        state_blackout_windows,
                    )
                    state_trading_enabled = state_trading_gate_reason is None
                    if price is not None:
                        st.last_price = round_to_step(price, spec.min_step)
                    if orderbook is not None:
                        st.last_order_book = orderbook
                    if candle is not None:
                        st.candles.append(
                            {
                                "open": quotation_to_float(candle.open),
                                "high": quotation_to_float(candle.high),
                                "low": quotation_to_float(candle.low),
                                "close": quotation_to_float(candle.close),
                                "volume": int(candle.volume),
                            }
                        )
                    if st.position is None and has_active_shadow(st):
                        process_open_state_exit(
                            st,
                            args,
                            portfolio,
                            states,
                            candle is not None,
                            auto_policy=active_auto_policy_local,
                            force_exit_reason="scheduled_force_close" if force_close_due else None,
                        )
                        if has_active_shadow(st):
                            continue
                    if st.position is None and not state_trading_enabled and state_trading_gate_reason:
                        if not st.last_reason.startswith("auto_policy pause_"):
                            st.last_reason = f"time_gate {state_trading_gate_reason}"
                    if st.position is None and state_trading_enabled and not force_close_due:
                        if st.attempts >= st.profile.max_attempts:
                            st.last_reason = "attempt_filter max_attempts_reached"
                            continue
                        if now < st.cooldown_until:
                            if not (
                                st.last_reason.startswith("auto_policy pause_")
                                or st.last_reason.startswith("brq6_loss_pause")
                            ):
                                st.last_reason = "cooldown_filter wait_after_close"
                            continue
                        if st.last_entry_candle_count == len(st.candles):
                            st.last_reason = "candle_filter wait_new_candle"
                            continue
                        if has_open_ticker(states, spec.secid):
                            st.last_reason = "duplicate_filter ticker_already_open"
                            continue
                        external_open = refresh_external_open_positions(external_positions_state)
                        if spec.secid.upper() in set(external_open.get("tickers") or []):
                            st.last_reason = "duplicate_filter external_ticker_already_open"
                            continue
                        policy_reason = auto_policy_block_reason(st, contour, active_auto_policy_local, portfolio_group_name)
                        if policy_reason:
                            st.last_reason = policy_reason
                            continue
                        family_conflict = has_roll_family_conflict(
                            states,
                            spec,
                            state_family(st),
                            float(args.roll_observe_days),
                        )
                        if family_conflict:
                            st.last_reason = f"roll_family_filter open_family_position {family_conflict}"
                            continue
                        expiry_reason = expiry_new_entry_block_reason(spec, float(args.no_new_expiry_days))
                        if expiry_reason:
                            st.last_reason = expiry_reason
                            continue
                        direction, reason = signal(st, contour == "aggressive")
                        st.last_reason = reason
                        if direction:
                            allowed_direction = (st.profile.allowed_direction or "both").lower()
                            if allowed_direction in {"long", "short"} and direction != allowed_direction:
                                st.last_reason = f"direction_filter profile={allowed_direction} signal={direction}"
                                continue
                            entry_price, entry_source = executable_price(st, direction, "entry")
                            if entry_price is None:
                                st.last_reason = "book_filter no_executable_entry"
                                continue
                            sizing = paper_sizing(
                                portfolio,
                                states,
                                spec,
                                st.profile,
                                direction,
                                st.side_fee,
                                effective_max_full_stop_rub(float(args.max_full_stop_rub), active_auto_policy_local),
                            )
                            qty = sizing.qty
                            if qty < 1:
                                if sizing.margin_qty < 1:
                                    st.last_reason = f"capital_filter no_free_margin {sizing.reason}"
                                else:
                                    st.last_reason = f"risk_filter full_stop_gt_limit {sizing.reason}"
                                continue
                            entry_shadow_decisions = evaluate_entry_shadow_models(
                                st,
                                portfolio_group_name,
                                direction,
                                entry_price,
                                qty,
                                sizing,
                                contour == "aggressive",
                            )
                            entry_shadow_gate_reason = entry_shadow_gate_block_reason(
                                entry_shadow_decisions,
                                active_auto_policy_local,
                                portfolio_group_name,
                                contour,
                            )
                            if entry_shadow_gate_reason:
                                activate_blocked_entry_shadow_tracking(
                                    st,
                                    direction=direction,
                                    entry_price=entry_price,
                                    qty=qty,
                                    spec=spec,
                                    actual_exit_model=str(getattr(args, "actual_exit_model", "")),
                                    decisions=entry_shadow_decisions,
                                )
                                st.last_entry_candle_count = len(st.candles)
                                st.last_reason = f"{entry_shadow_gate_reason} shadow_only_tracking"
                                write_open_positions(Path(args.open_positions_log), states)
                                continue
                            st.attempts += 1
                            st.position = open_position(direction, entry_price, qty, st.profile.stop_ticks, st.profile.trail_ticks, spec)
                            st.shadow_positions = {
                                "stream_stoplimit": clone_position(st.position),
                                "candle_like": clone_position(st.position),
                            }
                            st.shadow_closed = {}
                            st.shadow_close_details = {}
                            st.shadow_entry_mode = ""
                            st.shadow_entry_anchor_model = ""
                            st.entry_shadow_decisions = entry_shadow_decisions
                            st.last_entry_candle_count = len(st.candles)
                            write_open_positions(Path(args.open_positions_log), states)
                            print(
                                f"{now_str()} OPEN {contour} {spec.secid} {direction} qty={qty} "
                                f"entry={entry_price:g} source={entry_source} stop={st.position.stop_price:g} "
                                f"margin={position_margin(spec, direction, qty):.2f} "
                                f"full_stop_risk={sizing.full_stop_rub:.2f} {sizing.reason} {reason}",
                                flush=True,
                            )
                            write_open_positions(Path(args.open_positions_log), states)
                    elif st.position is not None:
                        trigger_override = round_to_step(float(st.candles[-1]["close"]), spec.min_step) if candle is not None else None
                        process_open_state_exit(
                            st,
                            args,
                            portfolio,
                            states,
                            candle is not None,
                            actual_trigger_override=trigger_override,
                            actual_trigger_source_override="closed_1m_candle" if candle is not None else None,
                            force_exit_reason="scheduled_force_close" if force_close_due else None,
                        )
                if now >= next_report:
                    print_report(states, started)
                    print_portfolio_report(states, portfolio)
                    next_report += args.report_sec
                if now >= next_snapshot:
                    write_microstructure_snapshot(
                        Path(args.snapshot_log),
                        states,
                        trading_enabled,
                        no_trade_before=effective_no_trade_before(no_trade_before, active_auto_policy_local),
                        no_new_after=effective_no_new_after(no_new_after, active_auto_policy_local),
                        base_blackout_windows=base_blackout_windows,
                        auto_policy=active_auto_policy_local,
                        portfolio_group_name=portfolio_group_name,
                    )
                    write_open_positions(Path(args.open_positions_log), states)
                    write_bot_health(
                        Path(args.health_log),
                        states,
                        portfolio,
                        started,
                        last_stream_event,
                        int(runtime_state["reconnect_count"]),
                        str(runtime_state["last_stream_error"]),
                        "running",
                        active_auto_policy_local,
                    )
                    next_snapshot += args.snapshot_sec

        try:
            while time.monotonic() < started + args.runtime_sec:
                try:
                    for response in client.market_data_stream.market_data_stream(make_stream_requests(specs, args.orderbook_depth)):
                        if time.monotonic() >= started + args.runtime_sec:
                            break
                        handle_stream_response(response)
                except Exception as exc:
                    if time.monotonic() >= started + args.runtime_sec:
                        break
                    runtime_state["reconnect_count"] = int(runtime_state["reconnect_count"]) + 1
                    runtime_state["last_stream_error"] = f"{type(exc).__name__}: {exc}"
                    print(f"{now_str()} STREAM reconnect_after_error {runtime_state['last_stream_error']}", flush=True)
                    with lock:
                        write_bot_health(
                            Path(args.health_log),
                            states,
                            portfolio,
                            started,
                            last_stream_event,
                            int(runtime_state["reconnect_count"]),
                            str(runtime_state["last_stream_error"]),
                            "stream_reconnecting",
                            refresh_auto_policy(auto_policy_state),
                        )
                    time.sleep(3)
        finally:
            with lock:
                write_bot_health(
                    Path(args.health_log),
                    states,
                    portfolio,
                    started,
                    last_stream_event,
                    int(runtime_state["reconnect_count"]),
                    str(runtime_state["last_stream_error"]),
                    "stopping",
                    refresh_auto_policy(auto_policy_state),
                )
            stop_event.set()
        print_report(states, started)
        print_portfolio_report(states, portfolio)
        print(f"{now_str()} multi_paper done")


if __name__ == "__main__":
    main()
