You are given MOEX/T-Bank futures 1-minute candle data and instrument specs from prior ZIP packages.

This is a V2 full recalculation. The objective is NOT to ban instruments early. The objective is to recalculate everything under realistic conditions, classify every viable idea, and decide what can be traded now versus what needs another strategy variant.

Do not discard a ticker only because it fails current live-bot constraints. Calculate it anyway, then classify it.

Compute intensity requirement:
This must be a maximum-depth search, not a small sample. Use as much computation as available.
Do not stop after a small top-N pass if more parameter space can be evaluated.
Complete the full requested search and stress validation.

Goal:
Re-run the full all-futures search for the short-term momentum + trailing-stop idea with realistic costs, execution assumptions, overlap checks, and multiple strategy modes.

Files:
- tbank_1m_all_futures*.csv: 1-minute candles with secid,time,open,high,low,close,volume,is_complete.
- instrument_specs.csv: ticker,tick,tick_rub,go_buy,go_sell,expiration,API flags.
- row_counts*.csv: rows per ticker.

Universe:
- Load and process every ticker present in the candle files and instrument specs.
- Explicitly include all contracts expiring on or before 2026-12-31.
- Also process instruments expiring after 2026-12-31 and Neo/perp instruments, but report them separately.
- Output a universe coverage table:
  - total tickers loaded
  - tickers with candles
  - tickers expiring <= 2026-12-31
  - tickers expiring > 2026-12-31
  - Neo/perp/instruments with far expiration
  - tickers skipped only because of missing/bad data

Current live/paper constraints for comparison:
- One ticker may have only one open position inside the same external contour.
- No live entry if order book is empty or one side has zero size.
- Classic MOEX futures and Neo/perp instruments must be analyzed separately.

Important:
Do not use any fixed capital amount such as 200,000 RUB as a strategy-selection rule.
Capital and position sizing are operational paper-trading settings, not part of the edge search.

Core idea:
- Enter long/short after short-term directional movement.
- Immediately place stop.
- If price moves in favor, activate trailing stop.
- Exit by trailing stop or max hold.
- Everything must be after commission, slippage, and pessimistic execution.

Important principle:
Do NOT use fee/stop, spread/stop, liquidity, or a fixed capital size as early deletion filters.
Use them as classification dimensions.

Every ticker/profile must be assigned to one of:

1. LIVE_NOW
   Profile fits the current live-bot style and can be paper traded now.

2. NEEDS_WIDE_PROFILE
   Ticker may have edge, but current tight stop/trail parameters are too small relative to fees/spread.
   Must be re-tested with wider stops/trails/activation/targets and lower trade frequency.

3. NEEDS_MICROSTRUCTURE_VALIDATION
   Historical candle edge exists, but live order book/spread/liquidity is uncertain.

4. RESEARCH_ONLY
   Interesting historical behavior, but not ready for paper/live.

5. NO_EDGE
   No robust edge after realistic costs and stress.

6. REJECT_PATHOLOGICAL
   Bad data, broken specs, too few rows, impossible tick math, or obvious artifact.

Required calculation passes:

PASS 0: Full universe audit
- Before any strategy calculation, print actual row counts by ticker.
- Confirm every CSV part was loaded.
- Confirm all tickers from instrument_specs are accounted for.
- Confirm every ticker with rows is assigned to exactly one universe bucket:
  - classic_moex_expiring_to_2026_12_31
  - classic_moex_after_2026_12_31
  - neo_or_perp
  - bad_or_missing_data

PASS A: Current tight/live-bot style
- Search short-term profiles close to the current idea.
- Include tight and medium stops.
- This pass can produce LIVE_NOW only if it survives live-feasibility classification.

PASS B: Wide-profile rescue
- For tickers that fail fee_to_stop or spread_to_stop in Pass A, do NOT reject them.
- Recalculate with wider stops/trails/activation and less frequent entries.
- Search wider stop_ticks and percent stops:
  - min_stop_ticks: 21,34,55,89,144,233,377,610
  - stop_pct: 0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02,0.03,0.05
  - trail_ticks: 8,13,21,34,55,89,144,233
  - trail_activation_ticks: 13,21,34,55,89,144,233,377
  - max_hold_minutes: 15,30,60,90,120,180,240
  - cooldown_minutes: 3,5,10,20,40,60
- These profiles should be classified as NEEDS_WIDE_PROFILE or LIVE_NOW_WIDE_CANDIDATE.

PASS C: Stress and robustness
- Re-test top candidates from Pass A and Pass B.
- Train/test/full.
- Rolling walk-forward.
- Slippage 0,1,2,3,4,5 ticks round trip.
- Outlier removal.
- Neighborhood stability.
- Direction/time-of-day breakdown.

PASS D: Overlap and margin-normalized feasibility, without fixed capital
- Do not use a fixed account size.
- Report results per 1 contract and per normalized unit of margin.
- Simulate overlap constraints:
  - one open position per ticker
  - no duplicate strict/aggressive entry on the same ticker
- Report:
  - max simultaneous positions
  - max simultaneous margin per 1 contract
  - net PnL per 1 contract
  - return_on_margin using average/current GO only as a denominator
- Run variants:
  - LIVE_NOW only
  - LIVE_NOW + WIDE candidates
  - MOEX only
  - Neo/perp separately

Cost model:
For every candidate profile and every trade estimate:
- notional_rub = close / tick * tick_rub
- round_turn_fee_rub = max(2 * notional_rub * 0.00025, 1 * tick_rub)
- round_turn_fee_ticks = round_turn_fee_rub / tick_rub
- fee_to_stop_ratio = round_turn_fee_ticks / stop_ticks
- Report fee_to_stop_ratio for every final candidate.

