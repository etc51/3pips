from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

import pandas as pd


DEFAULT_SIDECAR = Path("reports/futures_scalp_profiles_v7_paper_20260525_gpt_shadow_params.csv")
DEFAULT_OUTPUT_DIR = Path("reports/paper_runs/v7_live_20260525/analysis/v7_exact_gpt")


def parse_list(value: str | None) -> set[str] | None:
    if not value:
        return None
    items = {item.strip() for item in value.split(",") if item.strip()}
    return items or None


def resolve_source(path_value: str, root: Path) -> Path:
    path = Path(str(path_value))
    if not path.is_absolute():
        path = root / path
    return path


def select_rows(df: pd.DataFrame, families: set[str] | None, tickers: set[str] | None) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    if families is not None:
        if "family" in df.columns:
            mask &= df["family"].astype(str).isin(families)
        else:
            inferred = df["ticker"].astype(str).str.extract(r"^([A-Za-z]+)", expand=False)
            mask &= inferred.isin(families)
    if tickers is not None:
        mask &= df["ticker"].astype(str).isin(tickers)
    return df.loc[mask].copy()


def sum_numeric(series: pd.Series) -> float:
    return float(pd.to_numeric(series, errors="coerce").fillna(0).sum())


def mean_numeric(series: pd.Series) -> float:
    values = pd.to_numeric(series, errors="coerce").dropna()
    return float(values.mean()) if len(values) else 0.0


