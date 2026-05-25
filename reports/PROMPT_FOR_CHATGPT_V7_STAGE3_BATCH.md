Use the uploaded V7 Stage 3 batch package.

This is one independent Stage 3 batch from completed V6 Stage 2.

Goal:
Run bounded deterministic Stage 3 local refinement only for the profiles in v7_selected_stage2_profiles.csv, then run Stage 4 stress and Stage 5 neighborhood for survivors from this batch.

Do not rerun Stage 1 or Stage 2.
Do not use any profiles outside v7_selected_stage2_profiles.csv.

Hard rules:
- No random search.
- No Sobol sampling.
- No Latin hypercube sampling.
- No stochastic candidate generation.
- No hidden top-N.
- No broad expansion back to all V6 survivors.
- If any stage is incomplete, explicitly say: No live-paper recommendation can be made from this run.

Files:
- v7_selected_stage2_profiles.csv
- v7_batch_metadata.json
- v6_top15_1m_candles.csv
- v6_top15_instrument_specs.csv
- v6_top15_row_counts.csv

Stage 0 audit:
1. Load all files.
2. Confirm selected profile count from v7_batch_metadata.json.
3. Confirm selected tickers/families from v7_batch_metadata.json.
4. Confirm row counts match candles.
5. Save v7_stage_counts.csv and v7_run_manifest.json.

Cost model:
For every trade:
- notional_rub = close / tick * tick_rub
- round_turn_fee_rub = max(2 * notional_rub * 0.00025, 1 * tick_rub)
- round_turn_fee_ticks = round_turn_fee_rub / tick_rub
- total_cost_ticks = round_turn_fee_ticks + slippage_ticks

Execution:
- No look-ahead.
- Signals use only closed candles before entry.
- Pessimistic intrabar execution.
- If both favorable move and stop can occur in one candle, assume worse event first unless trailing was already active before that candle.

Stage 3: bounded deterministic local refinement
For every selected Stage 2 profile:
- Keep ticker/family/signal_family/direction/entry_timing/session_filter fixed.
- Expand only active signal parameters by +/-1 declared grid index where available.
- Expand exit stop/trail/activation by +/-1 and +/-2 declared grid index where available.
- Expand cooldown_minutes by +/-1 declared grid index where available.
- Expand max_hold_minutes by +/-1 declared grid index where available.
- Do not expand inactive parameter families.

Declared grids are the same as V6/V7:
- momentum_pct: 0.0002,0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02
- momentum_ticks: 1,2,3,5,8,13,21,34,55,89,144
- breakout_lookback: 3,5,8,13,21,34,55,89,144
- volume_multiplier: 0,0.5,0.8,1.0,1.2,1.5,2.0,2.5,3.0,4.0
- volume_window: 20,40,60,120
- vwap_mode: disabled,rolling20,rolling60,session
- vwap_buffer_pct: 0,0.0001,0.0002,0.0005,0.001,0.002
- direct stop_ticks: 1,2,3,5,8,13,21,34,55,89,144,233,377
- direct trail_ticks: 1,2,3,5,8,13,21,34,55,89,144
- direct activation_ticks: 5,8,13,21,34,55,89,144,233,377
- stop_pct: 0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02
- trail_pct: 0.0002,0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01
- activation_pct: 0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02
- cooldown_minutes: 0,1,3,5,10,20,40,60
- max_hold_minutes: 5,10,15,30,60,90,120,180,240

Stage 3 survivor rule:
- test_trades >= 40
- test_net_2t > 0
- test_pf_2t >= 1.10
- remove_best_3_net_2t > 0
- best_trade_share_2t <= 0.30
- train_net_2t > 0

Stage 4 stress validation:
For every Stage 3 survivor:
- train/test/full
- slippage 0,1,2,3,4,5 ticks
- outlier removal best 1,3,5
- rolling walk-forward: 60d/20d, 90d/30d, expanding/next-month
- time-of-day breakdown
- direction breakdown
- fee/stop classification
- spread/stop proxy classification
- candle microstructure classification

Stage 5 neighborhood:
For every Stage 4 candidate:
- Evaluate all +/-1 and +/-2 grid-index neighbors for active parameters.
- Report neighborhood_expected_count, neighborhood_evaluated_count, neighborhood_profitable_2t_pct, neighborhood_median_net_2t, neighborhood_worst_net_2t.
- Mark PARAMETER_SPIKE if neighborhood_profitable_2t_pct < 50%.

Final LIVE_NOW rules:
- Stage 3 completed.
- Stage 4 completed.
- Stage 5 completed.
- test_trades >= 40
- test_net_2t > 0
- test_pf_2t >= 1.10
- remove_best_3_net_2t > 0
- rolling profitable windows at 2T >= 60%
- neighborhood_profitable_2t_pct >= 50%
- no single trade dominates profit
- not LOW_SAMPLE
- not PARAMETER_SPIKE
- not REJECT_OVERFIT
- not REJECT_FRAGILE

Required output:
- one archive v7_results.zip
- include all CSV/JSON/MD outputs inside it.

Final response:
Give only:
1. batch name,
2. stage completion status,
3. exact evaluated counts by stage,
4. LIVE_NOW candidates if any, else NONE,
5. one archive v7_results.zip.
