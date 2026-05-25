Use the uploaded V6 package.

This is a fresh-window V6 continuation based on completed V5 Stage 1.

Goal:
Complete Stage 2-5 validation for the selected top-15 futures families and selected deterministic Stage 1 entry profiles.

Important:
Do not rerun broad Stage 1.
Use v6_selected_stage1_entry_profiles.csv as the fixed Stage 1 survivor input.

Selected universe:
- 15 families
- 56 tickers
- 555 selected entry profiles
- maximum 10 entry profiles per ticker
- expected Stage 2 exit combinations: 555 * 2882 = 1,599,510

Hard rules:
- No random search.
- No Sobol sampling.
- No Latin hypercube sampling.
- No stochastic candidate generation.
- No hidden top-N.
- No partial preview.
- Stage 2 must evaluate all 1,599,510 required exit combinations.
- If any stage is incomplete, explicitly say: No live-paper recommendation can be made from this run.

Files:
- v6_top15_1m_candles.csv
- v6_top15_instrument_specs.csv
- v6_top15_row_counts.csv
- v6_selected_top15_families.csv
- v6_selected_stage1_entry_profiles.csv
- v6_top15_liquidity_by_family.csv
- v6_top15_liquidity_by_ticker.csv
- v6_metadata.json

Stage 0: audit
1. Load all files.
2. Confirm 15 families.
3. Confirm 56 tickers.
4. Confirm 555 selected entry profiles.
5. Confirm row counts match candles.
6. Confirm Stage 2 expected count = 1,599,510.
7. Save v6_stage_counts.csv and v6_run_manifest.json.

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

Stage 2: full exit grid
For every selected entry profile from v6_selected_stage1_entry_profiles.csv, keep all entry/signal/session/direction parameters fixed and evaluate every exit combination below.

Direct tick exits:
- stop_ticks: 1,2,3,5,8,13,21,34,55,89,144,233,377
- trail_ticks: 1,2,3,5,8,13,21,34,55,89,144
- activation_ticks: 5,8,13,21,34,55,89,144,233,377

Percent exits:
- stop_pct: 0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02
- trail_pct: 0.0002,0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01
- activation_pct: 0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02

For percent exits, convert to effective ticks using ticker tick size.
Enforce stop_ticks >= 1 and trail_ticks >= 1.

Expected combinations per entry profile:
- direct exits: 13 * 11 * 10 = 1,430
- percent exits: 12 * 11 * 11 = 1,452
- total = 2,882

Total required Stage 2 combinations:
- 555 * 2,882 = 1,599,510

Stage 2 survivor rule:
- test_trades >= 40
- test_net_2t > 0
- test_pf_2t >= 1.05
- remove_best_3_net_2t > 0
- best_trade_share_2t <= 0.35

Save:
- v6_stage2_exit_grid.csv
- v6_stage2_survivors.csv
- v6_fee_stop_viability.csv
- v6_stage_counts.csv

Stage 3: deterministic local refinement
For every Stage 2 survivor:
- Expand active signal parameters by +/-1 grid index where available.
- Expand active stop/trail/activation parameters by +/-1 and +/-2 grid index.
- Expand cooldown and max_hold by +/-1 grid index.
- Evaluate every local combination.
- No randomization.
- No cap.

Stage 3 survivor rule:
- test_trades >= 40
- test_net_2t > 0
- test_pf_2t >= 1.10
- remove_best_3_net_2t > 0
- train/test average trade does not collapse by more than 50%
- best_trade_share_2t <= 0.30

Save:
- v6_stage3_refinement.csv
- v6_stage3_survivors.csv
- v6_stage_counts.csv

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
- v6_stress_summary.csv
- v6_slippage_stress.csv
- v6_rolling_walkforward.csv
- v6_time_of_day_breakdown.csv
- v6_direction_breakdown.csv
- v6_outlier_removal.csv
- v6_microstructure_classification.csv

Stage 5: deterministic neighborhood validation
For every Stage 4 candidate:
- Evaluate all +/-1 and +/-2 grid-index neighbors for active parameters.
- Report neighborhood_expected_count.
- Report neighborhood_evaluated_count.
- Report neighborhood_profitable_2t_pct.
- Report neighborhood_median_net_2t.
- Report neighborhood_worst_net_2t.
- Mark PARAMETER_SPIKE if neighborhood_profitable_2t_pct < 50%.

Save:
- v6_neighborhood_summary.csv
- v6_final_live_paper_profiles.json
- v6_rejected_tickers.csv

Final LIVE_NOW rules:
- Stage 2 completed fully.
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

Required final files:
- v6_results.zip
- v6_run_manifest.json
- v6_stage_counts.csv
- v6_stage2_exit_grid.csv
- v6_stage2_survivors.csv
- v6_stage3_refinement.csv
- v6_stage3_survivors.csv
- v6_stress_summary.csv
- v6_slippage_stress.csv
- v6_rolling_walkforward.csv
- v6_time_of_day_breakdown.csv
- v6_direction_breakdown.csv
- v6_outlier_removal.csv
- v6_fee_stop_viability.csv
- v6_microstructure_classification.csv
- v6_neighborhood_summary.csv
- v6_final_live_paper_profiles.json
- v6_rejected_tickers.csv

Final response:
Give only:
1. stage completion status,
2. exact evaluated counts by stage,
3. LIVE_NOW candidates if any, else NONE,
4. one archive v6_results.zip containing all CSV/JSON/MD outputs.
