Use the uploaded ZIP files with MOEX/T-Bank futures 1-minute candles and instrument specs.

This task is a deterministic exhaustive-search audit and backtest.

The previous V2 run used randomized tight+wide search and is not considered exhaustive. Do not use V2 results as a trading basis. This run must not use random sampling.

Goal:
Run a deterministic exhaustive or deterministic staged-exhaustive search over the short-term momentum + trailing-stop strategy for all loaded futures.

Files:
- tbank_1m_all_futures*.csv
- instrument_specs.csv
- row_counts*.csv

First action:
1. Unzip all package parts.
2. Load all candle CSVs.
3. Load instrument_specs.csv.
4. Load row_counts if present.
5. Print and save row counts per secid.
6. Confirm every CSV part was loaded.
7. Confirm every ticker from instrument_specs is accounted for.
8. Assign every ticker to exactly one universe bucket:
   - classic_moex_expiring_to_2026_12_31
   - classic_moex_after_2026_12_31
   - neo_or_perp
   - bad_or_missing_data

Hard rule:
Do NOT use:
- random search
- randomized parameter sampling
- Sobol sampling
- Latin hypercube sampling
- stochastic candidate generation
- random subset selection
- silent top-N-only pruning
- "as much as possible" sampling

Before running any backtest:
1. Build the full Cartesian parameter manifest.
2. Save it to exhaustive_grid_manifest.csv.
3. Save parameter lists to exhaustive_parameter_values.json.
4. Save exact counts to exhaustive_combo_cardinality.csv.
5. Assign every valid and invalid combination a stable deterministic combo_id:
   combo_id = sha256(ticker + universe_bucket + profile_width_family + sizing_family + signal_family + direction + entry_timing + session_filter + all parameter names and values in canonical sorted order)
6. Report:
   - number of tickers
   - rows loaded
   - raw combinations per ticker
   - invalid combinations per ticker and reason
   - valid combinations per ticker
   - total valid combinations
   - expected slippage metric rows = valid combinations x 6

Parameter universe:

Signal families:
- momentum_breakout
- vwap_impulse
- range_expansion
- trend_pullback
- pure_trailing_after_impulse

Profile width families:
- tight
- wide

Sizing families:
- percent_based
- tick_based_direct

Direction:
- long
- short
- both

Momentum percent:
- 0.0002,0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02

Momentum ticks:
- 1,2,3,5,8,13,21,34,55,89,144

Breakout lookback:
- 3,5,8,13,21,34,55,89,144

Trend fast:
- 3,5,8,13,21

Trend slow:
- 8,13,21,34,55,89,144

Only evaluate trend pairs where trend_slow > trend_fast.
Record skipped invalid trend pairs as invalid_trend_pair.

Volume multiplier:
- 0,0.5,0.8,1.0,1.2,1.5,2.0,2.5,3.0,4.0

Volume window:
- 20,40,60,120

VWAP mode:
- disabled
- rolling20
- rolling60
- session

VWAP buffer pct:
- 0,0.0001,0.0002,0.0005,0.001,0.002

Percent sizing:
- stop_pct: 0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02,0.03,0.05
- trail_pct: 0.0002,0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02
- trail_activation_pct: 0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02,0.03

Tick/direct sizing:
- direct_stop_ticks: 1,2,3,5,8,13,21,34,55,89,144,233,377,610
- direct_trail_ticks: 1,2,3,5,8,13,21,34,55,89,144,233
- direct_activation_ticks: 5,8,13,21,34,55,89,144,233,377,610

Minimum tick constraints:
- min_stop_ticks: 1,2,3,5,8,13,21,34,55,89,144,233,377,610
- min_trail_ticks: 1,2,3,5,8,13,21,34,55,89,144,233

Wide profile additional values:
- wide_stop_ticks: 21,34,55,89,144,233,377,610
- wide_stop_pct: 0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02,0.03,0.05
- wide_trail_ticks: 8,13,21,34,55,89,144,233
- wide_activation_ticks: 13,21,34,55,89,144,233,377
- wide_max_hold_minutes: 15,30,60,90,120,180,240
- wide_cooldown_minutes: 3,5,10,20,40,60

Cooldown minutes:
- 0,1,3,5,10,20,40,60

Max hold minutes:
- 5,10,15,30,60,90,120,180,240

Entry timing:
- next_bar_open
- signal_bar_close
- adverse_1tick
- adverse_half_spread_proxy

Session filters:
- all_available_data
- main_session_only_if_identifiable
- exclude_first_last_10_minutes

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
- Use pessimistic intrabar execution.
- If both favorable movement and adverse stop can happen in the same candle, assume the worse event happens first unless the trailing stop was already active before that candle.

Fee/stop classification:
- fee_to_stop_ratio = round_turn_fee_ticks / stop_ticks
- FEE_EXCELLENT: <= 0.25
- FEE_OK: >0.25 and <=0.55
- FEE_HEAVY: >0.55 and <=1.00
- FEE_DOMINATES: >1.00

Spread/stop classification:
- spread_to_stop_proxy = (min_spread_ticks + 2 * slippage_ticks) / stop_ticks
- SPREAD_OK
- SPREAD_HEAVY
- SPREAD_DOMINATES

