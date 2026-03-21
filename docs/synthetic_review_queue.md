# Synthetic Cases Requiring Manual Review

Rule: all synthetic cases with `score < 7` must be reviewed before scoring.

| ID | Score | Proposed Situation | Client Email | Reasons |
|---|---|---|---|---|
| syn_needs_reply_true_oos_sounds_good__mixed_language__l1 | 6 | oos_followup | brian.davis@example.com | mixed language, ambiguous short question |
| syn_oos_followup_simple_yes__minimal_body__l1 | 4 | oos_followup | fiona.green@example.com | too short, insufficient intent signal |
| syn_new_order_after_shipped_state__intent_collision__l1 | 5 | new_order | evan.clark@example.com | mixed intent in one message, routing ambiguity likely |
| syn_new_order_direct_green_5__mixed_language__l1 | 6 | new_order | alice.johnson@example.com | mixed language, ambiguous short question |
| syn_stock_question_region_japan__mixed_language__l1 | 6 | stock_question | sam.tucker@example.com | mixed language, ambiguous short question |
| syn_payment_received_with_order_id__minimal_body__l1 | 4 | payment_received | rachel.kim@example.com | too short, insufficient intent signal |
| syn_payment_received_sent_it_items_in_state__intent_collision__l1 | 5 | payment_received | quentin.adams@example.com | mixed intent in one message, routing ambiguity likely |
| syn_oos_followup_provides_address__mixed_language__l1 | 6 | oos_followup | julia.nguyen@example.com | mixed language, ambiguous short question |
| syn_shipping_timeline_when_ship__intent_collision__l1 | 5 | shipping_timeline | frank.graham@example.com | mixed intent in one message, routing ambiguity likely |
| syn_not_new_order_hold_reserve__minimal_body__l1 | 4 | other | charlie.wang@example.com | too short, insufficient intent signal |
| syn_oos_followup_declines__intent_collision__l1 | 5 | oos_followup | hannah.lee@example.com | mixed intent in one message, routing ambiguity likely |
| syn_system_email_noreply_with_real_customer__mixed_language__l1 | 6 | new_order | nancy.olsen@example.com | mixed language, ambiguous short question |
| syn_needs_reply_false_simple_thanks__intent_collision__l1 | 5 | other | zachary.moore@example.com | mixed intent in one message, routing ambiguity likely |
| syn_price_with_qty_becomes_new_order__mixed_language__l1 | 6 | new_order | yvonne.fisher@example.com | mixed language, ambiguous short question |
| syn_cross_thread_new_order_different_product__minimal_body__l1 | 4 | new_order | jack.kelly@example.com | too short, insufficient intent signal |
| syn_stock_question_specific_japan_regular__intent_collision__l1 | 5 | stock_question | tina.garcia@example.com | mixed intent in one message, routing ambiguity likely |
| syn_new_order_not_oos_shipped_state__intent_collision__l1 | 5 | new_order | kevin.martinez@example.com | mixed intent in one message, routing ambiguity likely |
| syn_combined_messages_two_unread__minimal_body__l1 | 4 | new_order | gina.hart@example.com | too short, insufficient intent signal |
| syn_price_question_no_qty__intent_collision__l1 | 5 | price_question | wendy.park@example.com | mixed intent in one message, routing ambiguity likely |
| syn_new_order_multi_item__intent_collision__l1 | 5 | new_order | oscar.powell@example.com | mixed intent in one message, routing ambiguity likely |
| syn_stock_question_general_availability__minimal_body__l1 | 4 | stock_question | ursula.white@example.com | too short, insufficient intent signal |
| syn_tracking_where_is_order__minimal_body__l1 | 4 | tracking | derek.hughes@example.com | too short, insufficient intent signal |
| syn_new_order_question_format_with_qty__intent_collision__l1 | 5 | new_order | bob.smith@example.com | mixed intent in one message, routing ambiguity likely |
| syn_price_question_compound_flavor__minimal_body__l1 | 4 | price_question | xavier.reed@example.com | too short, insufficient intent signal |
| syn_payment_received_plus_tracking_multi_intent__minimal_body__l1 | 4 | payment_received | oliver.brown@example.com | too short, insufficient intent signal |
| syn_thread_history_previous_exchange__mixed_language__l1 | 6 | payment_received | henry.irwin@example.com | mixed language, ambiguous short question |
| syn_needs_reply_false_got_it_non_oos__intent_collision__l1 | 5 | other | carla.evans@example.com | mixed intent in one message, routing ambiguity likely |
| syn_oos_followup_that_works__minimal_body__l1 | 4 | oos_followup | lena.petrov@example.com | too short, insufficient intent signal |
| syn_payment_received_zelle__intent_collision__l1 | 5 | payment_received | natalie.chen@example.com | mixed intent in one message, routing ambiguity likely |
| syn_stock_question_multi_product__mixed_language__l1 | 6 | stock_question | victor.hall@example.com | mixed language, ambiguous short question |
| edge_new_order_vs_tracking_mix | 5 | new_order | edge.user1@example.com | mixed intent (order + tracking) |
| edge_payment_screenshot_style | 4 | other | edge.user2@example.com | attachment-only style message |
| edge_oos_soft_agreement | 6 | oos_followup | edge.user3@example.com | soft/conditional agreement wording |

