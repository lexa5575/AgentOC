# Manual Validation Batch 2 Decisions

Rule applied from user:
- `created_at` < `2026-03-03 00:00:00` -> `DROP_EXCLUDE`.

Decision legend:
- `DROP_EXCLUDE` = исключить из eval-набора.
- `KEEP_MANUAL_CONFIRMED` = оставить как валидный кейс по ручному подтверждению пользователя (без Gmail-thread верификации).
- `KEEP_BUG_CASE` = оставить как баг-кейс: текущее поведение неверное, используем для фикса промптов/логики.

| ID | Situation | Client Email | created_at | Decision | Note |
|---|---|---|---|---|---|
| 64 | discount_request | zylicz@gmail.com | 2026-02-28 18:41:15.177991 | DROP_EXCLUDE | Невалидный кейс: дата раньше 2026-03-03 (до запуска/валидного периода workflow). |
| 66 | shipping_timeline | sibelulku1223@gmail.com | 2026-02-28 21:09:28.809501 | DROP_EXCLUDE | Невалидный кейс: дата раньше 2026-03-03 (до запуска/валидного периода workflow). |
| 82 | discount_request | dschmidt95@gmail.com | 2026-03-01 07:39:12.409338 | DROP_EXCLUDE | Невалидный кейс: дата раньше 2026-03-03 (до запуска/валидного периода workflow). |
| 84 | discount_request | dschmidt95@gmail.com | 2026-03-01 07:40:15.914151 | DROP_EXCLUDE | Невалидный кейс: дата раньше 2026-03-03 (до запуска/валидного периода workflow). |
| 98 | shipping_timeline | client2@example.com | 2026-03-01 15:35:09.954045 | DROP_EXCLUDE | Невалидный кейс: дата раньше 2026-03-03 (до запуска/валидного периода workflow). |
| 111 | shipping_timeline | client2@example.com | 2026-03-01 15:43:19.805383 | DROP_EXCLUDE | Невалидный кейс: дата раньше 2026-03-03 (до запуска/валидного периода workflow). |
| 122 | shipping_timeline | client2@example.com | 2026-03-01 15:51:32.680967 | DROP_EXCLUDE | Невалидный кейс: дата раньше 2026-03-03 (до запуска/валидного периода workflow). |
| 133 | shipping_timeline | client2@example.com | 2026-03-01 15:54:42.140007 | DROP_EXCLUDE | Невалидный кейс: дата раньше 2026-03-03 (до запуска/валидного периода workflow). |
| 148 | shipping_timeline | client2@example.com | 2026-03-01 17:26:09.530548 | DROP_EXCLUDE | Невалидный кейс: дата раньше 2026-03-03 (до запуска/валидного периода workflow). |
| 159 | shipping_timeline | client2@example.com | 2026-03-01 17:36:10.432075 | DROP_EXCLUDE | Невалидный кейс: дата раньше 2026-03-03 (до запуска/валидного периода workflow). |
| 172 | shipping_timeline | client2@example.com | 2026-03-01 17:40:33.490138 | DROP_EXCLUDE | Невалидный кейс: дата раньше 2026-03-03 (до запуска/валидного периода workflow). |
| 201 | shipping_timeline | sibelulku1223@gmail.com | 2026-03-02 04:43:06.885990 | DROP_EXCLUDE | Невалидный кейс: дата раньше 2026-03-03 (до запуска/валидного периода workflow). |
| 296 | price_question | astromorgana.ma@gmail.com | 2026-03-03 19:46:18.129892 | KEEP_BUG_CASE | Подтверждено пользователем: фактическое поведение было неправильным; ответ ушел вручную. Ожидаемое поведение automation: отправить скрипт с предложениями замены (substitution script). |
| 300 | payment_question | alcoztrk@gmail.com | 2026-03-03 19:57:35.434155 | KEEP_MANUAL_CONFIRMED | Подтверждено пользователем: клиент сообщил, что получил посылку и затем оплатил; корректный следующий шаг — благодарность за оплату. Кейс валидный по логике, но без thread_id/Gmail-верификации. |
| 302 | payment_question | alcoztrk@gmail.com | 2026-03-03 20:02:15.007416 | KEEP_MANUAL_CONFIRMED | Подтверждено пользователем: продолжение того же потока оплаты; после подтверждения платежа корректный ответ — thank-you за оплату. Кейс валидный по логике, но без thread_id/Gmail-верификации. |