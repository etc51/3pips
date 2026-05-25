Use the uploaded ZIP files with MOEX/T-Bank futures 1-minute candles and instrument specs.

This is a fresh-window V4 deterministic staged backtest. Do not use any conclusions from previous chat context or previous V2/V3 results.

Goal:
Find robust short-term futures profiles for the momentum + trailing-stop strategy, but only through an auditable deterministic staged process. Do not claim live candidates unless all required stages for those candidates are completed.

Files to load:
- cloud_all_futures_grid_package_part_01.zip
- cloud_all_futures_grid_package_part_02.zip
- cloud_all_futures_grid_package_part_03.zip
- instrument_specs.csv
- row_counts.csv
- split_manifest.csv

Hard rules:
- No random search.
- No randomized sampling.
- No Sobol sampling.
- No Latin hypercube sampling.
- No stochastic candidate generation.
- No silent top-N pruning.
- No replacing required stages with summaries.
- No live recommendation unless fine grid, stress, rolling, neighborhood, direction, and time-of-day validation are completed for that exact profile.

First action:
1. Unzip all files.
2. Load all candle CSVs.
3. Load instrument_specs.csv.
4. Load row_counts.csv if present.
5. Confirm every ZIP part was loaded.
6. Print and save row counts per secid.
7. Confirm every specs ticker is accounted for.
8. Save v4_universe_coverage.csv.

Universe buckets:
- classic_moex_expiring_to_2026_12_31
- classic_moex_after_2026_12_31
- neo_or_perp
- bad_or_missing_data

Run mode:
The literal full Cartesian grid is expected to be infeasible. You must still calculate and save full-grid cardinality first, but do not attempt to materialize the full raw manifest if it is too large for memory.

Required audit files before strategy evaluation:
- v4_run_manifest.json
- v4_parameter_values.json
- v4_combo_cardinality.csv
- v4_stage_counts.csv

Every evaluated combination must have:
combo_id = sha256(ticker + universe_bucket + profile_width_family + sizing_family + signal_family + direction + entry_timing + session_filter + all parameter names and values in canonical sorted order)

Strategy families:
- momentum_breakout
- vwap_impulse
- range_expansion
- trend_pullback
- pure_trailing_after_impulse

Profile width:
- tight
- wide

Sizing:
- percent_based
- tick_based_direct

Direction:
- long
- short
- both

Entry timing:
- next_bar_open
- signal_bar_close
- adverse_1tick
- adverse_half_spread_proxy

Session filters:
- all_available_data
- main_session_only_if_identifiable
- exclude_first_last_10_minutes

Full parameter universe:
momentum_pct:
- 0.0002,0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02

momentum_ticks:
- 1,2,3,5,8,13,21,34,55,89,144

breakout_lookback:
- 3,5,8,13,21,34,55,89,144

trend_fast:
- 3,5,8,13,21

trend_slow:
- 8,13,21,34,55,89,144

Only valid trend pairs where trend_slow > trend_fast.

volume_multiplier:
- 0,0.5,0.8,1.0,1.2,1.5,2.0,2.5,3.0,4.0

volume_window:
- 20,40,60,120

vwap_mode:
- disabled
- rolling20
- rolling60
- session

vwap_buffer_pct:
- 0,0.0001,0.0002,0.0005,0.001,0.002

percent stop/trail:
- stop_pct: 0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02,0.03,0.05
- trail_pct: 0.0002,0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02
- trail_activation_pct: 0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02,0.03

tick/direct:
- direct_stop_ticks: 1,2,3,5,8,13,21,34,55,89,144,233,377,610
- direct_trail_ticks: 1,2,3,5,8,13,21,34,55,89,144,233
- direct_activation_ticks: 5,8,13,21,34,55,89,144,233,377,610

minimum tick constraints:
- min_stop_ticks: 1,2,3,5,8,13,21,34,55,89,144,233,377,610
- min_trail_ticks: 1,2,3,5,8,13,21,34,55,89,144,233

wide extra values:
- wide_stop_ticks: 21,34,55,89,144,233,377,610
- wide_stop_pct: 0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02,0.03,0.05
- wide_trail_ticks: 8,13,21,34,55,89,144,233
- wide_activation_ticks: 13,21,34,55,89,144,233,377
- wide_max_hold_minutes: 15,30,60,90,120,180,240
- wide_cooldown_minutes: 3,5,10,20,40,60

cooldown_minutes:
- 0,1,3,5,10,20,40,60

max_hold_minutes:
- 5,10,15,30,60,90,120,180,240

Cost model:
For every trade:
- notional_rub = close / tick * tick_rub
- round_turn_fee_rub = max(2 * notional_rub * 0.00025, 1 * tick_rub)
- round_turn_fee_ticks = round_turn_fee_rub / tick_rub
- total_cost_ticks = round_turn_fee_ticks + slippage_ticks

Evaluate slippage:
- 0,1,2,3,4,5 ticks round trip

Execution:
- No look-ahead.
- Signals use only closed candles before entry.
- Pessimistic intrabar execution.
- If both favorable movement and stop can occur in the same candle, assume the worse event happens first unless trailing was already active before that candle.

Fee/stop classification:
- fee_to_stop_ratio = round_turn_fee_ticks / stop_ticks
- FEE_EXCELLENT <= 0.25
- FEE_OK > 0.25 and <= 0.55
- FEE_HEAVY > 0.55 and <= 1.00
- FEE_DOMINATES > 1.00