## Preview
- `syn_needs_reply_true_oos_sounds_good__mixed_language__l1` score=6 :: From: brian.davis@example.com
Subject: Re: Stock alternatives
Body: sounds good

spasibo. Can u confirm?
- `syn_oos_followup_simple_yes__minimal_body__l1` score=4 :: From: fiona.green@example.com
Subject: Re: Out of stock notice
Body: ok
- `syn_new_order_after_shipped_state__intent_collision__l1` score=5 :: From: evan.clark@example.com
Subject: Re: Your order has shipped
Body: Thanks for the tracking! Can I have 2 Terea Sienna?

Also where is my
- `syn_new_order_direct_green_5__mixed_language__l1` score=6 :: From: alice.johnson@example.com
Subject: Order request
Body: Hi James, I want to order 5 boxes of Green. Please let me know the total. Thank
- `syn_stock_question_region_japan__mixed_language__l1` score=6 :: From: sam.tucker@example.com
Subject: Japan flavors
Body: Hey, what Japan do you have?

molim. Can u confirm?
- `syn_payment_received_with_order_id__minimal_body__l1` score=4 :: From: rachel.kim@example.com
Subject: Payment confirmation
Body: yes
- `syn_payment_received_sent_it_items_in_state__intent_collision__l1` score=5 :: From: quentin.adams@example.com
Subject: Re: Payment info
Body: Sent it

Also can you confirm total and shipping date?
- `syn_oos_followup_provides_address__mixed_language__l1` score=6 :: From: julia.nguyen@example.com
Subject: Re: Your order alternatives
Body: my address is 123 Oak St, Apt 4B, Portland OR 97201

molim. Can u 
- `syn_shipping_timeline_when_ship__intent_collision__l1` score=5 :: From: frank.graham@example.com
Subject: Shipping question
Body: When do you ship? I need it by Friday.

Also can you confirm total and shipp
- `syn_not_new_order_hold_reserve__minimal_body__l1` score=4 :: From: charlie.wang@example.com
Subject: Hold request
Body: yes

Hi James, please hold 3 boxes of Silver for me. I'll confirm the order next 
- `syn_oos_followup_declines__intent_collision__l1` score=5 :: From: hannah.lee@example.com
Subject: Re: Availability update
Body: no thanks, I'll pass. Maybe next time.

Also can I get discount if I tak
- `syn_system_email_noreply_with_real_customer__mixed_language__l1` score=6 :: From: noreply@shipmecarton.com
Subject: New Order #67890
Body: Firstname: Nancy
Email: nancy.olsen@example.com
Order: Tera Green made in Mid
- `syn_needs_reply_false_simple_thanks__intent_collision__l1` score=5 :: From: zachary.moore@example.com
Subject: Re: Your order
Body: Thanks!

Also can you confirm total and shipping date?
- `syn_price_with_qty_becomes_new_order__mixed_language__l1` score=6 :: From: yvonne.fisher@example.com
Subject: Price check
Body: how much for 5 boxes of Green?

grazie. Can u confirm?
- `syn_cross_thread_new_order_different_product__minimal_body__l1` score=4 :: From: jack.kelly@example.com
Subject: New order
Body: yes
- `syn_stock_question_specific_japan_regular__intent_collision__l1` score=5 :: From: tina.garcia@example.com
Subject: Question
Body: do you have japan regular?

Also I already paid yesterday, can you check?
- `syn_new_order_not_oos_shipped_state__intent_collision__l1` score=5 :: From: kevin.martinez@example.com
Subject: Re: Tracking info
Body: Hey James, I want 3 green please. New order.

Also where is my previous tr
- `syn_combined_messages_two_unread__minimal_body__l1` score=4 :: From: gina.hart@example.com
Subject: Order
Body: ?
- `syn_price_question_no_qty__intent_collision__l1` score=5 :: From: wendy.park@example.com
Subject: Price inquiry
Body: How much is Turquoise?

Also can you confirm total and shipping date?
- `syn_new_order_multi_item__intent_collision__l1` score=5 :: From: oscar.powell@example.com
Subject: Bulk order
Body: Hi James, I'd like to order:
- 3 Green EU
- 2 Silver EU
- 1 Turquoise ME
Please sen