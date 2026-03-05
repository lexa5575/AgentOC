Now I have a thorough understanding of the codebase. Let me create the comprehensive Phase 2 plan. Let me think about the architecture.

Current State Analysis
What Phase 1 achieved:
product_catalog table with (category, name_norm) unique constraint
stock_items.product_id FK to catalog
Auto-assignment during sync_stock via ensure_catalog_entry()
~102 catalog entries, all stock_items backfilled
Current order processing flow:
Email comes in → classifier.py (LLM) or email_parser.py (regex) produces EmailClassification with order_items: [OrderItem(product_name, base_flavor, quantity)]

pipeline.py extracts these items and calls resolve_order_items() to fuzzy match against distinct product names from stock

check_stock_for_order() then looks up actual inventory using case-insensitive product name matching

Throughout the system, handlers reference products by their string names rather than catalog IDs

Key problems to solve in Phase 2:
product_resolver.py needs to fetch known names from the catalog instead of stock items
check_stock_for_order should match using product IDs rather than fuzzy string comparisons
search_stock currently relies on substring matching without any product ID layer
The entire pipeline passes around flavor strings with no deterministic way to resolve them to actual products
There's no alias resolution system—if a customer says "Tera Purple," we're stuck with fuzzy matching instead of a reliable lookup
Out-of-stock alternatives and price calculations both depend on string-based product names from stock rather than the catalog
What Phase 2 should do:
The real fix is replacing this fuzzy string-matching approach with a deterministic flow that maps customer input through aliases to product IDs, then uses those IDs consistently throughout the pipeline.

The product_resolver needs to shift from returning normalized string names to returning actual product IDs from the catalog. Instead of just pulling distinct product names from stock and fuzzy-matching against them, it should query the product catalog directly to get the normalized names, stock names, IDs, and categories. The matching logic stays similar—strip brand and region info, then try exact matches first before falling back to fuzzy matching—but now it returns a product ID that every downstream function can use for reliable lookups and joins.

The tricky part is handling all the customer variations: regional suffixes like "Purple" vs "T Purple" depending on whether it's Japan or Armenia, typos like "Sillver" or "Turquise", and descriptive additions like "made in Middle East". The catalog already has name_norm for basic cleanup, but I need an alias layer that maps these customer inputs to the right catalog entries based on context.

My approach is to enhance the product resolver to pull directly from the product catalog instead of stock items, keeping the fuzzy matching but now matching against normalized catalog names and returning the product_id alongside the resolved name. Then I'll thread that product_id through the order processing pipeline so check_stock_for_order() can do exact lookups.

For backward compatibility, I'll keep the current ILIKE behavior as a fallback when product_id isn't available. The same product_id will also let calculate_order_price() fetch category information reliably from the catalog rather than inferring it from stock entries. Finally, I need to update all the handlers to propagate this product_id through the pipeline.

Actually, let me reconsider the scope here. The user's goal is specifically about moving from string-based product matching to stable product_id references in the customer order processing flow, handling the messy variations in how customers describe items. So the focus should be on the email-to-order path, not the stock sync side which already works fine.

Looking at what needs to change: currently the system classifies emails into order items with product names, then fuzzy matches those names against stock items to resolve them. The target is to introduce a ProductCatalog that maps those fuzzy customer descriptions to stable product IDs, so downstream operations like stock checks and pricing can use product_id joins instead of string matching.

The plan is to add a resolve_to_catalog() function that normalizes and fuzzy matches customer product names against the ProductCatalog's canonical names, returning a ResolveResult with the matched product_id. Then resolve_order_items() gets updated to use this function, so by the time we check stock or calculate prices, we're working with product IDs rather than strings. I need to propagate the product_id through the pipeline in pipeline.py, making sure it flows through stock issue tracking and out-of-stock alternatives, then update the handler files to leverage product_id when available. No database migration is needed since ProductCatalog and product_id already exist in stock_items—this is purely code changes. I'm reconsidering whether we need a product_aliases table, but the user specified no manual cataloging, so aliases should come from the existing normalization logic and brand prefix stripping.

There's a mismatch though: the current normalization strips brand and region info to get the core name, but the catalog's name_norm field only does basic lowercasing and whitespace collapsing. So when a customer writes "Tera Purple" and it normalizes to "Purple," it won't match the catalog entry "T Purple" which normalizes to "t purple." I need to rethink the matching strategy to normalize both the customer input and catalog entries consistently.

