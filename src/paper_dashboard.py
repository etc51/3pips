from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
SPEC_CACHE: dict[str, tuple[float, float]] = {}
LOCAL_SPEC_CACHE: dict[str, tuple[float, float]] | None = None
PORTFOLIO_CAPITAL_RUB = 800_000.0


def expected_session_status(portfolio: str, now: datetime | None = None) -> tuple[str, str] | None:
    current = now or datetime.now()
    if current.weekday() >= 5:
        return "рынок закрыт", "сегодня выходной, потока не будет"

    minutes = current.hour * 60 + current.minute
    portfolio_key = (portfolio or "").lower()
    futures_start = 10 * 60 + 15
    futures_new_entry_stop = 17 * 60 + 45
    futures_end = 23 * 60 + 50

    if "stock" in portfolio_key:
        if minutes < 10 * 60 or minutes > (18 * 60 + 45):
            return "вне сессии", "активная сессия акций сейчас закрыта"
        return None

    if portfolio_key == "neo":
        if minutes > futures_end:
            return "вне сессии", "торговое окно neo сейчас закрыто"
        return None

    if minutes < futures_start or minutes > futures_end:
        return "вне сессии", "активная фьючерсная сессия сейчас закрыта"
    if minutes >= futures_new_entry_stop:
        return "вне окна входа", "после 17:45 новые входы закрыты, открытые позиции только сопровождаются"
    return None


def load_local_specs() -> dict[str, tuple[float, float]]:
    global LOCAL_SPEC_CACHE
    if LOCAL_SPEC_CACHE is not None:
        return LOCAL_SPEC_CACHE
    specs: dict[str, tuple[float, float]] = {}
    for path in REPORTS.glob("paper_runs/*/*instrument_specs.csv"):
        df = read_csv(path)
        if df.empty:
            continue
        for _, row in df.iterrows():
            ticker = str(row.get("ticker") or "")
            tick = number(row.get("tick"), 8)
            tick_rub = number(row.get("tick_rub"), 8)
            if ticker and tick and tick_rub:
                specs[ticker.upper()] = (float(tick), float(tick_rub))
    for path in [
        REPORTS / "cloud_all_futures_grid_package" / "instrument_specs.csv",
        REPORTS / "cloud_30_pct_grid_package" / "instrument_specs.csv",
        REPORTS / "cloud_new_candidates_stress_package" / "instrument_specs.csv",
    ]:
        if not path.exists():
            continue
        df = read_csv(path)
        if df.empty:
            continue
        for _, row in df.iterrows():
            ticker = str(row.get("ticker") or "")
            tick = number(row.get("tick"), 8)
            tick_rub = number(row.get("tick_rub"), 8)
            if ticker and tick and tick_rub:
                specs[ticker.upper()] = (float(tick), float(tick_rub))
    LOCAL_SPEC_CACHE = specs
    return specs


def read_portfolio_config(base_dir: Path) -> dict:
    config = read_json(base_dir / "portfolio_config.json")
    if isinstance(config, dict):
        portfolios = config.get("portfolios")
        if isinstance(portfolios, dict):
            return portfolios
    return {}


def portfolio_path(base_dir: Path, portfolio: str, suffix: str) -> Path:
    if portfolio == "strong":
        default_path = base_dir / suffix
        if default_path.exists():
            return default_path
    return base_dir / f"{portfolio}_{suffix}"


def portfolio_from_name(name: str, suffix: str) -> str | None:
    if name == suffix:
        return "strong"
    marker = f"_{suffix}"
    if name.endswith(marker):
        return name[: -len(marker)]
    return None


def discover_portfolios(base_dir: Path) -> list[str]:
    config = read_portfolio_config(base_dir)
    names = list(config)
    seen = set(names)
    suffixes = [
        "multi_futures_paper_trades.csv",
        "live_orderbook_snapshots.csv",
        "paper_open_positions.json",
        "startup_status.csv",
    ]
    for suffix in suffixes:
        for path in base_dir.glob(f"*{suffix}"):
            name = portfolio_from_name(path.name, suffix)
            if name and name not in seen:
                names.append(name)
                seen.add(name)
    if not names:
        names = ["strong", "weak", "rejected", "neo"]
    return names


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.read_csv(path, engine="python", on_bad_lines="skip")


def read_json(path: Path):
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return []


def number(value, digits: int = 2):
    try:
        value = float(value)
    except Exception:
        return None
    if not math.isfinite(value):
        return None
    return round(value, digits)


def clean_record(row: dict) -> dict:
    return {k: (None if pd.isna(v) else v) for k, v in row.items()}


