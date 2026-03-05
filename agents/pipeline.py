"""
Email Pipeline
--------------

Core orchestration pipeline:
  - process_classified_email: classify email → build result dict
  - classify_and_process: full end-to-end orchestration entry point
  - private helpers: _update_inbound_state, _persist_results

All database persistence is owned by this module.
All Telegram side-effects are delegated to agents.notifier.
"""

import logging
from datetime import datetime, timezone

from agents.checker import check_reply
from agents.classifier import build_classifier_context, run_classification
from agents.context import load_policy
from agents.formatters import format_result
from agents.notifier import (
    build_oos_message,
    notify_checker_issues,
    notify_new_client,
    notify_oos_with_draft,
    notify_price_alerts,
    notify_reply_ready,
)
from agents.router import route_to_handler
from agents.state_updater import update_conversation_state
from db.conversation_state import save_state
from db.memory import (
    calculate_order_price,
    check_stock_for_order,
    get_client,
    get_stock_summary,
    resolve_order_items,
    save_email,
    save_order_items,
    select_best_alternatives,
    update_client,
)
from utils.telegram import send_telegram

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# process_classified_email (moved from agents/reply_templates.py)
# ---------------------------------------------------------------------------

def process_classified_email(classification) -> dict:
    """Process a classified email: classify metadata and prepare router context.

    This function uses ZERO tokens and does not generate text replies.
    It prepares client/stock context and signals whether routing is needed.
    """
    result = {
        "needs_reply": classification.needs_reply,
        "situation": classification.situation,
        "client_email": classification.client_email,
        "client_name": classification.client_name,
        "client_found": False,
        "client_data": None,
        "template_used": False,
        "draft_reply": None,
        "needs_routing": False,
        "stock_issue": None,
    }

    # Look up client via memory layer (always — even if no reply needed)
    client = get_client(classification.client_email)
    if client:
        result["client_found"] = True
        result["client_data"] = client

    # No reply needed — stop here
    if not classification.needs_reply:
        result["draft_reply"] = "(No reply needed)"
        if not client:
            result["client_data"] = {"payment_type": "unknown", "name": "unknown"}
        return result

    # Client not found — can't generate auto-reply
    if not client:
        result["client_data"] = {"payment_type": "unknown", "name": "unknown"}
        result["draft_reply"] = "(Клиент не в базе — авто-ответ не генерируется)"
        return result

    # Stock check for new_order with structured items
    if (
        classification.situation == "new_order"
        and classification.order_items
    ):
        # Guard: skip if stock table is empty (sync hasn't run yet)
        summary = get_stock_summary()
        if summary["total"] > 0:
            items_for_check = [
                {
                    "product_name": oi.product_name,
                    "base_flavor": oi.base_flavor,
                    "quantity": oi.quantity,
                }
                for oi in classification.order_items
            ]

            # Resolve misspelled product names (multi-tier cascade)
            items_for_check, resolve_alerts = resolve_order_items(items_for_check)
            if resolve_alerts:
                result["resolve_alerts"] = resolve_alerts
                logger.warning(
                    "Product name resolution alerts for %s: %s",
                    classification.client_email,
                    resolve_alerts,
                )

                # Tier 4: all tiers failed → route to LLM handler
                # Don't auto-process the order — products are unidentified.
                unresolved_lines = []
                for a in resolve_alerts:
                    cands = ", ".join(a.get("candidates", [])[:3])
                    unresolved_lines.append(
                        f'- "{a["original"]}" '
                        f'(confidence: {a["confidence"]}, '
                        f'candidates: {cands or "none"})'
                    )
                result["situation"] = "other"
                result["needs_routing"] = True
                result["unresolved_context"] = (
                    "UNRESOLVED PRODUCTS in this order:\n"
                    + "\n".join(unresolved_lines)
                    + "\n\nThe system could not identify these products "
                    "after checking aliases, fuzzy matching, and LLM. "
                    "Ask the customer to clarify which products they want, "
                    "or escalate to the operator."
                )
                return result

            stock_result = check_stock_for_order(items_for_check)

            if not stock_result["all_in_stock"]:
                # Select up to three alternatives per insufficient item.
                best_alternatives = {}
                already_suggested: set[str] = set()
                for insuff in stock_result["insufficient_items"]:
                    best = select_best_alternatives(
                        client_email=classification.client_email,
                        base_flavor=insuff["base_flavor"],
                        max_options=3,
                        client_summary=client.get("llm_summary", "") if client else "",
                        excluded_products=already_suggested,
                    )
                    best_alternatives[insuff["base_flavor"]] = best
                    already_suggested.update(
                        a["alternative"]["product_name"]
                        for a in best.get("alternatives", [])
                    )

                result["stock_issue"] = {
                    "stock_check": stock_result,
                    "best_alternatives": best_alternatives,
                }
                result["needs_routing"] = True
                logger.info(
                    "Stock insufficient for %s: %s (alternatives: %s)",
                    classification.client_email,
                    [i["base_flavor"] for i in stock_result["insufficient_items"]],
                    {k: v.get("reason", "none_available") for k, v in best_alternatives.items()},
                )
                return result

            # --- Price calculation (all items in stock) ---
            calculated_price = calculate_order_price(stock_result["items"])
            result["calculated_price"] = calculated_price

            if (
                classification.parser_used
                and classification.price
                and calculated_price is not None
            ):
                site_clean = classification.price.replace("$", "").replace(",", "")
                try:
                    site_num = float(site_clean)
                    if abs(site_num - calculated_price) > 0.01:
                        result["price_alert"] = {
                            "type": "mismatch",
                            "site_price": classification.price,
                            "calculated_price": f"${calculated_price:.2f}",
                        }
                except (ValueError, TypeError):
                    pass

            if not classification.parser_used and calculated_price is None:
                result["price_alert"] = {
                    "type": "unmatched",
                    "items": [
                        item["base_flavor"]
                        for item in stock_result["items"]
                    ],
                }

    # All reply generation is delegated to specialized handlers via router.
    result["needs_routing"] = True
    return result


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _update_inbound_state(
    gmail_thread_id: str | None,
    email_text: str,
    classification,
    pre_state_record: dict | None,
) -> dict | None:
    """Update ConversationState after inbound classification.

    Returns the updated state dict, or None if update was skipped/failed.
    """
    if not gmail_thread_id:
        return None

    try:
        # Reuse state from build_classifier_context (avoid double DB query)
        current_state = pre_state_record.get("state") if pre_state_record else None

        updated_state = update_conversation_state(
            current_state=current_state,
            email_text=email_text,
            situation=classification.situation,
            direction="inbound",
            client_email=classification.client_email,
            order_id=classification.order_id,
            price=classification.price,
        )

        # Protect pending_oos_resolution from LLM state updater
        if (
            current_state
            and current_state.get("facts", {}).get("pending_oos_resolution")
            and not updated_state.get("facts", {}).get("pending_oos_resolution")
            and classification.situation == "oos_followup"
        ):
            updated_state.setdefault("facts", {})["pending_oos_resolution"] = (
                current_state["facts"]["pending_oos_resolution"]
            )
            logger.warning(
                "Restored pending_oos_resolution stripped by state updater for %s",
                classification.client_email,
            )

        save_state(
            gmail_thread_id=gmail_thread_id,
            client_email=classification.client_email,
            state_json=updated_state,
            situation=classification.situation,
        )

        logger.info(
            "Conversation state updated: thread=%s, status=%s",
            gmail_thread_id, updated_state.get("status"),
        )
        return updated_state

    except Exception as e:
        logger.error("Failed to update conversation state: %s", e, exc_info=True)
        return None