The solution is to normalize the customer input with the same _normalize() function that strips brand and region, then also apply that same normalization to the catalog's stock_name for comparison. This is essentially what the current resolver already does with known_names from the database—just pulling from the catalog instead. One complication is that the same base name can appear across multiple categories (like "Green" in TEREA_EUROPE, ARMENIA, and KZ_TEREA), so I need to handle matching across all those variants.

The real issue is that each product_id is tied to a specific (category, name_norm) pair, meaning "Green" in TEREA_EUROPE and "Green" in ARMENIA are technically different products. But when a customer just says "I want 5 Green" without specifying a category, the current system handles it by searching across all categories with a case-insensitive match and aggregating quantities—which works because the price is consistent across those categories.

The challenge for Phase 2 is deciding whether to resolve to multiple product_ids (one per category) or keep the string-based aggregation for stock checking while using product_id for individual item tracking. The real identifier should probably be something like (product_type, normalized_flavor) that maps to multiple catalog entries, but that's not how the current structure works.

The simplest approach is having the resolver return a set of product_ids across all matching categories, then use those for stock aggregation and pricing lookups. But I'm realizing the core issue the user flagged: the current system relies on fuzzy matching against product names, which creates fragility with duplicates and ambiguous matches.

The real fix is shifting the resolver to match against the ProductCatalog (the deduplicated source of truth) and return a canonical normalized name, then use that for exact lookups instead of the fragile ILIKE substring matching. This handles the case where "Green" appears in multiple categories by returning all matching product_ids at once. exact matching on product names instead of fuzzy ILIKE searches that could accidentally match unrelated products, plus a deduplicated resolver source from the catalog rather than duplicated stock entries across warehouses. The comment confirms it's an exact match, which is correct—substring search would incorrectly group unrelated products. The real problem is that the resolver currently matches against raw product names that vary across warehouses, so Phase 2's main improvements are using a deduplicated catalog for resolution, then leveraging product_id for precise stock lookups without any string matching, and threading that ID through the pipeline to eliminate string comparisons downstream. Now let me formalize the plan, though I should also consider how the alias layer handles the user's point about non-canonical names and typos—currently using fuzzy matching with SequenceMatcher.

But beyond typos, there are systematic variations like regional prefixes and suffixes ("Purple" vs "T Purple" for Japan, "Green made in Middle East" vs "Green" in Armenia, "Turquoise EU" vs "Turquoise" in TEREA_EUROPE). The normalization function already strips brand prefixes and region suffixes, and there's a heuristic for the Japan T-prefix. Rather than requiring manual catalogization, I could auto-generate aliases from the existing catalog data—so a TEREA_JAPAN entry for "T Purple" would automatically create aliases like "purple," "t purple," "tera purple," and "terea purple."

I'm leaning against adding a dedicated alias table for Phase 2. Instead, I'll keep the current approach: use the catalog as the source of truth, maintain the normalization logic and fuzzy matching as a fallback, but match against catalog entries directly and return product IDs for downstream matching. If an alias layer becomes necessary later, it can be added in Phase 3.

Now I'm structuring the plan to write out in Russian since that's what the user prefers.

Looking at the current state: Phase 1 is working well—the product catalog exists, product IDs are populated across all stock items, and the sync automatically creates catalog entries on each upsert with proper deduplication. The resolver has normalization and fuzzy matching, and there's clear separation between stick and device categories. The weak points are that the resolver still works with raw strings from StockItem.product_name (which creates duplicates across warehouses), and check_stock_for_order uses case-insensitive string matching without leveraging product IDs.

The pipeline has two separate normalization functions doing similar work, search_stock relies on substring matching that could return false positives, and out-of-stock alternatives are tracked only by strings rather than product IDs. The price calculation depends on category from stock entries, which works but feels fragile.

For the target data model, the database schema stays as is from Phase 1, but the key shift is making ProductCatalog the source of truth instead of raw StockItem.product_name strings, and updating the pipeline to work through product IDs consistently.

Now I need to refactor the resolver to pull from the catalog directly — creating a function that fetches catalog entries with their normalized names and stock display names, then matching against those entries instead of raw stock items. The resolver will return product IDs for all matching catalog entries across categories.

