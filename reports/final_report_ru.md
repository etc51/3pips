# Research-проект MOEX Natural Gas futures NG/NGM

Дата запуска: 2026-05-22 17:46 UTC. Период данных: 2020-01-01 - 2026-05-22.

## Executive summary

- Найдено контрактов в MOEX history: 108.
- Дневных строк MOEX history: 12,460. Дневных candle-строк: 11,600. Часовых candle-строк: 147,968.
- Continuous rows: 3,934. Calendar spread rows: 4,219.
- Проверено pattern/holding/instrument комбинаций: 16,568. Robust-кандидатов после BH-FDR и bootstrap/walk-forward: 1,587; в top CSV экспортировано: 300. Rejected: 14,981.
- T-Банк token check: working (file:Yu1j-fuZQi.txt).

## Методология без look-ahead

- Сигналы строятся только на данных текущей или прошлой даты.
- Вход выполняется на следующий торговый день через `signal.shift(1)`.
- Доходность считается на горизонтах 1/2/3/5/10/20 торговых дней.
- Для outright используется процентная доходность, для календарных спредов - изменение спреда в пунктах.
- Transaction cost = 3.0 bps, slippage = 5.0 bps.
- Минимальный liquidity filter: volume >= 1.0, trades >= 1.
- Walk-forward score считается на последовательных временных блоках; multiple testing корректируется Benjamini-Hochberg.
- Bootstrap CI строится по сделочным доходностям. Robust требует положительный mean, CI-low > 0, walk-forward test > 0 и BH q <= 10%.

## Покрытие контрактов

| family   |   contracts | first_trade         | last_trade          |   total_volume |
|:---------|------------:|:--------------------|:--------------------|---------------:|
| NG       |          83 | 2020-02-03 00:00:00 | 2026-05-21 00:00:00 |    1.63585e+09 |
| NGM      |          25 | 2024-12-02 00:00:00 | 2026-05-21 00:00:00 |    9.04252e+08 |

## Top robust patterns

