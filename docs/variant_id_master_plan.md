# Variant ID First Master Plan (v2.0)

## 0. Why This Document Exists
This document is the single source of truth for the `variant_id` migration.

Main goal:
- Eliminate wrong-region fulfillment and wrong `maks_sales` updates caused by text re-resolution (`Silver` -> `[EU, ME, KZ]`).

Secondary goals:
- Keep customer-facing replies accurate.
- Keep warehouse selection deterministic.
- Keep rollback possible after every phase.

This file is intentionally detailed so implementation can be executed with low ambiguity and low token waste.

---

## 1. How To Use This Plan (Mandatory)
For every phase, implementer must:
1. Read this file from start to finish before coding.
2. Implement only one phase at a time.
3. Run required tests for that phase plus targeted regressions.
4. Report exactly in the format from section `19. Phase Report Template`.
5. Stop and wait for approval before next phase.

Non-compliance means phase is not accepted.

---

## 2. Real Incident (Ground Truth)
Real incident (customer `jl@kikucapital.com`) that exposed the risk:
- Proposed: `4 Japan Smooth + 2 EU Bronze + 3 EU Silver + 2 EU Teak`.
- System previously resolved items partially without preserving region in all paths.
- `Silver` could map to multiple categories (`TEREA_EUROPE`, `ARMENIA`, `KZ_TEREA`).
- Stock check/fulfillment could aggregate across wrong categories.

Business impact:
- Client could receive wrong product region.
- Admin could see misleading """in stock""" and incorrect fulfillment updates.

This migration is to make that class of error structurally impossible on fulfillment path.

---

## 3. Current Code Snapshot (As Of Plan Start)
Reference map (current code, before this migration):
- `ClientOrderItem` has no `variant_id`: [db/models.py:154](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/db/models.py:154)
- Old unique constraint: [db/models.py:159](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/db/models.py:159)
- Stock check uses dual path (`product_id IN` else `ILIKE`): [db/stock.py:337](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/db/stock.py:337)
- Fulfillment query uses dual path (`product_id IN` else `ILIKE`): [db/fulfillment.py:114](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/db/fulfillment.py:114)
- payment_received re-resolves from text: [db/fulfillment.py:364](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/db/fulfillment.py:364)
- OOS extraction + region normalization is present: [agents/handlers/oos_followup.py:426](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/agents/handlers/oos_followup.py:426)
- Fulfillment trigger currently relies on `_stock_check_items` or read-path function: [agents/handlers/fulfillment_trigger.py:39](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/agents/handlers/fulfillment_trigger.py:39)

---

## 4. Non-Negotiable Architecture Rules
These are hard rules. Do not reinterpret.

1. `variant_id` means `product_catalog.id`.
2. `variant_id` is written only for unambiguous resolver result:
   - if `len(product_ids) == 1` -> write this id.
   - else write `NULL`.
3. Any ambiguous item blocks auto-fulfillment for whole order.
4. Any strict-mode unresolved item (`variant_id IS NULL`) blocks auto-fulfillment for whole order.
5. For `new_order_postpay`, missing `order_id` blocks auto-fulfillment.
6. `details_json` is the only fulfillment audit payload field (no parallel audit column).
7. Region parser/resolver in `db/product_resolver.py` stays (input parsing still needed).
8. `search_stock()` keeps broad text search; critical order paths eventually become product_id-only.

---

## 5. Business Policy (Customer + Operator)
### 5.1 Customer-facing behavior
If order has ambiguous variants:
- Do not imply automatic shipping succeeded.
- If template is still sent, it must not promise """we ship ASAP automatically""" in that blocked scenario.
- Prefer manual-hold clarification path when ambiguity detected pre-fulfillment.

### 5.2 Operator-facing behavior
When blocked:
- Admin output must clearly show status `blocked_ambiguous_variant`.
- Must include affected flavors and why blocked.

### 5.3 Why strict blocking is required
Shipping wrong region is worse than delayed automation.

---

## 6. Data Contracts (Canonical Structures)

### 6.1 Resolved order item contract (runtime)
```json
{
  "product_name": "Bronze EU",
  "base_flavor": "Bronze",
  "quantity": 2,
  "product_ids": [52],
  "display_name": "Terea Bronze EU"
}
```

