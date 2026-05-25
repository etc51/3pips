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
PORTFOLIO_CAPITAL_RUB = 200_000.0


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
        return json.loads(path.read_text(encoding="utf-8"))
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
    if text == "restored_open_position":
        return "позиция восстановлена после рестарта"
    if text.startswith("expiry_filter"):
        return "фильтр: близко экспирация, новые входы запрещены"
    if text.startswith("roll_family_filter"):
        return "фильтр: уже есть позиция в этом семействе на переносе"
    if text.startswith("entry_signal long"):
        return "сигнал на вход: long"
    if text.startswith("entry_signal short"):
        return "сигнал на вход: short"
    if text.startswith("watch_conditions") or text.startswith("p="):
        return "условия наблюдаются, входа нет"
    if text == "duplicate_filter ticker_already_open" or text == "duplicate_filter_ticker_already_open":
        return "позиция по тикеру уже открыта"
    if text == "capital_filter no_free_margin":
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
    if not trades.empty and "time" in trades:
        trades["_time_sort"] = pd.to_datetime(trades["time"], errors="coerce")
        trades = trades.sort_values("_time_sort", na_position="last")

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

    closed = trades[pd.to_numeric(trades.get("net_pnl_rub"), errors="coerce").notna()].copy() if not trades.empty else pd.DataFrame()
    stats = equity_stats(closed.get("net_pnl_rub", pd.Series(dtype=float)))
    open_net_values = [item.get("unrealized_net_rub") for item in open_positions if isinstance(item, dict)]
    open_net = sum(float(v) for v in open_net_values if v is not None and math.isfinite(float(v)))
    stats["open_net"] = number(open_net)
    stats["total_net"] = number((stats.get("net") or 0.0) + open_net)
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
            ]
        )
    stats["last_update"] = latest_mtime(watched_paths)

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
        closed_net = float(pd.to_numeric(closed_part.get("net_pnl_rub", pd.Series(dtype=float)), errors="coerce").dropna().sum())
        open_part = [p for p in open_positions if isinstance(p, dict) and p.get("portfolio") == portfolio]
        open_net = sum(float(p.get("unrealized_net_rub") or 0.0) for p in open_part)
        margin = sum(float(p.get("margin_rub") or 0.0) for p in open_part)
        total = closed_net + open_net
        portfolio_overview.append(
            {
                "portfolio": portfolio,
                "capital": capital,
                "closed_net": number(closed_net),
                "open_net": number(open_net),
                "total_net": number(total),
                "equity": number(capital + total),
                "return_pct": number(total / capital * 100, 3) if capital else None,
                "used_margin": number(margin),
                "free_capital": number(capital + total - margin),
                "open_positions": len(open_part),
                "closed_trades": int(len(closed_part)),
            }
        )

    recent_trades_cols = [
        "time",
        "portfolio",
        "contour",
        "ticker",
        "direction",
        "qty",
        "entry_price",
        "exit_price",
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

    positions_by_ticker: dict[tuple[str, str], list[str]] = {}
    for item in open_positions:
        if not isinstance(item, dict):
            continue
        key = (str(item.get("portfolio") or ""), str(item.get("ticker") or ""))
        side = str(item.get("direction") or "").upper()
        entry = item.get("entry_price")
        stop = item.get("stop_price")
        positions_by_ticker.setdefault(key, []).append(f"{side} {entry} / стоп {stop}")

    ticker_overview = []
    seen_tickers = set()
    startup_by_key = {}
    if not startup_status.empty and {"portfolio", "ticker"}.issubset(startup_status.columns):
        for _, row in startup_status.groupby(["portfolio", "ticker"], dropna=True).tail(1).iterrows():
            startup_by_key[(str(row.get("portfolio") or ""), str(row.get("ticker") or ""))] = clean_record(row.to_dict())
    for rec in market_now:
        key = (str(rec.get("portfolio") or ""), str(rec.get("target_contract") or ""))
        seen_tickers.add(key)
        overview = {
            "portfolio": key[0],
            "ticker": key[1],
            "position": "; ".join(positions_by_ticker.get(key, [])) or "-",
            "state": rec.get("state_label"),
            "last": rec.get("last_price_target"),
            "bid": rec.get("bid_target"),
            "ask": rec.get("ask_target"),
            "spread": rec.get("spread_ticks_target"),
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
            if startup_state == "skipped":
                state = readiness_label(0, startup_reason)
                comment = human_reason(startup_reason)
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

    return {
        "base_dir": str(base_dir),
        "stats": stats,
        "by_ticker": by_ticker,
        "portfolio_overview": portfolio_overview,
        "ticker_overview": ticker_overview,
        "open_positions": open_positions,
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
    .cards { grid-template-columns: repeat(4, minmax(140px, 1fr)); }
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
      <div class="card"><div class="label">Реальный доход ₽</div><div id="net" class="value">0</div></div>
      <div class="card"><div class="label">Открыто позиций</div><div id="open" class="value">0</div></div>
      <div class="card"><div class="label">Сделок закрыто</div><div id="closed" class="value">0</div></div>
      <div class="card"><div class="label">W / L</div><div id="wl" class="value">0 / 0</div></div>
    </div>
    <section>
      <h2>Капитал контуров</h2>
      <div class="table-wrap"><table id="portfolios"></table></div>
    </section>
    <section>
      <h2>Открытые позиции</h2>
      <div id="positions"></div>
    </section>
    <section>
      <h2>Тикеры</h2>
      <div class="table-wrap"><table id="tickers"></table></div>
    </section>
    <section>
      <h2>Последние сделки</h2>
      <div class="table-wrap"><table id="trades"></table></div>
    </section>
  </main>
  <script>
    const fmt = (n, d = 2) => n === null || n === undefined || Number.isNaN(Number(n)) ? "-" : Number(n).toLocaleString("ru-RU", {maximumFractionDigits: d});
    const cls = n => Number(n) > 0 ? "good" : Number(n) < 0 ? "bad" : "";
    const stateClass = s => s === "близко" ? "ready" : s === "наблюдаем" ? "watch" : s === "прогрев" ? "wait" : s === "фильтр" ? "block" : "";
    const cell = (r, c) => {
      const value = c[3] ? fmt(r[c[1]], c[3]) : (r[c[1]] ?? "-");
      if (c[4] === "state") return `<td><span class="pill ${stateClass(value)}">${value}</span></td>`;
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
      const netEl = document.getElementById('net');
      netEl.textContent = fmt(s.net);
      netEl.className = "value " + cls(s.net);
      document.getElementById('closed').textContent = s.closed_trades || 0;
      document.getElementById('open').textContent = s.open_positions || 0;
      document.getElementById('wl').textContent = `${s.wins || 0} / ${s.losses || 0}`;
      table(document.getElementById('portfolios'), [
        ["Контур","portfolio"], ["Старт ₽","capital",false,2], ["Реальный доход ₽","closed_net",true,2], ["Доход %","return_pct",true,3], ["ГО занято ₽","used_margin",false,2], ["Свободно ₽","free_capital",false,2], ["Позиций","open_positions"], ["Сделок","closed_trades"]
      ], data.portfolio_overview || []);
      table(document.getElementById('tickers'), [
        ["Контур","portfolio"], ["Тикер","ticker"], ["Позиция","position"], ["Готовность","state",false,null,"state"], ["Last","last"], ["Bid","bid"], ["Ask","ask"], ["Спред","spread"], ["Стакан","book"], ["Комментарий","comment",false,null,"comment"]
      ], data.ticker_overview || []);
      table(document.getElementById('trades'), [
        ["Время","time"], ["Портфель","portfolio"], ["Контур","contour"], ["Тикер","ticker"], ["Напр.","direction"], ["Qty","qty"], ["Entry","entry_price"], ["Exit","exit_price"], ["Ticks","ticks",true,2], ["Fee","fees_rub",false,2], ["Net","net_pnl_rub",true,2], ["Статус","fill_status"], ["Причина","skip_reason"]
      ], data.recent_trades || []);
      const positions = data.open_positions || [];
      const posEl = document.getElementById('positions');
      if (!positions.length) posEl.innerHTML = '<div class="empty">Нет открытых позиций</div>';
      else {
        posEl.innerHTML = '<div class="table-wrap"><table id="positionsTable"></table></div>';
        table(document.getElementById('positionsTable'), [
          ["Контур","portfolio"], ["Режим","contour"], ["Тикер","ticker"], ["Напр.","direction"], ["Qty","qty"], ["ГО ₽","margin_rub",false,2], ["Entry","entry_price"], ["Last","last_price"], ["Mark","mark_price"], ["Stop","stop_price"], ["Тики","unrealized_ticks",true,2], ["Грязными ₽","gross_pnl_rub",true,2], ["Комиссия ₽","fees_rub",false,2], ["Сейчас ₽","unrealized_net_rub",true,2], ["Открыта","opened_at"]
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
