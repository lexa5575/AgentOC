# Golden Dataset Testing Plan

## Dataset Snapshot
- Source: `/Users/aleksejcuprynin/Desktop/AgentOC/ag infra up/golden_dataset.json`
- Total cases: 132
- Situation distribution:
  - `oos_followup`: 20
  - `new_order`: 20
  - `stock_question`: 18
  - `payment_received`: 20
  - `payment_question`: 10
  - `discount_request`: 6
  - `tracking`: 5
  - `other`: 20
  - `shipping_timeline`: 10
  - `price_question`: 3
- Benchmark lanes:
  - `gen+route` (есть historical outbound): 98
  - `route-only` (outbound отсутствует): 34

## Goal Split
1. Validate automation end-to-end (`classifier -> router -> handler -> state/persistence`)
2. Validate each remaining LLM handler independently (после удаления части handler’ов)
3. Validate prompt edits with regression-safe checks

## Phase 0: Data Hygiene Gate (must pass first)
Checks:
- JSON valid
- unique IDs
- `situation` matches top-level group key
- `inbound_text` non-empty
- stats report generated (counts, missing outbound, missing profile)

Artifacts:
- `/Users/aleksejcuprynin/Desktop/AgentOC/ag infra up/docs/golden_dataset_case_review.md`
- `/Users/aleksejcuprynin/Desktop/AgentOC/ag infra up/docs/golden_dataset_case_review.csv`

## Phase 1: Routing/Classification Eval (all 132)
Purpose:
- confirm that each case still routes into expected `situation`
- confirm critical extraction fields (`order_items`, quantities, regions, dialog_intent for OOS)

Assertions:
- `predicted_situation == dataset_group`
- For `oos_followup`: intent quality (`agrees/asks/declines`) is scored
- For `stock_question`/`price_question`: product extraction quality is scored

Gate:
- macro situation accuracy >= 95%
- `oos_followup` intent accuracy >= 90%
- no crash cases

## Phase 2: Handler Contract Tests (post-refactor)
Purpose:
- after replacing handlers with Python scripts, verify router contracts remain stable

For each active situation handler:
- input: normalized classification + context
- output contract:
  - `draft_reply` is string
  - `template_used` boolean set correctly
  - `needs_routing == False`
- policy contract:
  - no forbidden hallucinations (price/tracking/product)
  - region suffix constraints where required

Gate:
- 100% contract pass

## Phase 3: LLM Prompt Eval (only active LLM handlers)
Use only `gen+route` lane by default (98 cases), `route-only` for non-generation checks.

Per-case checks (auto):
- style: ends with `Thank you!` when policy requires
- groundedness: no product not present in context/stock tool output
- pricing: no invented totals/amounts
- payment-type consistency: no mixing prepay/postpay rules
- region suffix presence for product mentions

Per-case checks (judge-assisted, optional):
- semantic match against historical outbound (allow paraphrase)

Gate (recommended):
- hard-rule violations: 0
- groundedness pass >= 98%
- payment-type consistency 100%
- overall prompt score >= 4.3/5

## Phase 4: E2E Automation Replay
Replay full pipeline on golden cases (with deterministic mocks where needed):
- state update
- routing
- handler generation
- checker
- persistence side effects (state/order item updates)

Critical E2E assertions:
- no exceptions
- no lost `pending_oos_resolution` in OOS flows
- correct branch for `payment_received` prepay+pending OOS guard
- no fulfillment trigger when source is untrusted (`classifier`)

Gate:
- 100% crash-free
- 100% pass on critical business invariants

## Phase 5: CI Gates and Workflow
PR gates:
1. Data hygiene
2. Classifier/routing eval
3. Handler contract tests
4. LLM prompt eval (active handlers only)
5. E2E replay smoke

Release gate:
- all PR gates pass
- no regression in previous baseline metrics

## Suggested Execution Order After Refactor
1. Freeze current golden dataset and baseline metrics
2. Remove/replace handlers with Python implementations
3. Run Phase 1 + Phase 2 (must be green)
4. Start prompt edits
5. After each prompt change run Phase 3 subset (by affected situation)
6. Nightly run full Phase 3 + Phase 4
