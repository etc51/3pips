# Реалистичный portfolio-level backtest MOEX NG/NGM

## Исправление методологии
Старые equity curves из `results/equity_curves.csv` не используются для выводов по этим стратегиям: они были построены как средняя доходность сигналов и могли компаундировать перекрывающиеся 20-дневные сигналы. Новый тест ведет портфель по дням, разрешает не более одной открытой позиции на стратегию, входит только на следующей дневной свече после появления сигнала и считает PnL по ногам.

## Формулы спредов
- `front_next = front - next`. Long spread: buy front + sell next. Short spread: sell front + buy next.
- `summer_winter = summer - winter`. Long spread: buy summer + sell winter. Short spread: sell summer + buy winter.
- `front_winter = front - winter`. Long spread: buy front + sell winter. Short spread: sell front + buy winter.

Эти формулы проверены по коду построения `data/processed/calendar_spreads.csv`: поле `price` считается как первая указанная нога минус вторая указанная нога.

## Допущения исполнения
- Initial capital: 500,000 RUB; risk per trade: 0.50%; max margin usage: 20%.
- PnL считается по отдельным ногам и дневному `USD/RUB`: `daily_pnl = side * delta_price * lotvolume * USD/RUB * contracts`.
- `lotvolume`: NG = 100, NR/NGM = 1. Историческое ГО оценивается консервативно как 30% notional по каждой ноге без межмесячного offset.
- Комиссия: 3 bps per side; slippage в sensitivity: 5/10/20 bps per side.
- Liquidity filter по каждой ноге: volume >= 50.0, trades >= 5.0, open interest >= 100.0. Bid/ask в ISS history отсутствует, поэтому slippage bps используется как proxy.
- Stop-loss проверяется по дневному settlement спреда/контракта. Intraday пересечение по стакану не моделируется, потому что в текущем архиве нет синхронного bid/ask/last по обеим ногам.

## Точные ноги стратегий
- A `strategy_A_aug_front_next_short`: `front_next = front - next`, short spread = sell front + buy next, вход на следующей дневной свече после первого августовского сигнала.
- B `strategy_B_oct_nov_front_next_long`: `front_next = front - next`, long spread = buy front + sell next, вход на следующей дневной свече после старта окна Oct-Nov.
- C `strategy_C_nov_second_short`: outright short второго месячного контракта, вход на следующей дневной свече после первого ноябрьского сигнала.