| family   | series   | spread        | instrument_type   | pattern                   |   holding_days |   n_trades |   mean_return |   ann_sharpe |    p_adj_bh |   bootstrap_ci_low |   bootstrap_ci_high |   walk_test_mean |   max_drawdown |
|:---------|:---------|:--------------|:------------------|:--------------------------|---------------:|-----------:|--------------:|-------------:|------------:|-------------------:|--------------------:|-----------------:|---------------:|
| NG       | nan      | front_next    | spread            | season_window_10_2m_long  |             20 |        257 |     0.242811  |      4.47808 | 4.48615e-51 |          0.219375  |           0.265068  |        0.272738  |      -0.781069 |
| NG       | nan      | summer_winter | spread            | month_Mar_short           |             20 |        129 |     0.882786  |      6.55255 | 8.84551e-40 |          0.792059  |           0.966321  |        0.94752   |      -0.45207  |
| NG       | nan      | summer_winter | spread            | season_window_03_1m_short |             20 |        129 |     0.882786  |      6.55255 | 8.84551e-40 |          0.792059  |           0.966321  |        0.94752   |      -0.45207  |
| NG       | nan      | summer_winter | spread            | season_window_03_2m_short |             20 |        137 |     0.835695  |      5.90763 | 6.54144e-38 |          0.746116  |           0.93264   |        0.902837  |      -0.45207  |
| NG       | nan      | front_next    | spread            | season_window_10_3m_long  |             20 |        388 |     0.204246  |      2.6685  | 1.90371e-36 |          0.181688  |           0.231194  |        0.232106  |      -2.31641  |
| NG       | nan      | front_next    | spread            | season_window_07_3m_short |             20 |        392 |     0.113226  |      2.55627 | 2.48149e-34 |          0.0960283 |           0.127753  |        0.124564  |      -3.46519  |
| NG       | nan      | front_next    | spread            | season_window_07_2m_short |             20 |        264 |     0.110568  |      3.15348 | 3.87491e-32 |          0.0951639 |           0.125294  |        0.0952687 |      -1.3013   |
| NG       | second   | nan           | outright          | season_window_04_6m_long  |             20 |        779 |     0.0679137 |      1.61821 | 8.19723e-31 |          0.0569546 |           0.0779105 |        0.0559885 |      -0.99764  |
| NG       | nan      | summer_winter | spread            | season_window_02_3m_short |             20 |        262 |     0.483764  |      3.02441 | 6.24408e-30 |          0.419902  |           0.552433  |        0.52961   |      -3.58268  |
| NG       | nan      | summer_winter | spread            | season_window_02_2m_short |             20 |        254 |     0.496596  |      3.08408 | 6.42538e-30 |          0.429928  |           0.571719  |        0.540547  |      -3.58268  |
| NG       | second   | nan           | outright          | season_window_07_3m_long  |             20 |        392 |     0.0942105 |      2.30434 | 6.34219e-29 |          0.0811505 |           0.108836  |        0.0665313 |      -0.997701 |
| NG       | second   | nan           | outright          | injection_long            |             20 |        911 |     0.0587402 |      1.41709 | 2.62592e-28 |          0.0506155 |           0.0681159 |        0.0506872 |      -0.998684 |
| NG       | nan      | summer_winter | spread            | season_window_03_3m_short |             20 |        187 |     0.613066  |      3.64289 | 3.51004e-28 |          0.51518   |           0.705189  |        0.693124  |      -3.13965  |
| NG       | nan      | front_next    | spread            | month_Nov_long            |             20 |        125 |     0.266021  |      4.81779 | 2.40077e-27 |          0.24042   |           0.300381  |        0.318762  |      -0.781069 |
| NG       | nan      | front_next    | spread            | season_window_11_1m_long  |             20 |        125 |     0.266021  |      4.81779 | 2.40077e-27 |          0.24042   |           0.300381  |        0.318762  |      -0.781069 |
| NG       | nan      | summer_winter | spread            | season_window_02_4m_short |             20 |        312 |     0.406728  |      2.53934 | 3.90565e-27 |          0.352413  |           0.476531  |        0.46213   |      -4.57046  |
| NG       | front    | nan           | outright          | injection_long            |             20 |        911 |     0.0635275 |      1.37309 | 9.00025e-27 |          0.0545097 |           0.0733378 |        0.0550641 |      -0.999763 |
| NG       | nan      | front_next    | spread            | season_window_08_2m_short |             20 |        260 |     0.139277  |      2.76232 | 6.78327e-26 |          0.117525  |           0.160513  |        0.157145  |      -3.46519  |
| NG       | second   | nan           | outright          | month_Nov_short           |             20 |        125 |     0.142427  |      4.56748 | 1.33826e-25 |          0.124203  |           0.157829  |        0.132296  |      -0.226285 |
| NG       | second   | nan           | outright          | season_window_11_1m_short |             20 |        125 |     0.142427  |      4.56748 | 1.33826e-25 |          0.124203  |           0.157829  |        0.132296  |      -0.226285 |
| NGM      | nan      | front_next    | spread            | season_window_02_2m_short |             20 |         82 |     0.0979298 |      6.29842 | 2.258e-24   |          0.0864741 |           0.10952   |        0.107479  |      -0.061184 |
| NG       | nan      | summer_winter | spread            | season_window_02_5m_short |             20 |        415 |     0.308595  |      2.00329 | 3.29317e-24 |          0.256824  |           0.358867  |        0.356295  |      -7.49821  |
| NG       | nan      | front_next    | spread            | month_Oct_long            |             20 |        132 |     0.220831  |      4.18828 | 3.39989e-24 |          0.189126  |           0.251464  |        0.229988  |      -0.59768  |
| NG       | nan      | front_next    | spread            | season_window_10_1m_long  |             20 |        132 |     0.220831  |      4.18828 | 3.39989e-24 |          0.189126  |           0.251464  |        0.229988  |      -0.59768  |
| NG       | second   | nan           | outright          | season_window_07_4m_long  |             20 |        524 |     0.0716376 |      1.75259 | 3.78725e-24 |          0.0596654 |           0.0838641 |        0.048368  |      -0.998684 |
| NG       | second   | nan           | outright          | season_window_04_5m_long  |             20 |        651 |     0.0649284 |      1.53068 | 1.81704e-23 |          0.0536097 |           0.076666  |        0.053729  |      -0.995567 |
| NG       | nan      | summer_winter | spread            | season_window_03_4m_short |             20 |        290 |     0.399349  |      2.39113 | 6.39336e-23 |          0.323752  |           0.461847  |        0.458182  |      -5.93295  |
| NG       | nan      | front_next    | spread            | month_Aug_short           |             20 |        132 |     0.159224  |      4.009   | 7.48386e-23 |          0.135791  |           0.185156  |        0.130552  |      -0.246092 |
| NG       | nan      | front_next    | spread            | season_window_08_1m_short |             20 |        132 |     0.159224  |      4.009   | 7.48386e-23 |          0.135791  |           0.185156  |        0.130552  |      -0.246092 |
| NG       | second   | nan           | outright          | season_window_03_6m_long  |             20 |        800 |     0.0559731 |      1.34124 | 1.3264e-22  |          0.0461481 |           0.0649367 |        0.0463746 |      -0.999579 |

## Rejected patterns sample

