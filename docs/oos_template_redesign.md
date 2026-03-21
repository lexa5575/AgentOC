# OOS Template Redesign — Plan

## Дата: 2026-03-21
## Статус: Approved, ready for implementation

---

## 1. Проблема

Когда клиент делает заказ и товаров нет в наличии (OOS), система генерирует ответ через Python-шаблон `fill_out_of_stock_template`. Текущий шаблон работает, но выдаёт некрасивые и неудобные ответы.

### Реальный пример (Order 23685, клиент Ruta Helterbrand)

Клиент заказал: 1 x Amber EU, 2 x Yellow EU, 1 x Silver EU ($440).
Все три позиции — OOS. Для всех трёх есть идентичный товар в ME-регионе.

**Что выдал текущий шаблон:**
```
Hi!
How are you?
Unfortunately, we just ran out of Terea Amber EU, Terea Yellow EU and Terea Silver EU

What can we offer? Please choose one of the options below.
1. We have alternatives:
   For Terea Amber EU: Terea Amber ME (same product, different region), Terea Silver ME, Terea Sof Fuse EU
   For Terea Yellow EU: Terea Yellow ME (same product, different region), Terea Summer ME, Terea Purple made in Japan
   For Terea Silver EU: Terea Silver ME (same product, different region), Terea Beige ME, Terea TEAK ME
2. Check our website for substitutions and ready to ship sticks.

Link for the sticks substitution
https://shipmecarton.com

Please let us know what you think
```

### Конкретные проблемы

1. **Избыточность**: Для каждого вкуса расписаны 3 альтернативы по отдельности, хотя достаточно было сказать "Amber ME, Yellow ME, Silver ME — same product, different region".

2. **Потеряны количества**: Клиент заказал 2 x Yellow EU, но в альтернативах просто "Yellow ME" без количества. `ordered_qty` есть в данных, но шаблон его не использует.