## Base case
Base case: holding_days=20, ATR stop=1.5 ATR, take-profit отсутствует, slippage=10 bps.
| strategy_id | family | period | total_return | CAGR | max_drawdown | Sharpe | Calmar | number_of_trades | win_rate | average_trade_rub | profit_factor | max_consecutive_losses | worst_trade_rub | average_margin_usage | liquidity_rejection_count |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| strategy_A_aug_front_next_short | NG | all | -1.43% | -0.23% | -1.77% | -0.3800 | -0.1298 | 6 | 16.67% | -1195.7252 | 0.4131 | 4 | -3742.0731 | 0.68% | 0 |
| strategy_A_aug_front_next_short | NG | recent_2024_2026 | -0.95% | -0.40% | -0.95% | -1.3996 | -0.4215 | 2 | 0.00% | -2365.9137 | 0.0000 | 2 | -2524.8112 | 0.18% | 0 |
| strategy_A_aug_front_next_short | NG | test_2024_2026 | -0.95% | -0.40% | -0.95% | -1.3996 | -0.4215 | 2 | 0.00% | -2365.9137 | 0.0000 | 2 | -2524.8112 | 0.18% | 0 |
| strategy_A_aug_front_next_short | NG | train_2020_2023 | -0.49% | -0.13% | -0.84% | -0.1687 | -0.1490 | 4 | 25.00% | -610.6309 | 0.6740 | 2 | -3742.0731 | 0.99% | 0 |
| strategy_A_aug_front_next_short | NGM | all | -0.52% | -0.35% | -0.52% | -1.1948 | -0.6833 | 1 | 0.00% | -2575.4682 | 0.0000 | 1 | -2575.4682 | 0.16% | 0 |
| strategy_A_aug_front_next_short | NGM | recent_2024_2026 | -0.52% | -0.35% | -0.52% | -1.1948 | -0.6833 | 1 | 0.00% | -2575.4682 | 0.0000 | 1 | -2575.4682 | 0.16% | 0 |
| strategy_A_aug_front_next_short | NGM | test_2024_2026 | -0.52% | -0.35% | -0.52% | -1.1948 | -0.6833 | 1 | 0.00% | -2575.4682 | 0.0000 | 1 | -2575.4682 | 0.16% | 0 |
| strategy_A_aug_front_next_short | NGM | train_2020_2023 | 0.00% | 0.00% |  |  |  | 0 |  |  |  | 0 |  | 0.00% | 0 |
| strategy_B_oct_nov_front_next_long | NG | all | -1.61% | -0.26% | -2.73% | -0.3555 | -0.0944 | 6 | 16.67% | -1341.6933 | 0.3555 | 5 | -4638.7521 | 0.52% | 0 |
| strategy_B_oct_nov_front_next_long | NG | recent_2024_2026 | -1.58% | -0.67% | -1.58% | -1.1809 | -0.4223 | 2 | 0.00% | -3954.2952 | 0.0000 | 2 | -4638.7521 | 0.18% | 0 |
| strategy_B_oct_nov_front_next_long | NG | test_2024_2026 | -1.58% | -0.67% | -1.58% | -1.1809 | -0.4223 | 2 | 0.00% | -3954.2952 | 0.0000 | 2 | -4638.7521 | 0.18% | 0 |
| strategy_B_oct_nov_front_next_long | NG | train_2020_2023 | -0.03% | -0.01% | -1.40% | -0.0051 | -0.0052 | 4 | 25.00% | -35.3924 | 0.9691 | 3 | -3016.7274 | 0.72% | 0 |
| strategy_B_oct_nov_front_next_long | NGM | all | -0.80% | -0.55% | -0.80% | -0.7700 | -0.6836 | 1 | 0.00% | -3987.7804 | 0.0000 | 1 | -3987.7804 | 0.59% | 0 |
| strategy_B_oct_nov_front_next_long | NGM | recent_2024_2026 | -0.80% | -0.55% | -0.80% | -0.7700 | -0.6836 | 1 | 0.00% | -3987.7804 | 0.0000 | 1 | -3987.7804 | 0.59% | 0 |
| strategy_B_oct_nov_front_next_long | NGM | test_2024_2026 | -0.80% | -0.55% | -0.80% | -0.7700 | -0.6836 | 1 | 0.00% | -3987.7804 | 0.0000 | 1 | -3987.7804 | 0.59% | 0 |
| strategy_B_oct_nov_front_next_long | NGM | train_2020_2023 | 0.00% | 0.00% |  |  |  | 0 |  |  |  | 0 |  | 0.00% | 0 |
| strategy_C_nov_second_short | NG | all | 3.03% | 0.48% | -2.20% | 0.3938 | 0.2161 | 5 | 40.00% | 3029.0738 | 2.2553 | 2 | -4900.1543 | 0.14% | 0 |
| strategy_C_nov_second_short | NG | recent_2024_2026 | -1.43% | -0.60% | -1.43% | -1.0203 | -0.4221 | 2 | 0.00% | -3582.5070 | 0.0000 | 2 | -4452.9355 | 0.05% | 0 |
| strategy_C_nov_second_short | NG | test_2024_2026 | -1.43% | -0.60% | -1.43% | -1.0203 | -0.4221 | 2 | 0.00% | -3582.5070 | 0.0000 | 2 | -4452.9355 | 0.05% | 0 |
| strategy_C_nov_second_short | NG | train_2020_2023 | 4.46% | 1.13% | -2.20% | 0.7593 | 0.5116 | 3 | 66.67% | 7436.7943 | 5.5530 | 1 | -4900.1543 | 0.20% | 0 |
| strategy_C_nov_second_short | NGM | all | -0.59% | -0.41% | -0.59% | -0.8519 | -0.6834 | 1 | 0.00% | -2965.4221 | 0.0000 | 1 | -2965.4221 | 0.03% | 0 |
| strategy_C_nov_second_short | NGM | recent_2024_2026 | -0.59% | -0.41% | -0.59% | -0.8519 | -0.6834 | 1 | 0.00% | -2965.4221 | 0.0000 | 1 | -2965.4221 | 0.03% | 0 |
| strategy_C_nov_second_short | NGM | test_2024_2026 | -0.59% | -0.41% | -0.59% | -0.8519 | -0.6834 | 1 | 0.00% | -2965.4221 | 0.0000 | 1 | -2965.4221 | 0.03% | 0 |
| strategy_C_nov_second_short | NGM | train_2020_2023 | 0.00% | 0.00% |  |  |  | 0 |  |  |  | 0 |  | 0.00% | 0 |

