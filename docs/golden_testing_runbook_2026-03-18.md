# Golden Testing Runbook (2026-03-18)

## Current Ground Truth Split
- Source dataset: `/Users/aleksejcuprynin/Desktop/AgentOC/ag infra up/golden_dataset.json`
- Manual audit decisions: `/Users/aleksejcuprynin/Desktop/AgentOC/ag infra up/docs/manual_validation_master_decisions.json`
- Generated eval sets: `/Users/aleksejcuprynin/Desktop/AgentOC/ag infra up/docs/eval_sets`

Current counts:
- `accept`: 96
- `bug`: 3
- `pending`: 4
- `exclude`: 29

## 1) Rebuild Eval Sets (always first)
```bash
cd "/Users/aleksejcuprynin/Desktop/AgentOC/ag infra up"
python scripts/build_golden_eval_sets.py
```

## 2) Core Regression Gate (must pass before prompt work)
```bash
cd "/Users/aleksejcuprynin/Desktop/AgentOC/ag infra up"
python -m pytest \
  tests/test_handler_templates.py \
  tests/test_oos_followup_intents.py \
  tests/test_email_agent_pipeline_smoke.py \
  tests/test_email_agent_router_regression.py
```

## 3) Classifier/Router Gate
```bash
cd "/Users/aleksejcuprynin/Desktop/AgentOC/ag infra up"
python -m tests.eval.run_classifier_eval
```

## 4) Prompt Iteration Loop (per changed handler)
For each prompt/handler change:
1. Run step 2 + step 3.
2. Evaluate only relevant `accept` cases by `situation`.
3. Check `bug` cases to ensure expected corrected behavior.

Quick way to inspect IDs by bucket/situation:
```bash
cd "/Users/aleksejcuprynin/Desktop/AgentOC/ag infra up"
python - <<'PY'
import json
rows=json.load(open("docs/eval_sets/all_cases_with_validation.json"))
for bucket in ("accept_manual","accept_auto","bug","pending","exclude"):
    ids=[r["id"] for r in rows if r["validation"]["bucket"]==bucket]
    print(bucket, len(ids), ids[:20])
PY
```

## 4.1) Synthetic Edge Cases (with score 1-10)
Generate synthetic cases and auto-split:
- score `>= 7` -> ready for auto eval
- score `< 7` -> manual review queue

```bash
cd "/Users/aleksejcuprynin/Desktop/AgentOC/ag infra up"
python scripts/generate_synthetic_eval_cases.py
```

Generated files:
- `/Users/aleksejcuprynin/Desktop/AgentOC/ag infra up/tests/eval/generated/synthetic_cases_ready.json`
- `/Users/aleksejcuprynin/Desktop/AgentOC/ag infra up/tests/eval/generated/synthetic_cases_review_required.json`
- `/Users/aleksejcuprynin/Desktop/AgentOC/ag infra up/docs/synthetic_review_queue.md`
- `/Users/aleksejcuprynin/Desktop/AgentOC/ag infra up/tests/eval/generated/classifier_eval_cases_extended.json`

Run classifier eval on extended set (base + synthetic ready):
```bash
cd "/Users/aleksejcuprynin/Desktop/AgentOC/ag infra up"
python -m tests.eval.run_classifier_eval \
  --cases-path tests/eval/generated/classifier_eval_cases_extended.json
```

## 5) Bug Case Expectations (explicit)
From manual review:
- `296` (`price_question`): expected substitution script, not manual fallback behavior.
- `603` (`other`): expected ignore for screenshot-only payment message.
- `732` (`payment_question`): expected Zelle instruction template.

These 3 IDs must be tracked as targeted fixes until behavior is stable.

## 6) Pending Cases
Pending means intent is valid but workflow output was not generated yet:
- `1127`, `1131`, `1145`, `1154`

After live workflow run, re-check in Gmail and re-label these cases as `accept` or `bug`.

## 7) Release Rule
Before merging prompt changes:
1. No regression in step 2 + step 3.
2. No new violations on `bug` expectations.
3. `pending` not used for pass-rate scoring.