### 6.2 Ambiguous resolved item example
```json
{
  "product_name": "Silver",
  "base_flavor": "Silver",
  "quantity": 3,
  "product_ids": [10, 30, 54],
  "display_name": "Terea Silver"
}
```
Expected handling:
- persistence writes `variant_id=NULL`
- fulfillment blocked in strict gate

### 6.3 Persistent row target (`client_order_items`)
Target for new rows:
- `client_email`
- `order_id`
- `product_name`
- `base_flavor`
- `quantity`
- `variant_id` (nullable)
- `display_name_snapshot` (nullable)

### 6.4 Fulfillment event details payload (`details_json`)
All new writes must include `"v": 2`.

`processing`:
```json
{"v":2,"matched_count":3}
```

`updated`:
```json
{
  "v": 2,
  "updated": 2,
  "skipped": 0,
  "errors": [],
  "details": [
    {
      "variant_id": 52,
      "product_name": "Bronze",
      "old_maks": 6,
      "new_maks": 8,
      "source_row": 44,
      "maks_col": 9
    }
  ]
}
```

`blocked_ambiguous_variant`:
```json
{
  "v": 2,
  "reason": "ambiguous_variant",
  "skipped_items": [
    {"base_flavor": "Silver", "product_ids_count": 3}
  ]
}
```

Backward compatibility:
- rows without `v` are treated as v1 legacy.

---

## 7. Concrete Risk Register (Code-Level)

### R1 - Text re-resolution on payment path
- Location: [db/fulfillment.py:364](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/db/fulfillment.py:364)
- Risk: `resolve_product_to_catalog(item.base_flavor)` without region context may return multi-category ids.

### R2 - Mixed-category quantity sum + single-row increment
- Location: [db/fulfillment.py:162](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/db/fulfillment.py:162), [db/fulfillment.py:167](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/db/fulfillment.py:167)
- Risk: sum across entries then increment one primary row can touch wrong category when ids are broad.

### R3 - ILIKE fallback in critical paths
- Location: [db/stock.py:347](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/db/stock.py:347), [db/fulfillment.py:130](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/db/fulfillment.py:130)
- Risk: text fallback can cross-match unintended variants.

### R4 - Unique on base flavor blocks multi-region same flavor
- Location: [db/models.py:159](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/db/models.py:159)
- Risk: cannot store `Silver EU` + `Silver ME` in same order.

### R5 - Missing `order_id` for dedup
- Locations: [db/fulfillment.py:410](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/db/fulfillment.py:410), [agents/handlers/fulfillment_trigger.py:86](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/agents/handlers/fulfillment_trigger.py:86)
- Risk: weak idempotency for `NULL order_id`.

---

## 8. Explicit Target State
After all phases:
1. New trusted orders persist `variant_id` where resolver is unambiguous.
2. payment_received reads `variant_id` directly, no text re-resolution in strict mode.
3. Ambiguous or unresolved items block whole fulfillment, with clear operator status.
4. Order-critical matching uses `product_id` only.
5. Old base-flavor unique constraint removed only after variant coverage is stable.

---

## 9. File-by-File Implementation Blueprint
This section tells exactly what to do in each file.

## 9.1 `db/models.py`
Current:
- `ClientOrderItem` fields end at `quantity`, `created_at`.

Required additions:
```python
variant_id = Column(Integer, ForeignKey("product_catalog.id"), nullable=True, index=True)
display_name_snapshot = Column(String, nullable=True)
```

Constraint strategy:
- Keep existing `uq_client_order_item` during transition.
- Add new partial unique index via migration (not immediate model-only swap).

## 9.2 `db/stock.py`
Add helper:
```python
def _extract_variant_id(product_ids: list[int] | None) -> int | None:
    ids = product_ids or []
    if len(ids) == 1:
        return ids[0]
    if len(ids) > 1:
        logger.warning("variant_id ambiguous: product_ids=%s", ids)
    return None
```

Add helper:
```python
def _has_ambiguous_variants(items: list[dict]) -> list[str]:
    return [
        item.get("base_flavor", "?")
        for item in items
        if len(item.get("product_ids") or []) > 1
    ]
```

Modify `save_order_items()`:
- accept optional `variant_id`, `display_name_snapshot` keys in each item dict.
- add guard: if `order_id` missing -> skip and warn.
- keep savepoint behavior.

