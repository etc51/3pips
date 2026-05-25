# Execution validation MOEX NG lead-lag

Проверяется только уже выбранный кандидат: `fixed_plus1_only`, horizon `30m`, portfolio `global_no_overlap`. Новые фичи, feature selection и оптимизация стратегии не добавлялись.

Unit logic: `tick_value_usd=0.1`, `tick_value_rub=0.1*USD/RUB`. Historical bid/ask/order book в этом pipeline недоступен, поэтому bid/ask execution не проверен.

## 2 ticks + 2 RUB fee
- old_open_next: net=22911.64 RUB, trades=558, positive_months=14/20, maxDD=-4312.70 RUB, skipped=0, fill_unverified=0
- next_1m_after_signal: net=13611.05 RUB, trades=498, positive_months=11/19, maxDD=-4115.92 RUB, skipped=0, fill_unverified=0
- delayed_one_10m_bar: net=2423.16 RUB, trades=442, positive_months=9/18, maxDD=-6431.26 RUB, skipped=68, fill_unverified=0
- adverse_1m_fill: net=4781.84 RUB, trades=498, positive_months=9/19, maxDD=-5789.15 RUB, skipped=0, fill_unverified=129
- high_low_touch_check: net=13611.05 RUB, trades=498, positive_months=11/19, maxDD=-4115.92 RUB, skipped=0, fill_unverified=0

## Required answers
- Сколько PnL было в old_open_next: net=22911.64 RUB, trades=558, positive_months=14/20, maxDD=-4312.70 RUB, skipped=0, fill_unverified=0
- Сколько осталось в next_1m_after_signal: net=13611.05 RUB, trades=498, positive_months=11/19, maxDD=-4115.92 RUB, skipped=0, fill_unverified=0
- Сколько осталось в delayed_one_10m_bar: net=2423.16 RUB, trades=442, positive_months=9/18, maxDD=-6431.26 RUB, skipped=68, fill_unverified=0
- Сколько осталось в adverse_1m_fill: net=4781.84 RUB, trades=498, positive_months=9/19, maxDD=-5789.15 RUB, skipped=0, fill_unverified=129
- Проходит после 2 ticks + 2 RUB fee на adverse_1m_fill: `False`.
- Проходит после 4 ticks + 10 RUB fee на adverse_1m_fill: `False`.
- Fill/order book: bid/ask не проверен; см. `reports/execution_validation_fill_quality.csv`.

## Decision
Edge не проходит реалистичную execution validation на базовом adverse/fill сценарии. Следующий шаг - только paper/order-book исследование.

## Files
- `reports/execution_validation_trades.csv`
- `reports/execution_validation_summary.csv`
- `reports/execution_validation_by_month.csv`
- `reports/execution_validation_fill_quality.csv`