For the stock checking logic, I'll update it to use those product IDs when available — querying stock items by their product_id field directly rather than name matching. If product IDs aren't resolved, it falls back to the current string-based lookup. The category for pricing comes from the catalog entry itself, not from the stock records.

Adding product_ids parameter to the search function...

Now I need to wire this through the pipeline so that when we classify an email and resolve product IDs, those get passed along to stock checks and alternative selection. The handlers for price questions, stock availability, and out-of-stock follow-ups all need to use these resolved product IDs instead of relying on text matching.

There's an important distinction I'm realizing between two separate normalization approaches. The catalog has its own normalization for deduplicating stock entries—handling case differences and whitespace variations. The product resolver has a different normalization that strips brand prefixes and region suffixes to match customer language against actual stock names. These need to stay separate since they serve different purposes, but the resolver needs to account for what the catalog has already normalized when doing its matching.

Looking at the matching logic, when a customer writes something like "Tera Purple made in Middle East," the resolver strips the brand and region info to get "Purple," then needs to find the exact match in the catalog's normalized entries. The key insight is that I should apply the resolver's normalization to both the customer input and each catalog stock name, then compare them directly—so "Purple" matches the ARMENIA entry's "Purple" exactly, but doesn't incorrectly match "T Purple" or "Black Purple Menthol" since those are distinct products after normalization.

The existing T-prefix heuristic that prefers "T Purple" (Japan) when the customer writes "Tera Purple" without a region suffix should stay intact—this just changes where we pull the known product names from, not how the matching algorithm works.

Now I need to refine how I'm querying the catalog for these known names.

Querying the product catalog...

Actually, both approaches return nearly identical results since the catalog's stock_name field is the original product name from the sheet, and distinct queries on either table yield the same deduplicated set after normalization. The real advantage is that the catalog approach gives us a single source of truth that's already been validated and deduplicated at import time, rather than relying on whatever happens to be in the stock items table.

But there's a complication: if "Green" exists across multiple categories, the resolver still can't deterministically pick a single product_id—it can only return the set of matching ones. Let me think through whether we should resolve to a normalized name or to a product_id directly.

Option A keeps things simple by resolving to name_norm and letting the query handle the category filtering. Option B is more explicit, having the resolver return all matching product_ids upfront. Option C would introduce a new product_groups table to group catalog entries by flavor, but that adds schema complexity.

I'm leaning toward Option B—it's explicit about which products we're considering, uses the existing schema without new tables, and makes the product_id travel clearly through the pipeline.

Now I'm updating the ResolveResult dataclass to include the product_ids we've matched and the canonical normalized name, so downstream functions have everything they need without additional lookups.

In resolve_order_items(), I'm populating those new fields into the resolved item dictionary, and then in check_stock_for_order(), I'm checking if product_ids are available to query the stock table directly—falling back to string matching on product names only when we don't have the IDs.

For unresolved items with low confidence, the product_ids list stays empty, so the fallback path kicks in and we use the existing ILIKE logic, which means no regression in behavior. The alert system continues to flag these items for operator review via Telegram.

Now I'm thinking through the stock_question handler...

Refining the stock lookup strategy...

I'm refining how the "T prefix" heuristic works with the catalog system. Instead of searching through a flat list of known names, I can now filter catalog entries directly by the normalized name and TEREA_JAPAN category, then return the product IDs alongside the resolved stock name for more precise lookups downstream.

Now I'm thinking through the region suffix problem more carefully. When someone specifies "made in Middle East" or "made in Armenia," that's valuable context that currently gets stripped away during normalization. The resolver returns just the product name without knowing which regional variant the customer actually wanted. For the next phase, I could preserve that region hint and use it to filter the catalog by category, so "Purple made in Armenia" would specifically resolve to the ARMENIA category variant rather than any other Purple in the system.

But actually, I'm reconsidering this approach. The region suffix should influence the priority and ordering of results, not which categories we search. If we're out of Green in the EU region, we should still check Armenia for the same product rather than failing the search entirely. The stock availability check needs to be comprehensive across all regions, but the region hint helps us rank which variant to present to the customer first.

This is a nice refinement for Phase 2, so I'll note it as a potential enhancement. For now, I'm going to focus on documenting the core plan.