## Прошло out-of-sample
Критерий: test 2024-2026 положительный, есть сделки, max drawdown не хуже заданного лимита.
Нет строк.

## Отклонено в base case
| strategy_id | family | total_return | max_drawdown | Sharpe | number_of_trades | win_rate | average_trade_rub | profit_factor | liquidity_rejection_count |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| strategy_A_aug_front_next_short | NG | -0.95% | -0.95% | -1.3996 | 2 | 0.00% | -2365.9137 | 0.0000 | 0 |
| strategy_A_aug_front_next_short | NGM | -0.52% | -0.52% | -1.1948 | 1 | 0.00% | -2575.4682 | 0.0000 | 0 |
| strategy_B_oct_nov_front_next_long | NG | -1.58% | -1.58% | -1.1809 | 2 | 0.00% | -3954.2952 | 0.0000 | 0 |
| strategy_B_oct_nov_front_next_long | NGM | -0.80% | -0.80% | -0.7700 | 1 | 0.00% | -3987.7804 | 0.0000 | 0 |
| strategy_C_nov_second_short | NG | -1.43% | -1.43% | -1.0203 | 2 | 0.00% | -3582.5070 | 0.0000 | 0 |
| strategy_C_nov_second_short | NGM | -0.59% | -0.59% | -0.8519 | 1 | 0.00% | -2965.4221 | 0.0000 | 0 |

