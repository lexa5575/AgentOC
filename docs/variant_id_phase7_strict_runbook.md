# Phase 7: REQUIRE_VARIANT_ID=true — Strict Mode Runbook

## 1. Preflight Checklist

Run all checks **before** flipping the flag.

### 1.1 Coverage Query
```sql
SELECT
  COUNT(*) AS total,
  COUNT(*) FILTER (WHERE variant_id IS NOT NULL) AS with_variant,
  COUNT(*) FILTER (WHERE variant_id IS NULL) AS null_variant,
  ROUND(100.0 * COUNT(*) FILTER (WHERE variant_id IS NOT NULL)
        / NULLIF(COUNT(*), 0), 2) AS pct_with_variant
FROM client_order_items;
```
**Threshold**: `pct_with_variant >= 95.00`

### 1.2 Ambiguous/Unresolved Snapshot
```sql
-- Recent blocked events (last 24h)
SELECT status, COUNT(*) AS cnt
FROM fulfillment_events
WHERE status IN ('blocked_ambiguous_variant')
  AND created_at > NOW() - INTERVAL '24 hours'
GROUP BY status;
```

### 1.3 Stuck Processing Events
```sql
SELECT id, client_email, trigger_type, created_at
FROM fulfillment_events
WHERE status = 'processing'
ORDER BY created_at DESC;
```
**Threshold**: 0 stuck rows. If any exist — investigate before switching.

### 1.4 Duplicate Groups (partial unique index readiness)
```sql
SELECT client_email, order_id, variant_id, COUNT(*) AS cnt
FROM client_order_items
WHERE variant_id IS NOT NULL AND order_id IS NOT NULL
GROUP BY client_email, order_id, variant_id
HAVING COUNT(*) > 1;
```
**Threshold**: 0 duplicate groups.

### 1.5 Partial Unique Index Exists
```sql
-- PostgreSQL
SELECT indexname FROM pg_indexes WHERE indexname = 'uq_client_order_variant';
```
**Threshold**: must return 1 row. If missing — run Phase 6.5 migration first:
```bash
docker exec agentos-api python scripts/migrate_variant_unique_index.py
```

### 1.6 Unresolved Strict Events (last 24h)
Strict-mode unresolved events are `blocked_ambiguous_variant` events with
`reason=unresolved_variant_strict` in `details_json`. The readiness script
parses `details_json` in Python (no SQL JSON functions needed):
```bash
docker exec agentos-api python scripts/check_variant_id_readiness.py
# Check "unresolved_strict_last_24h" field in output
```

### 1.7 Automated Preflight
```bash
docker exec agentos-api python scripts/check_variant_id_readiness.py
```
Exit code 0 = go. Exit code 1 = no-go (review `reasons[]` in JSON output).

---

## 2. Go / No-Go Thresholds

| Metric | Threshold | Action if failed |
|--------|-----------|-----------------|
| variant_id coverage | >= 95% | Run backfill again, review ambiguous report |
| Ambiguous rate | <= 3% | Manual resolution of ambiguous rows |
| Stuck processing events | 0 | Investigate and finalize/error stuck events |
| Duplicate groups for uq_client_order_variant | 0 | Resolve duplicates before enabling |
| uq_client_order_variant index | exists | Run Phase 6.5 migration script |

All thresholds must pass for go.

---

## 3. Switch Steps

### 3.1 Set Environment Variable
On VPS (`/root/agentos/`):
```bash
# Edit .env
nano /root/agentos/.env

# Add or change:
REQUIRE_VARIANT_ID=true
```

### 3.2 Restart
```bash
cd /root/agentos
docker compose restart agentos-api
```

### 3.3 Verify Container Started
```bash
docker logs agentos-api --tail 20
```
Confirm no startup errors.

---

## 4. Post-Switch Verification

### 4.1 Immediate (first 15 min)

**Check logs for strict-mode behavior:**
```bash
docker logs agentos-api --since 15m 2>&1 | grep -i "variant_id"
```

Expected to see:
- `"Fulfillment blocked: N/M items missing variant_id"` for any NULL variant_id orders
- No `"resolve_product_to_catalog"` calls from `get_order_items_for_fulfillment`

**Check fulfillment event statuses:**
```sql
SELECT status, COUNT(*) AS cnt
FROM fulfillment_events
WHERE created_at > NOW() - INTERVAL '15 minutes'
GROUP BY status
ORDER BY cnt DESC;
```