Let me structure this according to what the user needs: current state diagnosis, target data model for Phase 2, detailed step-by-step implementation plan, testing strategy, deployment and rollback procedures, and a list of open questions.
Phase 2 — product_id в обработке заказов
1. Диагноз текущего состояния
Что уже хорошо (Phase 1):
product_catalog таблица с UNIQUE(category, name_norm) — 102 записи
Все stock_items имеют product_id (0 NULL)
sync_stock() автоматически создаёт catalog entry при upsert
Каталог дедуплицирован: один товар на разных складах → один product_id
Что рискованно / хрупко:
1. Resolver работает со строками StockItem, не с каталогом
product_resolver.py:107-116 — get_known_product_names() делает SELECT DISTINCT product_name FROM stock_items. Каталог не используется вообще. Результат resolver'а — строка base_flavor, не product_id.

2. Две разные функции нормализации

catalog.normalize_product_name() — lower + trim + collapse spaces (для дедупликации склада)
product_resolver._normalize() — strip brand prefix + region suffix (для матчинга клиентского текста)
Они НЕ конфликтуют (разные задачи), но resolver не знает о name_norm каталога.
3. check_stock_for_order — ILIKE по строке
stock.py:246 — StockItem.product_name.ilike(flavor) — case-insensitive exact match. Работает, но:

Зависит от того, что resolver вернул ТОЧНО правильную строку
Не использует product_id вообще
Если resolver ошибся на символ — нет результата
4. search_stock — substring ILIKE
stock.py:161 — ILIKE '%{query}%' — substring search. "Purple" матчит "T Purple", "Black Purple Menthol", "Purple Wave". Для search_stock_tool (LLM-агентам) это ок, но для programmatic lookups — нет.

5. Pipeline гоняет только строки
process_classified_email() → resolve_order_items() → check_stock_for_order() → calculate_order_price() — вся цепочка оперирует base_flavor (строка). Product_id нигде не появляется в order processing.

6. OOS alternatives — строковое исключение
select_best_alternatives() исключает OOS товар по product_name.ilike('%{flavor}%') — substring. Может случайно исключить/включить не то.

2. Целевая модель данных
Схема БД: НЕ МЕНЯЕТСЯ
Phase 1 уже создала всё нужное. Никаких новых таблиц или колонок.

Alias layer: НЕ НУЖЕН
Текущая нормализация + fuzzy matching покрывают 95%+ случаев. Ручная каталогизация запрещена требованиями. Если понадобится — это Phase 3.

Ключевое архитектурное изменение — в коде:

БЫЛО (Phase 1):
  client text → _normalize() → fuzzy vs StockItem.product_name → resolved string
       → ILIKE по строке → stock entries → цена по category из entries

СТАНЕТ (Phase 2):
  client text → _normalize() → fuzzy vs ProductCatalog.stock_name → product_ids
       → WHERE product_id IN (:ids) → stock entries → цена по category из catalog
ResolveResult расширяется:

@dataclass
class ResolveResult:
    original: str
    resolved: str | None       # stock_name для отображения
    confidence: str
    score: float
    candidates: list[str]
    product_ids: list[int] = field(default_factory=list)  # NEW
    name_norm: str | None = None                           # NEW
Order item после resolution:

{
    "product_name": "Green",
    "base_flavor": "Green",
    "quantity": 5,
    "product_ids": [1, 5, 9],  # NEW: все catalog IDs для этого name_norm + allowed categories
}
3. Подробный план по этапам
Step 2.1 — Catalog-backed resolver
Что делаем:
Переводим source of truth для resolver'а с StockItem.product_name на ProductCatalog.

Файлы:

Файл	Действие
db/product_resolver.py	Основные изменения
db/catalog.py	Новая функция get_catalog_products()
Изменения в db/catalog.py:


def get_catalog_products() -> list[dict]:
    """Get all catalog entries for resolver matching.
    
    Returns: [{id, category, name_norm, stock_name}, ...]
    Deduplicated by definition (UNIQUE constraint).
    """
Изменения в db/product_resolver.py:

Новая функция get_known_from_catalog() → вызывает get_catalog_products(), возвращает list[dict] вместо list[str]. Старый get_known_product_names() не удаляем (backward compat), но помечаем # DEPRECATED.

Новая функция resolve_product_to_catalog():

