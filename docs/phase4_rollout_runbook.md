# Phase 4: VPS Rollout & Verification Runbook

## Pre-flight Checklist

| # | Check | Command / Action | Expected |
|---|-------|-----------------|----------|
| 1 | Code pushed to GitHub | `git log --oneline -5` (local) | All Phase 3 commits present |
| 2 | Tests pass locally | `docker exec agentos-api python -m pytest tests/test_fulfillment.py -v` | 52 passed |
| 3 | Sheets token has WRITE scope | Check `.env`: `SHEETS_REFRESH_TOKEN` | Token generated with `spreadsheets` scope |

---

## Step 1: Google Sheets OAuth Re-authorization (LOCAL machine)

The current token may have `spreadsheets.readonly` scope. Must re-authorize with write scope.

### 1.1 Always re-authorize

Reliable scope verification requires a real write attempt against Google Sheets API.
Initializing `SheetsClient()` does NOT verify scope — it only checks that credentials exist.
For rollout, always re-authorize the token with `spreadsheets` (write) scope to be safe.

### 1.2 Re-authorize (run on LOCAL machine with browser)

```bash
cd '/Users/aleksejcuprynin/Desktop/AgentOC/ag infra up'
python scripts/sheets_auth.py
```

**What happens:**
1. Opens browser at `localhost:8086`
2. Log in with the email that has access to stock spreadsheets
3. Grant "See, edit, create, and delete" permission for Google Sheets
4. Script prints three values:

```
SHEETS_CLIENT_ID=...
SHEETS_CLIENT_SECRET=...
SHEETS_REFRESH_TOKEN=...
```

### 1.3 Update VPS .env

```bash
# SSH to VPS
ssh root@46.225.93.9

# Edit .env
cd /root/agentos
nano .env

# Update these three lines with values from step 1.2:
# SHEETS_CLIENT_ID=<new value>
# SHEETS_CLIENT_SECRET=<new value>
# SHEETS_REFRESH_TOKEN=<new value>
```

---

## Step 2: Deploy Code to VPS

```bash
# On VPS
cd /root/agentos

# Pull latest
git pull origin main

# Rebuild and restart (zero-downtime is not critical)
docker-compose -f compose.prod.yaml up -d --build
```

### 2.1 Verify startup

```bash
# Watch logs for errors
docker-compose -f compose.prod.yaml logs -f agentos-api 2>&1 | head -50

# Check tables created
docker exec agentos-api python -c "
from db.models import Base, engine, FulfillmentEvent
from sqlalchemy import inspect
insp = inspect(engine)
tables = insp.get_table_names()
print('fulfillment_events exists:', 'fulfillment_events' in tables)
if 'fulfillment_events' in tables:
    cols = [c['name'] for c in insp.get_columns('fulfillment_events')]
    print('Columns:', cols)
    idxs = insp.get_unique_constraints('fulfillment_events')
    print('Unique constraints:', idxs)
"
```

**Expected output:**
```
fulfillment_events exists: True
Columns: ['id', 'client_email', 'order_id', 'gmail_message_id', 'trigger_type', 'status', 'warehouse', 'details_json', 'created_at']
Unique constraints: [
  {'name': 'uq_fulfillment_gmail_trigger', 'column_names': ['gmail_message_id', 'trigger_type']},
  {'name': 'uq_fulfillment_email_order_trigger', 'column_names': ['client_email', 'order_id', 'trigger_type']}
]
```

---

## Step 3: Verify DB Schema

```bash
# Direct PostgreSQL check
docker exec agentos-db psql -U ai -d ai -c "
  \d fulfillment_events
"
```

**Expected:** Table with all 9 columns and 2 unique constraints.

```bash
# Verify unique constraints specifically
docker exec agentos-db psql -U ai -d ai -c "
  SELECT conname, contype
  FROM pg_constraint
  WHERE conrelid = 'fulfillment_events'::regclass
  AND contype = 'u';
"
```

**Expected:**
```
              conname              | contype
-----------------------------------+---------
 uq_fulfillment_gmail_trigger      | u
 uq_fulfillment_email_order_trigger | u
```

---

## Step 4: Run Tests in Container

```bash
docker exec agentos-api python -m pytest tests/test_fulfillment.py -v --tb=short
```

**Expected:** 52 passed, 0 failed.

