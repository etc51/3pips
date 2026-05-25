Use the uploaded V5 top-50 liquid futures family package.

This is a fresh-window V5 run. Ignore previous V2/V3/V4 conclusions.

Goal:
Backtest the short-term momentum + trailing-stop strategy only on the top 50 most liquid futures families by 2026 candle history. Use an auditable deterministic hierarchical search, not a single impossible Cartesian product.

Loaded data:
- v5_top50_1m_candles.csv
- v5_top50_instrument_specs.csv
- v5_top50_row_counts.csv
- v5_top50_liquid_families_2026.csv
- v5_2026_liquidity_by_ticker.csv
- v5_top50_metadata.json

Important:
The selected universe is by family, not by one current monthly contract. Example: BMM6/BMU6/BMZ6 belong to BM. Use every loaded contract inside selected families.

Hard rules:
- No random search.
- No Sobol sampling.
- No Latin hypercube sampling.
- No stochastic candidate generation.
- No silent top-N substitution.
- Every evaluated combo must be logged with deterministic combo_id.
- Do not classify LIVE_NOW unless all required stages are completed for that exact profile.

combo_id:
combo_id = sha256(ticker + family + stage + signal_family + direction + entry_timing + session_filter + all parameter names and values in canonical sorted order)

Stage 0: load and audit
1. Load all files.
2. Confirm candle row counts by ticker.
3. Confirm selected families = 50.
4. Confirm all selected specs have tick, tick_rub, go_buy/go_sell if present.
5. Save:
   - v5_universe_coverage.csv
   - v5_stage_counts.csv
   - v5_run_manifest.json

Cost model:
For every trade:
- notional_rub = close / tick * tick_rub
- round_turn_fee_rub = max(2 * notional_rub * 0.00025, 1 * tick_rub)
- round_turn_fee_ticks = round_turn_fee_rub / tick_rub
- total_cost_ticks = round_turn_fee_ticks + slippage_ticks

Slippage:
- Always report 0,1,2,3,4,5 ticks round trip.
- Selection must primarily survive 2 ticks.

Execution:
- No look-ahead.
- Signals use only closed candles before entry.
- Entry is next_bar_open unless the tested entry_timing says otherwise.
- Pessimistic intrabar execution.
- If both favorable move and stop can occur in the same candle, assume worse event first unless trailing was already active before that candle.

Session filters:
- all_available_data
- main_session_only_if_identifiable
- exclude_first_last_10_minutes

Entry timing:
- next_bar_open
- signal_bar_close
- adverse_1tick
- adverse_half_spread_proxy

Signal families:
- momentum_breakout
- vwap_impulse
- range_expansion
- trend_pullback
- pure_trailing_after_impulse

Direction:
- long
- short
- both

Stage 1: deterministic entry/direction scan
Purpose: find which ticker/family/session/entry/signal/direction combinations have directional edge before spending compute on stop/trail grids.

Use fixed exploratory exits:
- exploratory_stop_ticks: 8,21,55
- exploratory_trail_ticks: 3,8,21
- exploratory_activation_ticks: 8,21,55
- max_hold_minutes: 15,60,180

Use these signal parameters:
- momentum_pct: 0.0005,0.0015,0.005,0.015
- momentum_ticks: 3,8,21,55
- breakout_lookback: 5,13,34,89
- trend pairs: 3/8,3/21,5/34,8/55,13/89,21/144
- volume_multiplier: 0,0.8,1.5,3.0
- volume_window: 20,60,120
- vwap_mode: disabled, rolling20, rolling60, session
- vwap_buffer_pct: 0,0.0002,0.001
- cooldown_minutes: 0,3,10,40

Evaluate every Stage 1 combination for every selected ticker.

Stage 1 survivor rule:
- test_trades >= 20
- test_net_2t > 0
- test_pf_2t >= 1.00
- remove_best_1_net_2t not catastrophic
- data quality not pathological

Save:
- v5_stage1_entry_scan.csv
- v5_stage1_survivors.csv
- v5_pruning_log.csv
- v5_stage_counts.csv

Stage 2: deterministic stop/trail grid around Stage 1 survivors
For every Stage 1 survivor, keep its entry/signal/session/direction parameters fixed and search exits.