Принимает raw_name: str, catalog_entries: list[dict] | None
Извлекает уникальные stock_name из catalog entries
Применяет текущий алгоритм (_normalize, exact match, T-prefix heuristic, fuzzy)
На match: собирает ВСЕ product_ids с совпадающим name_norm и подходящими категориями
Возвращает ResolveResult с заполненным product_ids
resolve_order_items():

Внутри переключается на resolve_product_to_catalog()
Resolved items получают поле product_ids
Если product_ids пустой (medium/low confidence) — downstream fallback на строки
Логика матчинга (подробно):


Customer: "Tera Purple made in Middle East"
→ _normalize(): "Purple"
→ Exact match vs catalog stock_names (тоже через _normalize): "Purple" == "Purple" ✓
→ Нашли: catalog entries с _normalize(stock_name).lower() == "purple"
→ Фильтр: category IN STICK_CATEGORIES
→ product_ids: [id для ARMENIA/Purple, id для TEREA_EUROPE/Purple, ...]
→ ResolveResult(resolved="Purple", product_ids=[3, 7, ...])
T-prefix heuristic (сохраняется):


Customer: "Tera Purple" (нет region suffix)
→ _normalize(): "Purple"
→ Проверка: есть ли в каталоге entry с stock_name = "T Purple" (TEREA_JAPAN)?
→ Если да: prefer Japan variant → ResolveResult(resolved="T Purple", product_ids=[japan_id])
Fuzzy matching (сохраняется):


Customer: "Sillver"
→ _normalize(): "Sillver"
→ Exact match: нет
→ SequenceMatcher vs каждый catalog stock_name (нормализованный)
→ Best: "Silver" (score=0.92) → собрать product_ids для "silver"
Риски:

Если каталог пустой (sync не запускался) — fallback на пустой результат (как сейчас)
Нужно учесть что stock_name в каталоге может отличаться регистром от product_name в stock_items (на практике совпадают, т.к. stock_name = product_name.strip())
Критерий готовности:

resolve_product_name() с catalog_entries возвращает product_ids для exact/high confidence
resolve_order_items() возвращает items с product_ids
Все существующие тесты test_product_resolver.py проходят
Новые тесты: матчинг с каталогом, product_ids в результате
Step 2.2 — check_stock_for_order с product_id
Что делаем:
Когда item имеет product_ids — используем exact lookup вместо ILIKE.

Файлы:

Файл	Действие
db/stock.py	check_stock_for_order — dual path
Изменения в check_stock_for_order():


for item in order_items:
    flavor = item["base_flavor"].strip()
    ordered_qty = item.get("quantity", 1)
    product_type = get_product_type(flavor)
    allowed_cats = _get_allowed_categories(product_type)
    
    # NEW: product_id path (Phase 2)
    product_ids = item.get("product_ids")
    if product_ids:
        stock_entries = (
            session.query(StockItem)
            .filter(
                StockItem.product_id.in_(product_ids),
                StockItem.category.in_(allowed_cats),
            )
        )
    else:
        # FALLBACK: string path (legacy / unresolved items)
        stock_entries = (
            session.query(StockItem)
            .filter(
                StockItem.product_name.ilike(flavor),
                StockItem.category.in_(allowed_cats),
            )
        )
Риски:

Минимальные: fallback path сохраняет текущее поведение на 100%
product_ids фильтруются ещё и по allowed_cats — двойная защита от cross-type leaking
Критерий готовности:

check_stock_for_order с product_ids в items возвращает корректные stock entries
check_stock_for_order БЕЗ product_ids работает как прежде
Тесты test_stock.py проходят
Step 2.3 — Pipeline integration
Что делаем:
Пробрасываем product_ids через process_classified_email().

Файлы:

Файл	Действие
agents/pipeline.py	product_ids в items → stock check → OOS
Изменения:

В process_classified_email(), строки 98-117:


items_for_check = [
    {
        "product_name": oi.product_name,
        "base_flavor": oi.base_flavor,
        "quantity": oi.quantity,
    }
    for oi in classification.order_items
]

# Resolve + now returns product_ids
items_for_check, resolve_alerts = resolve_order_items(items_for_check)
# items_for_check теперь содержат product_ids

