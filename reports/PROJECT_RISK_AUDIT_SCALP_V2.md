# Scalp Strategy V2 Risk Audit

Дата: 2026-05-24

## Главный вывод

Старые облачные прогоны нельзя считать финальной валидацией стратегии. В них комиссия и slippage учитывались как стоимость сделки, но не было жесткого live-feasibility фильтра до оптимизации:

`round_trip_fee_ticks <= stop_ticks * max_fee_to_stop_ratio`

Из-за этого в рекомендации могли попасть профили, которые исторически выглядят прибыльно, но live-бот обязан их блокировать через fee_filter.

## Найденные бомбы

1. Fee/stop viability не был hard filter.
   - Старое поведение: профиль мог быть выбран, если PnL после комиссии положительный.
   - Правильное поведение: профиль не должен даже попадать в рейтинг, если комиссия кругом слишком велика относительно стопа.

2. Backtest не моделировал live order book gate.
   - В live появился запрет `book_filter empty_book`.
   - Старые свечные тесты могли принимать входы, где стакан в реальности пустой, нулевой или не подтверждает объем.

3. Дублирование strict/aggressive по одному тикеру.
   - Старый live мог открыть один и тот же тикер двумя внутренними режимами.
   - Исправлено: один тикер = одна открытая позиция внутри внешнего контура.

4. Свечная модель оптимистична по исполнению.
   - Внутри 1m свечи неизвестен порядок high/low.
   - Stop/trail по high/low может быть лучше, чем реальное исполнение.
   - V2 должен считать pessimistic intrabar: если в одной свече возможны и стоп, и favorable move, считать худший порядок.

5. Spread не был hard gate.
   - Slippage ticks не заменяет проверку текущего spread.
   - V2 должен запрещать профили, где типичный spread_ticks слишком большой относительно stop/trail/expected move.

6. Комиссия считалась оценочно.
   - Для T-Bank Premium нужна консервативная модель: `round_turn_fee_rub = max(2 * notional_rub * 0.00025, observed_or_min_fee)`.
   - Для NG ранее была ручная проверка факта: около 5.257 руб/контракт/side.
   - V2 должен явно выводить fee_ticks и fee_to_stop_ratio по каждому профилю.

7. Текущая стоимость тика и ГО применяются к истории.
   - Для historical family contracts это приближение.
   - V2 должен помечать `CURRENT_SPEC_ON_HISTORY_WARNING`, если используются текущие specs для старых контрактов.

8. Volume candle != executable liquidity.
   - Малый объем не надо автоматически выкидывать, но он должен влиять на sizing и risk class.
   - V2 должен отдельно считать zero-volume share, median 1m volume, trade_count_per_day и microstructure risk.

9. Capital/sizing не должен быть критерием edge.
   - 200 000 руб на контур - это настройка текущего paper-бота для удобного наблюдения.
   - V2 не должен отбирать стратегию под конкретный размер счета.
   - Вместо этого считать 1-contract PnL, return_on_margin и overlap/margin-normalized метрики.

10. Neo/perp нельзя смешивать с классическими MOEX futures.
    - У них другая ликвидность/режим/специфика.
    - V2 должен держать Neo отдельно.

## Уже исправлено в live

- Startup fee/stop preflight: тикер не загружается в stream, если не проходит `round_trip_fee_ticks <= stop_ticks * 0.55`.
- Запрет дубля по тикеру внутри внешнего контура.
- Запрет входа при пустом/нулевом стакане.
- Виртуальный капитал 200 000 руб на каждый внешний контур оставлен только в live paper-боте.
- Сайзинг по ГО в paper-боте нужен для наблюдения, но не является правилом отбора стратегии.

## Что считать заново

Пересчитать all-futures universe с V2 realistic classification.

Важно: fee/stop, spread/stop и ликвидность не должны удалять тикер из исследования на входе. Они должны классифицировать профиль:

- `LIVE_NOW`: подходит текущему live-боту.
- `LIVE_NOW_WIDE_CANDIDATE`: широкая версия выглядит пригодной для paper.
- `NEEDS_WIDE_PROFILE`: tight-версия не подходит, надо искать wide.
- `NEEDS_MICROSTRUCTURE_VALIDATION`: свечная история есть, но стакан/спред не подтверждены.
- `RESEARCH_ONLY`: интересно, но не для текущего paper.
- `NO_EDGE`: edge не найден.
- `REJECT_PATHOLOGICAL`: плохие данные/спеки/невозможная математика.

Новый расчет должен включать:

- fee/stop viability как классификацию, не ранний запрет
- отдельный wide-profile rescue для дорогих по комиссии/спреду тикеров
- spread/stop viability как классификацию
- pessimistic intrabar execution
- train/test/walk-forward
- outlier removal
- neighborhood stability
- overlap/margin-normalized simulation без фиксированного капитала
- separate MOEX / Neo outputs
