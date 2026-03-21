# Synthetic Review Decisions Template

Use one of decisions:
- `APPROVE` = expected values are correct, include case in eval later
- `FIX_EXPECTED` = keep case but adjust expected label/fields
- `DROP` = remove synthetic case

| ID | Score | Proposed Situation | Client Email | Decision | Comment |
|---|---|---|---|---|---|
| syn_needs_reply_true_oos_sounds_good__mixed_language__l1 | 6 | oos_followup | brian.davis@example.com |  |  |
| syn_oos_followup_simple_yes__minimal_body__l1 | 4 | oos_followup | fiona.green@example.com |  |  |
| syn_new_order_after_shipped_state__intent_collision__l1 | 5 | new_order | evan.clark@example.com |  |  |
| syn_new_order_direct_green_5__mixed_language__l1 | 6 | new_order | alice.johnson@example.com |  |  |
| syn_stock_question_region_japan__mixed_language__l1 | 6 | stock_question | sam.tucker@example.com |  |  |
| syn_payment_received_with_order_id__minimal_body__l1 | 4 | payment_received | rachel.kim@example.com |  |  |
| syn_payment_received_sent_it_items_in_state__intent_collision__l1 | 5 | payment_received | quentin.adams@example.com |  |  |
| syn_oos_followup_provides_address__mixed_language__l1 | 6 | oos_followup | julia.nguyen@example.com |  |  |
| syn_shipping_timeline_when_ship__intent_collision__l1 | 5 | shipping_timeline | frank.graham@example.com |  |  |
| syn_not_new_order_hold_reserve__minimal_body__l1 | 4 | other | charlie.wang@example.com |  |  |
| syn_oos_followup_declines__intent_collision__l1 | 5 | oos_followup | hannah.lee@example.com |  |  |
| syn_system_email_noreply_with_real_customer__mixed_language__l1 | 6 | new_order | nancy.olsen@example.com |  |  |
| syn_needs_reply_false_simple_thanks__intent_collision__l1 | 5 | other | zachary.moore@example.com |  |  |
| syn_price_with_qty_becomes_new_order__mixed_language__l1 | 6 | new_order | yvonne.fisher@example.com |  |  |
| syn_cross_thread_new_order_different_product__minimal_body__l1 | 4 | new_order | jack.kelly@example.com |  |  |
| syn_stock_question_specific_japan_regular__intent_collision__l1 | 5 | stock_question | tina.garcia@example.com |  |  |
| syn_new_order_not_oos_shipped_state__intent_collision__l1 | 5 | new_order | kevin.martinez@example.com |  |  |
| syn_combined_messages_two_unread__minimal_body__l1 | 4 | new_order | gina.hart@example.com |  |  |
| syn_price_question_no_qty__intent_collision__l1 | 5 | price_question | wendy.park@example.com |  |  |
| syn_new_order_multi_item__intent_collision__l1 | 5 | new_order | oscar.powell@example.com |  |  |
| syn_stock_question_general_availability__minimal_body__l1 | 4 | stock_question | ursula.white@example.com |  |  |
| syn_tracking_where_is_order__minimal_body__l1 | 4 | tracking | derek.hughes@example.com |  |  |
| syn_new_order_question_format_with_qty__intent_collision__l1 | 5 | new_order | bob.smith@example.com |  |  |
| syn_price_question_compound_flavor__minimal_body__l1 | 4 | price_question | xavier.reed@example.com |  |  |
| syn_payment_received_plus_tracking_multi_intent__minimal_body__l1 | 4 | payment_received | oliver.brown@example.com |  |  |
| syn_thread_history_previous_exchange__mixed_language__l1 | 6 | payment_received | henry.irwin@example.com |  |  |
| syn_needs_reply_false_got_it_non_oos__intent_collision__l1 | 5 | other | carla.evans@example.com |  |  |
| syn_oos_followup_that_works__minimal_body__l1 | 4 | oos_followup | lena.petrov@example.com |  |  |
| syn_payment_received_zelle__intent_collision__l1 | 5 | payment_received | natalie.chen@example.com |  |  |
| syn_stock_question_multi_product__mixed_language__l1 | 6 | stock_question | victor.hall@example.com |  |  |
| edge_new_order_vs_tracking_mix | 5 | new_order | edge.user1@example.com |  |  |
| edge_payment_screenshot_style | 4 | other | edge.user2@example.com |  |  |
| edge_oos_soft_agreement | 6 | oos_followup | edge.user3@example.com |  |  |