Modify `replace_order_items()`:
- persist `variant_id`, `display_name_snapshot`.
- keep existing non-empty `order_id` and non-empty list guards.

Phase-8 cleanup in this file:
- remove ILIKE branch in `check_stock_for_order()`.
- remove text exclusion branch in `_get_available_items()` for order-critical context.

## 9.3 `db/fulfillment.py`
Add status:
```python
STATUS_BLOCKED_AMBIGUOUS = "blocked_ambiguous_variant"
```

Modify `get_order_items_for_fulfillment()` contract:
- return tuple `(items_ready, skipped_items)`.
- for each `ClientOrderItem` row:
  - if `variant_id` exists: `product_ids=[variant_id]`.
  - else:
    - if strict mode enabled (`REQUIRE_VARIANT_ID=true`) -> add to `skipped_items` and do not resolve.
    - if strict mode disabled -> legacy re-resolve path (temporary transition only).

Strict hard-block rule:
- if strict mode and any skipped item exists -> return `([], skipped_items)`.

Modify `_query_stock_entries()` in phase 8:
- if no `product_ids`, return empty list (no ILIKE fallback).

Modify `increment_maks_sales()`:
- include `v:2` and `variant_id` in details entries.

## 9.4 `agents/pipeline.py`
At new-order resolved path after `_stock_check_items` assignment ([agents/pipeline.py:220](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/agents/pipeline.py:220)):
- run ambiguity detector.
- if ambiguous:
  - set `result["fulfillment_blocked"] = True`
  - set `result["ambiguous_flavors"] = [...]`
  - do not set any fulfillment-eligible flag

In `_persist_results()`:
- native `new_order` save path:
  - guard `order_id` not null before `save_order_items()`.
- OOS canonical replace path:
  - pass `variant_id` + `display_name_snapshot` from canonical items.

Mapping example for OOS canonical item:
```python
{
  "product_name": item.get("product_name", item.get("base_flavor", "")),
  "base_flavor": item.get("base_flavor", ""),
  "quantity": item.get("ordered_qty", item.get("quantity", 1)),
  "variant_id": _extract_variant_id(item.get("product_ids")),
  "display_name_snapshot": item.get("display_name"),
}
```

## 9.5 `agents/handlers/oos_followup.py`
In `_apply_confirmation_flags()`:
- preserve current source logic.
- add ambiguity gate over `resolved_items`:
  - if ambiguous, set `result["fulfillment_blocked"] = True`, `result["ambiguous_flavors"] = [...]`, and do not enable fulfillment eligibility.

In extraction/pending/classifier successful paths:
- ensure `resolved` items with `product_ids` and `display_name` are propagated unchanged to `_stock_check_items`.

Do not remove:
- `_normalize_extracted_region()` and region token logic (until final cleanup decision, currently keep).

## 9.6 `agents/handlers/fulfillment_trigger.py`
Before fulfillment selection:
1. Check `fulfillment_blocked` flag.
2. If true:
   - write `result["fulfillment"] = {"status": "blocked_ambiguous_variant", ...}`
   - claim event with blocked status + details_json v2 skipped list
   - return

For `new_order_postpay`:
- if `order_id` is missing, skip fulfillment with blocked/unresolved status and log warning.

For payment_received path:
- unpack new read signature:
  - `stock_items, skipped_items = get_order_items_for_fulfillment(...)`
- if strict mode and `skipped_items` present -> block whole fulfillment.

Keep source trust gate from OOS paths.

## 9.7 `agents/formatters.py`
Add explicit branch for blocked status:
- print reason `ambiguous_variant`.
- print affected flavors.
- print that `maks_sales` was not updated.

Example output block:
```text
==================================================
FULFILLMENT
==================================================
Status: blocked_ambiguous_variant
Reason: ambiguous variant mapping
maks_sales was NOT updated
Affected: Silver (3 ids), Bronze (2 ids)
```

## 9.8 `db/product_resolver.py`
Keep intact:
- `_extract_region_categories`
- `_has_origin_suffix`
- `_normalize`
- region prefix/suffix maps

Reason:
- these functions are still required to convert text input to canonical variant ids.

---

## 10. Migration SQL Plan (Exact)
Use proper migration tooling (Alembic). SQL below is canonical intent.