```bash
# Full test suite (sanity check)
docker exec agentos-api python -m pytest --tb=short -q
```

**Expected:** 417+ passed, 0 failed.

---

## Step 5: Verification Scenarios

### Important: These are manual scenarios. Process a real (or test) email through the pipeline and verify the output.

---

### Scenario 1: new_order + postpay (single warehouse) -> updated

**Setup:** Send/process an email from a known postpay client with items available in one warehouse.

**Trigger command (example):**
```
обработай заказ <postpay_client_email>
```

**What to check:**

| Check | Where | Expected |
|-------|-------|----------|
| FULFILLMENT section | Agent output (Telegram/admin) | `Status: updated`, `Warehouse: LA_MAKS` (or whichever), `Updated rows: N` |
| Google Sheet | Open spreadsheet, find the product row | `maks_sales` column value = old_value + ordered_qty |
| fulfillment_events | DB | New row: `status=updated`, `trigger_type=new_order_postpay`, `warehouse=LA_MAKS` |
| StockItem | DB | `maks_sales` field updated to match Sheet |

**DB verification:**
```bash
docker exec agentos-db psql -U ai -d ai -c "
  SELECT id, client_email, order_id, trigger_type, status, warehouse, created_at
  FROM fulfillment_events
  ORDER BY created_at DESC
  LIMIT 5;
"
```

---

### Scenario 2: payment_received + prepay (single warehouse) -> updated

**Setup:** Process a payment confirmation email from a known prepay client who has a recent order in ClientOrderItem table.

**What to check:**

| Check | Where | Expected |
|-------|-------|----------|
| FULFILLMENT section | Agent output | `Status: updated`, `Warehouse: <name>`, `Updated rows: N` |
| Order items source | Logs | Items from `ClientOrderItem` table (NOT conversation_state) |
| Google Sheet | Spreadsheet | `maks_sales` incremented |
| fulfillment_events | DB | `trigger_type=payment_received_prepay`, `status=updated` (verify trigger_type here, not in agent output) |

---

### Scenario 3: Split warehouse -> skipped_split

**Setup:** Process an order where some items are ONLY available in warehouse A and other items ONLY in warehouse B.

**What to check:**

| Check | Where | Expected |
|-------|-------|----------|
| FULFILLMENT section | Agent output | `Status: skipped_split`, `Reason: no single warehouse can fulfill all items`, `maks_sales was NOT updated` |
| Google Sheet | Spreadsheet | NO changes to maks_sales |
| fulfillment_events | DB | `status=skipped_split`, `warehouse=NULL` |
| Tried warehouses | Agent output | `Tried: LA_MAKS, CHICAGO_MAX, MIAMI_MAKS` |

---

### Scenario 4: Duplicate processing -> skipped_duplicate

**Setup:** Re-process the same email from Scenario 1 (same gmail_message_id).

**What to check:**

| Check | Where | Expected |
|-------|-------|----------|
| FULFILLMENT section | Agent output | `Status: skipped_duplicate`, `Reason: already processed (duplicate)` |
| Google Sheet | Spreadsheet | NO additional changes |
| fulfillment_events | DB | Only ONE row for this email+trigger (no second row) |

---

### Scenario 5: Draft failure -> no fulfillment

**Setup:** This is verified by contract — fulfillment only runs if `result.get("gmail_draft_id")` is truthy. If Gmail draft creation fails (API error, auth issue), fulfillment is skipped entirely.

**What to check:**
- In logs: no `"Fulfillment event claimed"` message when draft fails
- No `fulfillment_events` row created
- No Sheet changes

---

### Scenario 6: new_order + prepay -> NO fulfillment

**Setup:** Process an order from a prepay client (new_order situation).

**What to check:**

| Check | Where | Expected |
|-------|-------|----------|
| FULFILLMENT section | Agent output | NOT present (no FULFILLMENT block) |
| Google Sheet | Spreadsheet | NO changes |
| fulfillment_events | DB | NO new row |

---

## Step 6: Verification Log Template

Fill this during execution:

```
| # | Scenario                    | Expected Status    | Actual Status | Sheet Changed? | DB Row? | Pass/Fail | Notes |
|---|-----------------------------|--------------------|---------------|----------------|---------|-----------|-------|
| 1 | new_order/postpay single WH | updated            |               |                |         |           |       |
| 2 | payment_received/prepay     | updated            |               |                |         |           |       |
| 3 | split warehouse             | skipped_split      |               |                |         |           |       |
| 4 | duplicate (re-run #1)       | skipped_duplicate  |               |                |         |           |       |
| 5 | draft failure               | no fulfillment     |               |                |         |           |       |
| 6 | new_order/prepay            | no fulfillment     |               |                |         |           |       |
```

---

## Risk Checklist

| # | Risk | Mitigation | Rollback |
|---|------|------------|----------|
| 1 | Wrong Sheets token (read-only) | Step 1 re-auth with `spreadsheets` scope | Fulfillment silently errors; no data corruption |
| 2 | fulfillment_events table missing | Auto-created by `Base.metadata.create_all()` on startup | Manual: `docker exec agentos-api python -c "from db.models import Base, engine; Base.metadata.create_all(engine)"` |
| 3 | Duplicate maks_sales increment | DB unique constraints prevent this | Check `fulfillment_events` for duplicates |
| 4 | Sheet row shift (stock sync changed rows) | `source_row` is refreshed every 5 min by stock sync | If stale: wrong cell updated. Mitigation: verify Sheet after first real update |
| 5 | Wrong warehouse selected | Geographic priority + availability check | Verify `tried_warehouses` in output |
| 6 | STOCK_WAREHOUSES env not set | Falls back to legacy `STOCK_SPREADSHEET_ID` | Check env: `docker exec agentos-api env | grep STOCK` |
| 7 | Fulfillment blocks pipeline | `try_fulfillment` is wrapped in try/except, never raises | Check logs for `"Fulfillment trigger failed"` |

---

## Rollback Plan

If critical issues are found:

### Option A: Disable fulfillment (quick, no redeploy)

Comment out the trigger in `agents/pipeline.py` line 577-579:
```python
# if result.get("gmail_draft_id"):
#     from agents.handlers.fulfillment_trigger import try_fulfillment
#     try_fulfillment(classification, result, gmail_message_id)
```
Then redeploy:
```bash
cd /root/agentos && git pull && docker-compose -f compose.prod.yaml up -d --build
```

### Option B: Revert to pre-fulfillment commit

```bash
cd /root/agentos
git log --oneline -10  # find the last pre-fulfillment commit
git checkout <commit_hash> -- agents/pipeline.py
docker-compose -f compose.prod.yaml up -d --build
```

### Option C: Fix incorrect maks_sales value

If a wrong cell was updated in Sheets:
1. Check `fulfillment_events` for the event details
2. Read `details_json` to find `old_maks`, `new_maks`, `source_row`, `maks_col`
3. Manually revert the cell in Google Sheets to `old_maks`
4. Update local DB: `UPDATE stock_items SET maks_sales = <old_value> WHERE id = <stock_item_id>`

---

## Monitoring Commands (post-rollout)

```bash
# Watch fulfillment events in real-time
docker exec agentos-db psql -U ai -d ai -c "
  SELECT id, client_email, trigger_type, status, warehouse, created_at
  FROM fulfillment_events
  ORDER BY created_at DESC
  LIMIT 10;
"

# Check for stuck 'processing' events (should be 0)
docker exec agentos-db psql -U ai -d ai -c "
  SELECT count(*) as stuck_count
  FROM fulfillment_events
  WHERE status = 'processing';
"

# Check for errors
docker exec agentos-db psql -U ai -d ai -c "
  SELECT id, client_email, trigger_type, details_json, created_at
  FROM fulfillment_events
  WHERE status = 'error'
  ORDER BY created_at DESC
  LIMIT 5;
"

# Fulfillment-related logs
docker-compose -f compose.prod.yaml logs agentos-api 2>&1 | grep -i fulfillment | tail -20
```

---

## Execution Order Summary

1. **Local:** Run `scripts/sheets_auth.py` -> get new `SHEETS_REFRESH_TOKEN`
2. **VPS:** Update `.env` with new token
3. **VPS:** `git pull origin main`
4. **VPS:** `docker-compose -f compose.prod.yaml up -d --build`
5. **VPS:** Verify DB schema (Step 3 commands)
6. **VPS:** Run tests in container (Step 4)
7. **Manual:** Execute scenarios 1-6, fill verification log
8. **Monitor:** Watch for errors in first 24h

Phase 4 complete, waiting for approval.
