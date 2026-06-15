from __future__ import annotations

import argparse
import math
import os
import re
import sys
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import requests
from scipy import stats

warnings.filterwarnings("ignore", category=RuntimeWarning)

ROOT = Path(__file__).resolve().parents[1]
DATA_RAW = ROOT / "data" / "raw"
DATA_PROCESSED = ROOT / "data" / "processed"
RESULTS = ROOT / "results"
FIGURES = RESULTS / "figures"
REPORTS = ROOT / "reports"

MONTH_CODES = {
    "F": 1,
    "G": 2,
    "H": 3,
    "J": 4,
    "K": 5,
    "M": 6,
    "N": 7,
    "Q": 8,
    "U": 9,
    "V": 10,
    "X": 11,
    "Z": 12,
}
MONTH_NAMES = {
    1: "Jan",
    2: "Feb",
    3: "Mar",
    4: "Apr",
    5: "May",
    6: "Jun",
    7: "Jul",
    8: "Aug",
    9: "Sep",
    10: "Oct",
    11: "Nov",
    12: "Dec",
}
WINTER_MONTHS = {11, 12, 1, 2, 3}
SUMMER_MONTHS = {4, 5, 6, 7, 8, 9, 10}


@dataclass
class RunConfig:
    date_from: str
    date_till: str
    force: bool
    hourly: bool
    max_contracts: int | None
    request_sleep: float
    min_obs: int
    min_trades: int
    min_volume: float
    cost_bps: float
    slippage_bps: float
    bootstrap_samples: int


def ensure_dirs() -> None:
    for path in [DATA_RAW, DATA_PROCESSED, RESULTS, FIGURES, REPORTS]:
        path.mkdir(parents=True, exist_ok=True)


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def request_json(url: str, params: dict | None = None, retries: int = 4, sleep: float = 0.5) -> dict:
    last_error = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=40)
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(sleep * (attempt + 1))
    raise RuntimeError(f"Request failed: {url} {params}") from last_error


def iss_table(payload: dict, name: str) -> pd.DataFrame:
    block = payload.get(name, {})
    cols = block.get("columns", [])
    data = block.get("data", [])
    return pd.DataFrame(data, columns=cols)


def paged_iss_table(url: str, table: str, params: dict | None = None, sleep: float = 0.1) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    start = 0
    while True:
        q = dict(params or {})
        q["start"] = start
        payload = request_json(url, q, sleep=sleep)
        df = iss_table(payload, table)
        if df.empty:
            break
        frames.append(df)
        if len(df) < 100:
            break
        start += len(df)
        time.sleep(sleep)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def parse_contract(secid: str, first_trade_year: int | None = None) -> dict:
    m = re.fullmatch(r"(NG|NR)([FGHJKMNQUVXZ])(\d)", str(secid))
    if not m:
        return {}
    family = "NG" if m.group(1) == "NG" else "NGM"
    prefix, month_code, year_digit = m.groups()
    month = MONTH_CODES[month_code]
    year_digit_i = int(year_digit)
    if first_trade_year is None:
        base_year = 2020 + year_digit_i
    else:
        candidates = [y for y in range(first_trade_year - 2, first_trade_year + 11) if y % 10 == year_digit_i]
        base_year = min(candidates, key=lambda y: abs(y - first_trade_year)) if candidates else 2020 + year_digit_i
        if base_year < first_trade_year and month < 7:
            next_candidate = base_year + 10
            if next_candidate <= first_trade_year + 3:
                base_year = next_candidate
    return {
        "prefix": prefix,
        "family": family,
        "contract_month_code": month_code,
        "contract_month": month,
        "contract_year": base_year,
        "contract_ym": base_year * 100 + month,
    }


def contract_universe() -> list[str]:
    secids = []
    for prefix in ["NG", "NR"]:
        for digit in range(10):
            for code in MONTH_CODES:
                secids.append(f"{prefix}{code}{digit}")
    return secids


def download_current_specs(cfg: RunConfig) -> pd.DataFrame:
    path = DATA_RAW / "moex_current_specs.csv"
    if path.exists() and not cfg.force:
        return pd.read_csv(path)
    log("Downloading MOEX current specs")
    url = "https://iss.moex.com/iss/engines/futures/markets/forts/securities.json"
    cols = ",".join(
        [
            "SECID",
            "BOARDID",
            "SHORTNAME",
            "SECNAME",
            "PREVSETTLEPRICE",
            "DECIMALS",
            "MINSTEP",
            "LASTTRADEDATE",
            "LASTDELDATE",
            "SECTYPE",
            "LATNAME",
            "ASSETCODE",
            "PREVOPENPOSITION",
            "LOTVOLUME",
            "INITIALMARGIN",
            "STEPPRICE",
            "LASTSETTLEPRICE",
        ]
    )
    payload = request_json(
        url,
        {"iss.meta": "off", "iss.only": "securities", "securities.columns": cols},
        sleep=cfg.request_sleep,
    )
    df = iss_table(payload, "securities")
    if df.empty:
        df.to_csv(path, index=False)
        return df
    mask = df["SECID"].astype(str).str.match(r"^(NG|NR)[FGHJKMNQUVXZ]\d$")
    df = df.loc[mask].copy()
    parsed = pd.DataFrame([parse_contract(x) for x in df["SECID"]])
    df = pd.concat([df.reset_index(drop=True), parsed.reset_index(drop=True)], axis=1)
    df.to_csv(path, index=False)
    return df


def download_moex_history(cfg: RunConfig) -> pd.DataFrame:
    path = DATA_RAW / "moex_history_daily.csv"
    if path.exists() and not cfg.force:
        return pd.read_csv(path, parse_dates=["TRADEDATE"])
    log("Downloading MOEX daily history with settlement/OI")
    rows = []
    secids = contract_universe()
    if cfg.max_contracts:
        secids = secids[: cfg.max_contracts]
    cols = ",".join(
        [
            "BOARDID",
            "TRADEDATE",
            "SECID",
            "OPEN",
            "LOW",
            "HIGH",
            "CLOSE",
            "OPENPOSITIONVALUE",
            "VALUE",
            "VOLUME",
            "OPENPOSITION",
            "SETTLEPRICE",
            "WAPRICE",
            "CHANGE",
            "QTY",
            "NUMTRADES",
            "SHORTNAME",
            "ASSETCODE",
        ]
    )
    base = "https://iss.moex.com/iss/history/engines/futures/markets/forts/boards/RFUD/securities/{secid}.json"
    for i, secid in enumerate(secids, 1):
        url = base.format(secid=secid)
        df = paged_iss_table(
            url,
            "history",
            {
                "from": cfg.date_from,
                "till": cfg.date_till,
                "iss.meta": "off",
                "history.columns": cols,
            },
            sleep=cfg.request_sleep,
        )
        if not df.empty:
            rows.append(df)
            log(f"  {secid}: {len(df)} daily rows")
        if i % 20 == 0:
            log(f"  checked {i}/{len(secids)} contracts")
    out = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    if not out.empty:
        out["TRADEDATE"] = pd.to_datetime(out["TRADEDATE"])
        first_year = out.groupby("SECID")["TRADEDATE"].transform("min").dt.year
        parsed = pd.DataFrame([parse_contract(s, int(y)) for s, y in zip(out["SECID"], first_year)])
        out = pd.concat([out.reset_index(drop=True), parsed.reset_index(drop=True)], axis=1)
        out = out.sort_values(["family", "SECID", "TRADEDATE"]).drop_duplicates(["SECID", "TRADEDATE"])
    out.to_csv(path, index=False)
    return out