| family   | series   | spread        | instrument_type   | pattern                   |   holding_days |   n_trades |   mean_return |    p_adj_bh |
|:---------|:---------|:--------------|:------------------|:--------------------------|---------------:|-----------:|--------------:|------------:|
| NG       | nan      | front_next    | spread            | season_window_10_2m_short |             20 |        257 |    -0.24929   | 9.34883e-53 |
| NG       | nan      | summer_winter | spread            | month_Mar_long            |             20 |        129 |    -0.887679  | 7.05092e-40 |
| NG       | nan      | summer_winter | spread            | season_window_03_1m_long  |             20 |        129 |    -0.887679  | 7.05092e-40 |
| NG       | nan      | front_next    | spread            | season_window_10_3m_short |             20 |        388 |    -0.210574  | 3.21432e-38 |
| NG       | nan      | summer_winter | spread            | season_window_03_2m_long  |             20 |        137 |    -0.840495  | 3.9659e-38  |
| NG       | nan      | front_next    | spread            | season_window_07_3m_long  |             20 |        392 |    -0.119239  | 3.28536e-37 |
| NG       | nan      | front_next    | spread            | season_window_07_2m_long  |             20 |        264 |    -0.116424  | 5.50218e-35 |
| NG       | second   | nan           | outright          | season_window_04_6m_short |             20 |        779 |    -0.0695137 | 3.81773e-32 |
| NG       | nan      | summer_winter | spread            | season_window_02_3m_long  |             20 |        262 |    -0.488648  | 2.16533e-30 |
| NG       | nan      | summer_winter | spread            | season_window_02_2m_long  |             20 |        254 |    -0.50153   | 2.2592e-30  |
| NG       | second   | nan           | outright          | injection_short           |             20 |        911 |    -0.0603402 | 9.3152e-30  |
| NG       | second   | nan           | outright          | season_window_07_3m_short |             20 |        392 |    -0.0958105 | 9.3152e-30  |
| NG       | nan      | summer_winter | spread            | season_window_03_3m_long  |             20 |        187 |    -0.617992  | 1.84792e-28 |
| NG       | nan      | front_next    | spread            | month_Nov_short           |             20 |        125 |    -0.272715  | 3.85502e-28 |
| NG       | nan      | front_next    | spread            | season_window_11_1m_short |             20 |        125 |    -0.272715  | 3.85502e-28 |
| NG       | front    | nan           | outright          | injection_short           |             20 |        911 |    -0.0651275 | 5.22327e-28 |
| NG       | nan      | summer_winter | spread            | season_window_02_4m_long  |             20 |        312 |    -0.411675  | 1.23544e-27 |
| NG       | nan      | front_next    | spread            | season_window_08_2m_long  |             20 |        260 |    -0.14553   | 1.41397e-27 |
| NG       | second   | nan           | outright          | month_Nov_long            |             20 |        125 |    -0.144027  | 6.1886e-26  |
| NG       | second   | nan           | outright          | season_window_11_1m_long  |             20 |        125 |    -0.144027  | 6.1886e-26  |
| NGM      | nan      | front_next    | spread            | season_window_02_2m_long  |             20 |         82 |    -0.103558  | 7.06526e-26 |
| NG       | nan      | front_next    | spread            | month_Oct_short           |             20 |        132 |    -0.227108  | 2.97462e-25 |
| NG       | nan      | front_next    | spread            | season_window_10_1m_short |             20 |        132 |    -0.227108  | 2.97462e-25 |
| NG       | second   | nan           | outright          | season_window_07_4m_short |             20 |        524 |    -0.0732376 | 4.37358e-25 |
| NG       | nan      | summer_winter | spread            | season_window_02_5m_long  |             20 |        415 |    -0.313742  | 6.83629e-25 |
| NG       | second   | nan           | outright          | season_window_04_5m_short |             20 |        651 |    -0.0665284 | 1.71711e-24 |
| NG       | nan      | front_next    | spread            | season_window_10_3m_short |             10 |        388 |    -0.105968  | 2.03153e-24 |
| NG       | nan      | front_next    | spread            | month_Aug_long            |             20 |        132 |    -0.165397  | 3.83474e-24 |
| NG       | nan      | front_next    | spread            | season_window_08_1m_long  |             20 |        132 |    -0.165397  | 3.83474e-24 |
| NG       | second   | nan           | outright          | season_window_03_6m_short |             20 |        800 |    -0.0575731 | 8.53043e-24 |
| NG       | nan      | summer_winter | spread            | season_window_03_4m_long  |             20 |        290 |    -0.404568  | 2.17085e-23 |
| NG       | nan      | front_next    | spread            | season_window_06_4m_long  |             20 |        515 |    -0.0800358 | 3.14564e-23 |
| NGM      | nan      | front_next    | spread            | season_window_02_3m_long  |             20 |        119 |    -0.102168  | 3.14564e-23 |
| NG       | second   | nan           | outright          | season_window_08_2m_short |             20 |        260 |    -0.0984013 | 4.91999e-23 |
| NG       | front    | nan           | outright          | season_window_04_6m_short |             20 |        779 |    -0.0623032 | 8.70085e-23 |
| NG       | second   | nan           | outright          | season_window_07_2m_short |             20 |        264 |    -0.101199  | 1.55921e-21 |
| NG       | second   | nan           | outright          | season_window_11_2m_long  |             20 |        256 |    -0.107479  | 2.04161e-21 |
| NG       | nan      | front_next    | spread            | season_window_11_2m_short |             10 |        256 |    -0.120935  | 5.05989e-20 |
| NGM      | nan      | front_next    | spread            | season_window_10_2m_short |             20 |         43 |    -0.459727  | 1.11855e-19 |
| NG       | nan      | front_next    | spread            | season_window_11_2m_short |             20 |        256 |    -0.202048  | 1.6084e-19  |
| NG       | second   | nan           | outright          | season_window_06_4m_short |             20 |        515 |    -0.0683645 | 2.51583e-19 |
| NG       | nan      | front_next    | spread            | season_window_09_4m_short |             20 |        516 |    -0.128891  | 3.65801e-19 |
| NG       | second   | nan           | outright          | season_window_04_6m_short |             10 |        789 |    -0.0375428 | 7.36482e-19 |
| NG       | second   | nan           | outright          | month_Apr_short           |             20 |        145 |    -0.124241  | 8.26283e-19 |
| NG       | second   | nan           | outright          | season_window_04_1m_short |             20 |        145 |    -0.124241  | 8.26283e-19 |
| NG       | front    | nan           | outright          | season_window_04_5m_short |             20 |        651 |    -0.0602492 | 1.61286e-18 |
| NG       | nan      | front_next    | spread            | season_window_10_2m_short |             10 |        257 |    -0.118687  | 2.9618e-18  |
| NG       | second   | nan           | outright          | season_window_05_5m_short |             20 |        634 |    -0.0569971 | 3.10875e-18 |
| NG       | nan      | summer_winter | spread            | season_window_02_6m_long  |             20 |        524 |    -0.223209  | 5.9137e-18  |
| NG       | nan      | front_next    | spread            | month_Feb_long            |             20 |        136 |    -0.0800569 | 6.83192e-18 |

