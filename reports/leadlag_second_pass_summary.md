# Второй проход MOEX NG lead-lag

Проверка расширяет первый проход без подбора на будущем: модель обучается только на месяцах строго раньше test-month, сигнал формируется на `close[t]`, вход идет на `open[t+1]`, выход для 10/20/30m идет на `open[t+2]`/`open[t+3]`/`open[t+4]`.

## Ответы
- Edge на 30m: лучший no-threshold вариант при 0 bps: plus1_only: net_mean=0.000419862, trades=7819, positive_months=17/21.
- 30m после 6 bps: `не проходит устойчиво`; лучший вариант: plus1_only: net_mean=-0.000180138, trades=7819, positive_months=6/21.
- Threshold walk-forward: суммарный 30m net_sum при 6 bps по всем feature sets/test-month = `1.59195`; порог выбирался только на train.
- Лучшие feature sets по threshold WF 6 bps см. `reports/feature_ablation_summary.csv`; no-threshold 20m: plus1_only: net_mean=-0.000299735, trades=9523, positive_months=7/21; no-threshold 30m: plus1_only: net_mean=-0.000180138, trades=7819, positive_months=6/21.
- Out-of-sample сохранен механически: все строки второго прохода используют expanding train, где `train_month < test_month`.
- Концентрация по одному месяцу проверяется в monthly агрегатах `threshold_walkforward_by_month.csv` и `trade_simulation_30m_by_month.csv`; один месяц не используется для выбора глобального порога.
- 2025-08: month absent because target and +1/+2/+3 contracts have no exact common 10-minute begin timestamps before filters. Детали в `reports/missing_month_diagnostics.csv`.

## Multiple testing
- 10m: raw=12, BH-FDR=0, Bonferroni=0, tests=144.
- 20m: raw=4, BH-FDR=0, Bonferroni=0, tests=144.
- 30m: raw=5, BH-FDR=0, Bonferroni=0, tests=144.

## Placebo
- 20m outrights_all cost=0: real=0.000214649, placebo_mean=7.33443e-07, p=0.0040.
- 20m outrights_all cost=6: real=-0.000385351, placebo_mean=-0.000603969, p=0.0040.
- 20m outrights_plus_spreads cost=0: real=0.000244081, placebo_mean=3.06189e-06, p=0.0020.
- 20m outrights_plus_spreads cost=6: real=-0.000355919, placebo_mean=-0.000601473, p=0.0020.
- 30m outrights_all cost=0: real=0.00038144, placebo_mean=-7.97207e-06, p=0.0020.
- 30m outrights_all cost=6: real=-0.00021856, placebo_mean=-0.000615064, p=0.0020.
- 30m outrights_plus_spreads cost=0: real=0.000352773, placebo_mean=-2.71809e-05, p=0.0020.
- 30m outrights_plus_spreads cost=6: real=-0.000247227, placebo_mean=-0.000624104, p=0.0020.

## Можно ли торговать сейчас?
Нет как готовую стратегию. Даже если часть статистики и OOS-сигналов положительна, текущая 10m candle-модель использует proxy-издержки и не проверяет фактический bid/ask, очередь, проскальзывание, стакан и исполнение календарных связок.

## Следующий шаг
Проверить 1m candles, trades, order book, bid/ask и реальное исполнение входа `open[t+1]`/выхода по горизонту. Также стоит отдельно проверить устойчивость в днях высокой ликвидности и около rollover без ручной оптимизации.