def download_moex_candles(cfg: RunConfig, secids: list[str], interval: int) -> pd.DataFrame:
    path = DATA_RAW / f"moex_candles_{interval}.csv"
    if path.exists() and not cfg.force:
        return pd.read_csv(path, parse_dates=["begin", "end"])
    log(f"Downloading MOEX candles interval={interval}")
    rows = []
    base = "https://iss.moex.com/iss/engines/futures/markets/forts/boards/RFUD/securities/{secid}/candles.json"
    for i, secid in enumerate(secids, 1):
        url = base.format(secid=secid)
        df = paged_iss_table(
            url,
            "candles",
            {
                "from": cfg.date_from,
                "till": cfg.date_till,
                "interval": interval,
                "iss.meta": "off",
            },
            sleep=cfg.request_sleep,
        )
        if not df.empty:
            df["SECID"] = secid
            rows.append(df)
            log(f"  {secid}: {len(df)} candle rows")
        if i % 20 == 0:
            log(f"  candles checked {i}/{len(secids)}")
    out = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    if not out.empty:
        for c in ["begin", "end"]:
            if c in out:
                out[c] = pd.to_datetime(out[c])
    out.to_csv(path, index=False)
    return out


def fred_series(series_id: str, name: str) -> pd.DataFrame:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    df = pd.read_csv(url)
    df.columns = ["date", name]
    df["date"] = pd.to_datetime(df["date"])
    df[name] = pd.to_numeric(df[name].replace(".", np.nan), errors="coerce")
    return df


def cbr_usdrub(date_from: str, date_till: str) -> pd.DataFrame:
    dt1 = pd.to_datetime(date_from).strftime("%d/%m/%Y")
    dt2 = pd.to_datetime(date_till).strftime("%d/%m/%Y")
    url = "https://www.cbr.ru/scripts/XML_dynamic.asp"
    params = {"date_req1": dt1, "date_req2": dt2, "VAL_NM_RQ": "R01235"}
    r = requests.get(url, params=params, timeout=40)
    r.raise_for_status()
    import xml.etree.ElementTree as ET

    root = ET.fromstring(r.content)
    rows = []
    for rec in root.findall("Record"):
        rows.append(
            {
                "date": pd.to_datetime(rec.attrib["Date"], format="%d.%m.%Y"),
                "usdrub_cbr": float(rec.findtext("Value", "nan").replace(",", ".")),
            }
        )
    return pd.DataFrame(rows)


def eia_storage_history() -> pd.DataFrame:
    urls = [
        "https://ir.eia.gov/ngs/ngshistory.xls",
        "https://ir.eia.gov/ngs/wngsr.csv",
    ]
    for url in urls:
        try:
            if url.endswith(".xls"):
                history = pd.read_excel(url, sheet_name="html_report_history", header=6)
                changes = pd.read_excel(url, sheet_name="weekly_net_changes", header=6)
                history = history.rename(columns={"Week ending": "date", "Total Lower 48": "storage_bcf"})
                changes = changes.rename(columns={changes.columns[0]: "date", "Total Lower 48": "storage_change_bcf"})
                out = history[["date", "storage_bcf"]].merge(changes[["date", "storage_change_bcf"]], on="date", how="left")
                out["date"] = pd.to_datetime(out["date"], errors="coerce")
                out["storage_bcf"] = pd.to_numeric(out["storage_bcf"], errors="coerce")
                out["storage_change_bcf"] = pd.to_numeric(out["storage_change_bcf"], errors="coerce")
                out = out.dropna(subset=["date", "storage_bcf"]).drop_duplicates("date").sort_values("date")
                if not out.empty:
                    return out
            else:
                df = pd.read_csv(url)
                date_col = next((c for c in df.columns if "date" in c.lower() or "week" in c.lower()), df.columns[0])
                num_cols = [c for c in df.columns if c != date_col and pd.to_numeric(df[c], errors="coerce").notna().sum() > 3]
                out = pd.DataFrame({"date": pd.to_datetime(df[date_col], errors="coerce")})
                if num_cols:
                    out["storage_bcf"] = pd.to_numeric(df[num_cols[0]], errors="coerce")
                if len(num_cols) > 1:
                    out["storage_change_bcf"] = pd.to_numeric(df[num_cols[1]], errors="coerce")
                out = out.dropna(subset=["date"]).sort_values("date")
                if not out.empty:
                    return out
        except Exception:
            continue
    return pd.DataFrame(columns=["date", "storage_bcf", "storage_change_bcf"])


def add_storage_5y_features(storage: pd.DataFrame) -> pd.DataFrame:
    if storage.empty:
        return storage
    out = storage.sort_values("date").copy()
    out["iso_week"] = out["date"].dt.isocalendar().week.astype(int)
    avg_level = []
    avg_change = []
    for _, row in out.iterrows():
        start = row["date"] - pd.DateOffset(years=5)
        hist = out[(out["date"] < row["date"]) & (out["date"] >= start) & (out["iso_week"] == row["iso_week"])]
        avg_level.append(hist["storage_bcf"].mean())
        avg_change.append(hist["storage_change_bcf"].mean())
    out["storage_5y_avg"] = avg_level
    out["storage_surplus_bcf"] = out["storage_bcf"] - out["storage_5y_avg"]
    out["storage_change_5y_avg"] = avg_change
    out["storage_change_vs_5y"] = out["storage_change_bcf"] - out["storage_change_5y_avg"]
    return out.drop(columns=["iso_week"])


