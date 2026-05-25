# Unit-corrected third pass MOEX NG lead-lag

## Что изменилось
Старый research-layer считал log-return и bps-cost. Third pass пересчитывает сделки в единицах контракта: price delta -> ticks -> tick value in RUB через USD/RUB -> gross/net PnL RUB -> return on ГО. ГО не вычитается из PnL, а используется как denominator и margin constraint.

Важно: предыдущий third-pass результат был завышен для текущих контрактов из-за STEPPRICE currency bug. MOEX ISS `STEPPRICE` около 7.x является рублевой текущей оценкой тика, а не USD. Теперь для large NG используется спецификация: `min_step=0.001`, `contract_size=100`, `tick_value_usd=0.1`; `tick_value_rub=0.1*USD/RUB`. Например, 40 ticks при USD/RUB около 79 дают около 316 RUB gross PnL, а не около 22,500 RUB.
Сопоставимый old third-pass best был существенно завышен, потому что текущий MOEX `STEPPRICE` был ошибочно трактован как USD tick value. После фикса лучший `global_no_overlap` для `2 ticks + 2 RUB fee` стал около 22,912 RUB.

FX is daily approximation, not exact clearing FX. return_on_go uses approximate/current margin when historical ГО is unavailable.

## Лучший global_no_overlap результат
- strategy_mode: `fixed_plus1_only`
- cost: `0` ticks + `0` RUB fee
- net PnL RUB: `33,899.71`
- mean return on ГО: `0.00410359`
- positive months: `15/20`
- max drawdown RUB: `-3,780.51`
- trades: `558`

## Базовые cost scenarios, global_no_overlap, threshold=train_mean
- 1 tick + 1 RUB fee: best `fixed_plus1_only`, net=28,405.67 RUB, mean_net=50.91 RUB/trade, mean_ROGO=0.00344223, positive_months=15/20, maxDD=-3,947.21 RUB.
- 2 tick + 2 RUB fee: best `fixed_plus1_only`, net=22,911.64 RUB, mean_net=41.06 RUB/trade, mean_ROGO=0.00278086, positive_months=14/20, maxDD=-4,113.92 RUB.
- 3 tick + 5 RUB fee: best `fixed_plus1_only`, net=16,301.61 RUB, mean_net=29.21 RUB/trade, mean_ROGO=0.001985, positive_months=13/20, maxDD=-4,318.62 RUB.
- 4 tick + 10 RUB fee: best `fixed_plus1_only`, net=8,575.57 RUB, mean_net=15.37 RUB/trade, mean_ROGO=0.00105465, positive_months=13/20, maxDD=-4,561.33 RUB.

## Strategy modes
- fixed_plus1_only: net=22,911.64 RUB, ticks_mean=6.9122, return_on_GO=0.00278086, positive_months=14/20, best_month_share=0.2644958332631276.
- train_selected_feature: net=8,030.71 RUB, ticks_mean=5.3355, return_on_GO=0.00193717, positive_months=14/20, best_month_share=0.45676547997404765.
- ensemble_vote: net=20,589.13 RUB, ticks_mean=6.9329, return_on_GO=0.00285027, positive_months=14/20, best_month_share=0.24874100506662877.

## ГО и концентрация
- max blocked ГО no_overlap: `15,000.00` RUB.
- max blocked ГО global_no_overlap: `15,000.00` RUB.
- max blocked ГО front_month_only: `15,000.00` RUB.
- max blocked ГО allow_overlap: `45,000.00` RUB.
- Если результат положителен только в allow_overlap, он зависит от накопления позиций и не является тем же самым edge, что no_overlap.

## Front-only comparison
Сравнение `all_targets_curve_strategy`, `front_month_only`, `global_no_overlap` сохранено в `third_pass_strategy_summary.csv`. `front_month_only` выбирает только ближайший target/front contract на timestamp и не торгует одновременные NGH/NGJ/NGK как независимые фронты.

## Unit / data audit
- verified_units=True: `46914/46914` unit trade rows.
- anomalies rows saved: `5000`.
- specs with fallback: `24/28`.
- margins with fallback/approx warning: `24/28`.
- FX rows: `1984`, source daily approximation.
- Look-ahead audit passed: `True`.
- 2025-08 не исправлялся искусственно: месяц отсутствует из-за отсутствия common exact 10-minute begin timestamps.

## Можно ли торговать сейчас?
Нет. Даже положительный unit-corrected результат является кандидатом для следующей проверки, а не готовой торговой системой: нет bid/ask, стакана, очереди, partial fills, market impact и проверки исполнения календарной связки.

## Следующий шаг
Переходить на 1m/trades/order book можно и нужно. Проверять: bid/ask, реальный spread, очередь, partial fills, стакан, market impact, исполнение календарной связки, устойчивость в часы ликвидности и rollover.

## Artifact sync audit
- trades CSV: `passed`.
- summary CSV: `passed`.
- README: `passed`.
- same run artifacts: `passed`.