## Sensitivity: только варианты, прошедшие test-period
| strategy_id | family | holding_days_param | stop_mode | stop_value | take_profit_r | slippage_bps | test_total_return | test_max_drawdown | test_trades | test_win_rate | test_profit_factor |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| strategy_A_aug_front_next_short | NG | 20 | atr | 2.0000 | none | 5.0000 | 0.69% | -0.43% | 2 | 50.00% | 2.8101 |
| strategy_A_aug_front_next_short | NG | 20 | atr | 2.0000 | 2.0000 | 5.0000 | 0.69% | -0.43% | 2 | 50.00% | 2.8101 |
| strategy_A_aug_front_next_short | NG | 20 | atr | 2.0000 | none | 10.0000 | 0.57% | -0.44% | 2 | 50.00% | 2.2867 |
| strategy_A_aug_front_next_short | NG | 20 | atr | 2.0000 | 2.0000 | 10.0000 | 0.57% | -0.44% | 2 | 50.00% | 2.2867 |
| strategy_A_aug_front_next_short | NG | 20 | atr | 2.0000 | none | 20.0000 | 0.32% | -0.55% | 2 | 50.00% | 1.5713 |
| strategy_A_aug_front_next_short | NG | 20 | atr | 2.0000 | 2.0000 | 20.0000 | 0.32% | -0.55% | 2 | 50.00% | 1.5713 |
| strategy_B_oct_nov_front_next_long | NGM | 20 | fixed | 0.1500 | none | 10.0000 | 0.27% | -0.32% | 1 | 100.00% | inf |
| strategy_A_aug_front_next_short | NG | 20 | fixed | 0.1500 | none | 10.0000 | 0.23% | -0.12% | 2 | 100.00% | inf |
| strategy_A_aug_front_next_short | NG | 15 | atr | 2.0000 | none | 5.0000 | 0.16% | -0.43% | 2 | 50.00% | 1.4243 |
| strategy_A_aug_front_next_short | NG | 15 | atr | 2.0000 | 1.0000 | 5.0000 | 0.16% | -0.43% | 2 | 50.00% | 1.4243 |
| strategy_A_aug_front_next_short | NG | 15 | atr | 2.0000 | 2.0000 | 5.0000 | 0.16% | -0.43% | 2 | 50.00% | 1.4243 |
| strategy_A_aug_front_next_short | NG | 20 | atr | 2.0000 | 1.0000 | 5.0000 | 0.16% | -0.43% | 2 | 50.00% | 1.4243 |
| strategy_A_aug_front_next_short | NGM | 20 | fixed | 0.1500 | none | 10.0000 | 0.10% | -0.13% | 1 | 100.00% | inf |
| strategy_A_aug_front_next_short | NG | 15 | atr | 2.0000 | none | 10.0000 | 0.04% | -0.44% | 2 | 50.00% | 1.0823 |
| strategy_A_aug_front_next_short | NG | 15 | atr | 2.0000 | 1.0000 | 10.0000 | 0.04% | -0.44% | 2 | 50.00% | 1.0823 |
| strategy_A_aug_front_next_short | NG | 15 | atr | 2.0000 | 2.0000 | 10.0000 | 0.04% | -0.44% | 2 | 50.00% | 1.0823 |
| strategy_A_aug_front_next_short | NG | 20 | atr | 2.0000 | 1.0000 | 10.0000 | 0.04% | -0.44% | 2 | 50.00% | 1.0823 |

## Файлы
- `results/portfolio_strategy_trades.csv`
- `results/portfolio_strategy_equity.csv`
- `results/portfolio_strategy_summary.csv`
- `results/portfolio_strategy_sensitivity.csv`
- `reports/strategy_A_aug_front_next_short_ru.md`
- `reports/strategy_B_oct_nov_front_next_long_ru.md`
- `reports/strategy_C_nov_second_short_ru.md`