## Артефакты

- `data\raw\moex_history_daily.csv`
- `data\raw\moex_candles_24.csv`
- `data\raw\moex_candles_60.csv`
- `data\raw\moex_current_specs.csv`
- `data\raw\external_daily.csv`
- `data\processed\contract_summary.csv`
- `data\processed\continuous_daily.csv`
- `data\processed\calendar_spreads.csv`
- `results\full_results.csv`
- `results\top_robust_patterns.csv`
- `results\rejected_patterns.csv`
- `results\equity_curves.csv`
- `results\drawdowns.csv`

## Источники

- MOEX ISS securities: https://iss.moex.com/iss/engines/futures/markets/forts/securities
- MOEX ISS candles: https://iss.moex.com/iss/engines/futures/markets/forts/boards/RFUD/securities/{SECID}/candles.json
- MOEX ISS history fallback: https://iss.moex.com/iss/history/engines/futures/markets/forts/boards/RFUD/securities/{SECID}.json
- FRED Henry Hub: https://fred.stlouisfed.org/series/DHHNGSP
- FRED WTI: https://fred.stlouisfed.org/series/DCOILWTICO
- FRED Brent: https://fred.stlouisfed.org/series/DCOILBRENTEU
- EIA Weekly Natural Gas Storage Report: https://ir.eia.gov/ngs/ngs.html
- CBR USD/RUB XML: https://www.cbr.ru/scripts/XML_dynamic.asp
- T-Банк Invest API: использован только для проверки доступности токена, токен не сохранялся.

## Ограничения

- CME/NYMEX settlement history не включена как отдельный официальный ряд, если нет публичного стабильного источника без ключа/подписки. Внешний газовый фактор представлен Henry Hub spot FRED.
- Исторические спецификации MOEX восстановлены из ISS history/current securities и фактических first/last trade dates; полноценный архив всех изменений спецификаций может требовать отдельного архива биржевых документов.
- EIA storage берется из открытого WNGSR файла. Если EIA меняет структуру Excel, parser использует permissive fallback и сохраняет исходный результат в `data/raw/eia_storage_weekly.csv`.