Do not delete FEE_HEAVY or FEE_DOMINATES early. Record them and let final classification decide.

Stage 0: universe and cardinality audit
Must complete before any backtest.
Save:
- v4_universe_coverage.csv
- v4_combo_cardinality.csv
- v4_parameter_values.json
- v4_stage_counts.csv

Stage 1: deterministic coarse pass
Use this exact coarse grid. Do not invent a smaller one.

coarse signal_family:
- all 5 signal families

coarse profile_width:
- tight
- wide

coarse sizing:
- percent_based
- tick_based_direct

coarse direction:
- long
- short
- both

coarse entry_timing:
- next_bar_open
- signal_bar_close
- adverse_1tick
- adverse_half_spread_proxy

coarse session_filter:
- all_available_data
- main_session_only_if_identifiable
- exclude_first_last_10_minutes

coarse momentum_pct:
- 0.0003,0.0008,0.0015,0.003,0.008,0.015

coarse momentum_ticks:
- 1,3,8,21,55,144

coarse breakout_lookback:
- 3,8,21,55,144

coarse trend pairs:
- 3/8,3/21,3/55,5/13,5/34,5/89,8/21,8/55,13/34,13/89,21/55,21/144

coarse volume_multiplier:
- 0,0.8,1.2,2.0,3.0

coarse volume_window:
- 20,60,120

coarse vwap_mode:
- disabled
- rolling20
- rolling60
- session

coarse vwap_buffer_pct:
- 0,0.0002,0.001

coarse percent stop/trail:
- stop_pct: 0.0005,0.001,0.002,0.005,0.01,0.02
- trail_pct: 0.0003,0.0008,0.0015,0.003,0.008,0.015
- trail_activation_pct: 0.0008,0.0015,0.003,0.008,0.015,0.03

coarse direct ticks:
- direct_stop_ticks: 1,3,8,21,55,144,377
- direct_trail_ticks: 1,3,8,21,55,144
- direct_activation_ticks: 5,13,34,89,233,610

coarse min ticks:
- min_stop_ticks: 1,3,8,21,55,144,377
- min_trail_ticks: 1,3,8,21,55,144

coarse cooldown:
- 0,3,10,40

coarse max_hold:
- 5,15,60,120,240

Evaluate every coarse combination for every valid ticker. If this is too large for one response, process in ticker batches and continue until all 423 valid tickers are completed. Do not stop after a tiny preview.

Stage 1 preliminary survivor rule:
A coarse combo survives if:
- test_trades >= 20
- test_net_2t > 0
- test_pf_2t >= 1.00
- remove_best_1_net_2t is not catastrophic
- data_quality is not pathological

Save:
- v4_coarse_results.csv
- v4_pruning_log.csv
- v4_stage_counts.csv

Stage 2: deterministic fine expansion
For every Stage 1 survivor, expand to adjacent values from the full declared grid:
- +/-1 and +/-2 grid-index values for stop, trail, activation, momentum, lookback, trend pairs, volume, vwap buffer, cooldown, max_hold.
- Evaluate every local fine-grid combination.
- Do not cap by top-N.
- Do not randomize.

Save:
- v4_fine_results.csv
- v4_fine_survivors.csv
- v4_stage_counts.csv

Stage 3: full stress validation
For every Stage 2 survivor:
- train/test/full
- slippage 0,1,2,3,4,5
- outlier removal best 1,3,5
- rolling walk-forward 60d/20d, 90d/30d, expanding/next-month
- time-of-day breakdown
- direction breakdown
- fee/stop classification
- spread/stop classification
- microstructure classification

Save:
- v4_stress_summary.csv
- v4_slippage_stress.csv
- v4_rolling_walkforward.csv
- v4_time_of_day_breakdown.csv
- v4_direction_breakdown.csv
- v4_outlier_removal.csv
- v4_fee_stop_viability.csv
- v4_microstructure_classification.csv

Stage 4: deterministic neighborhood validation
For every Stage 3 candidate:
- Evaluate all +/-1 and +/-2 grid-index neighbors for the active parameters.
- Report neighborhood_expected_count and neighborhood_evaluated_count.
- Mark PARAMETER_SPIKE if neighborhood_profitable_2t_pct < 50%.

Save:
- v4_neighborhood_summary.csv
- v4_final_live_paper_profiles.json
- v4_rejected_tickers.csv
- v4_results.zip

Final live-paper rules:
Only classify as LIVE_NOW or LIVE_NOW_WIDE_CANDIDATE if all are true:
- Stage 1 completed for all valid tickers.
- Stage 2 completed for that profile.
- Stage 3 completed for that profile.
- Stage 4 completed for that profile.
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

Required final response:
1. Confirm whether Stage 1 completed for all valid tickers.
2. Confirm whether Stage 2 completed for all Stage 1 survivors.
3. Confirm whether Stage 3 completed for all Stage 2 survivors.
4. Confirm whether Stage 4 completed for all Stage 3 candidates.
5. Show exact evaluated counts by stage.
6. Show skipped/pruned counts and reasons.
7. Show LIVE_NOW candidates, if any.
8. If any stage is incomplete, explicitly say: "No live-paper recommendation can be made from this run."
9. Provide v4_results.zip and all required CSV/JSON files.
