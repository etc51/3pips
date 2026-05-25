Use the uploaded V7 package.

This is a fresh-window V7 run based on completed V6 Stage 2.

Goal:
Take the most profitable V6 Stage 2 survivor profiles and complete bounded Stage 3, Stage 4 stress, and Stage 5 neighborhood validation.

Do not rerun Stage 1 or Stage 2.
Use v7_selected_stage2_profiles.csv as the fixed input.

Selection logic already applied:
- source: completed V6 Stage 2 survivors
- profit-first ranking by test_net_2t
- guardrails:
  - remove_best_3_net_2t > 0
  - best_trade_share_2t <= 0.20
  - train_net_2t > 0
  - test_pf_2t >= 1.15
  - test_trades >= 40
- max 3 profiles per ticker
- max 80 classic profiles
- max 20 neo/perp profiles

Selected input:
- 89 Stage 2 profiles
- 32 tickers
- 14 families
- classic profiles: 80
- neo/perp profiles: 9

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
- v7_selected_summary_by_family.csv
- v7_metadata.json
- v6_top15_1m_candles.csv
- v6_top15_instrument_specs.csv
- v6_top15_row_counts.csv
- v6_selected_top15_families.csv
- v6_metadata.json

Stage 0: audit
1. Load all files.
2. Confirm 89 selected profiles.
3. Confirm 32 tickers and 14 families.
4. Confirm row counts match candles.
5. Save v7_run_manifest.json and v7_stage_counts.csv.

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
- Do not expand to all V6 Stage 2 survivors.

Declared grids:
momentum_pct:
- 0.0002,0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02

momentum_ticks:
- 1,2,3,5,8,13,21,34,55,89,144

breakout_lookback:
- 3,5,8,13,21,34,55,89,144

trend pairs:
- 3/8,3/13,3/21,3/34,3/55,3/89,3/144
- 5/8,5/13,5/21,5/34,5/55,5/89,5/144
- 8/13,8/21,8/34,8/55,8/89,8/144
- 13/21,13/34,13/55,13/89,13/144
- 21/34,21/55,21/89,21/144

volume_multiplier:
- 0,0.5,0.8,1.0,1.2,1.5,2.0,2.5,3.0,4.0

volume_window:
- 20,40,60,120

vwap_mode:
- disabled, rolling20, rolling60, session

vwap_buffer_pct:
- 0,0.0001,0.0002,0.0005,0.001,0.002

direct exit grids:
- stop_ticks: 1,2,3,5,8,13,21,34,55,89,144,233,377
- trail_ticks: 1,2,3,5,8,13,21,34,55,89,144
- activation_ticks: 5,8,13,21,34,55,89,144,233,377

percent exit grids:
- stop_pct: 0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02
- trail_pct: 0.0002,0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01
- activation_pct: 0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02

cooldown_minutes:
- 0,1,3,5,10,20,40,60

max_hold_minutes:
- 5,10,15,30,60,90,120,180,240

Stage 3 survivor rule:
- test_trades >= 40
- test_net_2t > 0
- test_pf_2t >= 1.10
- remove_best_3_net_2t > 0
- best_trade_share_2t <= 0.30
- train_net_2t > 0

Save:
- v7_stage3_refinement.csv
- v7_stage3_survivors.csv
- v7_stage_counts.csv

Stage 4: full stress validation
For every Stage 3 survivor:
- train/test/full
- slippage 0,1,2,3,4,5 ticks
- outlier removal best 1,3,5
- rolling walk-forward:
  - 60d train / 20d test
  - 90d train / 30d test
  - expanding train / next month test
- time-of-day breakdown
- direction breakdown
- fee/stop classification
- spread/stop proxy classification
- candle microstructure classification:
  - zero_volume_share
  - median_1m_volume
  - p10_1m_volume
  - active_minutes
  - active_days

Save:
- v7_stress_summary.csv
- v7_slippage_stress.csv
- v7_rolling_walkforward.csv
- v7_time_of_day_breakdown.csv
- v7_direction_breakdown.csv
- v7_outlier_removal.csv
- v7_fee_stop_viability.csv
- v7_microstructure_classification.csv

Stage 5: deterministic neighborhood validation
For every Stage 4 candidate:
- Evaluate all +/-1 and +/-2 grid-index neighbors for active parameters.
- Report neighborhood_expected_count.
- Report neighborhood_evaluated_count.
- Report neighborhood_profitable_2t_pct.
- Report neighborhood_median_net_2t.
- Report neighborhood_worst_net_2t.
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

Save final files:
- v7_final_live_paper_profiles.json
- v7_rejected_tickers.csv
- v7_results.zip

Final response:
Give only:
1. stage completion status,
2. exact evaluated counts by stage,
3. LIVE_NOW candidates if any, else NONE,
4. one archive v7_results.zip containing all CSV/JSON/MD outputs.