## 10.1 Phase 1 SQL
```sql
ALTER TABLE client_order_items ADD COLUMN variant_id INTEGER REFERENCES product_catalog(id);
ALTER TABLE client_order_items ADD COLUMN display_name_snapshot VARCHAR;
CREATE INDEX ix_client_order_items_variant_id ON client_order_items (variant_id);
```

Rollback:
```sql
DROP INDEX IF EXISTS ix_client_order_items_variant_id;
ALTER TABLE client_order_items DROP COLUMN display_name_snapshot;
ALTER TABLE client_order_items DROP COLUMN variant_id;
```

## 10.2 Phase 6.5 SQL (new unique)
```sql
CREATE UNIQUE INDEX uq_client_order_variant
ON client_order_items (client_email, order_id, variant_id)
WHERE variant_id IS NOT NULL AND order_id IS NOT NULL;
```

Rollback:
```sql
DROP INDEX IF EXISTS uq_client_order_variant;
```

## 10.3 Phase 9 SQL (drop old unique)
```sql
ALTER TABLE client_order_items DROP CONSTRAINT uq_client_order_item;
```

Rollback:
```sql
ALTER TABLE client_order_items
ADD CONSTRAINT uq_client_order_item UNIQUE (client_email, order_id, base_flavor);
```

---

## 11. Phased Execution Plan (Detailed)

## Phase 0 - Prep and freeze
Goal:
- freeze design and branch strategy before code.

Tasks:
1. Create branch `feature/variant-id-phase-1`.
2. Add tracking checklist issue with all phases.
3. Confirm migration approach (Alembic vs project migration flow).

Acceptance:
- no code yet, only planning artifacts.

---

## Phase 1 - Schema additions (no behavior change)
Files:
- [db/models.py](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/db/models.py)
- migration file(s)

Implementation steps:
1. Add new columns to model.
2. Create migration for new columns + index.
3. Run migration locally.
4. Run full regression.

Required tests:
- existing suite only (no behavior change expected).

Acceptance criteria:
- App starts cleanly.
- New columns exist and are nullable.
- 0 behavior regressions.

---

## Phase 2 - Write-path variant persistence
Files:
- [db/stock.py](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/db/stock.py)
- [agents/pipeline.py](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/agents/pipeline.py)
- [agents/handlers/oos_followup.py](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/agents/handlers/oos_followup.py)

Implementation steps:
1. Add `_extract_variant_id()`.
2. Extend `save_order_items()` and `replace_order_items()` to store new fields.
3. Add guard in `save_order_items`: no `order_id` -> skip.
4. In pipeline/new-order and OOS replace persistence mappings, pass:
   - `variant_id`
   - `display_name_snapshot`

Important implementation detail:
- Do not guess `variant_id` from text here; only use already-resolved `product_ids`.

Example mapping:
```python
variant_id = _extract_variant_id(item.get("product_ids"))
```

Required tests to add/update:
- `test_extract_variant_id_single`.
- `test_extract_variant_id_multi_returns_none`.
- `test_save_order_items_persists_variant_id`.
- `test_save_order_items_skips_when_order_id_missing`.
- `test_replace_order_items_persists_variant_fields`.

Acceptance criteria:
- New trusted writes persist `variant_id` when unambiguous.
- Multi-match persists NULL, with warning log.

---

## Phase 3 - Runtime ambiguity gates
Files:
- [db/stock.py](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/db/stock.py)
- [agents/pipeline.py](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/agents/pipeline.py)
- [agents/handlers/oos_followup.py](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/agents/handlers/oos_followup.py)
- [agents/handlers/fulfillment_trigger.py](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/agents/handlers/fulfillment_trigger.py)
- [agents/formatters.py](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/agents/formatters.py)

Implementation steps:
1. Add `_has_ambiguous_variants()` helper.
2. Add gate in new_order path after `_stock_check_items` prepared.
3. Add gate in OOS confirmation flag application.
4. Add gate in `try_fulfillment()` as final guard before selecting warehouse.
5. Add new status constant and formatter output.

Blocked behavior (required):
- if any item has `len(product_ids) > 1`, auto-fulfillment must not run.

Required tests:
- pipeline ambiguous item -> `fulfillment_blocked=True`.
- oos trusted path ambiguous item -> blocked flags set.
- fulfillment trigger blocked flag -> status blocked and no increment call.
- formatter renders blocked section.

Acceptance criteria:
- no ambiguous order can auto-update `maks_sales`.

