Use the uploaded V7 retry package.

Goal:
Continue an incomplete V7 batch that already completed Stage 3.

Do not rerun Stage 1, Stage 2, or Stage 3.
Use v7_stage3_survivors.csv as fixed input.

Run only:
- Stage 4 stress validation
- Stage 5 neighborhood validation

Hard rules:
- No random search.
- No hidden top-N.
- No broad expansion.
- Every Stage 3 survivor must be evaluated in Stage 4.
- Every Stage 4 candidate must be evaluated in Stage 5.

Stage 0:
Load files and confirm Stage 3 survivor count from v7_batch_metadata.json.

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