stock_result = check_stock_for_order(items_for_check)  # Автоматически использует product_ids
Важно: resolve_order_items() уже вызывается в pipeline.py. Всё что нужно — убедиться что items с product_ids передаются в check_stock_for_order() без потери поля.

Также: stock_issue tracking:


result["stock_issue"] = {
    "stock_check": stock_result,
    "best_alternatives": best_alternatives,
}
stock_result["items"] уже будут содержать корректные stock_entries найденные через product_id.

Риски:

Минимальные: это pass-through, не меняет логику
Если resolve_alerts есть — items без product_ids fallback на строковый путь (как сейчас)
Критерий готовности:

Pipeline прогоняет order от classification до draft_reply
Items с product_ids корректно проходят через stock check
Items без product_ids не ломаются
Step 2.4 — Handler updates
Что делаем:
Обновляем handlers которые самостоятельно вызывают resolve/stock functions.

Файлы:

Файл	Действие
agents/handlers/price_question.py	resolve → product_ids → check_stock
agents/handlers/stock_question.py	catalog-aware lookup
agents/handlers/oos_followup.py	product_ids в resolution
price_question.py (строки 153-163):
Уже вызывает resolve_order_items() и check_stock_for_order(). Product_ids автоматически пробросятся. Минимальные или нулевые изменения.

stock_question.py (строки 143-144):
Сейчас: search_stock(flavor) — substring ILIKE.
Phase 2:


# Попробовать resolve через каталог
from db.product_resolver import resolve_product_to_catalog
result = resolve_product_to_catalog(flavor)
if result.product_ids:
    # Exact lookup by product_id
    stock_items = search_stock_by_ids(result.product_ids)
else:
    # Fallback to current substring search
    stock_items = search_stock(flavor)
Для этого нужна новая функция search_stock_by_ids() в db/stock.py:


def search_stock_by_ids(product_ids: list[int]) -> list[dict]:
    """Get stock items by product catalog IDs."""
    session = get_session()
    try:
        items = session.query(StockItem).filter(
            StockItem.product_id.in_(product_ids),
        ).all()
        return [item.to_dict() for item in items]
    finally:
        session.close()
oos_followup.py (строки 105-118):
_match_alternative_from_text — string matching по email text. Это ОК, не меняем (клиент всё равно пишет текстом).
_resolve_oos_agreement → confirmed items для check_stock_for_order. Нужно добавить product_ids к confirmed items если они доступны из pending_oos_resolution.

Риски:

stock_question.py: самое сложное изменение, т.к. search_stock используется и как tool для LLM-агента
Нужно сохранить search_stock() с substring ILIKE как public API для stock_tools.py (LLM использует его)
Критерий готовности:

price_question тесты проходят
stock_question: in-stock reply использует catalog, OOS reply работает
oos_followup тесты проходят
Step 2.5 — select_best_alternatives с product_id
Что делаем:
OOS alternatives используют product_id для исключения OOS товара и выбора альтернатив.

Файлы:

Файл	Действие
db/stock.py	select_best_alternatives, _get_available_items
Изменения в _get_available_items():


def _get_available_items(
    allowed_cats: set[str],
    warehouse: str | None = None,
    exclude_flavor: str = "",
    exclude_product_ids: list[int] | None = None,  # NEW
) -> list[dict]:
Если exclude_product_ids передан — исключаем по product_id NOT IN (...) вместо NOT ILIKE '%flavor%'.

Изменения в select_best_alternatives():
Если product_ids доступен для OOS flavour — используем exclude_product_ids вместо exclude_flavor.

Риски:

Минимальные: добавляем новый путь, старый сохраняется как fallback
LLM alternatives selection не меняется (передаём тот же список available items)
Критерий готовности:

Alternatives не включают OOS товар
Тесты test_stock.py::test_alternatives_* проходят
Step 2.6 — Re-exports и cleanup
Файлы:

Файл	Действие
db/memory.py	re-export новых функций из catalog
db/catalog.py	export get_catalog_products
Риски: нулевые.

4. План тестирования
Новые тесты:
tests/test_product_resolver.py — расширить:

