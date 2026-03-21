# VPS Failed Tests Report (2026-03-18)

## A) Pytest Failures/Errors (combined run)
Run:
`pytest -q tests/test_handler_templates.py tests/test_oos_followup_intents.py tests/test_email_agent_pipeline_smoke.py tests/test_email_agent_router_regression.py`

Result: `2 failed`, `5 errors`, `133 passed`.

### 1) FAILED: test_pending_path_category_to_region
- File: `tests/test_oos_followup_intents.py:815`
- Submitted input (to handler): dialog_intent=`agrees_to_alternative`, pending alternative `{product_name: Bronze, category: TEREA_EUROPE}`, email text=`Ok`.
- Expected: resolver item should include region suffix `EU` in product_name.
- Actual: product_name was `Bronze` (without `EU`).
- Assertion: `AssertionError: 'EU' not found in 'Bronze'`.

### 2) FAILED: test_new_order_template_flow
- File: `tests/test_email_agent_pipeline_smoke.py:512`
- Submitted input email:
```text
From: noreply@shipmecarton.com
Reply-To: client1@example.com
Subject: Shipmecarton - Order 23432
Body:
1 Tera Green EU $110.00 2 $220.00
Payment amount: $220.00
Order ID: 23432
Firstname: Test Client One
Street address1: 123 Main St
Town/City: Springfield
State: Illinois
Postcode/Zip: 62701
Email: client1@example.com
```
- Expected output contains: `Thank you so much for placing an order`.
- Actual output draft: `We'll check and get back to you. Thank you!` (general fallback).
- Extra logs: template skipped because `{PRICE}` missing; fallback to general handler.

### 3) ERRORS (5 tests) in test_email_agent_router_regression.py
- Tests affected:
  - `test_no_reply_path_keeps_inbound_only`
  - `test_oos_telegram_still_sent_with_draft_preview`
  - `test_router_dict_reply_replaces_result_object`
  - `test_router_reply_is_written_to_history`
  - `test_template_path_routes_and_saves_outbound`
- Submitted input: N/A (tests fail in `setUpClass` before request execution).
- Actual error: `ImportError: cannot import name apply_region_preference from db.region_preference` during `import agents.pipeline`.

## B) Classifier Eval Failures (13/41)
Source details file:
`/Users/aleksejcuprynin/Desktop/AgentOC/ag infra up/docs/vps_classifier_failed_cases_details.json`

| Case ID | Input (short) | Expected | Actual | Mismatch |
|---|---|---|---|---|
| new_order_after_shipped_state | From: evan.clark@example.com Subject: Re: Your order has shipped Body: Thanks for the tracking! Can I have 2 Terea Sienn... | situation=new_order, intent=None, needs_reply=True | situation=new_order, intent=None, needs_reply=True | order_items_shape (item[0].region_preference: expected=None, got=['EU']) |
| oos_followup_simple_yes | From: fiona.green@example.com Subject: Re: Out of stock notice Body: yes pls | situation=oos_followup, intent=agrees_to_alternative, needs_reply=True | situation=oos_followup, intent=agrees_to_alternative, needs_reply=True | order_items_shape (item[0].region_preference: expected=['EU'], got=None) |
| oos_followup_asks_question | From: ivan.kowalski@example.com Subject: Re: Stock issue Body: do you have any silver? | situation=oos_followup, intent=asks_question, needs_reply=True | situation=oos_followup, intent=asks_question, needs_reply=True | order_items_presence; order_items_shape (expected 1 items, got null) |
| new_order_not_oos_shipped_state | From: kevin.martinez@example.com Subject: Re: Tracking info Body: Hey James, I want 3 green please. New order. | situation=new_order, intent=None, needs_reply=True | situation=new_order, intent=None, needs_reply=True | order_items_shape (item[0].region_preference: expected=None, got=['EU']) |
| oos_followup_that_works | From: lena.petrov@example.com Subject: Re: Product availability Body: that works for me | situation=oos_followup, intent=agrees_to_alternative, needs_reply=True | situation=oos_followup, intent=agrees_to_alternative, needs_reply=True | order_items_shape (item[0].region_preference: expected=['JAPAN'], got=None) |
| oos_followup_sounds_good_no_pending | From: marco.silva@example.com Subject: Re: Alternatives Body: sounds good | situation=oos_followup, intent=agrees_to_alternative, needs_reply=True | situation=oos_followup, intent=agrees_to_alternative, needs_reply=True | order_items_shape (item[0].region_preference: expected=['EU'], got=None) |
| payment_received_zelle | From: natalie.chen@example.com Subject: Payment sent Body: Hi James, I just sent $220 via Zelle. Let me know when you ge... | situation=payment_received, intent=confirms_payment, needs_reply=True | situation=payment_received, intent=confirms_payment, needs_reply=True | order_items_shape (item[0].region_preference: expected=['EU'], got=None) |
| payment_received_plus_tracking_multi_intent | From: oliver.brown@example.com Subject: Re: Payment instructions Body: Money sent. Where is my tracking? | situation=payment_received, intent=confirms_payment, needs_reply=True | situation=payment_received, intent=confirms_payment, needs_reply=True | order_items_shape (item[0].region_preference: expected=['EU'], got=None) |
| payment_received_with_order_id | From: rachel.kim@example.com Subject: Payment confirmation Body: Payment sent for order #12345. Paid $176 via Zelle. | situation=payment_received, intent=confirms_payment, needs_reply=True | situation=payment_received, intent=confirms_payment, needs_reply=True | order_items_shape (item[0].region_preference: expected=['EU'], got=None) |
| needs_reply_true_oos_sounds_good | From: brian.davis@example.com Subject: Re: Stock alternatives Body: sounds good | situation=oos_followup, intent=agrees_to_alternative, needs_reply=True | situation=oos_followup, intent=agrees_to_alternative, needs_reply=True | order_items_shape (item[0].region_preference: expected=['EU'], got=None) |
| combined_messages_two_unread | From: Gina Hart <gina.hart@example.com> Subject: Order Body: [2 messages from this client in the same thread]  --- Messa... | situation=new_order, intent=None, needs_reply=True | situation=new_order, intent=None, needs_reply=True | order_items_shape (item[0].region_preference: expected=['EU'], got=None; item[1].region_preference: expected=['EU'], got=None) |
| thread_history_previous_exchange | From: henry.irwin@example.com Subject: Re: Order and payment Body: Just sent the payment via Zelle. | situation=payment_received, intent=confirms_payment, needs_reply=True | situation=payment_received, intent=confirms_payment, needs_reply=True | order_items_shape (item[0].region_preference: expected=['EU'], got=None) |
| new_order_strict_region_eu_only | From: louis.mann@example.com Subject: Order EU only Body: 2 boxes of Silver EU only please. No other region. | situation=new_order, intent=None, needs_reply=True | situation=new_order, intent=None, needs_reply=True | order_items_shape (item[0].region_preference: expected=['EU'], got=None) |

### Pattern in classifier fails
- 12/13 failures are not situation errors; they are `order_items_shape` mismatches (mostly `region_preference expected [...], got None`).
- 1 case (`oos_followup_asks_question`) missed `order_items` presence entirely (`expected 1 items, got null`).