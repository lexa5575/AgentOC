# Manual Validation Batch 3 Decisions

Decision legend:
- `DROP_EXCLUDE` = исключить из eval-набора.
- `KEEP_MANUAL_CONFIRMED` = оставить как валидный кейс по ручному подтверждению пользователя.
- `KEEP_BUG_CASE` = оставить как баг-кейс для исправления prompt/logic.
- `PENDING_WORKFLOW` = валидный intent, но фактический ответ еще не сгенерирован workflow.

| ID | Situation | Status | Client Email | created_at | Thread ID | Decision | Note |
|---|---|---|---|---|---|---|---|
| 304 | other | no_thread_id | jillanov@gmail.com | 2026-03-03 20:09:34.322749 | - | DROP_EXCLUDE | Пользователь подтвердил: кейс от 2026-03-03 обработан вручную, невалиден для eval. |
| 317 | stock_question | no_thread_id | newcustomer@gmail.com | 2026-03-03 21:40:12.834677 | - | DROP_EXCLUDE | Пользователь подтвердил: кейс от 2026-03-03 обработан вручную, невалиден для eval. |
| 319 | stock_question | no_thread_id | airxbo@gmail.com | 2026-03-03 21:40:26.485394 | - | DROP_EXCLUDE | Пользователь подтвердил: кейс от 2026-03-03 обработан вручную, невалиден для eval. |
| 321 | stock_question | no_thread_id | dschmidt95@gmail.com | 2026-03-03 21:41:00.643425 | - | DROP_EXCLUDE | Пользователь подтвердил: кейс от 2026-03-03 обработан вручную, невалиден для eval. |
| 325 | stock_question | no_thread_id | teststock@example.com | 2026-03-03 21:50:46.370500 | - | DROP_EXCLUDE | Пользователь подтвердил: кейс от 2026-03-03 обработан вручную, невалиден для eval. |
| 347 | other | no_thread_id | gheorghet22@gmail.com | 2026-03-03 22:44:40.628965 | - | DROP_EXCLUDE | Пользователь подтвердил: кейс от 2026-03-03 обработан вручную, невалиден для eval. |
| 437 | stock_question | no_thread_id | xxx2thaz@gmail.com | 2026-03-05 03:21:55.963959 | - | KEEP_MANUAL_CONFIRMED | Пользователь подтвердил корректный flow (order -> payment instructions -> paid -> tracking script), оставить кейс. |
| 389 | other | ok | shanon_13@ymail.com | 2026-03-04 12:48:22.345233 | 19cb67e7db39cc7e | KEEP_MANUAL_CONFIRMED | Пользователь подтвердил: кейс корректный, поведение устраивает. |
| 402 | payment_question | ok | astromorgana.ma@gmail.com | 2026-03-04 15:53:27.240935 | 19b23628e81398ef | KEEP_MANUAL_CONFIRMED | Пользователь подтвердил: корректный flow (вопрос о цене -> ответ -> подтверждение оплаты -> tracking script). |
| 569 | other | ok | sean_kim@yahoo.com | 2026-03-06 17:24:37.633296 | 19afc6f03a51d8f4 | DROP_EXCLUDE | Пользователь подтвердил: плохой пример, исключить. |
| 603 | other | ok | ashleyelowe@yahoo.com | 2026-03-06 21:13:06.351794 | 19cb6a7bfeaf4366 | KEEP_BUG_CASE | Пользователь ожидает игнор сообщений со скрином оплаты без текста; текущий ответ считать нежелательным, использовать как bug-case. |
| 732 | payment_question | ok | avitoke@gmail.com | 2026-03-09 00:42:50.010778 | 193b25b39840e006 | KEEP_BUG_CASE | Пользователь отметил кейс как неправильный; ожидаемый ответ для таких ситуаций: шаблон с Zelle-реквизитами. |
| 959 | payment_received | ok | so.tene@yahoo.fr | 2026-03-13 14:23:00.269982 | 19ce6edb97a58903 | DROP_EXCLUDE | Пользователь подтвердил: плохой пример, исключить. |
| 1018 | payment_received | ok | jshin1003@gmail.com | 2026-03-15 15:10:51.668540 | 19a02b11d75533a6 | KEEP_MANUAL_CONFIRMED | Пользователь подтвердил кейс как показательный и по сути корректный (часть шагов выполнена вручную). |
| 1053 | payment_received | ok | elertaitek@gmail.com | 2026-03-16 11:49:33.913551 | 19b5aaee8cbc11ca | KEEP_MANUAL_CONFIRMED | Пользователь подтвердил: tilda кейс корректный, проблем нет. |
| 1079 | new_order | ok | georgiossammour@gmail.com | 2026-03-16 12:59:56.294596 | 19cf56e578eba2c3 | KEEP_MANUAL_CONFIRMED | Пользователь подтвердил: tilda кейс корректный, проблем нет. |
| 1125 | new_order | ok | bhardwaj.umang@gmail.com | 2026-03-17 15:00:55.150171 | 19cf9eb8b1ace935 | DROP_EXCLUDE | Пользователь склоняется к исключению сложного кейса (взято как решение вычеркнуть). |
| 1127 | new_order | ok | mar92a@yahoo.com | 2026-03-17 15:03:15.247047 | 19cfa0982696d191 | PENDING_WORKFLOW | Пользователь подтвердил логику, но ответ еще не обработан сегодня workflow; кейс pending. |
| 1129 | new_order | ok | sjc276@yahoo.com | 2026-03-17 15:07:31.123418 | 19cfb13e7a3076a5 | KEEP_MANUAL_CONFIRMED | Пользователь подтвердил: ответы корректные, часть ответа дана вручную. |
| 1141 | new_order | ok | email2kevin@gmail.com | 2026-03-17 16:03:27.673637 | 19cfc635fa90c96e | KEEP_MANUAL_CONFIRMED | Пользователь подтвердил: кейс корректный, претензий нет. |