def check_tbank_token() -> pd.DataFrame:
    rows = []
    candidates = []
    desktop = Path.home() / "Desktop"
    if desktop.exists():
        for p in sorted(desktop.glob("*.txt")):
            try:
                txt = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for token in re.findall(r"(?i)(?:t\.[A-Za-z0-9_-]{20,}|[A-Za-z0-9_-]{40,})", txt):
                candidates.append((f"file:{p.name}", token.strip()))
    for name in ["TBANK_TOKEN_READONLY", "TINKOFF_TOKEN"]:
        value = os.environ.get(name)
        if value:
            candidates.append((f"env:{name}", value.strip()))
    seen = set()
    unique = []
    for src, token in candidates:
        if token not in seen:
            seen.add(token)
            unique.append((src, token))
    try:
        try:
            from t_tech.invest import Client
        except Exception:
            from tinkoff.invest import Client

        for src, token in unique:
            try:
                with Client(token) as client:
                    accounts = client.users.get_accounts()
                rows.append({"source": src, "status": "working", "accounts_count": len(accounts.accounts)})
                break
            except Exception as exc:  # noqa: BLE001
                rows.append({"source": src, "status": f"failed:{type(exc).__name__}", "accounts_count": np.nan})
    except Exception as exc:  # noqa: BLE001
        rows.append({"source": "sdk", "status": f"unavailable:{type(exc).__name__}", "accounts_count": np.nan})
    if not rows:
        rows.append({"source": "none", "status": "not_found", "accounts_count": np.nan})
    out = pd.DataFrame(rows)
    out["_rank"] = (out["status"] != "working").astype(int)
    out = out.sort_values("_rank").drop(columns="_rank").reset_index(drop=True)
    out.to_csv(DATA_RAW / "tbank_token_check.csv", index=False)
    return out


def download_external(cfg: RunConfig) -> pd.DataFrame:
    path = DATA_RAW / "external_daily.csv"
    storage_path = DATA_RAW / "eia_storage_weekly.csv"
    if path.exists() and storage_path.exists() and not cfg.force:
        return pd.read_csv(path, parse_dates=["date"])
    log("Downloading external data: FRED, CBR, EIA storage")
    frames = [
        fred_series("DHHNGSP", "henry_hub_spot"),
        fred_series("DCOILWTICO", "wti_spot"),
        fred_series("DCOILBRENTEU", "brent_spot"),
        cbr_usdrub(cfg.date_from, cfg.date_till),
    ]
    ext = frames[0]
    for df in frames[1:]:
        ext = ext.merge(df, on="date", how="outer")
    ext = ext[(ext["date"] >= pd.to_datetime(cfg.date_from)) & (ext["date"] <= pd.to_datetime(cfg.date_till))]
    ext = ext.sort_values("date")
    for col in ["henry_hub_spot", "wti_spot", "brent_spot", "usdrub_cbr"]:
        if col in ext:
            ext[col] = ext[col].ffill()
    storage = eia_storage_history()
    if not storage.empty:
        storage = storage[(storage["date"] >= pd.to_datetime(cfg.date_from) - pd.Timedelta(days=370 * 5))]
        storage = add_storage_5y_features(storage)
    storage.to_csv(storage_path, index=False)
    if not storage.empty:
        storage_daily = storage.set_index("date").resample("D").ffill().reset_index()
        ext = ext.merge(storage_daily, on="date", how="left")
        storage_cols = [c for c in storage_daily.columns if c != "date"]
        ext[storage_cols] = ext[storage_cols].ffill()
    ext.to_csv(path, index=False)
    return ext


def build_contract_summary(history: pd.DataFrame, specs: pd.DataFrame) -> pd.DataFrame:
    if history.empty:
        return pd.DataFrame()
    h = history.copy()
    numeric = ["OPEN", "LOW", "HIGH", "CLOSE", "SETTLEPRICE", "VOLUME", "OPENPOSITION", "NUMTRADES"]
    for col in numeric:
        if col in h:
            h[col] = pd.to_numeric(h[col], errors="coerce")
    summary = (
        h.groupby(["family", "SECID", "contract_year", "contract_month", "contract_ym"], as_index=False)
        .agg(
            first_trade=("TRADEDATE", "min"),
            last_trade=("TRADEDATE", "max"),
            rows=("TRADEDATE", "size"),
            total_volume=("VOLUME", "sum"),
            avg_volume=("VOLUME", "mean"),
            avg_numtrades=("NUMTRADES", "mean"),
            max_open_interest=("OPENPOSITION", "max"),
        )
        .sort_values(["family", "contract_ym"])
    )
    if not specs.empty and "SECID" in specs:
        keep = [c for c in ["SECID", "SHORTNAME", "SECNAME", "LASTTRADEDATE", "LOTVOLUME", "MINSTEP", "STEPPRICE", "INITIALMARGIN"] if c in specs]
        summary = summary.merge(specs[keep].drop_duplicates("SECID"), on="SECID", how="left")
    summary["expiration_date"] = pd.to_datetime(summary.get("LASTTRADEDATE"), errors="coerce")
    summary["expiration_date"] = summary["expiration_date"].fillna(summary["last_trade"])
    summary.to_csv(DATA_PROCESSED / "contract_summary.csv", index=False)
    return summary


def normalize_history(history: pd.DataFrame, contract_summary: pd.DataFrame) -> pd.DataFrame:
    h = history.copy()
    h = h.rename(columns={"TRADEDATE": "date", "SECID": "secid"})
    h["date"] = pd.to_datetime(h["date"])
    for col in ["OPEN", "LOW", "HIGH", "CLOSE", "SETTLEPRICE", "VOLUME", "OPENPOSITION", "NUMTRADES"]:
        if col in h:
            h[col.lower()] = pd.to_numeric(h[col], errors="coerce")
    h["price"] = h["settleprice"].where(h["settleprice"].notna(), h["close"])
    h = h.merge(
        contract_summary[["SECID", "expiration_date", "first_trade", "last_trade"]].rename(columns={"SECID": "secid"}),
        on="secid",
        how="left",
    )
    h["expiration_date"] = pd.to_datetime(h["expiration_date"])
    h["dte"] = (h["expiration_date"] - h["date"]).dt.days
    return h


def select_nth_contract(group: pd.DataFrame, n: int) -> pd.Series | None:
    g = group[(group["price"].notna()) & (group["dte"] >= 1)].sort_values(["expiration_date", "contract_ym"])
    if len(g) < n:
        return None
    return g.iloc[n - 1]