test_resolve_to_catalog_exact_match — "Silver" → product_ids для всех категорий с Silver
test_resolve_to_catalog_with_brand_prefix — "Terea Silver" → product_ids для Silver
test_resolve_to_catalog_t_prefix_heuristic — "Tera Purple" (без suffix) → T Purple product_id (Japan)
test_resolve_to_catalog_with_region_suffix — "Green made in Middle East" → product_ids для Green
test_resolve_to_catalog_fuzzy — "Sillver" → product_ids для Silver
test_resolve_to_catalog_low_confidence_no_ids — "XyzFoo" → product_ids=[]
test_resolve_order_items_has_product_ids — batch resolve → items с product_ids
test_resolve_empty_catalog — пустой каталог → graceful fallback
tests/test_stock.py — расширить:

test_check_stock_with_product_ids — items с product_ids → correct stock lookup
test_check_stock_mixed_ids_and_strings — некоторые items с product_ids, некоторые без
test_check_stock_product_ids_cross_category — product_ids из разных категорий агрегируются
test_search_stock_by_ids — новая функция
test_alternatives_exclude_by_product_id — исключение OOS через product_id
Существующие тесты (обновить):
test_product_resolver.py — передавать catalog_entries вместо known_names (или оба пути)
test_stock.py — все check_stock тесты должны проходить без изменений (backward compat)
test_price_question.py — mock resolve_order_items должен возвращать items с product_ids
test_oos_followup_intents.py — mock stock functions должны принимать product_ids
Обязательный regression suite перед деплоем:

python -m pytest tests/test_product_resolver.py tests/test_stock.py tests/test_price_question.py tests/test_oos_followup_intents.py tests/test_stock_parser.py -v
5. План деплоя и rollback
Деплой:
Миграция БД: НЕ НУЖНА (никаких schema changes).

Порядок:

Локально: все тесты проходят
Commit + push
На сервере: git pull
docker compose -f compose.prod.yaml build agentos-api
docker compose -f compose.prod.yaml up -d agentos-api
Smoke check: отправить тестовый email с заказом
Проверить логи: docker logs agentos-api --tail 100
Убедиться: resolver логирует product_ids при resolution
Убедиться: stock check использует product_id path
Rollback:
Feature guard: в resolve_order_items() добавить env flag:


USE_CATALOG_RESOLVER = os.environ.get("USE_CATALOG_RESOLVER", "true").lower() == "true"
Если USE_CATALOG_RESOLVER=false:

resolve_order_items() использует старый get_known_product_names() (StockItem)
items НЕ содержат product_ids
check_stock_for_order() fallback на ILIKE path
Rollback process:

docker compose -f compose.prod.yaml down agentos-api
Добавить в compose.prod.yaml: USE_CATALOG_RESOLVER: "false"
docker compose -f compose.prod.yaml up -d agentos-api
Проверить что всё работает на старом пути
Разобраться с проблемой, исправить, переключить обратно
Или: git revert + rebuild + redeploy.

6. Открытые вопросы
Q1: Region-aware resolution?

Сейчас: "Green made in Middle East" → _normalize() → "Green" → product_ids для ВСЕХ категорий с "green".
Потенциально: парсить region suffix и фильтровать categories (ME → ARMENIA/KZ_TEREA, EU → TEREA_EUROPE).

Pro: более точный подбор, правильные цены если категории отличаются по цене
Con: усложнение resolver'а, risk of over-filtering (если товар есть только в другом регионе)
Рекомендация: НЕ делать в Phase 2. Все stick-категории стоят $110-115, ошибка цены мизерная. Добавить в Phase 3 если будет нужно.
Нужно подтверждение: оставляем без region-aware resolution?

Q2: Deprecation strategy для строкового пути?

Phase 2 добавляет catalog path, но сохраняет string fallback. Когда удаляем fallback?

Рекомендация: НЕ удалять в Phase 2. Удалить в Phase 3 после 2-4 недель стабильной работы. Feature flag USE_CATALOG_RESOLVER позволяет переключиться.
Q3: stock_question handler — нужен ли catalog lookup?

Текущий search_stock(flavor) — substring ILIKE. Для stock_question ("do you have Turquoise?") это работает. Стоит ли усложнять?

Pro: точный результат, нет ложных совпадений ("Purple" не матчит "Black Purple Menthol")
Con: доп. код, нужен resolve вызов
Рекомендация: СДЕЛАТЬ — это дешёвый и полезный change. Resolver один вызов, потом exact lookup.
Q4: Нужен ли product_id в OrderItem Pydantic модели?

