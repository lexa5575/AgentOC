# Email Agent Refactor: Detailed Phased Plan

## 0) Context and goals

### Problem
- `agents/email_agent.py` is a God Object (classification + orchestration + side effects + persistence).
- `agents/reply_templates.py` is overloaded (models + formatters + processing + templates).
- Current coupling makes changes risky and forces fragile test patching.

### Refactor goals
1. Enforce single responsibility by splitting into focused modules.
2. Keep runtime behavior stable while moving code.
3. Keep `tools/gmail_poller.py` import compatibility:
   - `from agents.email_agent import classify_and_process` must continue to work.
4. Migrate tests to patch real call sites after code moves.

### Strategy
- **Phased migration** (5 phases).
- One phase per PR, every phase ends green.
- No mixed “logic changes + structural moves” inside same phase.
- Temporary compatibility aliases are allowed until final cleanup.

## 1) Current dependency map (before refactor)

### Runtime imports from `agents.reply_templates`
- `agents/email_agent.py` imports:
  - `EmailClassification`, `OrderItem`
  - `format_result`, `format_thread_for_classifier`
  - `process_classified_email`
- `tools/email_parser.py` imports:
  - `EmailClassification`, `OrderItem`
- `agents/context.py` imports:
  - `format_email_history`
- `agents/client_profiler.py` imports:
  - `format_email_history`
- `agents/handlers/new_order.py` imports:
  - `fill_out_of_stock_template`
- `agents/handlers/template_utils.py` imports:
  - `REPLY_TEMPLATES`
- `agents/handlers/oos_followup.py` imports:
  - `REPLY_TEMPLATES`

### Test dependencies that will break if moved blindly
- `tests/test_pricing.py`:
  - imports and patches `agents.reply_templates.*`
- `tests/test_context.py`:
  - imports `EmailClassification` from `agents.reply_templates`
- `tests/test_email_agent.py`:
  - imports `save_order_items` via `agents.email_agent` namespace
- `tests/test_cross_thread_context.py`:
  - imports `_extract_sender_email`, `_format_other_threads` from `agents.email_agent`
- `tests/test_email_agent_pipeline_smoke.py`:
  - patches `self.reply_templates.*` and `self.email_agent.*`
- `tests/test_email_agent_router_regression.py`:
  - patches `self.email_agent.*`

## 2) Target architecture

```text
agents/
  models.py          # OrderItem, EmailClassification
  formatters.py      # format_email_history, format_thread_for_classifier, format_result
  classifier.py      # classifier_agent, parser/LLM classification flow, context helpers
  notifier.py        # all Telegram message builders/senders for pipeline
  pipeline.py        # process_classified_email + classify_and_process orchestration
  email_agent.py     # only Agent wiring + classify_and_process re-export
  reply_templates.py # only REPLY_TEMPLATES + OOS template helpers
```

## 3) Migration rules (mandatory)

1. One phase = one isolated PR.
2. Phase starts only after previous phase tests are green.
3. Do not delete compatibility aliases until Phase 5.
4. During moves, preserve function signatures and return contracts.
5. Test patching rule:
   - always patch function in module **where it is called**, not where originally defined.

## 4) Phase-by-phase execution

## Phase 1: Extract pure models/formatters (low risk)

### Objective
Move side-effect-free code first to reduce coupling early.

### Files to create
1. `agents/models.py`
   - `OrderItem` (from `reply_templates.py`)
   - `EmailClassification` (from `reply_templates.py`)
2. `agents/formatters.py`
   - `format_email_history`
   - `format_thread_for_classifier`
   - `format_result`

### Files to update
1. `agents/context.py`
   - import `format_email_history` from `agents.formatters`
2. `agents/client_profiler.py`
   - import `format_email_history` from `agents.formatters`
3. `tools/email_parser.py`
   - import models from `agents.models`
4. `agents/reply_templates.py`
   - keep temporary compatibility aliases for moved symbols:
     - import from `agents.models` and `agents.formatters`
     - expose old names until Phase 5 cleanup

### Non-goals in Phase 1
- No pipeline/orchestrator moves yet.
- No notifier extraction yet.
- No behavioral edits in classification logic.

### Validation commands
```bash
python -m pytest tests/test_context.py -v
python -m pytest tests/test_email_parser.py -v
python -m pytest tests/test_client_profiler.py -v
```

### Definition of done
1. New modules exist and are imported by runtime call sites.
2. Old imports still work through compatibility aliases.
3. Focused tests above are green.

## Phase 2: Extract classifier module

### Objective
Move all classification-related logic out of `email_agent.py`.

### File to create
1. `agents/classifier.py` with:
   - `classifier_instructions`
   - `classifier_agent`
   - `_find_value`
   - `_extract_sender_email`
   - `_format_other_threads`
   - `build_classifier_context(gmail_thread_id, email_text) -> tuple[str, dict | None]`
   - `run_classification(email_text, context_str) -> EmailClassification`

### Source mapping
- From `agents/email_agent.py`:
  - instruction block
  - helper functions (`_find_value`, `_extract_sender_email`, `_format_other_threads`)
  - classification and context-building segments from `classify_and_process`

