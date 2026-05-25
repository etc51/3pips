Use the uploaded V7 retry package.

Goal:
Complete the missing V7 batch by running a bounded retry:
- Stage 3 exit-only local refinement
- Stage 4 stress validation
- Stage 5 neighborhood validation

Do not rerun Stage 1 or Stage 2.
Use only v7_selected_stage2_profiles.csv as fixed input.

Why this retry exists:
The previous batch was incomplete because Stage 3 became too large or was not evaluated. This retry deliberately freezes entry/signal parameters and refines only exit parameters, so the batch is computable and still useful.

Hard rules:
- No random search.
- No Sobol / Latin hypercube.
- No hidden top-N.
- No broad expansion back to all V6 survivors.
- Do not expand signal parameters.
- Do not expand entry timing/session/direction.

Stage 0:
Load files and confirm selected profile count from v7_batch_metadata.json.

Stage 3 retry: exit-only local refinement
For every selected Stage 2 profile:
- Keep ticker/family/signal_family/direction/entry_timing/session_filter fixed.
- Keep all signal parameters fixed.
- Keep cooldown_minutes fixed.
- Keep max_hold_minutes fixed.
- Expand only exit stop/trail/activation by +/-1 and +/-2 declared grid index where available.

Direct grids:
- stop_ticks: 1,2,3,5,8,13,21,34,55,89,144,233,377
- trail_ticks: 1,2,3,5,8,13,21,34,55,89,144
- activation_ticks: 5,8,13,21,34,55,89,144,233,377

Percent grids:
- stop_pct: 0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02
- trail_pct: 0.0002,0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01
- activation_pct: 0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02

Stage 3 survivor rule:
- test_trades >= 40
- test_net_2t > 0
- test_pf_2t >= 1.10
- remove_best_3_net_2t > 0
- best_trade_share_2t <= 0.30
- train_net_2t > 0

Stage 4:
For every Stage 3 survivor, evaluate:
- slippage 0,1,2,3,4,5 ticks
- outlier removal best 1,3,5
- rolling walk-forward
- time-of-day breakdown
- direction breakdown
- fee/stop classification
- spread/stop proxy classification
- candle microstructure classification

Stage 5:
For every Stage 4 candidate:
- Evaluate deterministic +/-1 and +/-2 exit-parameter neighbors.
- Report neighborhood_expected_count, neighborhood_evaluated_count, neighborhood_profitable_2t_pct, neighborhood_median_net_2t, neighborhood_worst_net_2t.
- Mark PARAMETER_SPIKE if neighborhood_profitable_2t_pct < 50%.

Final output:
- one archive v7_retry_results.zip
- include all CSV/JSON/MD outputs.

Final response:
Give only:
1. batch name,
2. stage completion status,
3. exact evaluated counts by stage,
4. LIVE_NOW candidates if any, else NONE,
5. one archive v7_retry_results.zip.
