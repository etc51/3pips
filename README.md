# MOEX Natural Gas NG/NGM Research

Воспроизводимый research-проект по фьючерсам MOEX Natural Gas:

- большой контракт `NG{month_code}{year_digit}`;
- микро-контракт `NR{month_code}{year_digit}` (`NGM` в названии инструмента MOEX);
- дневные и часовые свечи MOEX ISS;
- settlement price, volume, open interest из исторического ISS;
- current specs из ISS securities;
- continuous front-month / second-month series;
- календарные спреды `front_next`, `front_winter`, `summer_winter`;
- внешние факторы: Henry Hub, Brent, WTI через FRED, EIA storage через открытый WNGSR файл, USD/RUB через CBR;
- optional проверка T-Банк Invest API, если найден рабочий токен на Desktop или в env;
- тестирование сезонности, тренда, mean reversion, RSI/ATR/breakout, liquidity/OI, term structure, storage, oil, FX и комбинаций без look-ahead.

## Lead-lag NG 10m

Отдельный Python 3.11 пайплайн для проверки гипотезы: предсказывают ли дальние месячные контракты NG `+1`, `+2`, `+3` доходность ближнего/текущего контракта на горизонтах 10, 20 и 30 минут.

```powershell
python -m pip install -r requirements.txt
python src/leadlag_ng_moex.py --force
```

Источник: MOEX ISS candles endpoint
`https://iss.moex.com/iss/engines/futures/markets/forts/securities/{SECID}/candles.json`,
`interval=10`, `from=2024-05-23`, `till=2026-05-23`, pagination через `start` до пустого ответа.

Universe:

```text
NGK4 NGM4 NGN4 NGQ4 NGU4 NGV4 NGX4 NGZ4
NGF5 NGG5 NGH5 NGJ5 NGK5 NGM5 NGN5 NGQ5 NGU5 NGV5 NGX5 NGZ5
NGF6 NGG6 NGH6 NGJ6 NGK6 NGM6 NGN6 NGQ6
```

Методология:

- rolling target month: каждый месяц с `2024-05` по `2026-05`;
- predictors: `target+1`, `target+2`, `target+3`;
- выравнивание строго по `begin`, без forward-fill в основном тесте;
- фильтры: `volume > 0` для target и всех predictors, непрерывные 10-минутные интервалы, исключение последних 3 торговых дней перед последней наблюдаемой свечой target-контракта;
- признаки: `ret_front_10m`, `ret_front_20m`, `ret_front_30m`, `ret_plus1_lag0`, `ret_plus2_lag0`, `ret_plus3_lag0`, `spread_plus1`, `d_spread_plus1`;
- критично: predictor не берется из будущего; торговая симуляция использует сигнал на `close[t]`, вход на `open[t+1]`, выход через две свечи на `open[t+3]`.

Артефакты:

- `data/raw/leadlag_ng_10m/moex_ng_10m_candles.csv`
- `data/raw/leadlag_ng_10m/moex_ng_10m_candles.parquet`
- `data/processed/leadlag_ng_10m/rolling_mapping.csv`
- `data/processed/leadlag_ng_10m/aligned_panel.csv`
- `data/processed/leadlag_ng_10m/features.csv`
- `reports/leadlag_correlations.csv`
- `reports/regression_summary.csv`
- `reports/walkforward_results.csv`
- `reports/trade_simulation.csv`
- `plots/lag_correlation_heatmap.png`
- `reports/leadlag_findings.json`
- `reports/leadlag_readme_appendix.md`

После запуска `reports/leadlag_readme_appendix.md` явно фиксирует:

- есть ли статистически значимый lead на 20 минут;
- сохраняется ли эффект out-of-sample;
- не концентрируется ли эффект в одном месяце;
- проходит ли эффект после комиссий/спреда через параметр `--slippage-bps`;
- какие контракты были неликвидны или низколиквидны.

Результат последнего полного запуска на `2024-05-23` - `2026-05-23`:

- статистически значимый 20m lead найден в отдельных месяцах/термах: 5 HAC-significant коэффициентов при `p < 0.05`;
- walk-forward 20m в среднем положительный: mean signed return около `0.000165`, положительные 11 из 18 fold;
- торговая симуляция после roundtrip slippage `6 bps` не проходит: суммарный net log return около `-3.4529`, hit rate около `46.1%`;
- эффект после издержек не является устойчивым торговым edge; положительные месяцы есть только в 4 из 18 OOS target-month;
- контрактов с нулевыми nonzero-volume 10m свечами не найдено; низколиквидных по порогу `< 50` nonzero-volume свечей не найдено.

Второй проход запускается той же командой:

```powershell
python src/leadlag_ng_moex.py --force --horizons 10 20 30
```

Он добавляет 30m simulation, cost grid, train-only threshold walk-forward, feature ablation, multiple-testing correction, placebo test и диагностику 2025-08. Ключевой вывод второго прохода: 30m без издержек положителен, но no-threshold 30m после `6 bps` не проходит; train-only threshold walk-forward улучшает сильные сигналы, но это пока research-кандидат, а не готовая торговая система. Месяц `2025-08` выпал потому, что `NGQ5`, `NGU5`, `NGV5`, `NGX5` не имеют общих exact `begin` timestamp на 10-минутных свечах до основных фильтров.

Подробный русский отчет: `reports/leadlag_second_pass_summary.md`.

Unit-corrected third pass:

```powershell
python src/leadlag_ng_unit_corrected_third_pass.py --force
```

Этот слой не использует bps/log-return как финальный trading result. Сделки 30m пересчитываются через `min_step`, официальный `tick_value_usd=0.1`, USD/RUB, tick/RUB costs, ГО как denominator и margin constraint. Подробный отчет: `reports/unit_corrected_third_pass_summary_ru.md`. В последнем синхронном прогоне после исправления STEPPRICE currency bug лучший `global_no_overlap` режим при `2 ticks + 2 RUB fee` - `fixed_plus1_only`: net PnL около `22,912 RUB`, 14/20 положительных месяцев, max drawdown около `-4,114 RUB`; это все еще research-кандидат, не готовая система без проверки bid/ask/order book.

## Быстрый запуск

```powershell
python -m pip install -r requirements.txt
python src/run_research.py --from 2020-01-01 --till 2026-05-22
```

## Скринер

После запуска research-пайплайна можно построить ежедневный скринер активных сигналов:

```powershell
python src/screener.py --date latest --top 30
```

Скринер читает `data/processed/continuous_daily.csv`, `data/processed/calendar_spreads.csv` и историческую статистику из `results/top_robust_patterns.csv`. Он пересчитывает сигналы на последнюю доступную дату и сохраняет:

- `results/screener_latest.csv`
- `reports/screener_latest.md`

Полезные варианты:

```powershell
python src/screener.py --family NG --top 15
python src/screener.py --source full --min-sharpe 0.5 --max-p-adj 0.2
python src/screener.py --date 2026-05-21 --family NGM
```

## Paper-бот для NG scalping

Первый безопасный слой для проверки ручной идеи с коротким стопом и трейлингом:

```powershell
python src/ng_scalper_bot.py --paper-only --secid NGK6 --direction auto --qty 30 --stop-ticks 3 --trail-ticks 3 --max-attempts 5 --commission-side-rub 157.71
```

Вариант через живой stream T-Банк SDK:

```powershell
python src/ng_scalper_bot.py --paper-only --source tbank-stream --secid NGK6 --direction auto --qty 30 --stop-ticks 3 --trail-ticks 3 --max-attempts 5 --commission-side-rub 157.71
```

Что делает:

- берет живую цену и спецификацию `NGK6` через MOEX ISS;
- в `--source tbank-stream` берет сделки, стакан, last price и минутные свечи из stream T-Банк SDK;
- открывает бумажную позицию по направлению `long`, `short` или `auto`-фильтру;
- ставит стартовый стоп на `--stop-ticks`;
- подтягивает стоп за лучшей ценой на расстоянии `--trail-ticks`;
- считает gross/net PnL с комиссией за сторону;
- пишет закрытые сделки в `reports/ng_scalper_paper_trades.csv`.

Для ручной проверки направления:

```powershell
python src/ng_scalper_bot.py --paper-only --secid NGK6 --direction long --qty 30 --stop-ticks 3 --trail-ticks 3 --max-attempts 5 --commission-side-rub 157.71
python src/ng_scalper_bot.py --paper-only --secid NGK6 --direction short --qty 30 --stop-ticks 3 --trail-ticks 3 --max-attempts 5 --commission-side-rub 157.71
```

Это paper-режим без реальных заявок. Боевой T-Банк executor надо подключать отдельным шагом после сверки бумажного журнала с ручной логикой.

## Реалистичный portfolio-level backtest

Для проверки выбранных идей как торговых стратегий с ногами, ГО, сайзингом и risk management:

```powershell
python src/portfolio_backtest.py
```

Скрипт проверяет:

- `strategy_A_aug_front_next_short`
- `strategy_B_oct_nov_front_next_long`
- `strategy_C_nov_second_short`

Артефакты:

- `results/portfolio_strategy_trades.csv`
- `results/portfolio_strategy_equity.csv`
- `results/portfolio_strategy_summary.csv`
- `results/portfolio_strategy_sensitivity.csv`
- `reports/strategy_realistic_backtest_ru.md`

Основные артефакты:

- `data/raw/moex_history_daily.csv`
- `data/raw/moex_candles_24.csv`
- `data/raw/moex_candles_60.csv`
- `data/raw/moex_current_specs.csv`
- `data/raw/external_daily.csv`
- `data/processed/continuous_daily.csv`
- `data/processed/calendar_spreads.csv`
- `results/full_results.csv`
- `results/top_robust_patterns.csv`
- `results/rejected_patterns.csv`
- `results/equity_curves.csv`
- `results/drawdowns.csv`
- `results/figures/*.png`
- `reports/final_report_ru.md`

## Важные ограничения

MOEX ISS является основным источником для NG/NR, так как он отдает settlement price и open interest. CME/NYMEX settlements без платного/ключевого источника не гарантируются; пайплайн использует публичный Henry Hub spot FRED как базовый внешний газовый фактор и фиксирует ограничение в отчете.

EIA `api.eia.gov` требует API key для storage, поэтому проект использует открытые файлы Weekly Natural Gas Storage Report (`ir.eia.gov/ngs/ngshistory.xls` и fallback `wngsr.csv`).

T-Банк токен не сохраняется в проект. Paper-runtime принимает только явно заданный `TBANK_TOKEN_READONLY` и не читает Desktop fallback; исследовательские утилиты по-прежнему могут искать рабочий токен в env/Desktop и записывают только статус проверки.
