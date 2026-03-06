# Auto-Increment maks_sales: Detailed Phased Execution Playbook for Claude

## 0. How to use this document
This file is the source of truth for implementation:
- Path: `docs/claude_maks_sales_phased_plan.md`
- Claude must read this file at the start of **every phase**
- Claude must execute only one phase per run and stop

## 1. Business goal
When an order is actually prepared for shipping (draft with shipping details is created), increment `maks_sales` in Google Sheets automatically.

This is needed so existing formulas in sheets reduce `quantity` correctly.

## 2. Hard business invariants (do not violate)
1. Update is allowed only for these shipping flows:
   - `("new_order", "postpay")`
   - `("payment_received", "prepay")`
2. Update is forbidden for:
   - `("new_order", "prepay")` (payment request only, no shipping yet)
3. If order requires split across multiple warehouses:
   - do not update any `maks_sales`
   - show explicit operator/admin message that update was skipped
4. `maks_sales` update must run only after successful Gmail draft creation.
5. Must be idempotent (no duplicate increments on retries/reprocessing).

## 3. Architecture constraints
1. Keep `template_utils.fill_template_reply()` pure (no heavy business side-effects).
2. Do not rely on LLM-managed `conversation_state` for deterministic fulfillment data.
3. Do not import private APIs from `tools/stock_sync.py` (no `_load_warehouse_configs`, `_get_client`).
4. Prefer deterministic DB-backed flow for `payment_received`.
5. Preserve backward compatibility for existing tests.

## 4. Data examples Claude should account for
### Address formats from real data
1. `Roseville, CA 95747`
2. `Los Angeles, California 90005`
3. `Auburn, Washington 98092`
4. `Danvers, Mass 01923`
5. `Freedom PA 15042-1960`

### A1 conversion examples
1. `0 -> A`
2. `25 -> Z`
3. `26 -> AA`
4. `27 -> AB`
5. `51 -> AZ`
6. `52 -> BA`

### Expected fulfillment statuses
1. `updated`
2. `skipped_split`
3. `skipped_unresolved_order`
4. `skipped_duplicate`
5. `error`

---

## Phase 1: Infrastructure (Sheets write + Geo parser)
### Phase objective
Create foundational primitives without touching runtime order-processing behavior.

### Files in scope
1. `tools/google_sheets.py`
2. `scripts/sheets_auth.py`
3. `db/warehouse_geo.py` (new)
4. `tests/test_warehouse_geo.py` (new)
5. `tests/test_google_sheets_utils.py` (new or append existing relevant test file)

### Required implementation details
1. Change scope from `spreadsheets.readonly` to `spreadsheets` in both runtime client and auth script.
2. Add robust A1 converter helper in sheets client.
3. Add safe write API (`update_cell` or batch-ready equivalent).
4. Add US state mapping:
   - 50 states + DC
   - full name to code mapping
   - common abbreviations used in real addresses (e.g., `Mass`)
5. Add `resolve_warehouse_from_address(city_state_zip) -> list[str]` with proximity fallback.

### Out of scope
1. No fulfillment engine.
2. No pipeline changes.
3. No handler changes.
4. No DB migrations.

### Test matrix (minimum)
1. A1 conversion examples above.
2. Address parsing for all listed real formats.
3. Empty/garbage address fallback order.

### Done criteria
1. Scope and auth script updated.
2. Geo parser returns stable warehouse priority.
3. Tests green.

### Prompt for Claude (copy/paste)
```text
Phase 1 only. Before coding, read:
docs/claude_maks_sales_phased_plan.md

In your first response print exactly:
PLAN_READ_OK_PHASE_1

Then implement only Phase 1 from this plan:
1) tools/google_sheets.py:
   - write scope
   - A1 conversion helper
   - safe write API
2) scripts/sheets_auth.py:
   - same write scope
3) db/warehouse_geo.py:
   - robust parser for US addresses and warehouse priority resolution
4) tests for A1 + address parser.

Do not modify pipeline/handlers/fulfillment logic in this phase.
Run only relevant tests and stop.

At the end provide:
1) changed files
2) tests run + results
3) known limitations
4) "Phase 1 complete, waiting for approval"
```