3. **Дублирование альтернатив**: Silver ME предложен и как замена для Amber EU (альтернатива #2), и как same_flavor для Silver EU. `excluded_products` в pipeline работает для product_name, но same_flavor проходит отдельным путём.

4. **"Same product, different region" повторяется** для каждого вкуса, хотя это одна мысль.

5. **Шаблон не масштабируется**: Чем больше OOS items в заказе, тем длиннее и запутаннее ответ. Для 5 OOS items будет 15 строк альтернатив.

6. **Нет персонализации**: Generic "Hi! How are you?" для returning customer с 9 заказами.

---

## 2. Исследование: что делает индустрия

Провели исследование best practices (Amazon, Walmart, Nordstrom, Adidas, Recharge/Klaviyo, + e-commerce блоги и case studies).

### Ключевые находки

- **Все крупные компании используют шаблоны, не LLM.** OOS-уведомления — транзакционные письма, где точность критична. Один fashion-бренд попробовал LLM — фрустрация клиентов выросла на 31% из-за рекомендаций несуществующих товаров.

- **1-2 альтернативы на OOS item, не больше.** Больше — choice overload (подтверждено исследованиями UX). При множественных OOS — 1 лучшая альтернатива на каждый.

- **Индустрия практически не имеет готовых паттернов для multi-item OOS** с альтернативами в одном заказе. Это наш специфический кейс (табачный магазин, ограниченный ассортимент, частые OOS по регионам).

- **Рекомендуемый подход**: шаблон для структуры + "умная" подстановка для содержания. Интеллект — в выборе альтернатив, не в тексте письма.

### Почему не полностью LLM

Мы протестировали отправку этого кейса на LLM (gpt-5.2). Результат:

```
Hi Ruta — good to see you back! Quick heads up: the EU-made Amber/Yellow/Silver
are currently out of stock, but we can swap you to the ME (Armenia) versions,
which are the same product (same taste/quality) just a different region label.
For your order, that would be Amber ME x1, Yellow ME x2, and Silver ME x1.
Want me to switch it over and get it moving? Thank you!
```

Ответ хороший, но:
- LLM не предназначена для этой задачи в нашей архитектуре
- Риск галлюцинаций в названиях/количествах при масштабировании
- Дороже и медленнее чем шаблон
- Индустрия подтверждает: шаблоны надёжнее для транзакционных писем

---

## 3. Решение: гибридный подход

### Архитектура

Шаблон остаётся **фиксированной рамкой** (Python, 0 токенов). Внутри шаблона есть одно динамическое место — строка с альтернативами, которую заполняет **маленькая специализированная LLM**.

```
Hi!                                                    ← Python (фикс)
How are you?                                           ← Python (фикс)
Unfortunately, we just ran out of {OOS_ITEMS}          ← Python (данные)

What can we offer? Please choose one of the options below.  ← Python (фикс)
1. {LLM_FORMATTED_ALTERNATIVES}                        ← LLM-форматтер
2. Check our website for substitutions and ready to ship sticks.  ← Python (фикс)

Link for the sticks substitution                       ← Python (фикс)
https://shipmecarton.com                               ← Python (фикс)

Please let us know what you think                      ← Python (фикс)
```

### Почему гибрид, а не чистый шаблон

Попытка покрыть все комбинации чистым Python-шаблоном приводит к комбинаторному взрыву:
- 1 OOS item vs 2+ OOS items — разное количество альтернатив
- Все same_flavor vs микс vs все разные — разный формат
- С количествами vs без — зависит от числа OOS items
- "(same product, different region)" — куда ставить, к каждому или один раз?

Каждая комбинация требует отдельной ветки в Python. LLM-форматтер решает это естественно — она получает структурированные данные и форматирует их в 1-3 строки.

### Почему это безопасно

- **LLM НЕ придумывает товары** — она получает уже отобранные альтернативы от `select_best_alternatives`
- **LLM НЕ выбирает альтернативы** — выбор остаётся за существующей системой (same_flavor priority → LLM alternatives agent → fallback)
- **LLM только форматирует** — задача: "вот данные, красиво уложи в 1-3 строки"
- **Модель**: gpt-4.1-mini достаточно (200-300 токенов, ~1 сек)
- **Шаблон валидирует**: если LLM вернёт пустую строку — fallback на текущий формат

---

## 4. Правила форматирования

### 4.1. Строка {OOS_ITEMS} (Python)

Названия товаров которых нет, **без количеств**, через запятую, последний через "and":
```
Terea Amber EU, Terea Yellow EU and Terea Silver EU
```

### 4.2. Блок {LLM_FORMATTED_ALTERNATIVES} (LLM-форматтер)

#### Входные данные для LLM

Для каждого OOS item:
- `display_name` — клиентское название ("Terea Amber EU")
- `ordered_qty` — сколько заказал клиент
- `alternatives` — список из 1-3 альтернатив, каждая с:
  - `display_name` — клиентское название альтернативы ("Terea Amber ME")
  - `reason` — почему предложена: `same_flavor` / `llm` / `history` / `fallback`

#### Правила по количеству альтернатив

| Сколько OOS items | Сколько альтернатив на каждый |
|-------------------|------------------------------|
| 1 | до 3 |
| 2+ | 1 (лучшая) |

Логика: при 1 OOS — есть место показать варианты. При 2+ — по одной, чтобы не перегружать.

#### Правила по количествам товара

| Сколько OOS items | Указывать quantity? |
|-------------------|-------------------|
| 1 | Нет (и так понятно) |
| 2+ | Да (`1 x Terea Amber ME, 2 x Terea Yellow ME`) |

#### Правила по "(same product, different region)"

- Если альтернатива имеет `reason=same_flavor` → добавить "(same product, different region)"
- Если ВСЕ альтернативы same_flavor → приписка один раз в конце строки
- Если МИКС → приписка только к same_flavor группе

#### Примеры ожидаемого вывода LLM

**Пример 1: 1 OOS item**
Вход: Amber EU (qty 1), alternatives: [Amber ME (same_flavor), Silver ME (llm), Sof Fuse EU (llm)]
```
We have alternatives: Terea Amber ME (same product, different region), Terea Silver ME, Terea Sof Fuse EU
```

**Пример 2: 3 OOS items, все same_flavor**
Вход: Amber EU (qty 1) → Amber ME, Yellow EU (qty 2) → Yellow ME, Silver EU (qty 1) → Silver ME
```
We have alternatives: 1 x Terea Amber ME, 2 x Terea Yellow ME, 1 x Terea Silver ME (same product, different region)
```

**Пример 3: 3 OOS items, микс**
Вход: Amber EU (qty 1) → Amber ME (same_flavor), Yellow EU (qty 2) → Yellow ME (same_flavor), Mauve EU (qty 1) → Purple Japan (llm)
```
We have alternatives:
   1 x Terea Amber ME, 2 x Terea Yellow ME (same product, different region)
   For Terea Mauve EU: 1 x Terea Purple Japan
```

**Пример 4: 2 OOS items, нет same_flavor**
Вход: Mauve EU (qty 1) → Purple Japan (llm), Russet EU (qty 3) → Bronze ME (llm)
```
We have alternatives:
   For Terea Mauve EU: 1 x Terea Purple Japan
   For Terea Russet EU: 3 x Terea Bronze ME
```

### 4.3. Mixed availability (часть заказа в наличии, часть нет)

Один и тот же шаблон. Упоминаем **только OOS items**. Товары которые есть — не упоминаем. Клиент поймёт: если товар не в списке проблемных, значит он в порядке.

Текущий `fill_mixed_availability_template` (с "We have reserved for you" + A/B choice) **заменяется** этим единым шаблоном.

---

## 5. Что меняется в коде

### Новое

- `agents/oos_formatter.py` — маленький LLM-агент (gpt-4.1-mini), единственная задача: получить структурированные данные об OOS items и альтернативах, вернуть 1-3 строки отформатированного текста.

### Изменяется

- `agents/reply_templates.py` → `fill_out_of_stock_template()` — вместо Python-форматирования вызывает LLM-форматтер для блока альтернатив, остальная рамка остаётся Python.
- `agents/handlers/new_order.py` → убрать разделение на `fill_out_of_stock_template` / `fill_mixed_availability_template`, использовать один шаблон.

### Не меняется

- `select_best_alternatives` — выбор альтернатив остаётся как есть
- `alternatives.py` — LLM-агент для подбора альтернатив остаётся как есть
- Pipeline routing — остаётся как есть
- Шаблоны с Zelle/ценами/tracking — остаются как есть
- `oos_agrees` / `oos_declines` шаблоны — остаются как есть

---

## 6. Сопутствующие баги (обнаружены при анализе, отдельные задачи)

### Баг 1: Backfill записывает незавершённые OOS-заказы

**Проблема**: Pipeline правильно пропускает `save_order_items` при `decision_required`. Но через 5 сек `_backfill_order_items` парсит Gmail, находит тот же заказ, и записывает его в `client_order_items`. Guard в backfill проверяет только `get_client_flavor_history` — для новых клиентов история пуста, backfill срабатывает.

**Последствие**: `get_last_order()` вернёт неподтверждённый OOS-заказ. Если клиент напишет "same order" — система предложит опять EU (которых нет).

**Фикс**: В `_backfill_order_items` исключать order_id из активных `pending_oos_resolution`.

### Баг 2: Outbound email_history создаётся для draft

**Проблема**: `save_email(direction="outbound")` вызывается до `gmail.create_draft()`. Outbound запись создаётся всегда когда есть `draft_reply`, независимо от того, отправлен ли он.

**Последствие**: `build_classifier_context()` покажет наш "ответ" в history, хотя клиент его не видел. Если оператор удалит драфт — в истории останется призрак.

**Фикс**: Сохранять outbound email только после `gmail.create_draft()` или пометить как `status=draft`.
