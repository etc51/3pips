You are given a compact ZIP with selected candidate futures from a previous all-universe MOEX/T-Bank futures search.

Important context:
- The current live/paper bot is already running these tickers and they must be excluded from recommendations:
  LKM6, BMM6, BRM6, S1M6, GDM6.
- This package contains only NEW candidates not currently running.
- Do not mix Neo/perp instruments with classic MOEX futures. Analyze them separately.

Goal:
Run a strict stress validation, not a broad discovery search. The task is to reduce these candidates to the best 5-8 for a second paper contour.

Files:
- selected_new_candidate_1m.csv: 1-minute candles with secid,time,open,high,low,close,volume,is_complete.
- instrument_specs.csv: instrument specs.
- candidate_seed_profiles.csv: best profiles from the all-futures first-pass search.
- candidate_groups.json: strong/watchlist/neo/current-running metadata.

Candidate groups:
- New strong MOEX candidates:
  GLM6, CHM6, TTM6, RNM6, BTM6
- New watchlist MOEX candidates:
  SiU6, CEM6, GNU6, FSM6, S1U6
- Neo/perp candidates, analyze separately:
  AMZNperpA, TSLAperpA, COINperpA, AMDperpA
- Extra high-risk KEEP candidates, analyze but do not recommend unless stress is exceptional:
  PDU6, PTU6

Validation rules:
1. Re-run each provided seed profile exactly.
2. For each ticker, also run a focused local search around the seed profile:
   - stop_pct/direct_stop_ticks nearby values +/- 20-50%
   - trail_pct/direct_trail_ticks nearby values +/- 20-50%
   - trail_activation_pct/direct_activation_ticks nearby values +/- 20-50%
   - momentum_pct/momentum_ticks nearby values
   - breakout/trend/lookback nearby values
   - cooldown and max_hold nearby values
3. Evaluate train/test/full:
   - train = first 70%
   - test = last 30%
   - full sample
4. Slippage stress:
   - round-trip slippage 0,1,2,3,4,5 ticks
   - main survival must be at 2T
   - 3T-5T are severe stress levels
5. Outlier stress:
   - remove best 1, 3, and 5 trades
   - profile is not robust if remove-best-3 destroys most profit
6. Rolling walk-forward:
   - 60d train / 20d test
   - 90d train / 30d test
   - expanding train / next month test
   - require at least 60% active profitable windows for KEEP
7. Neighborhood:
   - test at least 2000 nearby variants per final candidate if compute allows
   - report neighborhood_profitable_2t_pct
   - report neighborhood median and worst PnL
8. Microstructure:
   - classify LOW/MEDIUM/HIGH risk using volume, zero-volume share, row count, tick value, and expected trade frequency
   - high microstructure risk can be WATCHLIST but should not be main strong contour unless results are exceptional
9. Time filters:
   - all data
   - main session only if timestamps allow
   - exclude first/last 10 minutes of trading day
   - time-of-day breakdown by hour
10. Direction:
   - long-only, short-only, both
   - direction breakdown in output

Hard recommendation rules:
- Do NOT recommend current-running tickers: LKM6, BMM6, BRM6, S1M6, GDM6.
- Do NOT rank LOW_SAMPLE as strong.
- Do NOT rank a profile as strong if:
  - test_trades < 40
  - test_net_2t <= 0
  - test_pf_2t < 1.10
  - remove_best_3_net_2t <= 0
  - neighborhood_profitable_2t_pct < 0.50
  - rolling active profitable windows at 2T < 60%
- Neo/perp profiles must be reported separately and not mixed into MOEX contour.

Output files:
- new_candidates_stress_summary.csv
- new_candidates_final_profiles.json
- new_candidates_slippage_stress.csv
- new_candidates_rolling_walkforward.csv
- new_candidates_neighborhood_summary.csv
- new_candidates_outlier_removal.csv
- new_candidates_time_of_day_breakdown.csv
- new_candidates_direction_breakdown.csv
- new_candidates_portfolio_recommendation.csv

Final answer:
1. Confirm loaded tickers and row counts.
2. Show final recommended second-paper MOEX strong contour.
3. Show weak/watchlist contour.
4. Show Neo/perp contour separately.
5. Explicitly list rejected candidates and why.
6. Say whether any candidate is strong enough to replace a current-running ticker later.