def _persist_results(
    classification,
    result: dict,
    gmail_thread_id: str | None,
    gmail_message_id: str | None,
    email_text: str,
) -> None:
    """Persist emails, order items, address and conversation state to the DB."""
    # Step 5: Extract subject from email text
    subject = ""
    for line in email_text.split("\n"):
        if line.lower().startswith("subject:"):
            subject = line.split(":", 1)[1].strip()
            break

    # Step 5: Save inbound email
    save_email(
        client_email=classification.client_email,
        direction="inbound",
        subject=subject,
        body=email_text,
        situation=classification.situation,
        gmail_message_id=gmail_message_id,
        gmail_thread_id=gmail_thread_id,
    )

    if result["needs_reply"] and result.get("draft_reply"):
        # Save outbound reply
        save_email(
            client_email=classification.client_email,
            direction="outbound",
            subject=f"Re: {subject}" if subject else "",
            body=result["draft_reply"],
            situation=classification.situation,
            gmail_thread_id=gmail_thread_id,
        )

        # Update state with outbound draft (Python, no LLM needed)
        if gmail_thread_id and result.get("conversation_state"):
            try:
                state = result["conversation_state"]
                state.setdefault("last_exchange", {})["we_said"] = result["draft_reply"][:200]

                # Save pending_oos_resolution for oos_followup handler
                if (
                    classification.situation == "new_order"
                    and result.get("stock_issue")
                    and result.get("template_used")
                ):
                    stock_issue = result["stock_issue"]
                    stock_check = stock_issue["stock_check"]
                    state.setdefault("facts", {})["pending_oos_resolution"] = {
                        "order_id": classification.order_id,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "items": [
                            {
                                "base_flavor": i["base_flavor"],
                                "product_name": i["product_name"],
                                "requested_qty": i["ordered_qty"],
                                "available_qty": i["total_available"],
                            }
                            for i in stock_check["insufficient_items"]
                        ],
                        "alternatives": {
                            flavor: {
                                "alternatives": [
                                    {
                                        "product_name": a["alternative"]["product_name"],
                                        "category": a["alternative"]["category"],
                                    }
                                    for a in alt_data.get("alternatives", [])
                                ]
                            }
                            for flavor, alt_data in stock_issue.get("best_alternatives", {}).items()
                        },
                        "in_stock_items": [
                            {
                                "base_flavor": i["base_flavor"],
                                "product_name": i["product_name"],
                                "ordered_qty": i["ordered_qty"],
                            }
                            for i in stock_check["items"]
                            if i["is_sufficient"]
                        ],
                    }
                    logger.info(
                        "Saved pending_oos_resolution for %s (thread=%s)",
                        classification.client_email, gmail_thread_id,
                    )

                save_state(
                    gmail_thread_id=gmail_thread_id,
                    client_email=classification.client_email,
                    state_json=state,
                    situation=classification.situation,
                )
            except Exception as e:
                logger.error("Failed to update state for outbound: %s", e)

    # Step 6: Save structured order items for preference tracking
    if (
        classification.situation == "new_order"
        and classification.order_items
        and result["client_found"]
    ):
        save_order_items(
            client_email=classification.client_email,
            order_id=classification.order_id,
            order_items=[
                {
                    "product_name": oi.product_name,
                    "base_flavor": oi.base_flavor,
                    "quantity": oi.quantity,
                }
                for oi in classification.order_items
            ],
        )

    # Step 6.5: Auto-save client address if extracted from email
    if result["client_found"] and (
        classification.customer_street or classification.customer_city_state_zip
    ):
        address_updates = {}
        if classification.customer_street:
            address_updates["street"] = classification.customer_street
        if classification.customer_city_state_zip:
            address_updates["city_state_zip"] = classification.customer_city_state_zip
        try:
            update_client(classification.client_email, **address_updates)
            logger.info(
                "Auto-saved address for %s: %s",
                classification.client_email, address_updates,
            )
        except Exception as e:
            logger.warning("Failed to auto-save address: %s", e)

    # Step 7: Auto-refresh client summary if stale
    if result["client_found"]:
        try:
            from agents.client_profiler import maybe_refresh_summary
            maybe_refresh_summary(classification.client_email)
        except Exception as e:
            logger.error("Auto-refresh summary failed: %s", e)


