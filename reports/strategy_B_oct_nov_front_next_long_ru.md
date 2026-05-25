# strategy_B_oct_nov_front_next_long

Описание: October-November front_next long.
Позиция: long spread = buy front + sell next.
Инструмент: spread; spread=front_next; series=.

## Формула и ноги
`front_next = front - next`.
Для этой стратегии: buy first leg + sell second leg.

## Base Case Metrics
| family | period | total_return | CAGR | max_drawdown | Sharpe | Calmar | number_of_trades | win_rate | average_trade_rub | profit_factor | max_consecutive_losses | worst_trade_rub | average_margin_usage | liquidity_rejection_count |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| NG | all | -1.61% | -0.26% | -2.73% | -0.3555 | -0.0944 | 6 | 16.67% | -1341.6933 | 0.3555 | 5 | -4638.7521 | 0.52% | 0 |
| NG | recent_2024_2026 | -1.58% | -0.67% | -1.58% | -1.1809 | -0.4223 | 2 | 0.00% | -3954.2952 | 0.0000 | 2 | -4638.7521 | 0.18% | 0 |
| NG | test_2024_2026 | -1.58% | -0.67% | -1.58% | -1.1809 | -0.4223 | 2 | 0.00% | -3954.2952 | 0.0000 | 2 | -4638.7521 | 0.18% | 0 |
| NG | train_2020_2023 | -0.03% | -0.01% | -1.40% | -0.0051 | -0.0052 | 4 | 25.00% | -35.3924 | 0.9691 | 3 | -3016.7274 | 0.72% | 0 |
| NGM | all | -0.80% | -0.55% | -0.80% | -0.7700 | -0.6836 | 1 | 0.00% | -3987.7804 | 0.0000 | 1 | -3987.7804 | 0.59% | 0 |
| NGM | recent_2024_2026 | -0.80% | -0.55% | -0.80% | -0.7700 | -0.6836 | 1 | 0.00% | -3987.7804 | 0.0000 | 1 | -3987.7804 | 0.59% | 0 |
| NGM | test_2024_2026 | -0.80% | -0.55% | -0.80% | -0.7700 | -0.6836 | 1 | 0.00% | -3987.7804 | 0.0000 | 1 | -3987.7804 | 0.59% | 0 |
| NGM | train_2020_2023 | 0.00% | 0.00% |  |  |  | 0 |  |  |  | 0 |  | 0.00% | 0 |

## Passed Sensitivity Variants
| family | holding_days_param | stop_mode | stop_value | take_profit_r | slippage_bps | test_total_return | test_max_drawdown | test_trades | test_win_rate | test_profit_factor |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| NGM | 20 | fixed | 0.1500 | none | 10.0000 | 0.27% | -0.32% | 1 | 100.00% | inf |

## Base Case Trades
| family | entry_date | exit_date | exit_reason | qty | pnl_rub | entry_margin_usage | leg1_entry | leg2_entry | leg1_exit | leg2_exit |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| NG | 2020-10-02 00:00:00 | 2020-10-29 00:00:00 | contract_last_available | 3 | 4439.9329 | 7.65% | NGV0 | NGX0 | NGV0 | NGX0 |
| NG | 2021-10-04 00:00:00 | 2021-10-12 00:00:00 | atr_stop | 3 | -1223.6944 | 15.71% | NGV1 | NGX1 | NGV1 | NGX1 |
| NG | 2022-10-04 00:00:00 | 2022-10-10 00:00:00 | atr_stop | 3 | -3016.7274 | 14.39% | NGV2 | NGX2 | NGV2 | NGX2 |
| NG | 2023-10-03 00:00:00 | 2023-10-27 00:00:00 | contract_last_available | 5 | -341.0808 | 18.40% | NGV3 | NGX3 | NGV3 | NGX3 |
| NG | 2024-10-02 00:00:00 | 2024-10-07 00:00:00 | atr_stop | 5 | -4638.7521 | 17.44% | NGV4 | NGX4 | NGV4 | NGX4 |
| NG | 2025-10-02 00:00:00 | 2025-10-03 00:00:00 | atr_stop | 5 | -3269.8383 | 18.90% | NGV5 | NGX5 | NGV5 | NGX5 |
| NGM | 2025-10-02 00:00:00 | 2025-10-17 00:00:00 | atr_stop | 528 | -3987.7804 | 19.97% | NRV5 | NRX5 | NRV5 | NRX5 |

## Decision
Base case отклонен для реальной торговли: test 2024-2026 не положительный или сделок слишком мало.
В sensitivity есть положительные OOS-варианты, но они остаются кандидатами для наблюдения, а не готовой системой.