Do not delete FEE_HEAVY, FEE_DOMINATES, SPREAD_HEAVY, or SPREAD_DOMINATES early.
Use them as classification dimensions.

Microstructure metrics:
- zero_volume_share
- median_1m_volume
- p10_1m_volume
- expected_trades_per_day
- microstructure_risk LOW/MEDIUM/HIGH
- data_quality_flags

Splits:
- train = first 70%
- test = last 30%
- full sample
- rolling walk-forward:
  - 60d train / 20d test
  - 90d train / 30d test
  - expanding train / next month test

If full Cartesian evaluation is feasible:
- Evaluate every valid combination for every loaded ticker.
- Save every evaluated combo and metrics.

If full Cartesian evaluation is infeasible:
Do NOT randomize.
Use this deterministic staged exhaustive protocol:

Stage 1: Full coarse Cartesian grid
- Declare the coarse grid explicitly in exhaustive_parameter_values.json.
- Evaluate every coarse combination for every ticker.
- No randomization.

Stage 2: Deterministic pruning
A coarse combination survives only if:
- test_trades >= 20
- test_net_2t > 0
- test_pf_2t >= 1.00
- train_net_2t is not deeply negative
- remove_best_1_net_2t is not catastrophic
- data_quality is not pathological
Record prune_reason for every non-survivor.

Stage 3: Full fine Cartesian expansion
For every surviving coarse combination:
- Expand each parameter to adjacent declared full-grid values.
- Use +/-1 and +/-2 grid-index neighbors where applicable.
- Evaluate the full Cartesian local fine grid.
- No randomization and no top-N-only cap.

Stage 4: Full stress validation for every fine survivor
- train/test/full
- slippage 0,1,2,3,4,5
- outlier removal best 1,3,5
- rolling walk-forward
- time-of-day breakdown
- direction breakdown
- fee/stop classification
- spread/stop classification
- microstructure classification

Stage 5: Deterministic neighborhood validation
For every final candidate:
- Build neighborhood using adjacent declared grid values.
- Evaluate every neighborhood variant.
- Report neighborhood_expected_count.
- Report neighborhood_evaluated_count.
- Report neighborhood_profitable_2t_pct.
- Report neighborhood_median_net_2t.
- Report neighborhood_worst_net_2t.
- Mark PARAMETER_SPIKE if neighborhood_profitable_2t_pct < 50%.

Ranking and final classes:
Do not optimize for maximum historical PnL.
Classify every ticker/profile into:
- LIVE_NOW
- LIVE_NOW_WIDE_CANDIDATE
- NEEDS_WIDE_PROFILE
- NEEDS_MICROSTRUCTURE_VALIDATION
- RESEARCH_ONLY
- NO_EDGE
- REJECT_PATHOLOGICAL
- LOW_SAMPLE
- PARAMETER_SPIKE
- REJECT_OVERFIT
- REJECT_FRAGILE

Main live-paper rules:
- test_trades >= 40
- test_net_2t > 0
- test_pf_2t >= 1.10
- remove_best_3_net_2t > 0
- rolling active profitable windows at 2T >= 60%
- neighborhood_profitable_2t_pct >= 50%
- no single trade dominates profit
- not LOW_SAMPLE
- not PARAMETER_SPIKE
- not REJECT_OVERFIT
- not REJECT_FRAGILE

Required output files:
- exhaustive_run_manifest.json
- exhaustive_parameter_values.json
- exhaustive_grid_manifest.csv
- exhaustive_combo_cardinality.csv
- exhaustive_evaluation_log.csv
- exhaustive_skipped_combinations.csv
- exhaustive_pruning_log.csv
- exhaustive_stage_counts.csv
- exhaustive_feature_cache_manifest.csv
- v3_universe_coverage.csv
- v3_grid_results.csv
- v3_top_profiles_by_ticker.csv
- v3_stress_summary.csv
- v3_final_live_paper_profiles.json
- v3_rolling_walkforward.csv
- v3_slippage_stress.csv
- v3_neighborhood_summary.csv
- v3_time_of_day_breakdown.csv
- v3_direction_breakdown.csv
- v3_rejected_tickers.csv
- v3_fee_stop_viability.csv
- v3_wide_profile_rescue.csv
- v3_microstructure_classification.csv
- v3_overlap_margin_simulation.csv
- v3_project_diagnostics.json
- v3_results.zip

Required final response:
1. Confirm whether the run was full Cartesian or deterministic staged exhaustive.
2. Show tickers and rows loaded.
3. Show parameter cardinality:
   - combinations per ticker
   - total expected combinations
   - exact evaluated combinations
   - skipped combinations
   - skipped reasons
4. Show class counts.
5. Show classic MOEX LIVE_NOW candidates.
6. Show classic MOEX wide candidates.
7. Show Neo/perp separately.
8. Show rejected tickers by reason.
9. Provide all downloadable files.
10. If full Cartesian was infeasible, explicitly say so and show the deterministic staged protocol counts instead.

Bottom line:
Build and save the full Cartesian manifest before evaluating anything. If you cannot evaluate the full manifest, do not randomize; switch to deterministic staged exhaustive and log every reduction rule.