---

## Phase 2: Fulfillment core + idempotency
### Phase objective
Build deterministic fulfillment engine and duplicate-protection layer.

### Files in scope
1. `db/fulfillment.py` (new)
2. `db/models.py` (if adding idempotency table)
3. `db/memory.py` only if absolutely required and safe (prefer no new exports if avoidable)
4. `db/warehouse_geo.py` (consumption only, minor adjustments allowed)
5. `tests/test_fulfillment.py` (new)
6. Migration/init path if new DB table is introduced

### Required implementation details
1. Implement single-warehouse selection:
   - try warehouses by proximity order
   - only success if one warehouse covers all items with sufficient quantities
2. Split detection:
   - explicit structured result
   - no writes on split
3. Quantity checks must sum across matching rows, not `first()`.
4. Deterministic order-item source for `payment_received`:
   - from `ClientOrderItem` + robust resolution strategy
   - no conversation-state dependency
5. Idempotency layer:
   - recommended DB table `fulfillment_events`
   - unique keys preventing duplicate increments
6. No private imports from `tools/stock_sync.py` internals.
7. Use public/shared config accessor abstraction.

### Suggested idempotency schema
1. `fulfillment_events` columns:
   - `id`
   - `client_email`
   - `order_id`
   - `gmail_message_id`
   - `trigger_type` (`new_order_postpay` / `payment_received_prepay`)
   - `status`
   - `warehouse`
   - `created_at`
2. Unique constraints:
   - `(gmail_message_id, trigger_type)` when `gmail_message_id` exists
   - fallback unique on `(client_email, order_id, trigger_type)` if no gmail id

### Out of scope
1. No pipeline draft-gating integration yet.
2. No format_result/admin output changes yet.
3. No Telegram content changes yet.

### Test matrix (minimum)
1. single warehouse success
2. fallback warehouse success
3. split scenario skip
4. duplicate processing skip
5. unresolved payment_received order skip

### Done criteria
1. Core engine deterministic and tested.
2. Split and duplicates are safe.
3. No private stock_sync imports.

### Prompt for Claude (copy/paste)
```text
Phase 2 only. Before coding, read:
docs/claude_maks_sales_phased_plan.md

In your first response print exactly:
PLAN_READ_OK_PHASE_2

Implement only Phase 2:
1) Create deterministic fulfillment core in db/fulfillment.py
2) Implement split detection and no-write split behavior
3) Implement idempotency with DB-level protection
4) For payment_received, fetch items deterministically (not from conversation state)
5) Do not import private APIs from tools/stock_sync.py
6) Add unit tests for selection, increment, skip modes, idempotency.

Do not touch pipeline trigger timing yet.
Do not modify format_result yet.
Run relevant tests and stop.

At the end provide:
1) changed files
2) DB/migration changes
3) tests run + results
4) residual risks
5) "Phase 2 complete, waiting for approval"
```

---

## Phase 3: Runtime integration (draft-gated trigger + operator visibility)
### Phase objective
Wire fulfillment into live flow safely and visibly.

### Files in scope
1. `agents/pipeline.py`
2. `agents/handlers/new_order.py`
3. `agents/handlers/payment_received.py`
4. `agents/handlers/fulfillment_trigger.py` (new helper)
5. `agents/formatters.py`
6. `agents/notifier.py` (if operator alerts needed)
7. Integration tests in relevant test files

### Required implementation details
1. Draft-gated trigger:
   - run fulfillment only after successful `GmailClient.create_draft(...)`
   - on draft failure: fulfillment not executed
2. Trigger only for the two allowed business cases.
3. Keep `template_utils` pure.
4. Attach structured fulfillment result into `result` for formatter/admin visibility.
5. Add `FULFILLMENT` section in `format_result()` with explicit status text.