# ---------------------------------------------------------------------------
# Top-level orchestrator (moved from agents/email_agent.py)
# ---------------------------------------------------------------------------

def classify_and_process(
    email_text: str,
    gmail_message_id: str | None = None,
    gmail_thread_id: str | None = None,
) -> str:
    """Classify an incoming email and generate a reply draft.

    Handles classification (LLM), context prep (Python), and routed handling.
    Returns formatted result with classification, client data, and draft reply.

    Args:
        email_text: The full email text including From, Subject, Body etc.
        gmail_message_id: Optional Gmail message ID for deduplication.
        gmail_thread_id: Optional Gmail thread ID for thread tracking.

    Returns:
        Formatted classification result with draft reply if template exists.
    """
    try:
        # Step 0.5: Get conversation state + thread history for classifier context
        context_str, pre_state_record = build_classifier_context(gmail_thread_id, email_text)

        # Step 0.9 + 1: Deterministic parser or LLM classification
        classification = run_classification(email_text, context_str)

        logger.info(
            "Classified: email=%s, situation=%s, needs_reply=%s",
            classification.client_email, classification.situation, classification.needs_reply,
        )

        # Step 2: Python processes (0 tokens — pure logic)
        result = process_classified_email(classification)

        # Attach gmail_thread_id for downstream context building
        result["gmail_thread_id"] = gmail_thread_id

        # Step 2.5: State Updater LLM — update ConversationState
        result["conversation_state"] = _update_inbound_state(
            gmail_thread_id, email_text, classification, pre_state_record
        )

        # Telegram: notify if new client or price issues
        notify_new_client(classification, result)
        notify_price_alerts(classification, result, gmail_thread_id)

        tg_msg = None
        tg_sent = False

        # Build OOS telegram message (sent after routing + draft ready)
        if result.get("stock_issue"):
            tg_msg = build_oos_message(classification, result)

        # Step 3: Route to specialized handler
        if result.get("needs_routing") and result["needs_reply"]:
            logger.info(
                "Routing to handler: situation=%s, client=%s",
                classification.situation, classification.client_email,
            )
            result = route_to_handler(classification, result, email_text)
            result["needs_routing"] = False

            # Step 3.5: Checker — validate the draft (rule-based + LLM)
            checker_obj = None
            if result.get("draft_reply") and not result.get("template_used"):
                try:
                    checker_obj = check_reply(
                        draft=result["draft_reply"],
                        result=result,
                        conversation_state=result.get("conversation_state"),
                        policy_rules=load_policy(classification.situation),
                        run_llm_check=True,
                    )
                    result["check_result"] = {
                        "is_ok": checker_obj.is_ok,
                        "warnings": checker_obj.warnings,
                        "suggestions": checker_obj.suggestions,
                        "rule_violations": checker_obj.rule_violations,
                        "llm_issues": checker_obj.llm_issues,
                    }
                    if not checker_obj.is_ok:
                        logger.warning("Checker flagged issues: %s", checker_obj.warnings)
                except Exception as e:
                    logger.error("Checker failed: %s", e, exc_info=True)
                    result["check_result"] = None

            tg_sent = notify_oos_with_draft(tg_msg, result, checker_obj)
            if not tg_sent:
                tg_sent = notify_checker_issues(classification, result, checker_obj)

        # Send Telegram for all other processed emails
        if not tg_sent:
            notify_reply_ready(classification, result)

        # Step 4: Format the output
        logger.info(
            "Done: email=%s, template=%s, client_found=%s",
            classification.client_email, result["template_used"], result["client_found"],
        )
        formatted = format_result(result)

        # Step 5-7: Persist everything
        _persist_results(classification, result, gmail_thread_id, gmail_message_id, email_text)

        return formatted

    except Exception as e:
        logger.error("Email processing failed: %s", e, exc_info=True)
        send_telegram(
            f"\U0001f6a8 <b>Ошибка обработки email!</b>\n\n"
            f"Ошибка: {e}\n"
            f"Email: {email_text[:200]}...\n\n"
            f"Проверь логи контейнера."
        )
        return f"ERROR: Email processing failed — {e}"