Сейчас OrderItem: product_name, base_flavor, quantity. Добавлять ли product_id: Optional[int]?

Pro: product_id путешествует от classification до persist
Con: product_id не известен на этапе classification (до resolution)
Рекомендация: НЕ ДОБАВЛЯТЬ в OrderItem. product_id появляется ПОСЛЕ resolution, хранится в dict items, не в Pydantic модели.
Затрагиваемые файлы (сводка)
Файл	Действие	Step
db/catalog.py	+ get_catalog_products()	2.1
db/product_resolver.py	Переход на catalog, product_ids в результате	2.1
db/stock.py	check_stock_for_order dual path, search_stock_by_ids, alternatives update	2.2, 2.4, 2.5
agents/pipeline.py	Pass-through product_ids (минимальные изменения)	2.3
agents/handlers/price_question.py	Автоматически через resolve (0-1 строка)	2.4
agents/handlers/stock_question.py	Catalog-aware lookup	2.4
agents/handlers/oos_followup.py	product_ids в resolution (минимально)	2.4
db/memory.py	Re-exports	2.6
tests/test_product_resolver.py	Новые тесты для catalog path	2.1
tests/test_stock.py	Новые тесты для product_id path	2.2
Новые файлы: 0. Всё — изменения в существующих.

Update Todos

Read and analyze all specified files for Phase 2 planning

Write detailed Phase 2 technical plan
Вот полный план Phase 2. Ключевые моменты:

Схема БД не меняется — Phase 1 создала всё нужное
Главное изменение: resolver переходит на ProductCatalog как source of truth, возвращает product_ids
Backward compat: string fallback сохраняется для unresolved items
Feature flag USE_CATALOG_RESOLVER для мгновенного rollback
6 шагов — от resolver'а до handlers, каждый шаг независимо тестируем
В конце 4 открытых вопроса — нужны твои решения перед стартом реализации.

---

## Amendments (после review)

### A1. Display names для клиентов

**Проблема:** Сейчас в OOS шаблоне и других customer-facing текстах используются DB-имена:
- "we just ran out of **T Purple**" — клиент не знает что такое "T Purple"
- "For **Green**: T Purple made in Japan" — "Green" тоже сырое DB-имя

**Нужно:** "we just ran out of **Terea Purple**", альтернативы показывают человеческие имена.

**Решение:**

1. Новая функция `get_display_name(stock_name, category)` в `db/catalog.py`:
   - TEREA_JAPAN: "T Purple" → "Terea Purple made in Japan"
   - TEREA_EUROPE: "Purple" → "Terea Purple EU"
   - ARMENIA/KZ_TEREA: "Purple" → "Terea Purple"
   - УНИКАЛЬНАЯ_ТЕРЕА: "Fusion Menthol" → "Terea Fusion Menthol (Unique)"
   - Devices: без изменений

2. `_format_alternative()` в reply_templates.py → вызывает `get_display_name()` вместо дублирования логики.

3. `fill_out_of_stock_template()` → OOS items (problem description) тоже через `get_display_name()`.
   Нужен category для каждого insufficient_item → добавить category в stock_check result.

4. ResolveResult получает `display_name: str | None` — для handlers которые упоминают товар клиенту.

**Шаг:** вставляется между 2.1 и 2.2 (зависит от catalog, нужен до handlers).

### A2. Feature flag в одном месте

**Проблема:** USE_CATALOG_RESOLVER в плане только в resolve_order_items(), но stock_question вызывает resolve_product_to_catalog() напрямую.

**Решение:** Проверку флага ставим внутри `resolve_product_to_catalog()`. Если flag=false, функция возвращает пустой ResolveResult (product_ids=[], confidence="low"). Все вызывающие автоматически fallback на строковый путь.

### A3. INDONESIA и KZ_HEETS (отдельно от Phase 2)

Категории INDONESIA и KZ_HEETS существуют в stock parser, но отсутствуют в STICK_CATEGORIES и CATEGORY_PRICES. Это pre-existing баг. Фиксить отдельно когда узнаем цены.

### Решения по открытым вопросам

- Q1 (Region-aware): НЕТ — не делаем
- Q2 (Deprecation string path): оставляем fallback, удалим в Phase 3
- Q3 (stock_question catalog lookup): ДА — делаем
- Q4 (product_id в OrderItem): НЕТ — не добавляем