### Example expected output block
```text
==================================================
FULFILLMENT
==================================================
Status: updated
Warehouse: LA_MAKS
Updated rows: 2
- Silver: 40 -> 41
- Amber: 32 -> 33
```

Split example:
```text
==================================================
FULFILLMENT
==================================================
Status: skipped_split
Reason: no single warehouse can fulfill all items
maks_sales was NOT updated
```

### Out of scope
1. No OAuth operational steps.
2. No infra deployment changes.

### Test matrix (minimum)
1. `new_order/postpay` + draft success -> updated
2. `payment_received/prepay` + draft success -> updated
3. split -> skipped_split
4. draft failure -> no update
5. duplicate -> skipped_duplicate

### Done criteria
1. Runtime behavior follows invariants.
2. Operator sees exact reason for every skip.
3. Integration tests pass.

### Prompt for Claude (copy/paste)
```text
Phase 3 only. Before coding, read:
docs/claude_maks_sales_phased_plan.md

In your first response print exactly:
PLAN_READ_OK_PHASE_3

Implement only Phase 3:
1) Integrate fulfillment in runtime flow
2) Gate fulfillment strictly after successful Gmail draft creation
3) Keep template_utils side-effect free
4) Add explicit FULFILLMENT section in format_result with statuses:
   updated / skipped_split / skipped_unresolved_order / skipped_duplicate / error
5) Add/adjust integration tests for positive and negative paths.

Do not do VPS operations in this phase.
Run relevant tests and stop.

At the end provide:
1) changed files
2) tests run + results
3) behavior summary by scenario
4) "Phase 3 complete, waiting for approval"
```

---

## Phase 4: VPS rollout + verification
### Phase objective
Operational rollout and controlled validation in production-like environment.

### Files in scope
No feature coding unless critical hotfix is explicitly approved.

### Required steps
1. Re-authorize Sheets token with write scope and update env.
2. Run targeted tests in container.
3. Run manual scenario set:
   - postpay single-warehouse
   - prepay payment_received single-warehouse
   - split case
   - duplicate case
4. Verify:
   - Google Sheet changed only where expected
   - admin/operator output shows correct status

### Validation report format
1. scenario
2. expected
3. actual
4. pass/fail
5. logs/ids
6. rollback notes if any

### Done criteria
1. No unintended increments.
2. Split and duplicate safety confirmed.
3. Operator visibility confirmed.

### Prompt for Claude (copy/paste)
```text
Phase 4 only. Before running anything, read:
docs/claude_maks_sales_phased_plan.md

In your first response print exactly:
PLAN_READ_OK_PHASE_4

Run only rollout/verification:
1) OAuth token refresh with write scope
2) targeted tests in container
3) manual scenario verification (single, split, duplicate)
4) produce a strict validation report (expected vs actual).

No feature coding in this phase unless explicitly requested.
At the end provide:
1) commands executed
2) report table pass/fail
3) observed risks
4) rollback readiness
5) "Phase 4 complete"
```

---

## Quick copy block for you (short prompts)
### Short Phase 1
`Read docs/claude_maks_sales_phased_plan.md, print PLAN_READ_OK_PHASE_1, execute only Phase 1, run related tests, stop.`

### Short Phase 2
`Read docs/claude_maks_sales_phased_plan.md, print PLAN_READ_OK_PHASE_2, execute only Phase 2 with idempotency + deterministic payment_received source, run related tests, stop.`

### Short Phase 3
`Read docs/claude_maks_sales_phased_plan.md, print PLAN_READ_OK_PHASE_3, execute only Phase 3 with draft-gated trigger + FULFILLMENT output statuses, run related tests, stop.`

### Short Phase 4
`Read docs/claude_maks_sales_phased_plan.md, print PLAN_READ_OK_PHASE_4, execute only Phase 4 rollout/validation, provide pass/fail report, stop.`
