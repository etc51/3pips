You are given a ZIP with MOEX futures 1-minute candle data from T-Bank API.

Goal:
Run a broad first-pass search over ALL included futures without pre-filtering for liquidity. The objective is to find any futures where the short-term momentum + trailing-stop idea has a real candidate edge.

Files:
- tbank_1m_all_futures.csv: candles with secid,time,open,high,low,close,volume,is_complete.
- instrument_specs.csv: tick size, tick value in RUB, margin, expiration, API flags.
- row_counts.csv: rows per ticker.

Core idea to test:
- Enter long/short after short-term directional movement.
- Immediately place a stop.
- If price moves in favor, activate trailing stop.
- Exit by trailing stop or max hold.
- Everything must be after commission and slippage.
- Parameters may be expressed in percent of price and converted to ticks, or directly in ticks. Test both.

Important:
Do NOT discard a ticker only because volume is low. Low-liquidity futures may still be useful with 1 contract. But report liquidity/microstructure risk separately.

First-pass universe search:
Run a very large coarse grid across every ticker:
- signal families:
  - momentum_breakout
  - vwap_impulse
  - range_expansion
  - trend_pullback
  - pure_trailing_after_impulse
- direction: long, short, both
- momentum_pct: 0.0002,0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01
- momentum_ticks: 1,2,3,5,8,13,21,34,55,89
- breakout_lookback: 3,5,8,13,21,34,55,89,144
- trend_fast: 3,5,8,13,21
- trend_slow: 8,13,21,34,55,89
- volume_multiplier: 0,0.5,0.8,1.0,1.2,1.5,2.0,2.5,3.0,4.0
- volume_window: 20,40,60,120
- vwap_mode: disabled, rolling20, rolling60, session
- vwap_buffer_pct: 0,0.0001,0.0002,0.0005,0.001
- stop_pct: 0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02
- trail_pct: 0.0002,0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008
- trail_activation_pct: 0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015
- min_stop_ticks: 1,2,3,5,8,13,21,34,55,89
- min_trail_ticks: 1,2,3,5,8,13,21,34,55
- cooldown_minutes: 0,1,3,5,10,20
- max_hold_minutes: 5,10,15,30,60,90,120,180
- entry timing:
  - next_bar_open
  - signal_bar_close
  - adverse_1tick
- session filters:
  - all available data
  - main hours only
  - weekend separately if identifiable from timestamps
  - exclude first/last 10 minutes of day

Costs:
- Use tick_rub from instrument_specs.
- If exact broker commission is unknown, use conservative commission estimate:
  round_turn_fee_rub = max(2 * notional_rub * 0.00025, 1 tick_rub)
  where notional_rub = close / tick * tick_rub.
- Evaluate slippage 0,1,2,3,4,5 ticks round trip.
- Main robustness must survive at least 2T. 3T-5T are stress levels.

Split:
- train = first 70%
- test = last 30%
- full sample
- rolling walk-forward:
  - 60d train / 20d test
  - 90d train / 30d test
  - expanding train / next month test

Selection:
Do NOT optimize for highest historical PnL.
Primary target is robust live-paper candidates:
- test_trades >= 40, unless ticker has fewer than 5000 rows; then allow but label LOW_SAMPLE.
- test_net_2t > 0.
- test_profit_factor_2t >= 1.10.
- remove-best-1/3/5 trades must not destroy all profit.
- rolling active profitable windows at 2T >= 60% for KEEP.
- neighborhood stability:
  For top candidates, test at least 1000 nearby variants around the selected parameters.
  KEEP only if neighborhood_profitable_2t_pct >= 50%, unless explicitly labeled speculative.

Labels:
- KEEP_FOR_LIVE_PAPER
- WATCHLIST_SPECULATIVE
- REJECT_OVERFIT
- REJECT_FRAGILE
- LOW_SAMPLE
- NO_EDGE

Outputs to create:
- all_futures_grid_results.csv
- all_futures_top_profiles_by_ticker.csv
- all_futures_stress_summary.csv
- all_futures_final_live_paper_profiles.json
- all_futures_rolling_walkforward.csv
- all_futures_slippage_stress.csv
- all_futures_neighborhood_summary.csv
- all_futures_time_of_day_breakdown.csv
- all_futures_direction_breakdown.csv
- all_futures_rejected_tickers.csv

Final answer:
1. Count tickers and rows actually loaded.
2. Show all KEEP_FOR_LIVE_PAPER tickers.
3. Show WATCHLIST_SPECULATIVE tickers separately.
4. Explain rejected tickers by reason.
5. Recommend a live-paper portfolio split into:
   - strong contour
   - weak/speculative contour
6. Do not recommend rejected profiles as live candidates.
