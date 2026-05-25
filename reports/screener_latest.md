# NG/NGM screener

As of: 2026-05-21
Source: robust; filters: n_trades >= 20, Sharpe >= 0.0, q <= 0.1

## Regime
| family | secid | price | second_price | curve_state | curve_front_next_pct | basis_to_spot | henry_hub_spot | volume | open_interest | dte |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| NG | NGK6 | 3.0510 | 3.2050 | contango | 0.0505 | -0.0062 | 3.0700 | 787844.0000 | 347994.0000 | 6 |
| NGM | NRK6 | 3.0510 | 3.2060 | contango | 0.0508 | -0.0062 | 3.0700 | 968890.0000 | 1256792.0000 | 6 |

## Active Signals
| family | instrument_type | series | spread | secid | front_secid | back_secid | action | pattern | holding_days | score | ann_sharpe | hit_rate | n_trades | p_adj_bh | price | dte |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| NG | spread |  | summer_winter |  | NGK6 | NGX6 | SHORT | season_window_03_3m_short | 20 | 11.2032 | 3.6429 | 0.7861 | 187 | 0.0000 | -1.2440 | 6 |
| NGM | spread |  | summer_winter |  | NRK6 | NRX6 | SHORT | season_window_02_4m_short | 20 | 11.1574 | 3.0747 | 0.6410 | 78 | 0.0000 | -1.1890 | 6 |
| NGM | spread |  | summer_winter |  | NRK6 | NRX6 | SHORT | season_window_02_5m_short | 20 | 11.1574 | 3.0747 | 0.6410 | 78 | 0.0000 | -1.1890 | 6 |
| NGM | spread |  | summer_winter |  | NRK6 | NRX6 | SHORT | season_window_02_6m_short | 20 | 11.1574 | 3.0747 | 0.6410 | 78 | 0.0000 | -1.1890 | 6 |
| NG | spread |  | summer_winter |  | NGK6 | NGX6 | SHORT | season_window_02_4m_short | 20 | 7.6148 | 2.5393 | 0.6827 | 312 | 0.0000 | -1.2440 | 6 |
| NG | spread |  | summer_winter |  | NGK6 | NGX6 | SHORT | season_window_03_4m_short | 20 | 7.3280 | 2.3911 | 0.6517 | 290 | 0.0000 | -1.2440 | 6 |
| NG | spread |  | summer_winter |  | NGK6 | NGX6 | SHORT | season_window_03_3m_short | 10 | 6.9360 | 2.9362 | 0.5888 | 197 | 0.0000 | -1.2440 | 6 |
| NG | spread |  | summer_winter |  | NGK6 | NGX6 | SHORT | season_window_02_5m_short | 20 | 5.8058 | 2.0033 | 0.6145 | 415 | 0.0000 | -1.2440 | 6 |
| NGM | spread |  | front_next |  | NRK6 | NRM6 | LONG | season_window_05_2m_long | 20 | 5.3159 | 3.6685 | 0.9000 | 40 | 0.0000 | -0.1550 | 6 |
| NG | spread |  | summer_winter |  | NGK6 | NGX6 | SHORT | season_window_03_4m_short | 10 | 5.2431 | 2.3417 | 0.5433 | 300 | 0.0000 | -1.2440 | 6 |
| NGM | outright | second |  | NRM6 |  |  | SHORT | season_window_05_3m_short | 20 | 4.8459 | 3.1853 | 0.8254 | 63 | 0.0000 | 3.2060 | 36 |
| NG | spread |  | summer_winter |  | NGK6 | NGX6 | SHORT | season_window_02_4m_short | 10 | 4.6352 | 2.1054 | 0.5280 | 322 | 0.0000 | -1.2440 | 6 |
| NG | spread |  | summer_winter |  | NGK6 | NGX6 | SHORT | season_window_03_5m_short | 20 | 4.2497 | 1.5776 | 0.5865 | 399 | 0.0000 | -1.2440 | 6 |
| NG | outright | front |  | NGK6 |  |  | LONG | season_window_04_2m_long | 20 | 3.8850 | 2.0965 | 0.7803 | 264 | 0.0000 | 3.0510 | 6 |
| NG | spread |  | summer_winter |  | NGK6 | NGX6 | SHORT | season_window_02_5m_short | 10 | 3.8729 | 1.8650 | 0.5106 | 425 | 0.0000 | -1.2440 | 6 |
| NGM | spread |  | front_next |  | NRK6 | NRM6 | SHORT | season_window_01_5m_short | 20 | 3.7506 | 1.6503 | 0.8315 | 178 | 0.0000 | -0.1550 | 6 |
| NG | spread |  | summer_winter |  | NGK6 | NGX6 | SHORT | season_window_02_6m_short | 20 | 3.6935 | 1.4534 | 0.5725 | 524 | 0.0000 | -1.2440 | 6 |
| NG | outright | front |  | NGK6 |  |  | LONG | season_window_04_2m_long | 10 | 3.5507 | 2.3767 | 0.6788 | 274 | 0.0000 | 3.0510 | 6 |
| NG | spread |  | summer_winter |  | NGK6 | NGX6 | SHORT | season_window_03_5m_short | 10 | 3.3401 | 1.6329 | 0.5183 | 409 | 0.0000 | -1.2440 | 6 |
| NG | outright | second |  | NGM6 |  |  | LONG | season_window_04_2m_long | 20 | 3.3374 | 1.8239 | 0.6894 | 264 | 0.0000 | 3.2050 | 36 |
| NG | outright | second |  | NGM6 |  |  | LONG | season_window_04_2m_long | 10 | 3.2865 | 2.2008 | 0.6679 | 274 | 0.0000 | 3.2050 | 36 |
| NGM | spread |  | front_next |  | NRK6 | NRM6 | SHORT | season_window_01_6m_short | 20 | 3.2824 | 1.5025 | 0.7677 | 198 | 0.0000 | -0.1550 | 6 |
| NG | spread |  | summer_winter |  | NGK6 | NGX6 | SHORT | season_window_01_5m_short | 20 | 3.2708 | 1.2567 | 0.5625 | 432 | 0.0000 | -1.2440 | 6 |
| NGM | outright | front |  | NRK6 |  |  | SHORT | brent_mom_20_meanrev_short | 20 | 3.2092 | 1.7549 | 0.7372 | 156 | 0.0000 | 3.0510 | 6 |
| NG | spread |  | summer_winter |  | NGK6 | NGX6 | SHORT | season_window_03_6m_short | 20 | 2.8965 | 1.2532 | 0.5332 | 512 | 0.0000 | -1.2440 | 6 |
| NGM | outright | front |  | NRK6 |  |  | SHORT | season_window_03_5m_short | 20 | 2.8439 | 1.8241 | 0.6853 | 143 | 0.0000 | 3.0510 | 6 |
| NG | outright | second |  | NGM6 |  |  | LONG | season_window_04_6m_long | 20 | 2.8111 | 1.6182 | 0.6829 | 779 | 0.0000 | 3.2050 | 36 |
| NG | spread |  | summer_winter |  | NGK6 | NGX6 | SHORT | season_window_01_6m_short | 20 | 2.7587 | 1.0956 | 0.5327 | 535 | 0.0000 | -1.2440 | 6 |
| NG | outright | second |  | NGM6 |  |  | LONG | season_window_04_5m_long | 20 | 2.6849 | 1.5307 | 0.6667 | 651 | 0.0000 | 3.2050 | 36 |
| NG | spread |  | summer_winter |  | NGK6 | NGX6 | SHORT | season_window_02_6m_short | 10 | 2.6645 | 1.3818 | 0.4981 | 534 | 0.0000 | -1.2440 | 6 |

Note: signals are generated after the latest close and match the backtest convention of entering on the next trading day.