Percent exits:
- stop_pct: 0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02
- trail_pct: 0.0002,0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01
- activation_pct: 0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02

Tick/direct exits:
- stop_ticks: 1,2,3,5,8,13,21,34,55,89,144,233,377
- trail_ticks: 1,2,3,5,8,13,21,34,55,89,144
- activation_ticks: 5,8,13,21,34,55,89,144,233,377

For percent exits, convert to ticks using ticker tick size and enforce:
- stop_ticks >= 1
- trail_ticks >= 1

Do not delete high fee/stop profiles early. Record:
- fee_to_stop_ratio
- FEE_EXCELLENT <= 0.25
- FEE_OK > 0.25 and <= 0.55
- FEE_HEAVY > 0.55 and <= 1.00
- FEE_DOMINATES > 1.00

Stage 2 survivor rule:
- test_trades >= 40
- test_net_2t > 0
- test_pf_2t >= 1.05
- remove_best_3_net_2t > 0

Save:
- v5_stage2_exit_grid.csv
- v5_stage2_survivors.csv
- v5_fee_stop_viability.csv
- v5_stage_counts.csv

Stage 3: deterministic local refinement
For every Stage 2 survivor:
- Expand active signal parameters by +/-1 grid index.
- Expand active stop/trail/activation parameters by +/-1 and +/-2 grid index.
- Expand cooldown and max_hold by +/-1 grid index.
- Evaluate every local combination.
- No randomization and no cap.

Stage 3 survivor rule:
- test_trades >= 40
- test_net_2t > 0
- test_pf_2t >= 1.10
- remove_best_3_net_2t > 0
- train/test average trade does not collapse by more than 50%

Save:
- v5_stage3_refinement.csv
- v5_stage3_survivors.csv
- v5_stage_counts.csv

Stage 4: full stress validation
For every Stage 3 survivor:
- train/test/full
- slippage 0,1,2,3,4,5
- outlier removal best 1,3,5
- rolling walk-forward:
  - 60d train / 20d test
  - 90d train / 30d test
  - expanding train / next month test
- time-of-day breakdown
- direction breakdown
- fee/stop classification
- spread/stop proxy classification
- microstructure classification from candles:
  - zero_volume_share
  - median_1m_volume
  - p10_1m_volume
  - active_minutes
  - active_days

Save:
- v5_stress_summary.csv
- v5_slippage_stress.csv
- v5_rolling_walkforward.csv
- v5_time_of_day_breakdown.csv
- v5_direction_breakdown.csv
- v5_outlier_removal.csv
- v5_microstructure_classification.csv

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
- Stage 1 completed.
- Stage 2 completed.
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

Final classes:
- LIVE_NOW
- LIVE_NOW_WIDE_CANDIDATE
- NEEDS_MICROSTRUCTURE_VALIDATION
- RESEARCH_ONLY
- LOW_SAMPLE
- PARAMETER_SPIKE
- REJECT_OVERFIT
- REJECT_FRAGILE
- NO_EDGE
- REJECT_PATHOLOGICAL

Required final files:
- v5_results.zip
- v5_run_manifest.json
- v5_universe_coverage.csv
- v5_stage_counts.csv
- v5_stage1_entry_scan.csv
- v5_stage1_survivors.csv
- v5_stage2_exit_grid.csv
- v5_stage2_survivors.csv
- v5_stage3_refinement.csv
- v5_stage3_survivors.csv
- v5_stress_summary.csv
- v5_slippage_stress.csv
- v5_rolling_walkforward.csv
- v5_time_of_day_breakdown.csv
- v5_direction_breakdown.csv
- v5_outlier_removal.csv
- v5_fee_stop_viability.csv
- v5_microstructure_classification.csv
- v5_neighborhood_summary.csv
- v5_final_live_paper_profiles.json
- v5_rejected_tickers.csv

Required final response:
1. Confirm stages completed.
2. Show exact evaluated counts by stage.
3. Show selected top-50 families and loaded row counts.
4. Show class counts.
5. Show LIVE_NOW candidates if any.
6. Show candidates that are profitable but rejected, with reasons.
7. If any required stage is incomplete, explicitly say: No live-paper recommendation can be made from this run.