def human_reason(reason: object) -> str:
    text = "" if reason is None else str(reason)
    if not text:
        return "наблюдаем"
    if text.startswith("warmup="):
        return f"прогрев: {text.split('=', 1)[1]} мин"
    if text.startswith("fee_filter"):
        return "фильтр: комиссия велика к стопу"
    if text.startswith("startup_fee_filter"):
        return "на старте заблокировано: комиссия велика к стопу"
    if text.startswith("fee_ok"):
        return "комиссия к стопу в норме"
    if text.startswith("range_filter"):
        return "фильтр: мало движения"
    if text.startswith("risk_filter"):
        return "фильтр: полный стоп больше лимита ₽"
    if text.startswith("risk_governor paused_today"):
        return "риск: пауза до завтра, защищаем дневной плюс"
    if text.startswith("risk_governor profit_guard_aggressive_off"):
        return "риск: после дневного плюса агрессивный режим выключен"
    if text.startswith("risk_governor observe"):
        return "риск: новый контракт семьи, режим наблюдения и микро-размер"
    if text.startswith("daily_profit_guard_active"):
        return "риск: защита прибыли, только микро"
    if text.startswith("daily_profit_guard_floor"):
        return "риск: защищённый дневной плюс, новые входы стоп"
    if text.startswith("daily_profit_guard_aggressive_off"):
        return "риск: после дневного плюса агрессивный режим выключен"
    if text.startswith("risk_governor micro"):
        return "риск: только микро-размер"
    if text.startswith("risk_governor reduced"):
        return "риск: размер уменьшен"
    if text.startswith("risk_governor median_cap"):
        return "риск: стоп ограничен медианным плюсом"
    if text.startswith("daily_profit_guard"):
        return "риск: защита дневной прибыли"
    if text.startswith("brq6_spread_filter"):
        return "BR: спред слишком большой к стопу"
    if text.startswith("spread_filter"):
        return "фильтр: спред больше стопа"
    if text.startswith("stock_spread_filter"):
        return "акции: спред слишком большой для входа"
    if text.startswith("brq6_loss_pause"):
        return "BR: пауза после серии стопов"
    if text == "restored_open_position":
        return "позиция восстановлена после рестарта"
    if text.startswith("scheduled_force_close"):
        return "плановое закрытие перед переносом"
    if text.startswith("expiry_filter"):
        return "фильтр: близко экспирация, новые входы запрещены"
    if text.startswith("roll_family_filter"):
        return "фильтр: уже есть позиция в этом семействе на переносе"
    if text.startswith("roll_observe"):
        return "новый контракт семьи: режим наблюдения до набора статистики"
    if text.startswith("dte_above_observe_window"):
        return "до окна автопереноса ещё далеко"
    if "queued for auto load" in text:
        return "контракт поставлен в очередь на автоперенос"
    if text.startswith("direction_filter"):
        return "фильтр: сигнал против направления профиля"
    if text.startswith("entry_signal long"):
        return "сигнал на вход: long"
    if text.startswith("entry_signal short"):
        return "сигнал на вход: short"
    if text.startswith("gpt_entry_signal long"):
        return "GPT shadow: вход long"
    if text.startswith("gpt_entry_signal short"):
        return "GPT shadow: вход short"
    if text.startswith("gpt_watch_conditions"):
        return "GPT shadow: условия наблюдаются"
    if text.startswith("watch_conditions") or text.startswith("p="):
        return "условия наблюдаются, входа нет"
    if text == "duplicate_filter ticker_already_open" or text == "duplicate_filter_ticker_already_open":
        return "позиция по тикеру уже открыта"
    if text.startswith("capital_filter no_free_margin"):
        return "не хватает свободного ГО"
    if text == "book_filter no_executable_entry":
        return "нет цены для входа в стакане"
    if text == "book_filter no_executable_exit":
        return "нет цены для выхода в стакане"
    if text == "attempt_filter max_attempts_reached":
        return "лимит попыток по режиму исчерпан"
    if text == "cooldown_filter wait_after_close":
        return "пауза после закрытия"
    if text == "candle_filter wait_new_candle":
        return "ждём новую свечу"
    if text == "time_gate":
        return "торговля вне разрешённого времени"
    replacements = {
        "tbank_stream": "поток Т-Банка",
        "stream": "поток",
        "listening": "слушает рынок",
        "book": "стакан",
        "shadow_compare_active": "идёт сравнение моделей выхода",
        "gpt_profile_shadow": "GPT shadow",
        "gpt_shadow": "GPT shadow",
        "gpt_max_hold_exit": "GPT shadow: выход по max hold",
        "gpt_scheduled_force_close": "GPT shadow: плановое закрытие",
        "duplicate_filter ticker_already_open": "позиция по тикеру уже открыта",
        "duplicate_filter_ticker_already_open": "позиция по тикеру уже открыта",
        "duplicate_filter": "дубль заблокирован",
        "capital_filter": "фильтр ГО",
        "book_filter": "фильтр стакана",
        "attempt_filter": "фильтр попыток",
        "cooldown_filter": "пауза",
        "candle_filter": "свеча",
        "max_attempts_reached": "лимит попыток исчерпан",
        "wait_after_close": "ждём после закрытия",
        "wait_new_candle": "ждём новую свечу",
        "no_free_margin": "не хватает свободного ГО",
        "no_executable_entry": "нет цены для входа",
        "no_executable_exit": "нет цены для выхода",
        "ticker_already_open": "позиция по тикеру уже открыта",
        "already_open": "уже открыта",
        "duplicate_position": "позиция уже открыта",
        "no_book": "нет стакана",
        "no_bid": "нет цены покупки",
        "no_ask": "нет цены продажи",
        "bid": "цена покупки",
        "ask": "цена продажи",
        "fee": "комиссия",
        "range": "движение",
        "filter": "фильтр",
        "risk_governor": "риск",
        "paused_today": "пауза до завтра",
        "profit_guard_aggressive_off": "агрессивный выключен защитой прибыли",
        "daily_profit_guard_floor": "защищённый дневной плюс",
        "daily_profit_guard_active": "защита прибыли",
        "daily_profit_guard_aggressive_off": "агрессивный выключен защитой прибыли",
        "observe": "наблюдение",
        "micro": "микро",
        "reduced": "уменьшен",
        "median_cap": "лимит по медианному плюсу",
        "cap_x": "множитель лимита",
        "max_stop": "макс стоп",
        "profile_wins": "плюсов профиля",
        "family_wins": "плюсов семьи",
        "portfolio_wins": "плюсов контура",
        "normal": "норма",
        "daily_profit_guard": "защита дневной прибыли",
        "stop_to_median": "стоп к медианному плюсу",
        "family_tail": "хвост семьи",
        "profile_tail": "хвост профиля",
        "profile_loss_cluster": "серия убытков профиля",
        "profile_negative": "профиль в минусе",
        "family_negative": "семья в минусе",
        "probation_new_contract": "испытание нового контракта",
        "warmup": "прогрев",
    }
    out = text
    for src, dst in replacements.items():
        out = out.replace(src, dst)
    out = out.replace("_", " ")
    return out


def human_cell(value: object) -> object:
    if value is None:
        return None
    text = str(value)
    if not text:
        return text
    return human_reason(text)


def human_gpt_layer(value: object) -> str:
    text = "" if value is None else str(value)
    text = text.replace("gpt_profile_shadow", "gpt_full").replace("gpt_", "").strip().lower()
    return {
        "full": "строгий",
        "relaxed": "средний",
        "loose": "мягкий",
    }.get(text, text or "строгий")


def human_direction(value: object) -> str:
    text = "" if value is None else str(value).strip().lower()
    return {
        "long": "лонг",
        "short": "шорт",
        "both": "оба",
    }.get(text, text or "-")


def human_contour(value: object) -> str:
    text = "" if value is None else str(value).strip().lower()
    return {
        "strict": "строгий",
        "aggressive": "агрессивный",
        "reduced": "уменьшенный",
        "micro": "микро",
        "observe": "наблюдение",
    }.get(text, text or "-")


def human_risk_mode(value: object) -> str:
    text = "" if value is None else str(value).strip().lower()
    return {
        "normal": "норма",
        "observe": "наблюдение",
        "micro": "микро",
        "reduced": "уменьшен",
        "median_cap": "лимит по медиане",
        "paused": "пауза",
    }.get(text, text or "-")


def human_exit_source(value: object) -> str:
    text = "" if value is None else str(value).strip()
    mapping = {
        "broker_stop_limit_fill": "лимитный стоп исполнен",
        "stop_limit_touched_waiting_fill": "стоп сработал, лимит ждёт исполнения",
        "stop_limit_waiting": "лимитный стоп ещё не исполнен",
        "emergency_market_after_missed_limit": "маркет после несработавшей лимитки",
        "candle_like_stop_fill": "свечной стоп",
        "closed_1m_candle": "выход по закрытой минутной свече",
        "bid_exit": "выход по bid",
        "ask_exit": "выход по ask",
        "scheduled_force_close": "плановое закрытие",
        "gpt_scheduled_force_close": "GPT: плановое закрытие",
        "gpt_max_hold_exit": "GPT: выход по max hold",
    }
    return mapping.get(text, human_reason(text))


def human_exit_mode(value: object) -> str:
    text = "" if value is None else str(value).strip().lower()
    return {
        "direct_ticks": "тики профиля",
        "profile_ticks": "тики профиля",
        "percent": "процентный",
        "stream_stoplimit": "стрим + stop-limit",
        "candle_like": "мягкий свечной",
    }.get(text, text or "-")


def human_startup_status(value: object) -> str:
    text = "" if value is None else str(value).strip().lower()
    return {
        "loaded": "загружен",
        "skipped": "пропущен",
        "error": "ошибка",
    }.get(text, text or "-")


def human_load_reason(value: object) -> str:
    text = "" if value is None else str(value).strip().lower()
    return {
        "configured": "основной список",
        "auto_roll": "автоперенос",
        "probation_auto_roll": "автоперенос на испытании",
        "stock_watchlist": "акции",
    }.get(text, text or "-")


def human_roll_status(value: object) -> str:
    text = "" if value is None else str(value).strip().lower()
    return {
        "queued_roll_contract": "в очереди на автоперенос",
        "selected": "выбран",
        "blocked": "заблокирован",
        "not_near_roll": "ещё рано",
        "missing_profile": "нет профиля",
        "already_loaded": "уже загружен",
        "perpetual_or_far_expiration": "perp / дальняя экспирация",
    }.get(text, text or "-")


def human_system_status(value: object) -> str:
    text = "" if value is None else str(value).strip().lower()
    return {
        "healthy": "ok",
        "ok": "ok",
        "incident": "ошибка",
        "disabled": "выкл",
        "missing": "нет данных",
        "warning": "внимание",
        "stale": "устарело",
    }.get(text, text or "-")