### Imports expected in `agents/classifier.py`
```python
from agents.models import EmailClassification, OrderItem
from agents.formatters import format_thread_for_classifier
from db.conversation_state import get_state, get_client_states
from db.memory import get_full_thread_history
from tools.email_parser import try_parse_order, clean_email_body
```

### Files to update
1. `agents/email_agent.py`
   - stop owning classifier internals (import from `agents.classifier`)
   - keep top-level behavior unchanged
2. `tests/test_cross_thread_context.py`
   - import `_extract_sender_email` and `_format_other_threads` from `agents.classifier`
   - in stubs, replace `agents.reply_templates` formatter stub with `agents.formatters` if needed

### Validation commands
```bash
python -m pytest tests/test_cross_thread_context.py -v
python -m pytest tests/test_email_agent_pipeline_smoke.py -v
```

### Definition of done
1. Classifier logic is fully moved and called through new module.
2. `classify_and_process` output is unchanged on smoke scenarios.
3. Related tests are green.

## Phase 3: Extract pipeline + notifier

### Objective
Isolate orchestration and side effects from Agent wiring.

### Files to create
1. `agents/notifier.py`
   - `notify_new_client`
   - `notify_price_alerts`
   - `build_oos_message`
   - `notify_oos_with_draft`
   - `notify_checker_issues`
   - `notify_reply_ready`
2. `agents/pipeline.py`
   - `process_classified_email` (moved from `reply_templates.py`)
   - `_update_inbound_state(...)`
   - `_persist_results(...)`
   - `classify_and_process(...)`

### File to slim
1. `agents/email_agent.py`
   - keep only:
     - `email_agent_instructions`
     - `email_agent = Agent(...)`
     - `from agents.pipeline import classify_and_process` (re-export)
   - keep `tools=[classify_and_process]`

### Compatibility requirement
- `tools/gmail_poller.py` currently imports `classify_and_process` from `agents.email_agent`.
- That import path must remain valid after Phase 3.

### Validation commands
```bash
python -m pytest tests/test_email_agent_pipeline_smoke.py -v
python -m pytest tests/test_email_agent_router_regression.py -v
python -m pytest tests/test_cross_thread_context.py -v
```

### Definition of done
1. Pipeline owns orchestration and persistence.
2. Notifier owns all Telegram logic.
3. Email agent remains a thin wiring layer.
4. `gmail_poller` import contract remains intact.

## Phase 4: Test migration to new call sites

### Objective
Update test imports and patch targets after module moves.

### 4.1 `tests/test_pricing.py`
- Update imports:
  - `EmailClassification`, `OrderItem` from `agents.models`
  - `process_classified_email` from `agents.pipeline`
- Update patch decorators:
  - from `agents.reply_templates.*` to `agents.pipeline.*`

### 4.2 `tests/test_context.py`
- Replace `from agents.reply_templates import EmailClassification`
  with `from agents.models import EmailClassification`.

### 4.3 `tests/test_email_agent.py`
- Replace:
  - `from agents.email_agent import email_agent, save_order_items`
- With:
  - `from agents.email_agent import email_agent`
  - `from db.memory import save_order_items`

### 4.4 `tests/test_cross_thread_context.py`
- Replace imports:
  - from `agents.email_agent` private helpers
  - to `agents.classifier`.
- Ensure stubs reference `agents.formatters` (not old `agents.reply_templates` formatter entry point).

### 4.5 `tests/test_email_agent_pipeline_smoke.py`
- In `_install_import_stubs()`, clear moved modules on re-import:
  - `agents.pipeline`, `agents.classifier`, `agents.notifier`, `agents.formatters`, `agents.models`
- Do not fake-stub these new modules; import real modules with external deps stubbed.
- In `setUpClass`, import:
  - `agents.pipeline` as `self.agents_pipeline`
  - `agents.classifier` as `self.agents_classifier`
  - `agents.notifier` as `self.agents_notifier`
- Update patch targets:
  - `self.reply_templates.get_client` -> `self.agents_pipeline.get_client`
  - `self.reply_templates.get_stock_summary` -> `self.agents_pipeline.get_stock_summary`
  - `self.reply_templates.resolve_order_items` -> `self.agents_pipeline.resolve_order_items`
  - `self.reply_templates.check_stock_for_order` -> `self.agents_pipeline.check_stock_for_order`
  - `self.reply_templates.select_best_alternatives` -> `self.agents_pipeline.select_best_alternatives`
  - `self.email_agent.save_email` -> `self.agents_pipeline.save_email`
  - `self.email_agent.save_order_items` -> `self.agents_pipeline.save_order_items`
  - `self.email_agent.send_telegram` -> `self.agents_notifier.send_telegram`
  - `self.email_agent.check_reply` -> `self.agents_pipeline.check_reply`
  - `self.email_agent.update_conversation_state` -> `self.agents_pipeline.update_conversation_state`
  - `self.email_agent.classifier_agent.run` -> `self.agents_classifier.classifier_agent.run`

