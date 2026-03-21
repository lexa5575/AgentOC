# Manual Validation Batch 1 Decisions (User Confirmed)

Legend:
- `KEEP_NO_REPLY` = оставить кейс, для этого шага expected outbound пустой.
- `DROP_EXCLUDE` = исключить кейс из eval-набора.
- `PENDING_WORKFLOW` = кейс валиден по intent, но фактический ответ не был сгенерирован, т.к. workflow ещё не запускался.
- `MANUAL_OVERRIDE` = кейс обработан вручную вне стандартного workflow; не использовать как эталон для prompt eval.

| ID | Decision | Notes from user |
|---|---|---|
| 1154 | PENDING_WORKFLOW | Клиенту не успели ответить; ожидаемый драфт после оплаты: "Thank you very much... We received your payment..." + tracking + address + fulfillment marks. |
| 1131 | PENDING_WORKFLOW | Та же причина: сообщения пришли сегодня, workflow еще не запускался. |
| 1145 | PENDING_WORKFLOW | Та же причина: workflow не запускался. |
| 574 | MANUAL_OVERRIDE | Нестандартно обработан вручную (tracking до оплаты), workflow не запускался. |
| 808 | DROP_EXCLUDE | Исключить пример. |
| 871 | KEEP_NO_REPLY | Пользователь подтверждает корректную структуру потока; кейс считать валидным. |
| 666 | DROP_EXCLUDE | Клиент шлет скрин оплаты без текста; пользователь предлагает удалить пример. |
| 424 | DROP_EXCLUDE | Рекламная рассылка, игнор. |
| 286 | KEEP_NO_REPLY | Ответы в таком треде пользователь предпочитает игнорировать и отвечать в основном шаблоне заказа. |
| 281 | DROP_EXCLUDE | Рекламная рассылка. |
| 329 | DROP_EXCLUDE | Пользователь не понимает кейс и просит вычеркнуть; старый test/sim пример до запуска workflow. |
| 346 | DROP_EXCLUDE | Пользователь подтвердил: старый ошибочно попавший кейс до запуска workflow, удалить. |
| 343 | DROP_EXCLUDE | Пользователь подтвердил: старый ошибочно попавший кейс до запуска workflow, удалить. |
| 337 | DROP_EXCLUDE | Пользователь подтвердил: старый ошибочно попавший кейс до запуска workflow, удалить. |

Dates for dropped 4 cases (`created_at` from dataset):
- `329`: 2026-03-03 21:51:23 (client: dschmidt95@gmail.com, thread: test-drew-oos-thread)
- `346`: 2026-03-03 22:42:53 (client: gheorghet22@gmail.com, thread: test-gheorghe-fresh-001)
- `343`: 2026-03-03 22:36:38 (client: jillanov@gmail.com, thread: sim-order-23561-thread-test2)
- `337`: 2026-03-03 22:03:35 (client: jillanov@gmail.com, thread: sim-thread-23561)