def human_service_name(value: object) -> str:
    text = "" if value is None else str(value).strip().lower()
    return {
        "watchdog": "автоконтроль",
        "auto-update": "автообновление",
        "supervisor": "супервизор",
    }.get(text, text or "-")


def readiness_label(score: object, reason: object) -> str:
    try:
        value = int(score)
    except Exception:
        value = 0
    text = "" if reason is None else str(reason)
    if text.startswith("warmup="):
        return "прогрев"
    if text.startswith(("fee_filter", "range_filter")):
        return "фильтр"
    if text.startswith("startup_fee_filter") or text == "no_profile" or text.startswith("instrument_error"):
        return "отсеян"
    if value >= 6:
        return "близко"
    if value >= 3:
        return "наблюдаем"
    return "нет сигнала"


def moex_spec(secid: str) -> tuple[float | None, float | None]:
    if secid in SPEC_CACHE:
        return SPEC_CACHE[secid]
    local = load_local_specs().get(secid.upper())
    if local:
        SPEC_CACHE[secid] = local
        return local
    url = f"https://iss.moex.com/iss/engines/futures/markets/forts/securities/{secid}.json"
    try:
        resp = requests.get(
            url,
            params={"iss.meta": "off", "iss.only": "securities"},
            timeout=3,
        )
        resp.raise_for_status()
        block = resp.json().get("securities", {})
        columns = block.get("columns", [])
        data = block.get("data", [])
        if not data:
            return None, None
        row = dict(zip(columns, data[0]))
        spec = (float(row["MINSTEP"]), float(row["STEPPRICE"]))
        SPEC_CACHE[secid] = spec
        return spec
    except Exception:
        return None, None


def enrich_position_pnl(position: dict) -> dict:
    secid = str(position.get("ticker") or "")
    min_step, step_price = moex_spec(secid)
    if not min_step or not step_price:
        return {**position, "unrealized_ticks": None, "gross_pnl_rub": None, "fees_rub": None, "unrealized_net_rub": None}
    try:
        entry = float(position.get("entry_price"))
        last = float(position.get("mark_price") or position.get("last_price"))
        qty = int(float(position.get("qty") or 1))
    except Exception:
        return {**position, "unrealized_ticks": None, "gross_pnl_rub": None, "fees_rub": None, "unrealized_net_rub": None}
    direction = str(position.get("direction") or "").lower()
    sign = 1 if direction == "long" else -1
    ticks = sign * (last - entry) / min_step
    gross = ticks * step_price * qty
    side_fee = round((entry / min_step) * step_price * qty * 0.00025, 2)
    fees = 2 * side_fee
    return {
        **position,
        "unrealized_ticks": number(ticks, 2),
        "gross_pnl_rub": number(gross, 2),
        "fees_rub": number(fees, 2),
        "unrealized_net_rub": number(gross - fees, 2),
    }


def latest_mtime(paths: list[Path]) -> str:
    existing = [p.stat().st_mtime for p in paths if p.exists()]
    if not existing:
        return ""
    return datetime.fromtimestamp(max(existing)).strftime("%Y-%m-%d %H:%M:%S")


def file_age_sec(path: Path) -> int | None:
    if not path.exists():
        return None
    return int((datetime.now().timestamp()) - path.stat().st_mtime)