Fee/stop classification:
- FEE_EXCELLENT: fee_to_stop_ratio <= 0.25
- FEE_OK: 0.25 < fee_to_stop_ratio <= 0.55
- FEE_HEAVY: 0.55 < fee_to_stop_ratio <= 1.00
- FEE_DOMINATES: fee_to_stop_ratio > 1.00

Do not delete FEE_HEAVY or FEE_DOMINATES profiles automatically.
Instead:
- If tight profile is profitable but FEE_HEAVY/FEE_DOMINATES, send it to Pass B wide-profile rescue.
- Only mark NO_EDGE if wide-profile rescue also fails.

Spread/execution:
- If order book history is unavailable, use conservative proxy.
- Evaluate slippage 0,1,2,3,4,5 ticks round trip.
- Add spread_to_stop_proxy = (min_spread_ticks + 2 * slippage_ticks) / stop_ticks.
- Classify:
  - SPREAD_OK
  - SPREAD_HEAVY
  - SPREAD_DOMINATES
- Do not delete automatically; use classification and Pass B.

Pessimistic intrabar execution:
For 1-minute candles, if both favorable movement and stop can happen inside the same candle, assume the worse event happens first.
Do not use optimistic high/low ordering.

Microstructure:
Candle volume is not executable liquidity. Still calculate:
- zero_volume_share
- median_1m_volume
- p10_1m_volume
- expected_trades_per_day
- microstructure_risk LOW/MEDIUM/HIGH
- data_quality flags

Search space:
- signal families:
  - momentum_breakout
  - vwap_impulse
  - range_expansion
  - trend_pullback
  - pure_trailing_after_impulse
- direction: long, short, both
- momentum_pct: 0.0002,0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02
- momentum_ticks: 1,2,3,5,8,13,21,34,55,89,144
- breakout_lookback: 3,5,8,13,21,34,55,89,144
- trend_fast: 3,5,8,13,21
- trend_slow: 8,13,21,34,55,89,144
- volume_multiplier: 0,0.5,0.8,1.0,1.2,1.5,2.0,2.5,3.0,4.0
- volume_window: 20,40,60,120
- vwap_mode: disabled, rolling20, rolling60, session
- vwap_buffer_pct: 0,0.0001,0.0002,0.0005,0.001,0.002
- stop_pct: 0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02,0.03,0.05
- trail_pct: 0.0002,0.0003,0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02
- trail_activation_pct: 0.0005,0.0008,0.001,0.0015,0.002,0.003,0.005,0.008,0.01,0.015,0.02,0.03
- min_stop_ticks: 1,2,3,5,8,13,21,34,55,89,144,233,377,610
- min_trail_ticks: 1,2,3,5,8,13,21,34,55,89,144,233
- cooldown_minutes: 0,1,3,5,10,20,40,60
- max_hold_minutes: 5,10,15,30,60,90,120,180,240
- entry timing:
  - next_bar_open
  - signal_bar_close
  - adverse_1tick
  - adverse_half_spread_proxy
- session filters:
  - all available data
  - main session only if identifiable
  - exclude first/last 10 minutes of trading day

Exhaustive-search instruction:
- For every ticker, evaluate both tick-based and percent-based parameter families.
- For every ticker, evaluate tight and wide profile families.
- Do not only test the previous seed profile.
- Do not only test currently active paper tickers.
- Do not skip low-liquidity instruments unless data is pathological; calculate and classify them.
- The final output must include a row for every loaded ticker, even if the result is NO_EDGE or REJECT_PATHOLOGICAL.

Splits:
- train = first 70%
- test = last 30%
- full sample
- rolling walk-forward:
  - 60d train / 20d test
  - 90d train / 30d test
  - expanding train / next month test

Robustness:
Do NOT optimize for highest historical PnL.
Rank robust regions and classifications:
- test_trades >= 40 for main recommendation; if lower, label LOW_SAMPLE
- test_net_2t > 0
- test_profit_factor_2t >= 1.10
- remove_best_3_net_2t > 0
- rolling active profitable windows at 2T >= 60%
- neighborhood_profitable_2t_pct >= 50%
- no single trade dominates profit

If a profile fails one condition, do not silently discard it. Record why and place it in the appropriate class.

Required output files:
- v2_universe_coverage.csv
- v2_grid_results.csv
- v2_top_profiles_by_ticker.csv
- v2_stress_summary.csv
- v2_final_live_paper_profiles.json
- v2_rolling_walkforward.csv
- v2_slippage_stress.csv
- v2_neighborhood_summary.csv
- v2_time_of_day_breakdown.csv
- v2_direction_breakdown.csv
- v2_rejected_tickers.csv
- v2_fee_stop_viability.csv
- v2_wide_profile_rescue.csv
- v2_microstructure_classification.csv
- v2_overlap_margin_simulation.csv
- v2_project_diagnostics.json

Final answer:
1. Count tickers and rows actually loaded.
2. Show how many tickers/profiles fell into each class:
   LIVE_NOW, LIVE_NOW_WIDE_CANDIDATE, NEEDS_WIDE_PROFILE, NEEDS_MICROSTRUCTURE_VALIDATION, RESEARCH_ONLY, NO_EDGE, REJECT_PATHOLOGICAL.
3. Show classic MOEX LIVE_NOW list.
4. Show classic MOEX wide candidates.
5. Show Neo/perp separately.
6. Explicitly list tickers where tight strategy failed but wide rescue worked.
7. Explicitly list tickers where both tight and wide failed.
8. Do not present a fragile/overfit/low-sample result as a final trading recommendation, but still report it.