Expected statuses: `updated`, `blocked_ambiguous_variant`, `skipped_split`, `skipped_duplicate`.
NOT expected: `processing` (should be finalized quickly).

### 4.2 24-Hour Check

**No legacy re-resolve calls:**
```bash
docker logs agentos-api --since 24h 2>&1 | grep "Legacy path: re-resolve"
```
Expected: 0 results. Any result means strict mode is not fully active.

**Blocked events volume:**
```sql
SELECT
  DATE_TRUNC('hour', created_at) AS hour,
  status,
  COUNT(*)
FROM fulfillment_events
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY hour, status
ORDER BY hour DESC, status;
```

**Coverage trend (should not decrease):**
```sql
SELECT
  COUNT(*) AS total,
  COUNT(*) FILTER (WHERE variant_id IS NOT NULL) AS with_variant,
  ROUND(100.0 * COUNT(*) FILTER (WHERE variant_id IS NOT NULL)
        / NULLIF(COUNT(*), 0), 2) AS pct
FROM client_order_items;
```

---

## 5. Rollback

### 5.1 Immediate Rollback (< 30 sec)
```bash
# On VPS:
cd /root/agentos

# Change .env
sed -i 's/REQUIRE_VARIANT_ID=true/REQUIRE_VARIANT_ID=false/' .env

# Restart
docker compose restart agentos-api
```

### 5.2 Post-Rollback Verification
```bash
# Confirm env var
docker exec agentos-api env | grep REQUIRE_VARIANT_ID
# Expected: REQUIRE_VARIANT_ID=false

# Check logs — legacy re-resolve should appear again for NULL variant_id rows
docker logs agentos-api --since 5m 2>&1 | grep "Legacy path"
```

### 5.3 What Rollback Does NOT Affect
- variant_id values already written — stay in DB (no data loss)
- Partial unique index — stays (no migration rollback needed)
- v2 details_json — stays (backward compatible)
- Ambiguity gates (Phase 3) — still active (independent of strict flag)

### 5.4 When to Rollback
- Sudden spike in `blocked_ambiguous_variant` events beyond expected volume
- Orders with known-good variant_id being incorrectly blocked
- Any customer-visible fulfillment delay caused by false-positive blocks

---

## 6. Phase 9: Drop Old Unique Constraint

### 6.1 Purpose
Drop `uq_client_order_item UNIQUE (client_email, order_id, base_flavor)` so that
the same base_flavor with different `variant_id` values can coexist in one order
(e.g. "Silver EU" + "Silver ME" → both stored as base_flavor="Silver" but
variant_id=10 vs variant_id=20).

### 6.2 Prerequisite
`uq_client_order_variant` partial index **must** exist. If missing, run Phase 6.5 first:
```bash
docker exec agentos-api python scripts/migrate_variant_unique_index.py
```

### 6.3 Check-Only (dry run)
```bash
docker exec agentos-api python scripts/migrate_drop_old_unique_constraint.py --check-only
```
Output fields:
- `old_constraint_existed`: true if uq_client_order_item exists
- `prereq_index_exists`: true if uq_client_order_variant exists

### 6.4 Execute (drop)
```bash
docker exec agentos-api python scripts/migrate_drop_old_unique_constraint.py
```
Exit code 0 = success. Exit code 1 = blocked (check `reasons[]`).

Possible statuses:
| Status | Meaning |
|--------|---------|
| `dropped` | Constraint removed successfully |
| `noop` | Constraint already absent |
| `blocked` | Prerequisite index missing — run Phase 6.5 first |

### 6.5 Rollback (re-add constraint)
```bash
docker exec agentos-api python scripts/migrate_drop_old_unique_constraint.py --rollback
```

**Blocked** if old-key duplicates `(client_email, order_id, base_flavor)` exist.
If blocked: resolve duplicates manually before rollback, or accept the new schema.

Possible statuses:
| Status | Meaning |
|--------|---------|
| `restored` | Constraint re-added successfully |
| `noop` | Constraint already exists |
| `blocked` | Duplicates on old key prevent re-add (see `duplicates[]`) |

### 6.6 Post-Execute Verification
```sql
-- Confirm constraint gone
SELECT conname FROM pg_constraint
WHERE conrelid = 'client_order_items'::regclass AND conname = 'uq_client_order_item';
-- Expected: 0 rows

-- Confirm partial index still present
SELECT indexname FROM pg_indexes WHERE indexname = 'uq_client_order_variant';
-- Expected: 1 row
```