## Сделки base case
| strategy_id | family | entry_date | exit_date | exit_reason | qty | pnl_rub | entry_margin_usage | leg1_entry | leg2_entry | leg1_exit | leg2_exit |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| strategy_A_aug_front_next_short | NG | 2020-08-04 00:00:00 | 2020-08-24 00:00:00 | atr_stop | 10 | -3742.0731 | 19.79% | NGQ0 | NGU0 | NGQ0 | NGU0 |
| strategy_A_aug_front_next_short | NG | 2021-08-03 00:00:00 | 2021-08-27 00:00:00 | contract_last_available | 5 | 5048.9413 | 17.63% | NGQ1 | NGU1 | NGQ1 | NGU1 |
| strategy_A_aug_front_next_short | NG | 2022-08-02 00:00:00 | 2022-08-29 00:00:00 | contract_last_available | 2 | -265.2284 | 11.81% | NGQ2 | NGU2 | NGQ2 | NGU2 |
| strategy_A_aug_front_next_short | NG | 2023-08-02 00:00:00 | 2023-08-07 00:00:00 | atr_stop | 7 | -3484.1633 | 19.86% | NGQ3 | NGU3 | NGQ3 | NGU3 |
| strategy_A_aug_front_next_short | NG | 2024-08-02 00:00:00 | 2024-08-06 00:00:00 | atr_stop | 9 | -2524.8112 | 19.16% | NGQ4 | NGU4 | NGQ4 | NGU4 |
| strategy_A_aug_front_next_short | NG | 2025-08-04 00:00:00 | 2025-08-06 00:00:00 | atr_stop | 6 | -2207.0163 | 17.48% | NGQ5 | NGU5 | NGQ5 | NGU5 |
| strategy_A_aug_front_next_short | NGM | 2025-08-04 00:00:00 | 2025-08-06 00:00:00 | atr_stop | 685 | -2575.4682 | 20.00% | NRQ5 | NRU5 | NRQ5 | NRU5 |
| strategy_B_oct_nov_front_next_long | NG | 2020-10-02 00:00:00 | 2020-10-29 00:00:00 | contract_last_available | 3 | 4439.9329 | 7.65% | NGV0 | NGX0 | NGV0 | NGX0 |
| strategy_B_oct_nov_front_next_long | NG | 2021-10-04 00:00:00 | 2021-10-12 00:00:00 | atr_stop | 3 | -1223.6944 | 15.71% | NGV1 | NGX1 | NGV1 | NGX1 |
| strategy_B_oct_nov_front_next_long | NG | 2022-10-04 00:00:00 | 2022-10-10 00:00:00 | atr_stop | 3 | -3016.7274 | 14.39% | NGV2 | NGX2 | NGV2 | NGX2 |
| strategy_B_oct_nov_front_next_long | NG | 2023-10-03 00:00:00 | 2023-10-27 00:00:00 | contract_last_available | 5 | -341.0808 | 18.40% | NGV3 | NGX3 | NGV3 | NGX3 |
| strategy_B_oct_nov_front_next_long | NG | 2024-10-02 00:00:00 | 2024-10-07 00:00:00 | atr_stop | 5 | -4638.7521 | 17.44% | NGV4 | NGX4 | NGV4 | NGX4 |
| strategy_B_oct_nov_front_next_long | NG | 2025-10-02 00:00:00 | 2025-10-03 00:00:00 | atr_stop | 5 | -3269.8383 | 18.90% | NGV5 | NGX5 | NGV5 | NGX5 |
| strategy_B_oct_nov_front_next_long | NGM | 2025-10-02 00:00:00 | 2025-10-17 00:00:00 | atr_stop | 528 | -3987.7804 | 19.97% | NRV5 | NRX5 | NRV5 | NRX5 |
| strategy_C_nov_second_short | NG | 2020-11-03 00:00:00 | 2020-12-02 00:00:00 | time_stop | 4 | 10583.3723 | 6.22% | NGZ0 |  | NGZ0 |  |
| strategy_C_nov_second_short | NG | 2022-11-02 00:00:00 | 2022-11-07 00:00:00 | atr_stop | 1 | -4900.1543 | 2.44% | NGZ2 |  | NGZ2 |  |
| strategy_C_nov_second_short | NG | 2023-11-02 00:00:00 | 2023-11-30 00:00:00 | time_stop | 2 | 16627.1650 | 4.21% | NGZ3 |  | NGZ3 |  |
| strategy_C_nov_second_short | NG | 2024-11-02 00:00:00 | 2024-11-11 00:00:00 | atr_stop | 2 | -4452.9355 | 3.42% | NGZ4 |  | NGZ4 |  |
| strategy_C_nov_second_short | NG | 2025-11-03 00:00:00 | 2025-11-05 00:00:00 | atr_stop | 2 | -2712.0786 | 4.27% | NGZ5 |  | NGZ5 |  |
| strategy_C_nov_second_short | NGM | 2025-11-03 00:00:00 | 2025-11-05 00:00:00 | atr_stop | 220 | -2965.4221 | 4.70% | NRZ5 |  | NRZ5 |  |

## Вывод
Стратегии, прошедшие OOS, можно рассматривать только как кандидаты для наблюдения и дальнейшей проверки на intraday bid/ask и точном архиве ГО. Для реального счета текущий слой еще недостаточен: нет исторического стакана, точного межмесячного margin offset и фактической исполнимости стопов внутри дня.