def safe_int(value: float) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def aggregate(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for keys, group in df.groupby(group_cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        row = {col: key for col, key in zip(group_cols, keys)}
        row.update(
            {
                "profiles": len(group),
                "test_trades": safe_int(sum_numeric(group["test_trades"])),
                "train_trades": safe_int(sum_numeric(group["train_trades"])),
                "full_trades": safe_int(sum_numeric(group["full_trades"])),
                "test_net_2t": round(sum_numeric(group["test_net_2t"]), 4),
                "train_net_2t": round(sum_numeric(group["train_net_2t"]), 4),
                "full_net_2t": round(sum_numeric(group["full_net_2t"]), 4),
                "test_pf_2t_mean": round(mean_numeric(group["test_pf_2t"]), 4),
                "test_avg_2t_mean": round(mean_numeric(group["test_avg_2t"]), 4),
                "remove_best_3_net_2t_sum": round(sum_numeric(group["remove_best_3_net_2t"]), 4),
                "exact_matches": int((group["match_quality"] == "exact_exit_same_signal").sum())
                if "match_quality" in group.columns
                else 0,
                "closest_matches": int((group["match_quality"] == "closest_stage2_profile").sum())
                if "match_quality" in group.columns
                else 0,
            }
        )
        rows.append(row)
    out = pd.DataFrame(rows)
    if len(out):
        out = out.sort_values(["test_net_2t", "test_trades"], ascending=[False, False])
    return out


def load_exact_rows(sidecar: pd.DataFrame, root: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    source_cache: dict[Path, pd.DataFrame] = {}
    exact_rows: list[dict[str, object]] = []
    missing_rows: list[dict[str, object]] = []

    for _, sidecar_row in sidecar.iterrows():
        source_path = resolve_source(str(sidecar_row["source_file"]), root)
        stage2_id = str(sidecar_row["source_stage2_id"])
        if source_path not in source_cache:
            source_cache[source_path] = pd.read_csv(source_path)
        source = source_cache[source_path]
        match = source[source["stage2_id"].astype(str) == stage2_id]
        if match.empty:
            missing_rows.append(
                {
                    "ticker": sidecar_row.get("ticker"),
                    "source_stage2_id": stage2_id,
                    "source_file": str(source_path),
                    "reason": "stage2_id_not_found",
                }
            )
            continue

        source_dict = match.iloc[0].to_dict()
        for key in [
            "match_quality",
            "score",
            "source_batch",
            "source_file",
            "source_stop_ticks",
            "source_trail_ticks",
            "source_activation_ticks",
            "source_test_net_2t",
        ]:
            source_dict[key] = sidecar_row.get(key)
        source_dict["resolved_source_file"] = str(source_path)
        exact_rows.append(source_dict)

    return pd.DataFrame(exact_rows), pd.DataFrame(missing_rows)


def write_outputs(
    exact: pd.DataFrame,
    missing: pd.DataFrame,
    output_dir: Path,
    families: set[str] | None,
    tickers: set[str] | None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    exact.to_csv(output_dir / "v7_gpt_exact_source_profiles.csv", index=False, encoding="utf-8-sig")
    missing.to_csv(output_dir / "v7_gpt_exact_source_missing.csv", index=False, encoding="utf-8-sig")

    by_family = aggregate(exact, ["family"]) if "family" in exact.columns else pd.DataFrame()
    by_ticker = aggregate(exact, ["family", "ticker"]) if "family" in exact.columns else aggregate(exact, ["ticker"])
    by_quality = aggregate(exact, ["match_quality"]) if "match_quality" in exact.columns else pd.DataFrame()

    by_family.to_csv(output_dir / "v7_gpt_exact_source_summary_by_family.csv", index=False, encoding="utf-8-sig")
    by_ticker.to_csv(output_dir / "v7_gpt_exact_source_summary_by_ticker.csv", index=False, encoding="utf-8-sig")
    by_quality.to_csv(output_dir / "v7_gpt_exact_source_match_quality.csv", index=False, encoding="utf-8-sig")

    exit_cols = [
        "ticker",
        "family",
        "stage2_id",
        "match_quality",
        "signal_family",
        "direction",
        "entry_timing",
        "session_filter",
        "exit_mode",
        "exit_stop_value",
        "exit_trail_value",
        "exit_activation_value",
        "stop_ticks",
        "trail_ticks",
        "activation_ticks",
        "stop_pct",
        "trail_pct",
        "activation_pct",
        "avg_stop_ticks",
        "avg_trail_ticks",
        "avg_activation_ticks",
        "source_stop_ticks",
        "source_trail_ticks",
        "source_activation_ticks",
        "test_net_2t",
        "test_trades",
        "test_pf_2t",
        "source_file",
    ]
    existing_exit_cols = [col for col in exit_cols if col in exact.columns]
    exact[existing_exit_cols].to_csv(
        output_dir / "v7_gpt_exact_source_exit_params.csv", index=False, encoding="utf-8-sig"
    )

    manifest = {
        "profiles": int(len(exact)),
        "missing_profiles": int(len(missing)),
        "families_filter": sorted(families) if families else None,
        "tickers_filter": sorted(tickers) if tickers else None,
        "test_net_2t": round(sum_numeric(exact["test_net_2t"]), 4) if len(exact) else 0.0,
        "train_net_2t": round(sum_numeric(exact["train_net_2t"]), 4) if len(exact) else 0.0,
        "full_net_2t": round(sum_numeric(exact["full_net_2t"]), 4) if len(exact) else 0.0,
        "test_trades": safe_int(sum_numeric(exact["test_trades"])) if len(exact) else 0,
        "train_trades": safe_int(sum_numeric(exact["train_trades"])) if len(exact) else 0,
    }
    (output_dir / "v7_gpt_exact_source_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract exact V7/GPT source Stage2 profile metrics for selected live-paper profiles."
    )
    parser.add_argument("--sidecar", type=Path, default=DEFAULT_SIDECAR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--families", help="Comma-separated family filter, e.g. GL,BT,PD")
    parser.add_argument("--tickers", help="Comma-separated ticker filter, e.g. GLZ6,BTN6")
    args = parser.parse_args(list(argv) if argv is not None else None)

    root = Path.cwd()
    sidecar = pd.read_csv(args.sidecar)
    exact, missing = load_exact_rows(sidecar, root)

    families = parse_list(args.families)
    tickers = parse_list(args.tickers)
    exact = select_rows(exact, families, tickers)

    write_outputs(exact, missing, args.output_dir, families, tickers)

    print(
        json.dumps(
            {
                "profiles": int(len(exact)),
                "missing": int(len(missing)),
                "test_net_2t": round(sum_numeric(exact["test_net_2t"]), 4) if len(exact) else 0.0,
                "test_trades": safe_int(sum_numeric(exact["test_trades"])) if len(exact) else 0,
                "output_dir": str(args.output_dir),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
