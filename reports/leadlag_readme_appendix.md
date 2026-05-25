# MOEX NG Lead-Lag 10m

Run period: `2024-05-23` to `2026-05-23`. Source: MOEX ISS candles endpoint
`https://iss.moex.com/iss/engines/futures/markets/forts/securities/{SECID}/candles.json` with `interval=10`.

## Methodology
- Rolling target month is each calendar month from 2024-05 to 2026-05.
- Predictors are target+1, target+2 and target+3 monthly NG contracts.
- Candles are aligned only by exact `begin` timestamp; the main test does not forward-fill.
- Filters require `volume > 0` for target and all predictors, remove discontinuous 10-minute jumps, and exclude the configured final trading days before the target contract's last observed candle.
- No future predictor values are used. Trading simulation uses signal at `close[t]`, entry at `open[t+1]`, exit at `open[t+3]` for a 20-minute hold.

## Required conclusions
- Statistically significant 20m lead: `True`.
- Effect survives out-of-sample walk-forward: `True`.
- Effect concentrated in one month: `False`.
- Effect passes spread/slippage after `6` bps roundtrip: `False`.
- Illiquid contracts: `none with zero nonzero-volume rows`.
- Low-liquidity contracts: `none under threshold`.

## Artifacts
- `data/raw/leadlag_ng_10m/moex_ng_10m_candles.csv` and `.parquet`
- `data/processed/leadlag_ng_10m/rolling_mapping.csv`
- `data/processed/leadlag_ng_10m/aligned_panel.csv`
- `data/processed/leadlag_ng_10m/features.csv`
- `reports/leadlag_correlations.csv`
- `reports/regression_summary.csv`
- `reports/walkforward_results.csv`
- `reports/trade_simulation.csv`
- `plots/lag_correlation_heatmap.png`

## Run
```powershell
python -m pip install -r requirements.txt
python src/leadlag_ng_moex.py --force
```