def build_continuous(history: pd.DataFrame, contract_summary: pd.DataFrame, external: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    h = normalize_history(history, contract_summary)
    records = []
    for (family, date), g in h.groupby(["family", "date"]):
        front = select_nth_contract(g, 1)
        second = select_nth_contract(g, 2)
        if front is not None:
            records.append(
                {
                    "date": date,
                    "family": family,
                    "series": "front",
                    "secid": front["secid"],
                    "price": front["price"],
                    "volume": front.get("volume", np.nan),
                    "open_interest": front.get("openposition", np.nan),
                    "numtrades": front.get("numtrades", np.nan),
                    "dte": front["dte"],
                    "contract_month": front["contract_month"],
                    "contract_year": front["contract_year"],
                    "contract_ym": front["contract_ym"],
                }
            )
        if second is not None:
            records.append(
                {
                    "date": date,
                    "family": family,
                    "series": "second",
                    "secid": second["secid"],
                    "price": second["price"],
                    "volume": second.get("volume", np.nan),
                    "open_interest": second.get("openposition", np.nan),
                    "numtrades": second.get("numtrades", np.nan),
                    "dte": second["dte"],
                    "contract_month": second["contract_month"],
                    "contract_year": second["contract_year"],
                    "contract_ym": second["contract_ym"],
                }
            )
    cont = pd.DataFrame(records).sort_values(["family", "series", "date"])
    if not cont.empty:
        cont["ret1"] = cont.groupby(["family", "series"])["price"].pct_change()
        cont["roll"] = cont.groupby(["family", "series"])["secid"].transform(lambda s: s != s.shift(1))
        cont = cont.merge(external, on="date", how="left")
        cont.to_csv(DATA_PROCESSED / "continuous_daily.csv", index=False)

    spreads = []
    for (family, date), g in h.groupby(["family", "date"]):
        tradable = g[(g["price"].notna()) & (g["dte"] >= 1)].sort_values(["expiration_date", "contract_ym"])
        if len(tradable) >= 2:
            f, n = tradable.iloc[0], tradable.iloc[1]
            spreads.append(
                {
                    "date": date,
                    "family": family,
                    "spread": "front_next",
                    "front_secid": f["secid"],
                    "back_secid": n["secid"],
                    "price": f["price"] - n["price"],
                    "front_price": f["price"],
                    "back_price": n["price"],
                    "volume": min(float(f.get("volume", np.nan) or np.nan), float(n.get("volume", np.nan) or np.nan)),
                    "open_interest": min(float(f.get("openposition", np.nan) or np.nan), float(n.get("openposition", np.nan) or np.nan)),
                    "dte": f["dte"],
                    "front_month": f["contract_month"],
                    "back_month": n["contract_month"],
                }
            )
        winter = tradable[tradable["contract_month"].isin(WINTER_MONTHS)]
        if not winter.empty and len(tradable) >= 1:
            f, w = tradable.iloc[0], winter.iloc[0]
            if f["secid"] != w["secid"]:
                spreads.append(
                    {
                        "date": date,
                        "family": family,
                        "spread": "front_winter",
                        "front_secid": f["secid"],
                        "back_secid": w["secid"],
                        "price": f["price"] - w["price"],
                        "front_price": f["price"],
                        "back_price": w["price"],
                        "volume": min(float(f.get("volume", np.nan) or np.nan), float(w.get("volume", np.nan) or np.nan)),
                        "open_interest": min(float(f.get("openposition", np.nan) or np.nan), float(w.get("openposition", np.nan) or np.nan)),
                        "dte": f["dte"],
                        "front_month": f["contract_month"],
                        "back_month": w["contract_month"],
                    }
                )
        summer = tradable[tradable["contract_month"].isin(SUMMER_MONTHS)]
        winter_after = tradable[tradable["contract_month"].isin(WINTER_MONTHS)]
        if not summer.empty and not winter_after.empty:
            s, w = summer.iloc[0], winter_after.iloc[0]
            if s["secid"] != w["secid"]:
                spreads.append(
                    {
                        "date": date,
                        "family": family,
                        "spread": "summer_winter",
                        "front_secid": s["secid"],
                        "back_secid": w["secid"],
                        "price": s["price"] - w["price"],
                        "front_price": s["price"],
                        "back_price": w["price"],
                        "volume": min(float(s.get("volume", np.nan) or np.nan), float(w.get("volume", np.nan) or np.nan)),
                        "open_interest": min(float(s.get("openposition", np.nan) or np.nan), float(w.get("openposition", np.nan) or np.nan)),
                        "dte": s["dte"],
                        "front_month": s["contract_month"],
                        "back_month": w["contract_month"],
                    }
                )
    spr = pd.DataFrame(spreads).sort_values(["family", "spread", "date"])
    if not spr.empty:
        spr["ret1"] = spr.groupby(["family", "spread"])["price"].diff()
        spr = spr.merge(external, on="date", how="left")
        spr.to_csv(DATA_PROCESSED / "calendar_spreads.csv", index=False)
    return cont, spr


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.sort_values("date").copy()
    out["month"] = out["date"].dt.month
    out["dow"] = out["date"].dt.dayofweek
    out["is_thursday"] = (out["dow"] == 3).astype(int)
    out["injection"] = out["month"].between(4, 10).astype(int)
    out["withdrawal"] = out["month"].isin([11, 12, 1, 2, 3]).astype(int)
    px = out["price"].astype(float)
    out["ret_1"] = px.pct_change(fill_method=None)
    for w in [1, 3, 5, 10, 20]:
        out[f"moex_mom_{w}"] = px.pct_change(w, fill_method=None)
        if "henry_hub_spot" in out:
            out[f"hh_mom_{w}"] = out["henry_hub_spot"].pct_change(w, fill_method=None)
        if "brent_spot" in out:
            out[f"brent_mom_{w}"] = out["brent_spot"].pct_change(w, fill_method=None)
        if "wti_spot" in out:
            out[f"wti_mom_{w}"] = out["wti_spot"].pct_change(w, fill_method=None)
        if "usdrub_cbr" in out:
            out[f"usdrub_mom_{w}"] = out["usdrub_cbr"].pct_change(w, fill_method=None)
    delta = px.diff()
    gain = delta.clip(lower=0).rolling(14, min_periods=8).mean()
    loss = (-delta.clip(upper=0)).rolling(14, min_periods=8).mean()
    rs = gain / loss.replace(0, np.nan)
    out["rsi_14"] = 100 - 100 / (1 + rs)
    out["atr_proxy_14"] = out["ret_1"].abs().rolling(14, min_periods=8).mean()
    out["breakout_20"] = (px > px.shift(1).rolling(20, min_periods=10).max()).astype(float)
    out["breakdown_20"] = (px < px.shift(1).rolling(20, min_periods=10).min()).astype(float)
    out["volume_z_20"] = (out["volume"] - out["volume"].rolling(20, min_periods=10).mean()) / out["volume"].rolling(20, min_periods=10).std()
    out["oi_chg_5"] = out["open_interest"].pct_change(5, fill_method=None)
    if "henry_hub_spot" in out:
        hh = out["henry_hub_spot"]
        out["hh_z_20"] = (hh - hh.rolling(20, min_periods=10).mean()) / hh.rolling(20, min_periods=10).std()
    if "brent_spot" in out and "henry_hub_spot" in out:
        out["gas_oil_ratio"] = out["henry_hub_spot"] / out["brent_spot"]
        out["gas_oil_ratio_z_60"] = (
            out["gas_oil_ratio"] - out["gas_oil_ratio"].rolling(60, min_periods=30).mean()
        ) / out["gas_oil_ratio"].rolling(60, min_periods=30).std()
    if "storage_surplus_bcf" in out:
        out["storage_surplus_z"] = (
            out["storage_surplus_bcf"] - out["storage_surplus_bcf"].rolling(52, min_periods=20).mean()
        ) / out["storage_surplus_bcf"].rolling(52, min_periods=20).std()
    return out


def signal_library(df: pd.DataFrame) -> dict[str, pd.Series]:
    sig: dict[str, pd.Series] = {}
    month = df["month"]
    for m in range(1, 13):
        sig[f"month_{MONTH_NAMES[m]}_long"] = (month == m).astype(float)
        sig[f"month_{MONTH_NAMES[m]}_short"] = -(month == m).astype(float)
    for start in range(1, 13):
        for length in range(1, 7):
            months = {((start + i - 1) % 12) + 1 for i in range(length)}
            sig[f"season_window_{start:02d}_{length}m_long"] = month.isin(months).astype(float)
            sig[f"season_window_{start:02d}_{length}m_short"] = -month.isin(months).astype(float)
    sig["injection_long"] = df["injection"].astype(float)
    sig["injection_short"] = -df["injection"].astype(float)
    sig["withdrawal_long"] = df["withdrawal"].astype(float)
    sig["withdrawal_short"] = -df["withdrawal"].astype(float)
    for d in range(5):
        sig[f"dow_{d}_long"] = (df["dow"] == d).astype(float)
        sig[f"dow_{d}_short"] = -(df["dow"] == d).astype(float)
    sig["thursday_eia_long"] = df["is_thursday"].astype(float)
    sig["thursday_eia_short"] = -df["is_thursday"].astype(float)
    sig["after_storage_report_long"] = (df["dow"] == 4).astype(float)
    sig["after_storage_report_short"] = -(df["dow"] == 4).astype(float)
    for lo, hi in [(1, 5), (6, 10), (11, 20), (21, 30)]:
        mask = df["dte"].between(lo, hi)
        sig[f"time_to_expiry_T{lo}_{hi}_long"] = mask.astype(float)
        sig[f"time_to_expiry_T{lo}_{hi}_short"] = -mask.astype(float)
    sig["rollover_window_long"] = df["dte"].between(3, 7).astype(float)
    sig["rollover_window_short"] = -df["dte"].between(3, 7).astype(float)
    for base in ["moex", "hh", "brent", "wti", "usdrub"]:
        for w in [1, 3, 5, 10, 20]:
            col = f"{base}_mom_{w}" if base != "moex" else f"moex_mom_{w}"
            if col in df:
                sig[f"{col}_trend_long"] = np.sign(df[col]).clip(lower=0)
                sig[f"{col}_trend_short"] = -np.sign(df[col]).clip(upper=0).abs()
                sig[f"{col}_meanrev_long"] = (df[col] < 0).astype(float)
                sig[f"{col}_meanrev_short"] = -(df[col] > 0).astype(float)
    if "hh_z_20" in df:
        sig["hh_meanrev_oversold_long"] = (df["hh_z_20"] < -1).astype(float)
        sig["hh_meanrev_overbought_short"] = -(df["hh_z_20"] > 1).astype(float)
    sig["rsi_oversold_long"] = (df["rsi_14"] < 30).astype(float)
    sig["rsi_overbought_short"] = -(df["rsi_14"] > 70).astype(float)
    sig["atr_expansion_long"] = (df["atr_proxy_14"] > df["atr_proxy_14"].rolling(60, min_periods=20).median()).astype(float)
    sig["breakout_20_long"] = df["breakout_20"].fillna(0)
    sig["breakdown_20_short"] = -df["breakdown_20"].fillna(0)
    sig["volume_spike_long"] = (df["volume_z_20"] > 1.5).astype(float)
    sig["volume_spike_short"] = -(df["volume_z_20"] > 1.5).astype(float)
    sig["oi_rising_long"] = (df["oi_chg_5"] > 0).astype(float)
    sig["oi_falling_short"] = -(df["oi_chg_5"] < 0).astype(float)
    if "gas_oil_ratio_z_60" in df:
        sig["gas_oil_low_long"] = (df["gas_oil_ratio_z_60"] < -1).astype(float)
        sig["gas_oil_high_short"] = -(df["gas_oil_ratio_z_60"] > 1).astype(float)
    if "storage_surplus_z" in df:
        sig["storage_deficit_long"] = (df["storage_surplus_z"] < -1).astype(float)
        sig["storage_surplus_short"] = -(df["storage_surplus_z"] > 1).astype(float)
    if "storage_change_vs_5y" in df:
        sig["storage_draw_vs_5y_long"] = (df["storage_change_vs_5y"] < 0).astype(float)
        sig["storage_injection_vs_5y_short"] = -(df["storage_change_vs_5y"] > 0).astype(float)
    # Combination patterns: intentionally simple and known only after close.
    if "hh_mom_5" in df and "storage_surplus_z" in df:
        sig["combo_withdrawal_hh_up_storage_deficit_long"] = (
            (df["withdrawal"] == 1) & (df["hh_mom_5"] > 0) & (df["storage_surplus_z"] < 0)
        ).astype(float)
        sig["combo_injection_hh_down_storage_surplus_short"] = -(
            (df["injection"] == 1) & (df["hh_mom_5"] < 0) & (df["storage_surplus_z"] > 0)
        ).astype(float)
    if "brent_mom_10" in df and "usdrub_mom_10" in df:
        sig["combo_fx_oil_up_long"] = ((df["brent_mom_10"] > 0) & (df["usdrub_mom_10"] > 0)).astype(float)
        sig["combo_fx_oil_down_short"] = -((df["brent_mom_10"] < 0) & (df["usdrub_mom_10"] < 0)).astype(float)
    return sig


def bh_fdr(pvals: pd.Series) -> pd.Series:
    p = pvals.fillna(1.0).clip(0, 1)
    n = len(p)
    order = np.argsort(p.values)
    ranked = p.values[order]
    adj = np.empty(n)
    prev = 1.0
    for i in range(n - 1, -1, -1):
        rank = i + 1
        value = min(prev, ranked[i] * n / rank)
        adj[i] = value
        prev = value
    out = np.empty(n)
    out[order] = adj
    return pd.Series(out, index=pvals.index)


def bootstrap_ci(x: np.ndarray, samples: int = 500, seed: int = 7) -> tuple[float, float]:
    x = x[np.isfinite(x)]
    if len(x) < 10 or samples <= 0:
        return np.nan, np.nan
    rng = np.random.default_rng(seed)
    means = [rng.choice(x, size=len(x), replace=True).mean() for _ in range(samples)]
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return np.nan
    peak = equity.cummax()
    dd = equity / peak - 1
    return float(dd.min())


def walk_forward_score(returns: pd.Series, n_splits: int = 4) -> tuple[float, float]:
    r = returns.dropna()
    if len(r) < 40:
        return np.nan, np.nan
    chunks = np.array_split(r.values, n_splits)
    train_scores = []
    test_scores = []
    for i in range(1, len(chunks)):
        train = np.concatenate(chunks[:i])
        test = chunks[i]
        train_scores.append(np.nanmean(train))
        test_scores.append(np.nanmean(test))
    return float(np.nanmean(train_scores)), float(np.nanmean(test_scores))


def evaluate_patterns(panel: pd.DataFrame, instrument_type: str, cfg: RunConfig) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    results = []
    eq_frames = []
    dd_rows = []
    holding_periods = [1, 2, 3, 5, 10, 20]
    group_cols = ["family", "series"] if instrument_type == "outright" else ["family", "spread"]
    for keys, g in panel.groupby(group_cols):
        if not isinstance(keys, tuple):
            keys = (keys,)
        label = dict(zip(group_cols, keys))
        g = add_features(g)
        if len(g) < cfg.min_obs:
            continue
        liquidity = (g["numtrades"].fillna(0) >= cfg.min_trades) & (g["volume"].fillna(0) >= cfg.min_volume)
        signals = signal_library(g)
        price = g["price"].astype(float)
        for signal_name, signal in signals.items():
            signal = pd.Series(signal, index=g.index).replace([np.inf, -np.inf], np.nan).fillna(0)
            if signal.abs().sum() < 10:
                continue
            for hp in holding_periods:
                future_ret = price.shift(-hp) / price - 1
                if instrument_type == "spread":
                    future_ret = price.shift(-hp) - price
                pos = signal.shift(1).fillna(0)
                pos = pos.where(liquidity, 0)
                trade_ret = pos * future_ret
                traded = pos != 0
                roundtrip_cost = (cfg.cost_bps + cfg.slippage_bps) / 10000.0
                if instrument_type == "spread":
                    # Spread returns are in price points; approximate cost by front price scale if present.
                    scale = g.get("front_price", price).astype(float).abs().replace(0, np.nan)
                    trade_ret = trade_ret - traded.astype(float) * roundtrip_cost * scale
                else:
                    trade_ret = trade_ret - traded.astype(float) * roundtrip_cost
                valid = trade_ret[traded & trade_ret.notna()]
                if len(valid) < 10:
                    continue
                mean = float(valid.mean())
                std = float(valid.std(ddof=1))
                hit = float((valid > 0).mean())
                tstat, pval = stats.ttest_1samp(valid, 0.0, nan_policy="omit")
                ci_lo, ci_hi = bootstrap_ci(valid.values, cfg.bootstrap_samples)
                train_mean, test_mean = walk_forward_score(valid)
                sharpe = float(mean / std * math.sqrt(252 / hp)) if std and np.isfinite(std) else np.nan
                eq = (1 + trade_ret.fillna(0)).cumprod() if instrument_type == "outright" else trade_ret.fillna(0).cumsum()
                mdd = max_drawdown(eq) if instrument_type == "outright" else float((eq - eq.cummax()).min())
                result = {
                    **label,
                    "instrument_type": instrument_type,
                    "pattern": signal_name,
                    "direction": "long" if valid.mean() >= 0 else "short_or_negative_edge",
                    "holding_days": hp,
                    "n_trades": int(len(valid)),
                    "mean_return": mean,
                    "median_return": float(valid.median()),
                    "hit_rate": hit,
                    "std_return": std,
                    "ann_sharpe": sharpe,
                    "t_stat": float(tstat) if np.isfinite(tstat) else np.nan,
                    "p_value": float(pval) if np.isfinite(pval) else 1.0,
                    "bootstrap_ci_low": ci_lo,
                    "bootstrap_ci_high": ci_hi,
                    "walk_train_mean": train_mean,
                    "walk_test_mean": test_mean,
                    "max_drawdown": mdd,
                    "cost_bps": cfg.cost_bps,
                    "slippage_bps": cfg.slippage_bps,
                    "min_volume": cfg.min_volume,
                    "min_trades": cfg.min_trades,
                }
                results.append(result)
                if len(eq_frames) < 250:
                    eq_frames.append(
                        pd.DataFrame(
                            {
                                "date": g["date"].values,
                                **label,
                                "instrument_type": instrument_type,
                                "pattern": signal_name,
                                "holding_days": hp,
                                "equity": eq.values,
                            }
                        )
                    )
                dd_rows.append(
                    {
                        **label,
                        "instrument_type": instrument_type,
                        "pattern": signal_name,
                        "holding_days": hp,
                        "max_drawdown": mdd,
                    }
                )
    res = pd.DataFrame(results)
    eq = pd.concat(eq_frames, ignore_index=True) if eq_frames else pd.DataFrame()
    dd = pd.DataFrame(dd_rows)
    return res, eq, dd


def classify_results(results: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if results.empty:
        return results, results, results
    out = results.copy()
    out["p_adj_bh"] = bh_fdr(out["p_value"])
    out["robust"] = (
        (out["n_trades"] >= 20)
        & (out["mean_return"] > 0)
        & (out["bootstrap_ci_low"] > 0)
        & (out["walk_test_mean"] > 0)
        & (out["p_adj_bh"] <= 0.10)
    )
    sort_cols = ["robust", "p_adj_bh", "mean_return", "ann_sharpe", "n_trades"]
    out = out.sort_values(sort_cols, ascending=[False, True, False, False, False])
    top = out[out["robust"]].copy().head(300)
    rejected = out[~out["robust"]].copy()
    return out, top, rejected


def equity_for_selected(panel: pd.DataFrame, selected: pd.DataFrame, instrument_type: str, cfg: RunConfig) -> pd.DataFrame:
    if panel.empty or selected.empty:
        return pd.DataFrame()
    rows = []
    group_cols = ["family", "series"] if instrument_type == "outright" else ["family", "spread"]
    work = panel.copy()
    if instrument_type == "spread":
        work["numtrades"] = cfg.min_trades
    for _, sel in selected[selected["instrument_type"] == instrument_type].iterrows():
        mask = pd.Series(True, index=work.index)
        for c in group_cols:
            mask &= work[c].astype(str) == str(sel[c])
        g = work.loc[mask].sort_values("date")
        if g.empty:
            continue
        g = add_features(g)
        signals = signal_library(g)
        signal = signals.get(sel["pattern"])
        if signal is None:
            continue
        hp = int(sel["holding_days"])
        price = g["price"].astype(float)
        future_ret = price.shift(-hp) / price - 1
        if instrument_type == "spread":
            future_ret = price.shift(-hp) - price
        liquidity = (g["numtrades"].fillna(0) >= cfg.min_trades) & (g["volume"].fillna(0) >= cfg.min_volume)
        pos = pd.Series(signal, index=g.index).replace([np.inf, -np.inf], np.nan).fillna(0).shift(1).fillna(0)
        pos = pos.where(liquidity, 0)
        traded = pos != 0
        roundtrip_cost = (cfg.cost_bps + cfg.slippage_bps) / 10000.0
        trade_ret = pos * future_ret
        if instrument_type == "spread":
            scale = g.get("front_price", price).astype(float).abs().replace(0, np.nan)
            trade_ret = trade_ret - traded.astype(float) * roundtrip_cost * scale
            equity = trade_ret.fillna(0).cumsum()
        else:
            trade_ret = trade_ret - traded.astype(float) * roundtrip_cost
            equity = (1 + trade_ret.fillna(0)).cumprod()
        frame = pd.DataFrame(
            {
                "date": g["date"].values,
                "family": sel["family"],
                "instrument_type": instrument_type,
                "pattern": sel["pattern"],
                "holding_days": hp,
                "equity": equity.values,
            }
        )
        if "series" in sel.index:
            frame["series"] = sel.get("series")
        if "spread" in sel.index:
            frame["spread"] = sel.get("spread")
        rows.append(frame)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def run_backtests(cont: pd.DataFrame, spreads: pd.DataFrame, cfg: RunConfig) -> pd.DataFrame:
    log("Testing pattern library without look-ahead")
    all_results = []
    all_eq = []
    all_dd = []
    if not cont.empty:
        outright = cont[cont["series"].isin(["front", "second"])].copy()
        res, eq, dd = evaluate_patterns(outright, "outright", cfg)
        all_results.append(res)
        all_eq.append(eq)
        all_dd.append(dd)
    if not spreads.empty:
        spr = spreads.copy()
        spr["numtrades"] = cfg.min_trades
        res, eq, dd = evaluate_patterns(spr, "spread", cfg)
        all_results.append(res)
        all_eq.append(eq)
        all_dd.append(dd)
    nonempty_results = [x for x in all_results if not x.empty]
    results = pd.concat(nonempty_results, ignore_index=True) if nonempty_results else pd.DataFrame()
    results, top, rejected = classify_results(results)
    results.to_csv(RESULTS / "full_results.csv", index=False)
    top.to_csv(RESULTS / "top_robust_patterns.csv", index=False)
    rejected.to_csv(RESULTS / "rejected_patterns.csv", index=False)
    nonempty_dd = [x for x in all_dd if not x.empty]
    top_eq = []
    if not cont.empty:
        top_eq.append(equity_for_selected(cont[cont["series"].isin(["front", "second"])].copy(), top, "outright", cfg))
    if not spreads.empty:
        top_eq.append(equity_for_selected(spreads.copy(), top, "spread", cfg))
    top_eq = [x for x in top_eq if not x.empty]
    eq_all = pd.concat(top_eq, ignore_index=True) if top_eq else pd.DataFrame()
    dd_all = pd.concat(nonempty_dd, ignore_index=True) if nonempty_dd else pd.DataFrame()
    eq_all.to_csv(RESULTS / "equity_curves.csv", index=False)
    dd_all.to_csv(RESULTS / "drawdowns.csv", index=False)
    make_figures(top, eq_all, cont, spreads)
    return results


def make_figures(top: pd.DataFrame, eq: pd.DataFrame, cont: pd.DataFrame, spreads: pd.DataFrame) -> None:
    try:
        import matplotlib.pyplot as plt

        if not cont.empty:
            for family in sorted(cont["family"].dropna().unique()):
                fig, ax = plt.subplots(figsize=(11, 5))
                for series in ["front", "second"]:
                    g = cont[(cont["family"] == family) & (cont["series"] == series)]
                    if not g.empty:
                        ax.plot(g["date"], g["price"], label=series)
                ax.set_title(f"{family}: continuous front/second")
                ax.legend()
                ax.grid(True, alpha=0.25)
                fig.tight_layout()
                fig.savefig(FIGURES / f"{family}_continuous.png", dpi=140)
                plt.close(fig)
        if not spreads.empty:
            for family in sorted(spreads["family"].dropna().unique()):
                fig, ax = plt.subplots(figsize=(11, 5))
                for spread in sorted(spreads["spread"].dropna().unique()):
                    g = spreads[(spreads["family"] == family) & (spreads["spread"] == spread)]
                    if not g.empty:
                        ax.plot(g["date"], g["price"], label=spread)
                ax.set_title(f"{family}: calendar spreads")
                ax.legend()
                ax.grid(True, alpha=0.25)
                fig.tight_layout()
                fig.savefig(FIGURES / f"{family}_spreads.png", dpi=140)
                plt.close(fig)
        if not top.empty and not eq.empty:
            candidates = top.head(8)
            for i, row in candidates.iterrows():
                mask = (
                    (eq["instrument_type"] == row["instrument_type"])
                    & (eq["pattern"] == row["pattern"])
                    & (eq["holding_days"] == row["holding_days"])
                )
                for c in ["family", "series", "spread"]:
                    if c in row and c in eq and pd.notna(row[c]):
                        mask &= eq[c] == row[c]
                g = eq.loc[mask].copy()
                if g.empty:
                    continue
                fig, ax = plt.subplots(figsize=(11, 4))
                ax.plot(pd.to_datetime(g["date"]), g["equity"])
                ax.set_title(f"{row['instrument_type']} {row.get('family','')} {row['pattern']} hp={row['holding_days']}")
                ax.grid(True, alpha=0.25)
                fig.tight_layout()
                safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"top_{i}_{row['pattern']}_{row['holding_days']}")
                fig.savefig(FIGURES / f"{safe[:130]}.png", dpi=140)
                plt.close(fig)
    except Exception as exc:  # noqa: BLE001
        log(f"Figure generation skipped: {type(exc).__name__}: {exc}")


def generate_report(cfg: RunConfig, history: pd.DataFrame, candles24: pd.DataFrame, candles60: pd.DataFrame, contract_summary: pd.DataFrame, cont: pd.DataFrame, spreads: pd.DataFrame, results: pd.DataFrame) -> None:
    top_path = RESULTS / "top_robust_patterns.csv"
    rejected_path = RESULTS / "rejected_patterns.csv"
    def read_csv_if_nonempty(path: Path) -> pd.DataFrame:
        try:
            return pd.read_csv(path) if path.exists() and path.stat().st_size > 0 else pd.DataFrame()
        except pd.errors.EmptyDataError:
            return pd.DataFrame()

    top = read_csv_if_nonempty(top_path)
    rejected = read_csv_if_nonempty(rejected_path)
    tbank = pd.read_csv(DATA_RAW / "tbank_token_check.csv") if (DATA_RAW / "tbank_token_check.csv").exists() else pd.DataFrame()
    sources = [
        "MOEX ISS securities: https://iss.moex.com/iss/engines/futures/markets/forts/securities",
        "MOEX ISS candles: https://iss.moex.com/iss/engines/futures/markets/forts/boards/RFUD/securities/{SECID}/candles.json",
        "MOEX ISS history fallback: https://iss.moex.com/iss/history/engines/futures/markets/forts/boards/RFUD/securities/{SECID}.json",
        "FRED Henry Hub: https://fred.stlouisfed.org/series/DHHNGSP",
        "FRED WTI: https://fred.stlouisfed.org/series/DCOILWTICO",
        "FRED Brent: https://fred.stlouisfed.org/series/DCOILBRENTEU",
        "EIA Weekly Natural Gas Storage Report: https://ir.eia.gov/ngs/ngs.html",
        "CBR USD/RUB XML: https://www.cbr.ru/scripts/XML_dynamic.asp",
        "T-Банк Invest API: использован только для проверки доступности токена, токен не сохранялся.",
    ]
    by_family = contract_summary.groupby("family").agg(
        contracts=("SECID", "nunique"),
        first_trade=("first_trade", "min"),
        last_trade=("last_trade", "max"),
        total_volume=("total_volume", "sum"),
    )
    robust_cols = [
        "family",
        "series",
        "spread",
        "instrument_type",
        "pattern",
        "holding_days",
        "n_trades",
        "mean_return",
        "ann_sharpe",
        "p_adj_bh",
        "bootstrap_ci_low",
        "bootstrap_ci_high",
        "walk_test_mean",
        "max_drawdown",
    ]
    robust_table = top[[c for c in robust_cols if c in top]].head(30) if not top.empty else pd.DataFrame()
    rejected_table = rejected[[c for c in ["family", "series", "spread", "instrument_type", "pattern", "holding_days", "n_trades", "mean_return", "p_adj_bh"] if c in rejected]].head(50) if not rejected.empty else pd.DataFrame()
    report = []
    report.append("# Research-проект MOEX Natural Gas futures NG/NGM\n")
    report.append(f"Дата запуска: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}. Период данных: {cfg.date_from} - {cfg.date_till}.\n")
    report.append("## Executive summary\n")
    report.append(f"- Найдено контрактов в MOEX history: {contract_summary['SECID'].nunique() if not contract_summary.empty else 0}.")
    report.append(f"- Дневных строк MOEX history: {len(history):,}. Дневных candle-строк: {len(candles24):,}. Часовых candle-строк: {len(candles60):,}.")
    report.append(f"- Continuous rows: {len(cont):,}. Calendar spread rows: {len(spreads):,}.")
    robust_total = int(results["robust"].sum()) if "robust" in results else len(top)
    report.append(
        f"- Проверено pattern/holding/instrument комбинаций: {len(results):,}. "
        f"Robust-кандидатов после BH-FDR и bootstrap/walk-forward: {robust_total:,}; "
        f"в top CSV экспортировано: {len(top):,}. Rejected: {len(rejected):,}."
    )
    if not tbank.empty:
        report.append(f"- T-Банк token check: {tbank.iloc[0]['status']} ({tbank.iloc[0]['source']}).")
    report.append("\n## Методология без look-ahead\n")
    report.append("- Сигналы строятся только на данных текущей или прошлой даты.")
    report.append("- Вход выполняется на следующий торговый день через `signal.shift(1)`.")
    report.append("- Доходность считается на горизонтах 1/2/3/5/10/20 торговых дней.")
    report.append("- Для outright используется процентная доходность, для календарных спредов - изменение спреда в пунктах.")
    report.append(f"- Transaction cost = {cfg.cost_bps} bps, slippage = {cfg.slippage_bps} bps.")
    report.append(f"- Минимальный liquidity filter: volume >= {cfg.min_volume}, trades >= {cfg.min_trades}.")
    report.append("- Walk-forward score считается на последовательных временных блоках; multiple testing корректируется Benjamini-Hochberg.")
    report.append("- Bootstrap CI строится по сделочным доходностям. Robust требует положительный mean, CI-low > 0, walk-forward test > 0 и BH q <= 10%.\n")
    report.append("## Покрытие контрактов\n")
    report.append(by_family.to_markdown() if not by_family.empty else "Нет данных.")
    report.append("\n## Top robust patterns\n")
    report.append(robust_table.to_markdown(index=False) if not robust_table.empty else "Строго robust-паттернов по заданным фильтрам не найдено. См. `results/full_results.csv` для ранжирования до отсечки.")
    report.append("\n## Rejected patterns sample\n")
    report.append(rejected_table.to_markdown(index=False) if not rejected_table.empty else "Нет rejected результатов.")
    report.append("\n## Артефакты\n")
    for p in [
        DATA_RAW / "moex_history_daily.csv",
        DATA_RAW / "moex_candles_24.csv",
        DATA_RAW / "moex_candles_60.csv",
        DATA_RAW / "moex_current_specs.csv",
        DATA_RAW / "external_daily.csv",
        DATA_PROCESSED / "contract_summary.csv",
        DATA_PROCESSED / "continuous_daily.csv",
        DATA_PROCESSED / "calendar_spreads.csv",
        RESULTS / "full_results.csv",
        RESULTS / "top_robust_patterns.csv",
        RESULTS / "rejected_patterns.csv",
        RESULTS / "equity_curves.csv",
        RESULTS / "drawdowns.csv",
    ]:
        report.append(f"- `{p.relative_to(ROOT)}`")
    report.append("\n## Источники\n")
    report.extend(f"- {s}" for s in sources)
    report.append("\n## Ограничения\n")
    report.append("- CME/NYMEX settlement history не включена как отдельный официальный ряд, если нет публичного стабильного источника без ключа/подписки. Внешний газовый фактор представлен Henry Hub spot FRED.")
    report.append("- Исторические спецификации MOEX восстановлены из ISS history/current securities и фактических first/last trade dates; полноценный архив всех изменений спецификаций может требовать отдельного архива биржевых документов.")
    report.append("- EIA storage берется из открытого WNGSR файла. Если EIA меняет структуру Excel, parser использует permissive fallback и сохраняет исходный результат в `data/raw/eia_storage_weekly.csv`.")
    (REPORTS / "final_report_ru.md").write_text("\n".join(report), encoding="utf-8")


def parse_args(argv: list[str]) -> RunConfig:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="date_from", default="2020-01-01")
    p.add_argument("--till", dest="date_till", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-hourly", dest="hourly", action="store_false")
    p.add_argument("--max-contracts", type=int, default=None)
    p.add_argument("--request-sleep", type=float, default=0.05)
    p.add_argument("--min-obs", type=int, default=80)
    p.add_argument("--min-trades", type=int, default=1)
    p.add_argument("--min-volume", type=float, default=1.0)
    p.add_argument("--cost-bps", type=float, default=3.0)
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--bootstrap-samples", type=int, default=400)
    args = p.parse_args(argv)
    return RunConfig(**vars(args))


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv or sys.argv[1:])
    ensure_dirs()
    log("Starting MOEX NG/NGM research pipeline")
    specs = download_current_specs(cfg)
    tbank = check_tbank_token()
    if not tbank.empty:
        log(f"T-Bank token check: {tbank.iloc[0]['status']} from {tbank.iloc[0]['source']}")
    history = download_moex_history(cfg)
    if history.empty:
        raise RuntimeError("MOEX history returned no rows")
    secids = sorted(history["SECID"].dropna().unique().tolist())
    candles24 = download_moex_candles(cfg, secids, 24)
    candles60 = download_moex_candles(cfg, secids, 60) if cfg.hourly else pd.DataFrame()
    external = download_external(cfg)
    contract_summary = build_contract_summary(history, specs)
    cont, spreads = build_continuous(history, contract_summary, external)
    results = run_backtests(cont, spreads, cfg)
    generate_report(cfg, history, candles24, candles60, contract_summary, cont, spreads, results)
    log("Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
