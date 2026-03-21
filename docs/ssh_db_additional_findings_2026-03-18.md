# SSH DB Additional Findings (2026-03-18)

Server: `root@46.225.93.9:22`
Project path: `/root/agentos`
DB container: `agentos-db`
Schema: `ai`

## 1) Core DB/Infra confirmed
- Running containers:
  - `agentos-api`
  - `agentos-db`
- Core tables present: `email_history`, `conversation_states`, `clients`, `client_order_items`, `fulfillment_events`, `stock_items`, `product_catalog`, etc.

## 2) Real traffic distribution (email_history)
Inbound/Outbound by situation:
- `new_order`: `205 / 208`
- `oos_followup`: `84 / 83`
- `payment_received`: `70 / 66`
- `other`: `62 / 23`
- `stock_question`: `18 / 19`
- `payment_question`: `10 / 9`
- `shipping_timeline`: `10 / 10` (raw)
- `discount_request`: `6 / 6`
- `tracking`: `5 / 4`
- `price_question`: `3 / 3`
- plus service situations: `skipped_stale`, `merged`

Pairing inbound with first next outbound in same thread shows substantial no-reply buckets:
- `other`: `50` without reply (out of `62`)
- `shipping_timeline`: `9` without reply (out of `10`)
- `payment_received`: `15` without reply (out of `70`)
- `new_order`: `72` without reply (out of `205`)

Implication:
- strict generation scoring must not include all cases blindly; keep separate lanes:
  - `gen+route` (paired)
  - `route-only` (unpaired)

## 3) Golden dataset quality confirmed by DB evidence
Known anomalies in dataset are real DB artifacts, not parser bugs:
- many unpaired inbound cases exist in source DB
- one real reply-situation mismatch exists (`other -> tracking`, inbound id `471`)

Also found synthetic/test contamination in real history used for golden sampling:
- repeated `client2@example.com` shipping timeline cases (`Hey, when will my order be shipped?`) 
- `teststock@example.com` stock question
- HTML/spam entries under `other`

Implication:
- treat these as robustness tests, not prompt quality ground truth
- maintain a denylist/taglist for synthetic/spam/html

## 4) Conversation state risks relevant for testing
`conversation_states`:
- total states: `191`
- with `pending_oos_resolution`: `30`
- with `offered_alternatives`: `191`

`pending_oos_resolution` aging:
- total pending_oos: `30`
- older than 48h: `19`
- older than 7d: `18`

Unexpected pending_oos in non-OOS terminal situations exists:
- `last_situation=stock_question`: 1
- `last_situation=other`: 2

Implication:
- add explicit regression tests for stale `pending_oos_resolution` leakage/cleanup
- in prompt eval, verify handler does not over-trust stale pending facts

## 5) Fulfillment outcomes provide must-test invariants
`fulfillment_events` (`trigger_type`, `status`):
- `new_order_postpay`:
  - `updated`: `40`
  - `blocked_ambiguous_variant`: `4`
  - `skipped_split`: `5`
  - `skipped_unresolved_order`: `7`
- `payment_received_prepay`:
  - `updated`: `20`
  - `blocked_ambiguous_variant`: `3`
  - `skipped_split`: `3`
  - `skipped_unresolved_order`: `2`

Implication:
- E2E gates must include these invariants:
  - ambiguous variant => blocked
  - unresolved/split => skipped
  - trusted paths => updated

## 6) Stock/catalog signals impacting prompt tests
`stock_items` snapshot:
- categories with inventory pressure and region mix present (`TEREA_EUROPE`, `ARMENIA`, `TEREA_JAPAN`, `KZ_TEREA`, `УНИКАЛЬНАЯ_ТЕРЕА`)
- device categories (`ONE`, `STND`, `PRIME`) currently all zero in-stock rows

`product_catalog`:
- rows: `103`
- categories: `10`
- normalized names (`name_norm`): `79`
- flavor families: `6`

Implication:
- include explicit tests for:
  - region suffix correctness (`EU/ME/Japan`)
  - out-of-stock device handling (no hallucinated availability)

## 7) Recommended adjustments to eval process
1. Keep all cases for routing robustness.
2. For prompt quality scoring, exclude or separately weight:
   - unpaired cases
   - synthetic (`*@example.com`, `test*`)
   - HTML/spam noise
3. Add dedicated stale-state scenario pack (pending_oos older than 48h).
4. Add fulfillment-invariant checks based on observed status classes above.