---

## Phase 4 - Read-path variant-first fulfillment
Files:
- [db/fulfillment.py](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/db/fulfillment.py)
- [agents/handlers/fulfillment_trigger.py](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/agents/handlers/fulfillment_trigger.py)

Implementation steps:
1. Add `REQUIRE_VARIANT_ID` feature flag read (default false).
2. Change `get_order_items_for_fulfillment()` return signature to:
   - `tuple[list[dict], list[dict]]` -> `(ready_items, skipped_items)`.
3. Read logic:
   - if row has `variant_id`: exact path (`product_ids=[variant_id]`).
   - if no `variant_id` and strict false: temporary legacy re-resolve.
   - if no `variant_id` and strict true: skipped.
4. Hard block policy:
   - if strict true and any skipped -> return no ready items (block all).
5. Update trigger caller to unpack tuple and handle blocked status.

Required tests:
- variant_id present -> no resolver call.
- strict=false + null variant_id -> legacy path used.
- strict=true + null variant_id -> block order.
- mixed order strict=true (one resolved one unresolved) -> block whole order.

Acceptance criteria:
- strict mode never silently falls back to text re-resolution.

---

## Phase 5 - details_json v2 rollout
Files:
- [db/fulfillment.py](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/db/fulfillment.py)
- [agents/handlers/fulfillment_trigger.py](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/agents/handlers/fulfillment_trigger.py)

Implementation steps:
1. Ensure all new claim/finalize payloads include `v:2`.
2. Add blocked payload with skipped item metadata.
3. Keep reader backward compatible for v1 payloads.

Required tests:
- updated payload includes `v` and details keys.
- blocked payload includes `reason` + skipped list.
- v1 row still parse-safe.

Acceptance criteria:
- all new events are v2.

---

## Phase 6 - Backfill script
New file:
- `scripts/backfill_variant_id.py`

CLI requirements:
- `--dry-run` default true.
- `--execute` for actual update.
- `--batch-size` default 100.
- `--offset` support resume.
- `--report` output JSON report.

Algorithm:
1. Select rows where `variant_id IS NULL`.
2. Resolve by `product_name` first.
3. If not resolved, optional fallback by `base_flavor`.
4. Only write when unambiguous single id.
5. Never overwrite existing `variant_id`.
6. Commit by batch.

Minimum report fields:
```json
{
  "total": 1200,
  "resolved": 1100,
  "ambiguous": 70,
  "unresolved": 30,
  "rows": [
    {"id": 123, "product_name": "Silver", "candidate_ids": [10,30,54], "reason": "ambiguous"}
  ]
}
```

Acceptance thresholds before strict mode:
- coverage >= 95%
- ambiguous <= 3%

Required tests:
- dry-run no writes.
- execute writes only single-match.
- idempotent rerun.

---

## Phase 6.5 - Add partial unique index
Files:
- migration file only.

Steps:
1. Add `uq_client_order_variant` partial unique index.
2. Validate duplicate behavior with explicit SQL test.

Acceptance criteria:
- duplicate same `(client_email, order_id, variant_id)` blocked.
- null rows still allowed.

---

## Phase 7 - Enable strict mode in prod
Action:
- set `REQUIRE_VARIANT_ID=true` in environment.

Required checks (24h window):
1. Monitor count of blocked unresolved events.
2. Verify no legacy re-resolve logs in fulfillment read path.
3. Verify no unexpected fulfillment drop for healthy orders.

Acceptance criteria:
- strict mode stable, no silent fallback.

---

## Phase 8 - Remove ILIKE in order-critical paths
Files:
- [db/stock.py](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/db/stock.py)
- [db/fulfillment.py](/Users/aleksejcuprynin/Desktop/AgentOC/ag%20infra%20up/db/fulfillment.py)

Exact removals:
1. `check_stock_for_order()` ILIKE branch.
2. `_query_stock_entries()` ILIKE branch.
3. `_get_available_items()` critical text exclusion fallback.

Keep unchanged:
- `search_stock()` broad text behavior.
- region parser/resolver logic in product_resolver.

Required negative tests:
- no product_ids in stock check -> unresolved/not in stock.
- no product_ids in fulfillment query -> no entries.
- search_stock still uses broad query.

Acceptance criteria:
- no order-critical ILIKE behavior remains.

