You are given one or more ZIP files with MOEX TQBR stock data from T-Bank.

Task: test whether the short-term futures idea can be adapted to liquid MOEX stocks.

Use all uploaded ZIP parts. First unzip, load every CSV/parquet, and print exact row counts by ticker.

Data:
- tbank_1m_top_stocks.csv: 1-minute candles for 2026.
- stock_specs.csv: ticker, lot, tick, tick_rub_per_lot, short_enabled.
- stock_liquidity_rank.csv: recent turnover/liquidity ranking.
- current_orderbook_snapshot.csv: current spread/top book snapshot.
- microstructure/*.csv: live-collected orderbook and tape context, only from 2026-05-21 onward.

Important microstructure rule:
Do not use live microstructure data from 2026-05-21 onward as if it existed before that date. Use it only for execution filters, spread/liquidity classes, and live-paper sizing policy.

Core strategy idea:
- Short-term directional entry.
- Immediate protective stop.
- Favorable move activates trailing stop.
- Exit by trailing stop, protective stop, or max hold.
- Compare two exit models:
  1. soft_gpt_model: trailing activation and candle-like pessimistic execution as in the futures GPT backtests.
  2. hard_tick_model: stop follows every favorable tick after activation; if stop-limit is not filled, emergency market exit is assumed with extra slippage.

Costs:
- side_commission_rate = 0.00025 unless another exact tariff is supplied.
- round_turn_commission = 2 * side_commission_rate * traded_notional_rub.
- traded_notional_rub = price * lot * quantity_lots.
- Evaluate slippage stress: 0, 1, 2, 3, 4, 5 ticks round trip.
- Include spread cost stress using bid/ask proxy and current/live microstructure where available.

Sizing:
- Stocks have no futures GO. Use capital and ruble stop risk.
- For per-profile statistics assume 1 lot.
- Also report portfolio simulations with max full stop risk per ticker: 500, 1000, 2000, 4000 RUB.
- Position quantity must be reduced by ruble stop risk, not by narrowing the stop.

Search:
Use deterministic staged search. Do not use random sampling.

Stage 0: audit
- loaded tickers, row counts, start/end dates
- short_enabled split
- tick/lot/spread/turnover stats

Stage 1: broad deterministic coarse grid for every ticker
Signal families:
- momentum_breakout
- vwap_impulse
- range_expansion
- trend_pullback
- pure_trailing_after_impulse
- opening_range_impulse
- liquidity_filtered_momentum

Directions:
- long
- short only if short_enabled=true
- both only if short_enabled=true

Parameters:
- momentum_pct: 0.0002,0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01
- momentum_ticks: 1,2,3,5,8,13,21,34,55,89
- breakout_lookback: 3,5,8,13,21,34,55,89,144
- trend_fast: 3,5,8,13,21
- trend_slow: 8,13,21,34,55,89,144, only trend_slow > trend_fast
- volume_multiplier: 0,0.5,0.8,1.0,1.2,1.5,2.0,2.5,3.0,4.0
- volume_window: 20,40,60,120
- vwap_mode: disabled, rolling20, rolling60, session
- vwap_buffer_pct: 0,0.0001,0.0002,0.0005,0.001,0.002
- stop_pct: 0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02
- trail_pct: 0.0002,0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01
- trail_activation_pct: 0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02
- direct_stop_ticks: 1,2,3,5,8,13,21,34,55,89,144,233
- direct_trail_ticks: 1,2,3,5,8,13,21,34,55,89,144
- direct_activation_ticks: 1,2,3,5,8,13,21,34,55,89,144
- cooldown_minutes: 0,1,3,5,10,20,40,60
- max_hold_minutes: 3,5,10,15,30,60,90,120
- session_filter: main_session, exclude_first_last_10min, morning_only, afternoon_only, evening_if_data_exists

Stage 2: fine expansion
For every survivor, expand all adjacent declared parameter values. Do not silently top-N prune. If needed, split survivors into multiple result groups by ticker liquidity rank and profile family.

Stage 3: stress validation
- train first 70%, test last 30%, full
- rolling windows: 60d/20d, 90d/30d, expanding/next month
- slippage 0..5 ticks
- remove best 1/3/5 trades
- drawdown, losing streaks
- time-of-day breakdown
- long/short breakdown
- microstructure spread/liquidity classes

Stage 4: neighborhood validation
For final candidates, run deterministic neighbors within +/-1 and +/-2 grid indices. Mark PARAMETER_SPIKE if neighborhood profitable rate at 2T < 50%.

Ranking:
Do not rank by single highest historical profit. Prefer:
- profitable on test at 2T after commission
- survives 3T stress or degrades mildly
- remove_best_3 still positive
- stable rolling windows
- stable neighborhood
- full stop rub not too large relative to median winning trade
- high enough trade count
- low spread/stop and commission/stop pressure

Required output: put all results into one ZIP.
Inside the ZIP include:
- stock_stage0_audit.csv
- stock_grid_results.csv
- stock_top_profiles_by_ticker.csv
- stock_stress_summary.csv
- stock_final_live_paper_profiles.json
- stock_rejected_tickers.csv
- stock_slippage_stress.csv
- stock_rolling_walkforward.csv
- stock_neighborhood_summary.csv
- stock_time_of_day_breakdown.csv
- stock_direction_breakdown.csv
- stock_ruble_stop_risk_summary.csv
- stock_microstructure_policy.csv
- stock_portfolio_simulation.csv
- stock_project_diagnostics.json
- README_RESULT.md

Final answer in Russian:
1. Say whether results are enough for live-paper.
2. Show LIVE_NOW candidates.
3. Show WATCHLIST candidates.
4. Show rejected candidates by reason.
5. Show expected trades/day and commission/day.
6. Show per-ticker recommended quantity for stop-risk 500/1000/2000/4000 RUB.
7. Explicitly compare soft_gpt_model vs hard_tick_model.