def normalize_multi_trades(df: pd.DataFrame, portfolio: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "secid" in out:
        out["ticker"] = out["secid"]
    if "closed_at" in out:
        out["time"] = out["closed_at"]
    if "net_rub" in out:
        out["net_pnl_rub"] = pd.to_numeric(out["net_rub"], errors="coerce")
    if "fees_rub" in out:
        out["fees_rub"] = pd.to_numeric(out["fees_rub"], errors="coerce")
    if "ticks" in out:
        out["ticks"] = pd.to_numeric(out["ticks"], errors="coerce")
    out["source"] = "multi_futures"
    out["portfolio"] = portfolio
    return out


def normalize_execution_trades(df: pd.DataFrame, portfolio: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "target_contract" in out:
        out["ticker"] = out["target_contract"]
    if "exit_time" in out:
        out["time"] = out["exit_time"]
    elif "entry_time" in out:
        out["time"] = out["entry_time"]
    out["net_pnl_rub"] = pd.to_numeric(out.get("net_pnl_rub"), errors="coerce")
    out["ticks"] = pd.to_numeric(out.get("net_ticks"), errors="coerce")
    out["source"] = "execution"
    out["portfolio"] = portfolio
    return out


def normalize_gpt_shadow_trades(df: pd.DataFrame, portfolio: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "event_type" in out:
        out = out[out["event_type"].astype(str).str.lower() == "close"].copy()
    if out.empty:
        return pd.DataFrame()
    if "secid" in out:
        out["ticker"] = out["secid"]
    if "event_time" in out:
        out["time"] = out["event_time"]
    if "net_rub" in out:
        out["net_pnl_rub"] = pd.to_numeric(out["net_rub"], errors="coerce")
    if "fees_rub" in out:
        out["fees_rub"] = pd.to_numeric(out["fees_rub"], errors="coerce")
    if "ticks" in out:
        out["ticks"] = pd.to_numeric(out["ticks"], errors="coerce")
    if "model" not in out:
        out["model"] = "gpt_full"
    out["model"] = out["model"].fillna("gpt_full").astype(str).replace({"gpt_profile_shadow": "gpt_full"})
    out["model_label"] = out["model"].map(human_gpt_layer)
    out["source"] = "gpt_shadow"
    out["portfolio"] = portfolio
    return out


def equity_stats(pnl: pd.Series) -> dict:
    pnl = pd.to_numeric(pnl, errors="coerce").dropna()
    if pnl.empty:
        return {"net": 0.0, "max_drawdown": 0.0, "wins": 0, "losses": 0, "win_rate": 0.0}
    equity = pnl.cumsum()
    dd = equity - equity.cummax()
    return {
        "net": number(pnl.sum()),
        "max_drawdown": number(abs(dd.min())),
        "wins": int((pnl > 0).sum()),
        "losses": int((pnl < 0).sum()),
        "win_rate": number((pnl > 0).mean() * 100, 1),
    }


def build_state(base_dir: Path) -> dict:
    config = read_portfolio_config(base_dir)
    portfolio_names = discover_portfolios(base_dir)
    runtime_dir = base_dir.parent.parent / "runtime"
    trade_parts = []
    for portfolio in portfolio_names:
        trade_parts.append(
            normalize_multi_trades(
                read_csv(portfolio_path(base_dir, portfolio, "multi_futures_paper_trades.csv")),
                portfolio,
            )
        )
    trade_parts.append(normalize_execution_trades(read_csv(base_dir / "paper_execution_trades.csv"), "strong"))
    trades = pd.concat(trade_parts, ignore_index=True, sort=False)
    gpt_shadow_parts = []
    for portfolio in portfolio_names:
        gpt_shadow_parts.append(
            normalize_gpt_shadow_trades(
                read_csv(portfolio_path(base_dir, portfolio, "gpt_shadow_trades.csv")),
                portfolio,
            )
        )
    gpt_shadow_trades = pd.concat(gpt_shadow_parts, ignore_index=True, sort=False)
    if not trades.empty and "time" in trades:
        trades["_time_sort"] = pd.to_datetime(trades["time"], errors="coerce")
        trades = trades.sort_values("_time_sort", na_position="last")
    if not gpt_shadow_trades.empty and "time" in gpt_shadow_trades:
        gpt_shadow_trades["_time_sort"] = pd.to_datetime(gpt_shadow_trades["time"], errors="coerce")
        gpt_shadow_trades = gpt_shadow_trades.sort_values("_time_sort", na_position="last")

    snapshot_parts = []
    for portfolio in portfolio_names:
        part = read_csv(portfolio_path(base_dir, portfolio, "live_orderbook_snapshots.csv"))
        if not part.empty:
            part["portfolio"] = portfolio
        snapshot_parts.append(part)
    snapshots = pd.concat(snapshot_parts, ignore_index=True, sort=False)
    startup_parts = []
    for portfolio in portfolio_names:
        part = read_csv(portfolio_path(base_dir, portfolio, "startup_status.csv"))
        if not part.empty:
            part["portfolio"] = portfolio
        startup_parts.append(part)
    startup_status = pd.concat(startup_parts, ignore_index=True, sort=False)
    heartbeat = read_csv(base_dir / "paper_monitor_heartbeat.csv")
    summary = read_csv(base_dir / "paper_execution_summary.csv")
    open_positions = []
    for portfolio in portfolio_names:
        opened = read_json(portfolio_path(base_dir, portfolio, "paper_open_positions.json"))
        if isinstance(opened, list):
            for item in opened:
                if isinstance(item, dict):
                    item = {**item, "portfolio": portfolio}
                open_positions.append(item)
    open_positions = [enrich_position_pnl(item) if isinstance(item, dict) else item for item in open_positions]

    portfolio_health = []
    for portfolio in portfolio_names:
        health_path = portfolio_path(base_dir, portfolio, "health.json")
        payload = read_json(health_path)
        payload = payload if isinstance(payload, dict) else {}
        health_age = file_age_sec(health_path)
        stream_age = number(payload.get("last_stream_age_sec"), 1)
        reconnect_count = payload.get("reconnect_count")
        health_status = "нет данных"
        if not payload:
            health_status = "нет данных"
        elif health_age is not None and health_age > 180:
            health_status = "устарело"
        elif str(payload.get("status") or "").lower() != "running":
            health_status = "ошибка"
        elif stream_age is not None and stream_age > 20:
            health_status = "внимание"
        else:
            health_status = "ok"
        contour_stats = payload.get("contours") if isinstance(payload.get("contours"), dict) else {}
        contour_parts = []
        for contour_name, contour_payload in contour_stats.items():
            if not isinstance(contour_payload, dict):
                continue
            contour_parts.append(
                f"{human_contour(contour_name)}: "
                f"{int(contour_payload.get('open_positions') or 0)} поз / "
                f"{int(contour_payload.get('closed') or 0)} закр"
            )
        portfolio_health.append(
            {
                "portfolio": portfolio,
                "status": health_status,
                "updated": payload.get("timestamp") or latest_mtime([health_path]) or "-",
                "health_age_sec": health_age,
                "uptime_sec": number(payload.get("uptime_sec"), 1),
                "stream_age_sec": stream_age,
                "reconnect_count": reconnect_count,
                "open_positions": payload.get("open_positions"),
                "closed_trades": payload.get("closed_trades"),
                "closed_net": number(payload.get("closed_net")),
                "used_margin": number(payload.get("used_margin")),
                "last_stream_error": payload.get("last_stream_error") or "",
                "contours": "; ".join(contour_parts) or "-",
            }
        )

    system_overview = []
    watchdog_state = read_json(runtime_dir / "server_watchdog_state.json")
    if isinstance(watchdog_state, dict):
        system_overview.append(
            {
                "service": human_service_name("watchdog"),
                "status": human_system_status(watchdog_state.get("status") or "missing"),
                "updated": watchdog_state.get("last_change") or "-",
                "detail": watchdog_state.get("last_summary") or "-",
            }
        )
    else:
        system_overview.append(
            {"service": human_service_name("watchdog"), "status": "нет данных", "updated": "-", "detail": "state-файл не найден"}
        )

    autoupdate_state = read_json(runtime_dir / "docker_autoupdate_state.json")
    if isinstance(autoupdate_state, dict):
        system_overview.append(
            {
                "service": human_service_name("auto-update"),
                "status": "ok",
                "updated": autoupdate_state.get("updated_at") or "-",
                "detail": f"{autoupdate_state.get('previous_head', '-')[:7]} -> {autoupdate_state.get('current_head', '-')[:7]}",
            }
        )
    else:
        system_overview.append(
            {"service": human_service_name("auto-update"), "status": "нет данных", "updated": "-", "detail": "обновлений пока не было"}
        )

    supervisor_log = runtime_dir / "v7_paper_supervisor_20260525.log"
    supervisor_age = file_age_sec(supervisor_log)
    system_overview.append(
        {
            "service": human_service_name("supervisor"),
            "status": "ok" if supervisor_age is not None and supervisor_age <= 90 else "внимание",
            "updated": latest_mtime([supervisor_log]) or "-",
            "detail": f"последняя запись {supervisor_age} сек назад" if supervisor_age is not None else "лог не найден",
        }
    )

    closed = trades[pd.to_numeric(trades.get("net_pnl_rub"), errors="coerce").notna()].copy() if not trades.empty else pd.DataFrame()
    if not closed.empty and "time" in closed:
        closed["_day"] = pd.to_datetime(closed["time"], errors="coerce").dt.strftime("%Y-%m-%d")
    today_text = datetime.now().strftime("%Y-%m-%d")
    closed_today = closed[closed["_day"] == today_text].copy() if not closed.empty and "_day" in closed else pd.DataFrame()
    stats = equity_stats(closed.get("net_pnl_rub", pd.Series(dtype=float)))
    day_stats = equity_stats(closed_today.get("net_pnl_rub", pd.Series(dtype=float)))
    open_net_values = [item.get("unrealized_net_rub") for item in open_positions if isinstance(item, dict)]
    open_net = sum(float(v) for v in open_net_values if v is not None and math.isfinite(float(v)))
    stats["open_net"] = number(open_net)
    stats["total_net"] = number((stats.get("net") or 0.0) + open_net)
    stats["day_net"] = day_stats["net"]
    stats["day_max_drawdown"] = day_stats["max_drawdown"]
    stats["day_wins"] = day_stats["wins"]
    stats["day_losses"] = day_stats["losses"]
    stats["day_win_rate"] = day_stats["win_rate"]
    stats["day_closed_trades"] = int(len(closed_today))
    stats["closed_trades"] = int(len(closed))
    stats["open_positions"] = len(open_positions) if isinstance(open_positions, list) else 0
    watched_paths = [base_dir / "paper_execution_trades.csv", base_dir / "paper_monitor_heartbeat.csv"]
    for portfolio in portfolio_names:
        watched_paths.extend(
            [
                portfolio_path(base_dir, portfolio, "multi_futures_paper_trades.csv"),
                portfolio_path(base_dir, portfolio, "live_orderbook_snapshots.csv"),
                portfolio_path(base_dir, portfolio, "paper_open_positions.json"),
                portfolio_path(base_dir, portfolio, "startup_status.csv"),
                portfolio_path(base_dir, portfolio, "gpt_shadow_trades.csv"),
            ]
        )
    stats["last_update"] = latest_mtime(watched_paths)
    stats["watchdog_status"] = next((item["status"] for item in system_overview if item["service"] == human_service_name("watchdog")), "-")
    stats["system_incidents"] = int(sum(1 for item in system_overview if item["status"] not in {"ok", "нет данных", "выкл"}))

    by_ticker = []
    if not closed.empty and "ticker" in closed:
        for keys, g in closed.groupby(["portfolio", "ticker"], dropna=True):
            portfolio, ticker = keys
            s = equity_stats(g["net_pnl_rub"])
            by_ticker.append(
                {
                    "portfolio": str(portfolio),
                    "ticker": str(ticker),
                    "trades": int(len(g)),
                    "net": s["net"],
                    "max_drawdown": s["max_drawdown"],
                    "win_rate": s["win_rate"],
                    "avg_trade": number(g["net_pnl_rub"].mean()),
                    "last": number(g["net_pnl_rub"].iloc[-1]),
                }
            )
        by_ticker.sort(key=lambda x: x["net"] or 0, reverse=True)

    portfolio_overview = []
    for portfolio in portfolio_names:
        capital = float(config.get(portfolio, {}).get("capital", PORTFOLIO_CAPITAL_RUB)) if isinstance(config.get(portfolio), dict) else PORTFOLIO_CAPITAL_RUB
        closed_part = closed[closed["portfolio"] == portfolio] if not closed.empty and "portfolio" in closed else pd.DataFrame()
        day_part = closed_today[closed_today["portfolio"] == portfolio] if not closed_today.empty and "portfolio" in closed_today else pd.DataFrame()
        closed_net = float(pd.to_numeric(closed_part.get("net_pnl_rub", pd.Series(dtype=float)), errors="coerce").dropna().sum())
        day_net = float(pd.to_numeric(day_part.get("net_pnl_rub", pd.Series(dtype=float)), errors="coerce").dropna().sum())
        open_part = [p for p in open_positions if isinstance(p, dict) and p.get("portfolio") == portfolio]
        open_net = sum(float(p.get("unrealized_net_rub") or 0.0) for p in open_part)
        margin = sum(float(p.get("margin_rub") or 0.0) for p in open_part)
        total = closed_net + open_net
        portfolio_overview.append(
            {
                "portfolio": portfolio,
                "capital": capital,
                "day_net": number(day_net),
                "closed_net": number(closed_net),
                "open_net": number(open_net),
                "total_net": number(total),
                "equity": number(capital + total),
                "return_pct": number(total / capital * 100, 3) if capital else None,
                "used_margin": number(margin),
                "free_capital": number(capital + total - margin),
                "open_positions": len(open_part),
                "closed_trades": int(len(closed_part)),
                "risk_modes": ", ".join(sorted({human_risk_mode(p.get("risk_mode")) for p in open_part if p.get("risk_mode")})) or "-",
            }
        )

    gpt_shadow_overview = []
    if not gpt_shadow_trades.empty and "portfolio" in gpt_shadow_trades:
        group_cols = ["model", "portfolio"] if "model" in gpt_shadow_trades else ["portfolio"]
        for keys, g in gpt_shadow_trades.groupby(group_cols, dropna=True):
            if isinstance(keys, tuple):
                model, portfolio = keys
            else:
                model, portfolio = "gpt_full", keys
            pnl = pd.to_numeric(g.get("net_pnl_rub", pd.Series(dtype=float)), errors="coerce").dropna()
            s = equity_stats(pnl)
            gpt_shadow_overview.append(
                {
                    "model": human_gpt_layer(model),
                    "portfolio": str(portfolio),
                    "net": s["net"],
                    "trades": int(len(pnl)),
                    "wins": s["wins"],
                    "losses": s["losses"],
                    "win_rate": s["win_rate"],
                    "avg_trade": number(pnl.mean()) if not pnl.empty else 0,
                }
            )
        gpt_shadow_overview.sort(key=lambda x: x["net"] or 0, reverse=True)

    gpt_shadow_recent_cols = [
        "time",
        "model_label",
        "portfolio",
        "contour",
        "ticker",
        "signal_family",
        "entry_timing",
        "direction",
        "qty",
        "entry_price",
        "exit_price",
        "trigger_price",
        "ticks",
        "fees_rub",
        "net_pnl_rub",
        "exit_source",
    ]
    gpt_shadow_recent = []
    if not gpt_shadow_trades.empty:
        view = gpt_shadow_trades[[c for c in gpt_shadow_recent_cols if c in gpt_shadow_trades.columns]].tail(50)
        if "direction" in view:
            view["direction"] = view["direction"].map(human_direction)
        if "contour" in view:
            view["contour"] = view["contour"].map(human_contour)
        if "exit_source" in view:
            view["exit_source"] = view["exit_source"].map(human_exit_source)
        gpt_shadow_recent = json.loads(view.where(pd.notna(view), None).to_json(orient="records"))

    recent_trades_cols = [
        "time",
        "opened_at",
        "portfolio",
        "contour",
        "ticker",
        "direction",
        "qty",
        "entry_price",
        "exit_price",
        "trigger_price",
        "exit_source",
        "stop_limit_qty",
        "stop_overrun_ticks",
        "ticks",
        "fees_rub",
        "net_pnl_rub",
        "fill_status",
        "skip_reason",
        "source",
    ]
    recent_trades = []
    if not trades.empty:
        view = trades[[c for c in recent_trades_cols if c in trades.columns]].tail(50)
        if "direction" in view:
            view["direction"] = view["direction"].map(human_direction)
        if "contour" in view:
            view["contour"] = view["contour"].map(human_contour)
        if "exit_source" in view:
            view["exit_source"] = view["exit_source"].map(human_exit_source)
        for col in ("fill_status", "skip_reason", "source"):
            if col in view:
                view[col] = view[col].map(human_cell)
        recent_trades = json.loads(view.where(pd.notna(view), None).to_json(orient="records"))

    micro = {}
    if not snapshots.empty:
        last = snapshots.tail(1).iloc[0].to_dict()
        micro = clean_record(last)

    market_now = []
    if not snapshots.empty and "target_contract" in snapshots:
        view = snapshots.copy()
        if "snapshot_time" in view:
            view["_snapshot_sort"] = pd.to_datetime(view["snapshot_time"], errors="coerce")
            view = view.sort_values("_snapshot_sort", na_position="last")
        for _, row in view.groupby(["portfolio", "target_contract"], dropna=True).tail(1).iterrows():
            rec = clean_record(row.to_dict())
            reason = rec.get("last_reason") or rec.get("skip_reason") or ""
            status = rec.get("signal_status") or ""
            can_open = rec.get("can_open_new_paper_trade")
            score = 0
            if status == "listening":
                score += 2
            if can_open is True or str(can_open).lower() == "true":
                score += 1
            if reason and str(reason).startswith("entry_signal"):
                score += 3
            rec["near_score"] = score
            rec["state_label"] = readiness_label(score, reason)
            rec["display_reason"] = human_reason(reason)
            try:
                bid_size = int(float(rec.get("bid_size_target") or 0))
                ask_size = int(float(rec.get("ask_size_target") or 0))
                rec["book_display"] = f"{bid_size}/{ask_size}"
            except Exception:
                rec["book_display"] = "-"
            market_now.append(rec)
        market_now.sort(key=lambda x: (x.get("near_score") or 0, str(x.get("snapshot_time") or "")), reverse=True)

    wide_spread_watchlist = []
    for rec in market_now:
        spread_class = str(rec.get("spread_class") or "")
        try:
            ratio = float(rec.get("spread_to_stop_ratio"))
        except Exception:
            ratio = None
        review = str(rec.get("spread_review_flag") or "").lower() == "true"
        if review or spread_class in {"SPREAD_WATCH", "SPREAD_HEAVY", "SPREAD_DOMINATES"}:
            class_label = {
                "SPREAD_WATCH": "наблюдать",
                "SPREAD_HEAVY": "широкий",
                "SPREAD_DOMINATES": "доминирует",
                "NO_BOOK": "нет стакана",
            }.get(spread_class, spread_class or "-")
            wide_spread_watchlist.append(
                {
                    "portfolio": rec.get("portfolio"),
                    "ticker": rec.get("target_contract"),
                    "last": rec.get("last_price_target"),
                    "spread": rec.get("spread_ticks_target"),
                    "stop_ticks": rec.get("stop_ticks"),
                    "spread_to_stop_pct": number(ratio * 100, 1) if ratio is not None else None,
                    "spread_class": class_label,
                    "book": rec.get("book_display"),
                    "comment": rec.get("display_reason"),
                    "time": rec.get("snapshot_time"),
                }
            )
    wide_spread_watchlist.sort(key=lambda x: float(x.get("spread_to_stop_pct") or -1), reverse=True)

    positions_by_ticker: dict[tuple[str, str], list[str]] = {}
    for item in open_positions:
        if not isinstance(item, dict):
            continue
        key = (str(item.get("portfolio") or ""), str(item.get("ticker") or ""))
        side = str(item.get("direction") or "").upper()
        entry = item.get("entry_price")
        stop = item.get("stop_price")
        positions_by_ticker.setdefault(key, []).append(f"{side} {entry} / стоп {stop}")

    roll_overview = []
    for portfolio in portfolio_names:
        roll_payload = read_json(portfolio_path(base_dir, portfolio, "roll_state.json"))
        if not isinstance(roll_payload, dict):
            continue
        observe_days = number(roll_payload.get("roll_observe_days"), 1)
        for item in roll_payload.get("roll_events", []):
            if not isinstance(item, dict):
                continue
            dte = number(item.get("days_to_expiration"), 1)
            status_raw = str(item.get("status") or "")
            if status_raw == "not_near_roll" and (dte is None or (observe_days is not None and dte > observe_days)):
                continue
            candidates = item.get("candidates") if isinstance(item.get("candidates"), list) else []
            candidate_summary = "; ".join(
                f"{c.get('ticker')}: {human_roll_status(c.get('status'))}"
                for c in candidates[:4]
                if isinstance(c, dict)
            )
            roll_overview.append(
                {
                    "portfolio": portfolio,
                    "ticker": item.get("ticker"),
                    "family": item.get("family"),
                    "days_to_expiration": dte,
                    "status": human_roll_status(status_raw),
                    "selected": item.get("selected") or "-",
                    "profile_source": item.get("selected_profile_source") or "-",
                    "candidates": candidate_summary or "-",
                    "comment": human_reason(item.get("reason")) if item.get("reason") else "-",
                }
            )
    roll_overview.sort(key=lambda x: (999999 if x.get("days_to_expiration") is None else float(x["days_to_expiration"]), str(x.get("portfolio") or ""), str(x.get("ticker") or "")))

    startup_overview = []
    if not startup_status.empty and {"portfolio", "ticker"}.issubset(startup_status.columns):
        latest_startup = startup_status.groupby(["portfolio", "ticker"], dropna=True).tail(1).copy()
        for _, row in latest_startup.iterrows():
            rec = clean_record(row.to_dict())
            startup_overview.append(
                {
                    "portfolio": rec.get("portfolio"),
                    "ticker": rec.get("ticker"),
                    "family": rec.get("family"),
                    "status": human_startup_status(rec.get("status")),
                    "load_reason": human_load_reason(rec.get("load_reason")),
                    "profile_source": rec.get("profile_source") or "-",
                    "days_to_expiration": number(rec.get("days_to_expiration"), 1),
                    "go_buy": number(rec.get("go_buy")),
                    "go_sell": number(rec.get("go_sell")),
                    "comment": human_reason(rec.get("reason")) if rec.get("reason") else "-",
                }
            )
    startup_overview.sort(
        key=lambda x: (
            0 if x.get("status") == "пропущен" else 1 if x.get("load_reason") == "автоперенос" else 2,
            str(x.get("portfolio") or ""),
            str(x.get("ticker") or ""),
        )
    )

    ticker_overview = []
    seen_tickers = set()
    startup_by_key = {}
    if not startup_status.empty and {"portfolio", "ticker"}.issubset(startup_status.columns):
        for _, row in startup_status.groupby(["portfolio", "ticker"], dropna=True).tail(1).iterrows():
            startup_by_key[(str(row.get("portfolio") or ""), str(row.get("ticker") or ""))] = clean_record(row.to_dict())
    for rec in market_now:
        key = (str(rec.get("portfolio") or ""), str(rec.get("target_contract") or ""))
        seen_tickers.add(key)
        try:
            spread_to_stop_pct = number(float(rec.get("spread_to_stop_ratio")) * 100, 1)
        except Exception:
            spread_to_stop_pct = None
        overview = {
            "portfolio": key[0],
            "ticker": key[1],
            "position": "; ".join(positions_by_ticker.get(key, [])) or "-",
            "state": rec.get("state_label"),
            "risk": human_risk_mode(rec.get("risk_mode")),
            "risk_limit_rub": number(rec.get("risk_limit_rub")),
            "policy": human_reason(rec.get("risk_reason")) if rec.get("risk_reason") and str(rec.get("risk_reason")) != "risk_ok" else "-",
            "last": rec.get("last_price_target"),
            "bid": rec.get("bid_target"),
            "ask": rec.get("ask_target"),
            "spread": rec.get("spread_ticks_target"),
            "stop_ticks": rec.get("stop_ticks"),
            "spread_to_stop_pct": spread_to_stop_pct,
            "book": rec.get("book_display"),
            "comment": rec.get("display_reason"),
            "time": rec.get("snapshot_time"),
            "near_score": rec.get("near_score"),
        }
        ticker_overview.append(overview)
    for portfolio in portfolio_names:
        if not isinstance(config.get(portfolio), dict):
            continue
        for ticker in config[portfolio].get("tickers", []):
            key = (portfolio, str(ticker))
            if key in seen_tickers:
                continue
            startup = startup_by_key.get(key, {})
            startup_reason = startup.get("reason") or ""
            startup_state = str(startup.get("status") or "")
            session_hint = expected_session_status(portfolio)
            if startup_state == "skipped":
                continue
            elif session_hint is not None:
                state, comment = session_hint
                last = startup.get("last_price")
            elif startup_state == "loaded":
                state = "нет потока"
                comment = "подписан, ждём данные потока"
                last = startup.get("last_price")
            else:
                state = "нет потока"
                comment = "ждём данные потока"
                last = None
            ticker_overview.append(
                {
                    "portfolio": portfolio,
                    "ticker": str(ticker),
                    "position": "; ".join(positions_by_ticker.get(key, [])) or "-",
                    "state": state,
                    "risk": "-",
                    "risk_limit_rub": None,
                    "policy": human_reason(startup_reason) if startup_reason else "-",
                    "last": last,
                    "bid": None,
                    "ask": None,
                    "spread": None,
                    "book": "-",
                    "comment": comment,
                    "time": None,
                    "near_score": 0,
                }
            )

    heart = {}
    if not heartbeat.empty:
        last = heartbeat.tail(1).iloc[0].to_dict()
        heart = {k: (None if pd.isna(v) else v) for k, v in last.items()}

    exec_summary = {}
    if not summary.empty:
        row = summary.tail(1).iloc[0].to_dict()
        exec_summary = {k: (None if pd.isna(v) else v) for k, v in row.items()}

    open_positions_view = []
    for item in open_positions:
        if not isinstance(item, dict):
            continue
        open_positions_view.append(
            {
                **item,
                "contour": human_contour(item.get("contour")),
                "direction": human_direction(item.get("direction")),
                "risk_mode": human_risk_mode(item.get("risk_mode")),
                "risk_reason_text": human_reason(item.get("risk_reason")) if item.get("risk_reason") and str(item.get("risk_reason")) != "risk_ok" else "-",
                "exit_mode_label": human_exit_mode(item.get("exit_mode")) if item.get("exit_mode") else "-",
                "mark_source_label": human_exit_source(item.get("mark_source")) if item.get("mark_source") else "-",
            }
        )

    return {
        "base_dir": str(base_dir),
        "stats": stats,
        "by_ticker": by_ticker,
        "system_overview": system_overview,
        "portfolio_health": portfolio_health,
        "roll_overview": roll_overview,
        "startup_overview": startup_overview,
        "portfolio_overview": portfolio_overview,
        "ticker_overview": ticker_overview,
        "wide_spread_watchlist": wide_spread_watchlist,
        "gpt_shadow_overview": gpt_shadow_overview,
        "gpt_shadow_recent": gpt_shadow_recent,
        "open_positions": open_positions_view,
        "recent_trades": recent_trades,
        "micro": micro,
        "market_now": market_now,
        "heartbeat": heart,
        "execution_summary": exec_summary,
    }


HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Paper Dashboard</title>
  <style>
    :root {
      --bg: #0f1115;
      --panel: #171a21;
      --panel2: #1f2430;
      --text: #edf0f5;
      --muted: #9aa4b2;
      --line: #2c3340;
      --good: #30c48d;
      --bad: #ff6670;
      --warn: #e6b450;
      --blue: #6ea8fe;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, Segoe UI, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      letter-spacing: 0;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 16px 22px;
      border-bottom: 1px solid var(--line);
      background: #11141a;
      position: sticky;
      top: 0;
      z-index: 2;
    }
    h1 { font-size: 20px; margin: 0; font-weight: 700; }
    .status { color: var(--muted); font-size: 13px; text-align: right; }
    main { padding: 18px 22px 30px; max-width: 1480px; margin: 0 auto; }
    .grid { display: grid; gap: 14px; }
    .cards { grid-template-columns: repeat(7, minmax(130px, 1fr)); }
    .two { grid-template-columns: 1.2fr 0.8fr; align-items: start; }
    .card, section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .card { padding: 14px; min-height: 86px; }
    .label { color: var(--muted); font-size: 12px; }
    .value { font-size: 26px; margin-top: 8px; font-weight: 750; white-space: nowrap; }
    .good { color: var(--good); }
    .bad { color: var(--bad); }
    .warn { color: var(--warn); }
    section { margin-top: 14px; overflow: hidden; }
    h2 { font-size: 15px; padding: 12px 14px; margin: 0; border-bottom: 1px solid var(--line); }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 9px 10px; border-bottom: 1px solid var(--line); text-align: left; white-space: nowrap; }
    th { color: var(--muted); font-weight: 600; background: var(--panel2); }
    tr:last-child td { border-bottom: none; }
    td.comment { max-width: 360px; white-space: normal; line-height: 1.35; }
    .pill {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 999px;
      background: #283142;
      color: var(--text);
      font-size: 12px;
      font-weight: 700;
    }
    .pill.ready { background: rgba(48, 196, 141, 0.18); color: var(--good); }
    .pill.watch { background: rgba(110, 168, 254, 0.18); color: var(--blue); }
    .pill.wait { background: rgba(230, 180, 80, 0.18); color: var(--warn); }
    .pill.block { background: rgba(255, 102, 112, 0.16); color: var(--bad); }
    .table-wrap { overflow-x: auto; }
    .empty { color: var(--muted); padding: 14px; font-size: 13px; }
    @media (max-width: 1050px) {
      .cards { grid-template-columns: repeat(2, minmax(140px, 1fr)); }
      .two { grid-template-columns: 1fr; }
      header { align-items: flex-start; flex-direction: column; }
      .status { text-align: left; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Paper Dashboard</h1>
    <div class="status">
      <div>Обновлено: <span id="updated">-</span></div>
    </div>
  </header>
  <main>
    <div class="grid cards">
      <div class="card"><div class="label">Реализовано день ₽</div><div id="dayNet" class="value">0</div></div>
      <div class="card"><div class="label">Реализовано всего ₽</div><div id="net" class="value">0</div></div>
      <div class="card"><div class="label">Плавающее сейчас ₽</div><div id="openNet" class="value">0</div></div>
      <div class="card"><div class="label">Всего с позициями ₽</div><div id="totalNet" class="value">0</div></div>
      <div class="card"><div class="label">Открыто позиций</div><div id="open" class="value">0</div></div>
      <div class="card"><div class="label">Сделок сегодня</div><div id="closed" class="value">0</div></div>
      <div class="card"><div class="label">Автовосстановление</div><div id="watchdog" class="value">-</div></div>
    </div>
    <section>
      <h2>Система и восстановление</h2>
      <div class="table-wrap"><table id="system"></table></div>
    </section>
    <section>
      <h2>Здоровье контуров</h2>
      <div class="table-wrap"><table id="health"></table></div>
    </section>
    <section>
      <h2>Автоперенос контрактов</h2>
      <div class="table-wrap"><table id="rolls"></table></div>
    </section>
    <section>
      <h2>Загрузка контрактов</h2>
      <div class="table-wrap"><table id="startup"></table></div>
    </section>
    <section>
      <h2>Капитал контуров</h2>
      <div class="table-wrap"><table id="portfolios"></table></div>
    </section>
    <section>
      <h2>Открытые позиции</h2>
      <div id="positions"></div>
    </section>
    <section>
      <h2>GPT-модель в тени</h2>
      <div class="table-wrap"><table id="gptShadow"></table></div>
    </section>
    <section>
      <h2>Тикеры</h2>
      <div class="table-wrap"><table id="tickers"></table></div>
    </section>
    <section>
      <h2>Широкий спред</h2>
      <div class="table-wrap"><table id="wideSpread"></table></div>
    </section>
    <section>
      <h2>Последние сделки</h2>
      <div class="table-wrap"><table id="trades"></table></div>
    </section>
    <section>
      <h2>Последние сделки GPT-модели</h2>
      <div class="table-wrap"><table id="gptTrades"></table></div>
    </section>
  </main>
  <script>
    const fmt = (n, d = 2) => n === null || n === undefined || Number.isNaN(Number(n)) ? "-" : Number(n).toLocaleString("ru-RU", {maximumFractionDigits: d});
    const cls = n => Number(n) > 0 ? "good" : Number(n) < 0 ? "bad" : "";
    const badgeClass = s => {
      const v = String(s ?? "").toLowerCase();
      if (["близко","ok","загружен","выбран"].includes(v)) return "ready";
      if (["наблюдаем","наблюдать","в очереди на автоперенос","основной список","автоперенос"].includes(v)) return "watch";
      if (["прогрев","внимание","широкий","ещё рано"].includes(v)) return "wait";
      if (["фильтр","пропущен","ошибка","доминирует","устарело","нет данных","заблокирован"].includes(v)) return "block";
      return "";
    };
    const cell = (r, c) => {
      const value = c[3] ? fmt(r[c[1]], c[3]) : (r[c[1]] ?? "-");
      if (c[4] === "state" || c[4] === "badge") return `<td><span class="pill ${badgeClass(value)}">${value}</span></td>`;
      if (c[4] === "comment") return `<td class="comment">${value}</td>`;
      return `<td class="${c[2] ? cls(r[c[1]]) : ""}">${value}</td>`;
    };
    function table(el, cols, rows) {
      if (!rows || !rows.length) { el.innerHTML = '<tr><td class="empty">Нет данных</td></tr>'; return; }
      el.innerHTML = '<thead><tr>' + cols.map(c => `<th>${c[0]}</th>`).join("") + '</tr></thead><tbody>' +
        rows.map(r => '<tr>' + cols.map(c => cell(r, c)).join("") + '</tr>').join("") +
        '</tbody>';
    }
    let refreshing = false;
    async function refresh() {
      if (refreshing) return;
      refreshing = true;
      const sx = window.scrollX;
      const sy = window.scrollY;
      let data;
      try {
        const res = await fetch('/api/state');
        data = await res.json();
      } catch (e) {
        refreshing = false;
        return;
      }
      const s = data.stats || {};
      document.getElementById('updated').textContent = s.last_update || "-";
      const dayNetEl = document.getElementById('dayNet');
      dayNetEl.textContent = fmt(s.day_net);
      dayNetEl.className = "value " + cls(s.day_net);
      const netEl = document.getElementById('net');
      netEl.textContent = fmt(s.net);
      netEl.className = "value " + cls(s.net);
      const openNetEl = document.getElementById('openNet');
      openNetEl.textContent = fmt(s.open_net);
      openNetEl.className = "value " + cls(s.open_net);
      const totalNetEl = document.getElementById('totalNet');
      totalNetEl.textContent = fmt(s.total_net);
      totalNetEl.className = "value " + cls(s.total_net);
      document.getElementById('closed').textContent = s.day_closed_trades || 0;
      document.getElementById('open').textContent = s.open_positions || 0;
      const watchdogEl = document.getElementById('watchdog');
      watchdogEl.textContent = s.watchdog_status || "-";
      watchdogEl.className = "value " + (String(s.watchdog_status || "").toLowerCase() === "ok" ? "good" : String(s.watchdog_status || "").toLowerCase() === "ошибка" ? "bad" : "warn");
      table(document.getElementById('system'), [
        ["Сервис","service"], ["Статус","status",false,null,"badge"], ["Обновлено","updated"], ["Детали","detail",false,null,"comment"]
      ], data.system_overview || []);
      table(document.getElementById('health'), [
        ["Контур","portfolio"], ["Статус","status",false,null,"badge"], ["Обновление health","updated"], ["Возраст health, сек","health_age_sec"], ["Аптайм, сек","uptime_sec",false,1], ["Возраст потока, сек","stream_age_sec",false,1], ["Переподключения","reconnect_count"], ["Открыто","open_positions"], ["Закрыто","closed_trades"], ["Реализовано ₽","closed_net",true,2], ["Контуры","contours",false,null,"comment"]
      ], data.portfolio_health || []);
      table(document.getElementById('rolls'), [
        ["Контур","portfolio"], ["Тикер","ticker"], ["Семья","family"], ["DTE","days_to_expiration",false,1], ["Статус","status",false,null,"badge"], ["Выбран","selected"], ["Источник профиля","profile_source"], ["Кандидаты","candidates",false,null,"comment"], ["Комментарий","comment",false,null,"comment"]
      ], data.roll_overview || []);
      table(document.getElementById('startup'), [
        ["Контур","portfolio"], ["Тикер","ticker"], ["Семья","family"], ["Статус","status",false,null,"badge"], ["Откуда","load_reason",false,null,"badge"], ["Профиль","profile_source"], ["DTE","days_to_expiration",false,1], ["ГО buy","go_buy",false,2], ["ГО sell","go_sell",false,2], ["Комментарий","comment",false,null,"comment"]
      ], data.startup_overview || []);
      table(document.getElementById('portfolios'), [
        ["Контур","portfolio"], ["Старт ₽","capital",false,2], ["Реал. день ₽","day_net",true,2], ["Реал. всего ₽","closed_net",true,2], ["Плавающее ₽","open_net",true,2], ["Итого ₽","total_net",true,2], ["Доход %","return_pct",true,3], ["ГО занято ₽","used_margin",false,2], ["Свободно ₽","free_capital",false,2], ["Режимы риска","risk_modes"], ["Позиций","open_positions"], ["Сделок","closed_trades"]
      ], data.portfolio_overview || []);
      table(document.getElementById('gptShadow'), [
        ["Слой","model"], ["Контур","portfolio"], ["Тень ₽","net",true,2], ["Сделок","trades"], ["Плюс","wins"], ["Минус","losses"], ["Плюс %","win_rate",false,1], ["Средняя ₽","avg_trade",true,2]
      ], data.gpt_shadow_overview || []);
      table(document.getElementById('tickers'), [
        ["Контур","portfolio"], ["Тикер","ticker"], ["Позиция","position"], ["Готовность","state",false,null,"state"], ["Режим риска","risk"], ["Лимит риска ₽","risk_limit_rub",false,2], ["Политика","policy",false,null,"comment"], ["Последняя","last"], ["Bid","bid"], ["Ask","ask"], ["Спред","spread"], ["Стоп","stop_ticks"], ["Спред/стоп %","spread_to_stop_pct"], ["Стакан","book"], ["Комментарий","comment",false,null,"comment"]
      ], data.ticker_overview || []);
      table(document.getElementById('wideSpread'), [
        ["Контур","portfolio"], ["Тикер","ticker"], ["Последняя","last"], ["Спред","spread"], ["Стоп","stop_ticks"], ["Спред/стоп %","spread_to_stop_pct"], ["Класс","spread_class"], ["Стакан","book"], ["Комментарий","comment",false,null,"comment"]
      ], data.wide_spread_watchlist || []);
      table(document.getElementById('trades'), [
        ["Закрыта","time"], ["Открыта","opened_at"], ["Контур","portfolio"], ["Режим","contour"], ["Тикер","ticker"], ["Напр.","direction"], ["Qty","qty"], ["Вход","entry_price"], ["Триггер","trigger_price"], ["Выход цена","exit_price"], ["Выход","exit_source",false,null,"comment"], ["Лимит qty","stop_limit_qty"], ["Перелёт тиков","stop_overrun_ticks",true,2], ["Тики","ticks",true,2], ["Комиссия ₽","fees_rub",false,2], ["Результат ₽","net_pnl_rub",true,2], ["Статус","fill_status"], ["Причина","skip_reason",false,null,"comment"]
      ], data.recent_trades || []);
      table(document.getElementById('gptTrades'), [
        ["Время","time"], ["Слой","model_label"], ["Контур","portfolio"], ["Режим","contour"], ["Сигнал","signal_family"], ["Тикер","ticker"], ["Напр.","direction"], ["Qty","qty"], ["Вход","entry_price"], ["Триггер","trigger_price"], ["Выход цена","exit_price"], ["Тики","ticks",true,2], ["Комиссия ₽","fees_rub",false,2], ["Результат ₽","net_pnl_rub",true,2], ["Выход","exit_source",false,null,"comment"]
      ], data.gpt_shadow_recent || []);
      const positions = data.open_positions || [];
      const posEl = document.getElementById('positions');
      if (!positions.length) posEl.innerHTML = '<div class="empty">Нет открытых позиций</div>';
      else {
        posEl.innerHTML = '<div class="table-wrap"><table id="positionsTable"></table></div>';
        table(document.getElementById('positionsTable'), [
          ["Контур","portfolio"], ["Режим","contour"], ["Риск","risk_mode"], ["Политика","risk_reason_text",false,null,"comment"], ["Тикер","ticker"], ["Напр.","direction"], ["Qty","qty"], ["ГО ₽","margin_rub",false,2], ["Стоп ₽","full_stop_risk_rub",false,2], ["Модель выхода","exit_mode_label"], ["Вход","entry_price"], ["Последняя","last_price"], ["Mark","mark_price"], ["Источник mark","mark_source_label",false,null,"comment"], ["Стоп","stop_price"], ["Тики","unrealized_ticks",true,2], ["Грязными ₽","gross_pnl_rub",true,2], ["Комиссия ₽","fees_rub",false,2], ["Сейчас ₽","unrealized_net_rub",true,2], ["Открыта","opened_at"]
        ], positions);
      }
      window.scrollTo(sx, sy);
      refreshing = false;
    }
    refresh();
    setInterval(refresh, 10000);
  </script>
</body>
</html>"""


def make_handler(base_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self.send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/state":
                qs = parse_qs(parsed.query)
                selected = Path(qs.get("dir", [str(base_dir)])[0])
                if not selected.is_absolute():
                    selected = ROOT / selected
                state = build_state(selected)
                self.send(200, json.dumps(state, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")
                return
            self.send(404, b"not found", "text/plain; charset=utf-8")

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Local paper/live dashboard")
    parser.add_argument("--dir", default=str(REPORTS), help="Directory with paper CSV/JSON reports")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    base_dir = Path(args.dir).resolve()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(base_dir))
    print(f"Dashboard: http://{args.host}:{args.port}/")
    print(f"Reading: {base_dir}")
    server.serve_forever()


if __name__ == "__main__":
    main()