---

## Phase 9 - Swap out old unique constraint
Files:
- migration file only.

Steps:
1. Drop old `uq_client_order_item`.
2. Keep new partial unique index.
3. Verify same flavor multi-region order inserts now work.

Required test:
- same order can store `Silver EU` and `Silver ME` as two rows with different `variant_id`.

Acceptance criteria:
- text-based unique no longer blocks valid multi-region items.

---

## 12. ILIKE Policy Table (Final)
| Function | File | Allowed in final state | Reason |
|---|---|---|---|
| `search_stock` | `db/stock.py` | Yes | exploratory tool for agent/operator |
| `check_stock_for_order` | `db/stock.py` | No | order-critical decision path |
| `_query_stock_entries` | `db/fulfillment.py` | No | fulfillment-critical path |
| `_get_available_items` text exclusion | `db/stock.py` | No | OOS alternatives should be product-id scoped |

---

## 13. End-to-End Examples (Before/After)

## Example A - Correct EU fulfillment
Input (resolved):
```json
[{"base_flavor":"Silver","product_ids":[10],"quantity":3}]
```
Behavior:
- warehouse query uses id `10` only
- EU stock checked only
- if EU out-of-stock -> not sufficient
- no KZ substitution

## Example B - Ambiguous item
Input:
```json
[{"base_flavor":"Silver","product_ids":[10,30,54],"quantity":3}]
```
Behavior:
- persistence writes `variant_id=NULL`
- `fulfillment_blocked=True`
- fulfillment status `blocked_ambiguous_variant`
- no `maks_sales` write

## Example C - payment_received strict mode with partial unresolved
Rows:
- item1 variant_id=52
- item2 variant_id=NULL

Behavior:
- strict mode on -> whole order blocked
- no partial fulfillment
- operator alert lists unresolved item

---

## 14. Test Plan (Detailed)

## 14.1 `tests/test_stock.py`
Required cases:
1. `_extract_variant_id([42]) == 42`
2. `_extract_variant_id([1,2]) is None` + warning
3. `_extract_variant_id([]) is None`
4. `_has_ambiguous_variants` returns correct flavors
5. `save_order_items` stores `variant_id` and snapshot
6. `save_order_items` skips missing `order_id`
7. `replace_order_items` stores `variant_id` and snapshot
8. `check_stock_for_order` no product_ids behavior after ILIKE removal

## 14.2 `tests/test_fulfillment.py`
Required cases:
1. read path uses row.variant_id directly
2. strict=false null variant_id uses legacy fallback
3. strict=true null variant_id blocks whole order
4. trigger handles blocked flag and sets blocked status
5. order_id missing on new_order_postpay blocks
6. details_json v2 includes required keys
7. `_query_stock_entries` without product_ids returns empty after cleanup

## 14.3 `tests/test_oos_followup_intents.py`
Required cases:
1. trusted extraction path sets canonical items with product_ids
2. pending path sets product_ids and trusted flags
3. ambiguous product_ids in trusted path set fulfillment_blocked
4. classifier source remains untrusted for auto-fulfillment

## 14.4 `tests/test_email_agent_pipeline_smoke.py`
Required cases:
1. `_persist_results` passes variant fields to save/replace
2. no-order-id guard on new_order save path
3. ambiguous gate on `_stock_check_items` blocks fulfillment eligibility

## 14.5 New tests for backfill script
Create `tests/test_backfill_variant_id.py` with:
1. dry-run no writes
2. single-match write
3. ambiguous no write
4. idempotent rerun

---

## 15. Operator Runbook (Production)

## 15.1 Pre-deploy
1. DB backup.
2. Run migration on staging copy.
3. Run required tests.

## 15.2 Deploy order
1. Deploy phase code.
2. Apply migration for that phase.
3. Restart service.
4. Run smoke command (`process_email` known test case if available).
5. Verify SQL checks.

## 15.3 SQL checks
Coverage:
```sql
SELECT
  COUNT(*) AS total,
  COUNT(*) FILTER (WHERE variant_id IS NOT NULL) AS with_variant,
  ROUND(100.0 * COUNT(*) FILTER (WHERE variant_id IS NOT NULL) / NULLIF(COUNT(*),0), 2) AS pct_with_variant
FROM client_order_items;
```