### 4.6 `tests/test_email_agent_router_regression.py`
- In `_install_import_stubs()`, include moved modules in clear list:
  - `agents.pipeline`, `agents.classifier`, `agents.notifier`, `agents.formatters`, `agents.models`
- In `setUpClass`, import:
  - `self.agents_pipeline`
  - `self.agents_classifier`
  - `self.agents_notifier`
  - `self.agents_formatters` (optional, if direct patching needed)
- Update patch targets:
  - `self.email_agent.process_classified_email` -> `self.agents_pipeline.process_classified_email`
  - `self.email_agent.format_result` -> `self.agents_pipeline.format_result`
  - `self.email_agent.route_to_handler` -> `self.agents_pipeline.route_to_handler`
  - `self.email_agent.save_email` -> `self.agents_pipeline.save_email`
  - `self.email_agent.send_telegram` -> `self.agents_notifier.send_telegram`
  - `self.email_agent.classifier_agent.run` -> `self.agents_classifier.classifier_agent.run`

### Validation commands
```bash
python -m pytest tests/test_pricing.py -v
python -m pytest tests/test_context.py -v
python -m pytest tests/test_email_agent.py -v
python -m pytest tests/test_cross_thread_context.py -v
python -m pytest tests/test_email_agent_pipeline_smoke.py -v
python -m pytest tests/test_email_agent_router_regression.py -v
```

### Definition of done
1. All updated test suites pass.
2. No test still patches moved symbols on obsolete module paths.

## Phase 5: Final cleanup and removal of compatibility layer

### Objective
Finish SRP split and remove transitional exports.

### 5.1 Slim `agents/reply_templates.py`
- Keep only:
  - `REPLY_TEMPLATES`
  - `_format_alternative`
  - `fill_out_of_stock_template`
- Remove:
  - model classes
  - formatters
  - `process_classified_email`
  - compatibility aliases introduced in early phases

### 5.2 Slim `agents/email_agent.py`
- Keep only Agent wiring + `classify_and_process` import from pipeline.
- No re-exports except `classify_and_process` intentionally kept for compatibility with poller.

### 5.3 Final static checks
- Ensure no runtime imports from old locations remain:
  - no imports of `EmailClassification`, `OrderItem`, `format_*`, `process_classified_email` from `agents.reply_templates`
- Ensure all test imports use final module paths.

### Validation commands
```bash
python -m pytest tests/ -v
python -c "from agents.email_agent import email_agent, classify_and_process; from tools.gmail_poller import poll_gmail; print('import ok')"
```

### Definition of done
1. Full test suite passes.
2. Public compatibility paths required by runtime remain intact.
3. No stale compatibility alias remains.

## 5) Expected file change matrix

| File | Phase | Action |
|---|---:|---|
| `agents/models.py` | 1 | create |
| `agents/formatters.py` | 1 | create |
| `agents/classifier.py` | 2 | create |
| `agents/notifier.py` | 3 | create |
| `agents/pipeline.py` | 3 | create |
| `agents/context.py` | 1 | import update |
| `agents/client_profiler.py` | 1 | import update |
| `tools/email_parser.py` | 1 | import update |
| `agents/reply_templates.py` | 1,5 | temporary alias -> final slim |
| `agents/email_agent.py` | 2,3,5 | gradual thinning |
| `tests/test_pricing.py` | 4 | imports + patch targets |
| `tests/test_context.py` | 4 | imports |
| `tests/test_email_agent.py` | 4 | imports |
| `tests/test_cross_thread_context.py` | 2,4 | imports + stubs |
| `tests/test_email_agent_pipeline_smoke.py` | 4 | patch targets |
| `tests/test_email_agent_router_regression.py` | 4 | patch targets |

## 6) Risk register and mitigations

### Risk A: wrong patch target after move
- Symptom: tests pass/fail inconsistently or patch has no effect.
- Mitigation: patch functions in module where invoked (`agents.pipeline`, `agents.notifier`, `agents.classifier`).

### Risk B: import cycle introduced by premature cleanup
- Symptom: `ImportError` during module import.
- Mitigation: keep transitional aliases until Phase 5; remove only after tests migrated.

### Risk C: runtime break in gmail poller
- Symptom: `tools/gmail_poller.py` cannot import `classify_and_process`.
- Mitigation: keep re-export in `agents/email_agent.py`.

### Risk D: hidden behavior drift during move
- Symptom: different formatted output / different telegram side effects.
- Mitigation: phase-specific smoke/regression tests before proceeding.

## 7) Execution checklist template (per phase)

Use for every PR:

1. Scope limited to one phase only.
2. Code changes completed.
3. Relevant test commands run and green.
4. Diff reviewed for accidental unrelated changes.
5. Short release note recorded.

## 8) Suggested prompt pattern to executor (Claude/Codex)

```text
Выполни только Phase X из docs/EMAIL_AGENT_REFACTOR_PHASED_PLAN.md.
Ограничь изменения только файлами, перечисленными в этой фазе.
Не переходи к следующим фазам.
После изменений запусти только тесты из раздела Validation commands этой фазы.
Покажи:
1) список изменённых файлов
2) ключевой diff
3) результаты тестов
4) подтверждение, что scope фазы не нарушен
```