Blocked ambiguous events:
```sql
SELECT id, client_email, order_id, status, details_json, created_at
FROM fulfillment_events
WHERE status = 'blocked_ambiguous_variant'
ORDER BY created_at DESC
LIMIT 50;
```

Stuck processing events:
```sql
SELECT id, client_email, trigger_type, created_at
FROM fulfillment_events
WHERE status = 'processing'
ORDER BY created_at DESC;
```

---

## 16. Rollback Strategy (Per Phase)

## Phase 1 rollback
- migration down for added columns/index.

## Phase 2 rollback
- revert code changes in write path; columns may stay unused.

## Phase 3 rollback
- revert gate code if false positives happen.

## Phase 4 rollback
- set `REQUIRE_VARIANT_ID=false` and revert read-path changes if needed.

## Phase 5 rollback
- revert payload writer; v1/v2 coexist safely.

## Phase 6 rollback
- revert backfill updates by checkpointed id ranges if needed.

## Phase 6.5 rollback
- drop new partial unique index.

## Phase 7 rollback
- immediate config rollback: `REQUIRE_VARIANT_ID=false`.

## Phase 8 rollback
- reintroduce ILIKE fallback branches.

## Phase 9 rollback
- restore old unique constraint.

---

## 17. Do Not Remove List (Critical)
Do not delete during this migration:
- `db/product_resolver._extract_region_categories`
- `db/product_resolver._has_origin_suffix`
- `db/product_resolver._normalize`
- region prefix/suffix constants in resolver
- `search_stock()` broad behavior

Reason:
- these are still required for text input normalization to produce correct variant candidates.

---

## 18. Final Acceptance Checklist (Go/No-Go)
All must be true:
1. New trusted orders persist `variant_id` when single-match.
2. Multi-match never auto-fulfills.
3. Strict mode blocks unresolved payment_received orders.
4. No silent text re-resolution in strict fulfillment path.
5. Order-critical paths do not use ILIKE fallback.
6. Same flavor different region can coexist in one order after constraint swap.
7. New fulfillment event payloads use `v:2`.
8. Full regression passes.
9. Manual smoke on real OOS-agrees case confirms correct region behavior.

---

## 19. Phase Report Template (Mandatory for Claude)
Use exactly this structure after each phase:

```text
Phase X Report
1) Implemented sections
- section ids from this file

2) Changed files
- file path + exact change summary

3) Tests
- command
- pass/fail counts
- note pre-existing failures separately

4) Runtime behavior proof
- example input
- observed output/status

5) Residual risks
- concise, concrete

6) Ready for approval
- yes/no
```

---

## 20. Prompt Template For Each Phase (Copy/Paste)
Use this when asking Claude to implement a phase.

```text
Read /Users/aleksejcuprynin/Desktop/AgentOC/ag infra up/docs/variant_id_master_plan.md fully before coding.
Implement ONLY Phase <N> exactly as specified (no scope creep).

Hard requirements:
1) Follow section 4 architecture rules.
2) Keep section 17 "Do Not Remove" logic intact.
3) Add/update tests listed for this phase in section 14.
4) Run phase tests + targeted regressions.
5) Return report exactly in section 19 format.

If you find mismatch between plan and current code, stop and report with exact file:line and a minimal correction proposal.
Do not continue to next phase until approved.
```

---

## 21. Notes For Reviewer (Us)
What to reject immediately:
1. Any code that writes a guessed variant_id from text without single-match guarantee.
2. Any fulfillment path that can still proceed with ambiguous variants.
3. Any strict-mode behavior that silently re-resolves missing variant rows.
4. Any removal of resolver region parsing logic.
5. Any Phase 8 cleanup that removes `search_stock` broad behavior.

What is acceptable technical debt during transition:
- legacy rows with `variant_id=NULL` before strict flag enabled.
- temporary coexistence of old + new unique constraints.

---

## 22. Optional Fast Track (If We Need Speed)
If we decide to move faster, phases can be grouped safely as:
- Batch A: Phases 1+2
- Batch B: Phases 3+4+5
- Batch C: Phases 6+6.5
- Batch D: Phases 7+8+9

Only do this if each batch still has full test and rollback checkpoints.

---

## 23. Current Priority Recommendation
Recommended next phase now:
- Phase 1 (schema additions) only.

Reason:
- zero behavior change
- low deployment risk
- unblocks all subsequent work

