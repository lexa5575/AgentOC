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
import agents.state_updater as state_updater
from agents.state_updater import update_conversation_state
from db.conversation_state import save_state
from db.memory import (
    calculate_order_price,
    check_stock_for_order,
    get_client,
    get_stock_summary,
    replace_order_items,
    resolve_order_items,
    save_email,
    save_order_items,
    select_best_alternatives,
    update_client,
)
from db.region_preference import apply_region_preference
from db.stock import extract_variant_id, has_ambiguous_variants
from utils.telegram import send_telegram

logger = logging.getLogger(__name__)

# Phase D: statuses where the order cycle is still active
# (safe to reuse order_id, no state reset on new_order)
_ACTIVE_ORDER_STATUSES = {"new", "awaiting_oos_decision", "pending_response"}

from db.catalog import _enrich_display_name_with_region


# ═══════════════════════════════════════════════════════════════════════════
# Thread-backed narrowing helper
# ═══════════════════════════════════════════════════════════════════════════


def _apply_thread_hint_if_needed(items, gmail_thread_id, gmail_account):
    """Lazy-load catalog + thread and apply hint only if cross-family items remain."""
    if not gmail_thread_id:
        return items
    from db.region_family import is_same_family
    from db.catalog import get_catalog_products
    catalog_entries = get_catalog_products()
    cat_map = {e["id"]: e["category"] for e in catalog_entries}

    def _is_cross_family(pids):
        # Fail-closed: unknown pid → treat as cross-family
        if any(p not in cat_map for p in pids):
            return True
        return not is_same_family({cat_map[p] for p in pids})

    has_cross = any(
        len(pids) > 1 and _is_cross_family(pids)
        for item in items
        if (pids := item.get("product_ids") or [])
    )
    if not has_cross:
        return items
    from db.email_history import get_full_thread_history
    from db.region_preference import apply_thread_hint
    thread_messages = get_full_thread_history(gmail_thread_id, gmail_account=gmail_account)
    return apply_thread_hint(items, thread_messages, catalog_entries)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1: Pre-processing — stock checks, price, order ID
# ═══════════════════════════════════════════════════════════════════════════

# ---------------------------------------------------------------------------
# Phase C helpers: shared summary builder + confirmed subset
# ---------------------------------------------------------------------------

def _build_order_summary(
    stock_items: list[dict],
    items_for_check: list[dict],
    client_email: str,
) -> str:
    """Build region-aware order summary for a set of stock items.

    Shared by full-order path and confirmed-subset path (Phase C).
    """
    from db.catalog import get_display_name
    summary_parts = []
    for idx, item in enumerate(stock_items):
        display = item.get("display_name")
        if not display:
            cat = ""
            entries = item.get("stock_entries") or []
            if entries:
                cat = entries[0].get("category", "")
            name = item.get("product_name") or item.get("base_flavor", "")
            display = get_display_name(name, cat)
        sci = items_for_check[idx] if idx < len(items_for_check) else None
        if sci and sci.get("product_ids"):
            vid = extract_variant_id(
                sci["product_ids"], client_email=client_email,
            )
            if vid:
                display = _enrich_display_name_with_region(vid, display)
        summary_parts.append(f"{item['ordered_qty']} x {display}")
    return ", ".join(summary_parts)


def _apply_confirmed_subset_result(
    result: dict,
    stock_result: dict,
    items_for_check: list[dict],
    classification,
) -> None:
    """Set result fields for confirmed subset only (reservable items).

    After this call, the caller should NOT enter the full-order calc block
    — all downstream fields are already set for the subset.
    """
    reservable = [i for i in stock_result["items"] if i["is_sufficient"]]
    reservable_indices = [
        i for i, it in enumerate(stock_result["items"])
        if it["is_sufficient"]
    ]
    subset_items = [
        items_for_check[i] for i in reservable_indices
        if i < len(items_for_check)
    ]

    result["calculated_price"] = calculate_order_price(reservable)
    result["order_summary"] = _build_order_summary(
        reservable, subset_items, classification.client_email,
    )
    result["_stock_check_items"] = subset_items
    result["persist_order_items_override"] = [
        classification.order_items[i] for i in reservable_indices
        if i < len(classification.order_items)
    ]


def process_classified_email(
    classification,
    gmail_message_id: str | None = None,
    gmail_thread_id: str | None = None,
    gmail_account: str = "default",
) -> dict:
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
                    "original_product_name": oi.product_name,
                    "region_preference": getattr(oi, "region_preference", None),
                    "strict_region": getattr(oi, "strict_region", False),
                    "optional": getattr(oi, "optional", False),
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

            # Apply region preference: narrow product_ids to one family
            items_for_check = apply_region_preference(items_for_check)
            # Thread-backed narrowing for remaining cross-family items
            items_for_check = _apply_thread_hint_if_needed(
                items_for_check, gmail_thread_id, gmail_account,
            )

            stock_result = check_stock_for_order(items_for_check)

            if not stock_result["all_in_stock"]:
                # Phase C: split optional vs required OOS
                required_unresolved = []
                optional_unresolved = []
                for insuff in stock_result["insufficient_items"]:
                    if insuff.get("optional"):
                        optional_unresolved.append(insuff)
                    else:
                        required_unresolved.append(insuff)

                reservable = [
                    i for i in stock_result["items"] if i["is_sufficient"]
                ]
                in_order_flavors: set[str] = {
                    item["base_flavor"] for item in reservable
                }

                # --- Branch 1: only optional OOS, reservable exists ---
                # Treat as confirmed order for reservable subset + P.S.
                if not required_unresolved and reservable:
                    _apply_confirmed_subset_result(
                        result, stock_result, items_for_check, classification,
                    )
                    # Find 1 best alternative per optional OOS for P.S.
                    optional_with_alts = []
                    for opt_item in optional_unresolved:
                        best = select_best_alternatives(
                            client_email=classification.client_email,
                            base_flavor=opt_item["base_flavor"],
                            max_options=1,
                            client_summary=client.get("llm_summary", "") if client else "",
                            excluded_base_flavors=in_order_flavors,
                        )
                        optional_with_alts.append({
                            "item": opt_item,
                            "best_alternative": (
                                best.get("alternatives") or [None]
                            )[0],
                        })
                    result["optional_oos_items"] = optional_with_alts
                    logger.info(
                        "Optional-only OOS for %s: %s (reservable: %d)",
                        classification.client_email,
                        [i["base_flavor"] for i in optional_unresolved],
                        len(reservable),
                    )
                    # Fall through to normal order path (price/summary set)

                # --- Branch 3: all optional OOS, nothing reservable ---
                elif not required_unresolved and not reservable:
                    best_alternatives = {}
                    already_suggested: set[str] = set()
                    for insuff in optional_unresolved:
                        best = select_best_alternatives(
                            client_email=classification.client_email,
                            base_flavor=insuff["base_flavor"],
                            max_options=1,
                            client_summary=client.get("llm_summary", "") if client else "",
                            excluded_products=already_suggested,
                        )
                        best_alternatives[insuff["base_flavor"]] = best
                        already_suggested.update(
                            a["alternative"]["product_name"]
                            for a in best.get("alternatives", [])
                        )
                    result["all_oos_optional"] = True
                    result["stock_issue"] = {
                        "stock_check": stock_result,
                        "best_alternatives": best_alternatives,
                    }
                    result["availability_resolution"] = {
                        "decision_required": False,
                        "reservable_items": [],
                        "optional_unresolved_items": optional_unresolved,
                        "alternatives_by_flavor": best_alternatives,
                    }
                    result["needs_routing"] = True
                    logger.info(
                        "All-optional OOS for %s: %s",
                        classification.client_email,
                        [i["base_flavor"] for i in optional_unresolved],
                    )
                    return result

                # --- Branch 2: required OOS (+ maybe optional) ---
                else:
                    best_alternatives = {}
                    already_suggested: set[str] = set()
                    # Build alternatives for all insufficient (required + optional)
                    for insuff in stock_result["insufficient_items"]:
                        best = select_best_alternatives(
                            client_email=classification.client_email,
                            base_flavor=insuff["base_flavor"],
                            max_options=3,
                            client_summary=client.get("llm_summary", "") if client else "",
                            excluded_products=already_suggested,
                            original_product_name=insuff.get("original_product_name"),
                            excluded_base_flavors=in_order_flavors,
                        )
                        best_alternatives[insuff["base_flavor"]] = best
                        already_suggested.update(
                            a["alternative"]["product_name"]
                            for a in best.get("alternatives", [])
                        )

                    # Backward compat for oos_followup handler
                    result["stock_issue"] = {
                        "stock_check": stock_result,
                        "best_alternatives": best_alternatives,
                    }
                    result["availability_resolution"] = {
                        "decision_required": True,
                        "reservable_items": reservable,
                        "unresolved_items": stock_result["insufficient_items"],
                        "alternatives_by_flavor": best_alternatives,
                        "reservable_price": (
                            calculate_order_price(reservable)
                            if reservable else None
                        ),
                    }
                    result["needs_routing"] = True
                    logger.info(
                        "Stock insufficient for %s: %s (alternatives: %s, reservable: %d)",
                        classification.client_email,
                        [i["base_flavor"] for i in stock_result["insufficient_items"]],
                        {k: v.get("reason", "none_available") for k, v in best_alternatives.items()},
                        len(reservable),
                    )
                    return result

            # --- Price calculation (all items in stock or subset already set) ---
            # Skip if _apply_confirmed_subset_result already set the price
            if "calculated_price" not in result:
                calculated_price = calculate_order_price(stock_result["items"])
                result["calculated_price"] = calculated_price
            else:
                calculated_price = result["calculated_price"]

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

            # Build order summary (skip if subset already set by Branch 1)
            if "order_summary" not in result:
                result["order_summary"] = _build_order_summary(
                    stock_result["items"], items_for_check,
                    classification.client_email,
                )
                result["_stock_check_items"] = items_for_check

            # Phase 3 ambiguity gate: block auto-fulfillment if any item
            # has multiple product_ids (plan §9.4, rule §4.3).
            ambiguous = has_ambiguous_variants(
                items_for_check, client_email=classification.client_email,
            )
            if ambiguous:
                result["fulfillment_blocked"] = True
                result["ambiguous_flavors"] = ambiguous
                logger.warning(
                    "Ambiguous variants for %s: %s — fulfillment blocked",
                    classification.client_email, ambiguous,
                )

    # Dual-intent resolve for payment_received:
    # Classifier extracted order_items → client mentioned products + payment in same message.
    # Only activate when NO explicit order_id — if order_id exists, the order was already
    # created via new_order and ClientOrderItem records exist in DB.
    has_explicit_order_id = bool((classification.order_id or "").strip())
    # Set only for payment_received — controls latest-order fallback in trigger
    if classification.situation == "payment_received":
        result["has_explicit_order_id"] = has_explicit_order_id

    if (
        classification.situation == "payment_received"
        and classification.order_items
        and result["client_found"]
        and not has_explicit_order_id
    ):
        # Guard: without gmail_message_id we can't generate unique PAY-* order_id
        if not gmail_message_id:
            logger.warning(
                "Dual-intent payment_received for %s but no gmail_message_id — "
                "skipping resolve to protect idempotency",
                classification.client_email,
            )
            result["payment_items_unresolved"] = True
        else:
            items_for_check = [
                {
                    "product_name": oi.product_name,
                    "base_flavor": oi.base_flavor,
                    "quantity": oi.quantity,
                    "original_product_name": oi.product_name,
                    "region_preference": getattr(oi, "region_preference", None),
                    "strict_region": getattr(oi, "strict_region", False),
                    "optional": getattr(oi, "optional", False),
                }
                for oi in classification.order_items
            ]
            items_for_check, resolve_alerts = resolve_order_items(items_for_check)
            if resolve_alerts:
                logger.warning(
                    "Product resolution failed for payment_received %s: %s",
                    classification.client_email, resolve_alerts,
                )
                result["payment_items_unresolved"] = True
            else:
                # Validate: product_ids, quantity, base_flavor, product_name
                valid = all(
                    item.get("product_ids")
                    and item.get("quantity", 0) > 0
                    and item.get("base_flavor")
                    and item.get("product_name")
                    for item in items_for_check
                )
                if not valid:
                    logger.warning(
                        "Partially resolved items for payment_received %s — skipping",
                        classification.client_email,
                    )
                    result["payment_items_unresolved"] = True
                else:
                    # Apply region preference before ambiguity gate
                    items_for_check = apply_region_preference(items_for_check)
                    # Thread-backed narrowing for remaining cross-family items
                    items_for_check = _apply_thread_hint_if_needed(
                        items_for_check, gmail_thread_id, gmail_account,
                    )
                    result["_stock_check_items"] = items_for_check

                    # Generate PAY-* order_id (order_id guaranteed empty here)
                    auto_id = f"PAY-{gmail_message_id[-12:]}"
                    classification.order_id = auto_id
                    logger.info(
                        "Auto-generated order_id=%s for payment_received %s",
                        auto_id, classification.client_email,
                    )

                    # Ambiguous variant gate (same as new_order above)
                    ambiguous = has_ambiguous_variants(
                        items_for_check, client_email=classification.client_email,
                    )
                    if ambiguous:
                        result["fulfillment_blocked"] = True
                        result["ambiguous_flavors"] = ambiguous
                        logger.warning(
                            "Ambiguous variants for payment_received %s: %s — fulfillment blocked",
                            classification.client_email, ambiguous,
                        )

    # All reply generation is delegated to specialized handlers via router.
    result["needs_routing"] = True
    return result


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2: State management
# ═══════════════════════════════════════════════════════════════════════════

def _update_inbound_state(
    gmail_thread_id: str | None,
    email_text: str,
    classification,
    pre_state_record: dict | None,
    result: dict | None = None,
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
            classification=classification,
            result=result,
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


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3: Persistence — all DB writes after processing
# ═══════════════════════════════════════════════════════════════════════════

def _persist_results(
    classification,
    result: dict,
    gmail_thread_id: str | None,
    gmail_message_id: str | None,
    email_text: str,
    gmail_account: str = "default",
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

                # Fix 1: Transition to awaiting_payment for confirmed prepay orders.
                # Structural signals only — no text sniffing.
                # Guard: stock_issue means OOS template (not payment instructions).
                _effective = result.get("effective_situation") or classification.situation
                _payment_type = (result.get("client_data") or {}).get("payment_type")
                if (
                    state.get("status") in ("new", "awaiting_oos_decision", "pending_response")
                    and _effective == "new_order"
                    and _payment_type == "prepay"
                    and result.get("template_used")
                    and not result.get("stock_issue")
                ):
                    state["status"] = "awaiting_payment"
                    _facts = state.setdefault("facts", {})
                    _facts["payment_method"] = "Zelle"
                    _facts["payment_request_sent"] = True
                    logger.info(
                        "Status -> awaiting_payment (confirmed prepay order) for %s",
                        classification.client_email,
                    )

                # Fix 4b: Mark payment as confirmed when payment_received template sent.
                # Guard: template_situation must be "payment_received" (not "oos_agrees"
                # which re-sends Zelle for pending_oos_resolution clients).
                if (
                    classification.situation == "payment_received"
                    and result.get("template_situation") == "payment_received"
                    and result.get("template_used")
                ):
                    _facts = state.setdefault("facts", {})
                    _facts["payment_confirmed"] = True
                    state["status"] = "pending_response"
                    oq = state.get("open_questions")
                    if oq and isinstance(oq, list):
                        state["open_questions"] = [
                            q for q in oq
                            if "payment" not in q.lower() and "zelle" not in q.lower()
                        ]
                    logger.info(
                        "Payment confirmed: status->pending_response for %s",
                        classification.client_email,
                    )

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
                        "reservable_price": (
                            result.get("availability_resolution", {})
                            .get("reservable_price")
                        ),
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
    # Source-of-truth: resolved _stock_check_items when available (has
    # product_ids, display_name, normalized product_name/base_flavor).
    # Fallback: raw classification.order_items (no variant data).
    if (
        classification.situation in ("new_order", "payment_received")
        and classification.order_items
        and result["client_found"]
    ):
        # Phase B guard: don't persist provisional items when decision pending
        if result.get("availability_resolution", {}).get("decision_required"):
            logger.info(
                "Skipping save_order_items: decision_required for %s",
                classification.client_email,
            )
        else:
            # Phase C: use subset override if optional items were excluded
            source_items = (
                result.get("persist_order_items_override")
                or classification.order_items
            )
            stock_check_items = result.get("_stock_check_items") or []
            use_resolved = len(stock_check_items) == len(source_items)

            if classification.situation == "payment_received" and not use_resolved:
                # For payment_received: ONLY save if items were resolved.
                # Raw classifier items without product_ids are garbage for fulfillment.
                logger.info(
                    "Skipping ClientOrderItem save for payment_received %s: items not resolved",
                    classification.client_email,
                )
            else:
                order_items_for_save = []
                for i, oi in enumerate(source_items):
                    if use_resolved:
                        sci = stock_check_items[i]
                        variant_id = extract_variant_id(
                            sci.get("product_ids"),
                            client_email=classification.client_email,
                        )
                        display_name = sci.get("display_name")
                        if variant_id and display_name:
                            display_name = _enrich_display_name_with_region(
                                variant_id, display_name,
                            )
                        item = {
                            "product_name": sci.get("product_name", oi.product_name),
                            "base_flavor": sci.get("base_flavor", oi.base_flavor),
                            "quantity": sci.get("quantity", oi.quantity),
                            "variant_id": variant_id,
                            "display_name_snapshot": display_name,
                        }
                    else:
                        item = {
                            "product_name": oi.product_name,
                            "base_flavor": oi.base_flavor,
                            "quantity": oi.quantity,
                        }
                    order_items_for_save.append(item)
                save_order_items(
                    client_email=classification.client_email,
                    order_id=classification.order_id,
                    order_items=order_items_for_save,
                )

    # Step 6.1: OOS canonical replace — trusted source persistence gate (plan §7.3)
    from agents.handlers.oos_constants import TRUSTED_SOURCES as _TRUSTED_PERSISTENCE_SOURCES
    if result.get("effective_situation") == "new_order":
        source = result.get("confirmation_source")
        if source in _TRUSTED_PERSISTENCE_SOURCES:
            order_id_norm = (getattr(classification, "order_id", None) or "").strip() or None
            canonical = result.get("canonical_confirmed_items")
            if order_id_norm and canonical:
                oos_stock_check = result.get("_stock_check_items") or []
                oos_items_for_replace = []
                for i, item in enumerate(canonical):
                    entry = {
                        "product_name": item.get("product_name", item.get("base_flavor", "")),
                        "base_flavor": item.get("base_flavor", ""),
                        "quantity": item.get("ordered_qty", item.get("quantity", 1)),
                    }
                    if i < len(oos_stock_check):
                        sci = oos_stock_check[i]
                        oos_variant = extract_variant_id(
                            sci.get("product_ids"),
                            client_email=classification.client_email,
                        )
                        entry["variant_id"] = oos_variant
                        oos_display = sci.get("display_name") or item.get("display_name")
                        if oos_variant and oos_display:
                            oos_display = _enrich_display_name_with_region(
                                oos_variant, oos_display,
                            )
                        entry["display_name_snapshot"] = oos_display
                    oos_items_for_replace.append(entry)
                replace_order_items(
                    client_email=classification.client_email,
                    order_id=order_id_norm,
                    order_items=oos_items_for_replace,
                )
                logger.info(
                    "OOS canonical replace for %s order %s (%d items, source=%s)",
                    classification.client_email, order_id_norm, len(canonical), source,
                )
            else:
                reasons = []
                if not order_id_norm:
                    reasons.append("order_id=None")
                if not canonical:
                    reasons.append("canonical_items empty")
                logger.info(
                    "OOS canonical replace skipped for %s: %s",
                    classification.client_email, ", ".join(reasons),
                )
        else:
            logger.info(
                "OOS canonical replace skipped for %s: source=%s not trusted",
                classification.client_email, source,
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

    # Step 6.6: Save OrderShippingAddress for trusted situations
    _oid = (getattr(classification, "order_id", None) or "").strip()
    _is_trusted_snapshot_source = (
        classification.situation == "new_order"
        or (classification.situation == "payment_received" and _oid.startswith("PAY-"))
    )
    if (
        _oid
        and classification.customer_street
        and classification.customer_city_state_zip
        and _is_trusted_snapshot_source
    ):
        try:
            from db.shipping import save_order_shipping_address
            save_order_shipping_address(
                email=classification.client_email,
                order_id=_oid,
                name=getattr(classification, "client_name", "") or client.get("name", ""),
                street=classification.customer_street,
                csz=classification.customer_city_state_zip,
            )
        except Exception as e:
            logger.warning("Failed to save shipping address snapshot: %s", e)

    # Step 7: Auto-refresh client summary if stale
    if result["client_found"]:
        try:
            from agents.client_profiler import maybe_refresh_summary
            maybe_refresh_summary(classification.client_email, gmail_account=gmail_account)
        except Exception as e:
            logger.error("Auto-refresh summary failed: %s", e)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 4: Main orchestrator
# ═══════════════════════════════════════════════════════════════════════════

def classify_and_process(
    email_text: str,
    gmail_message_id: str | None = None,
    gmail_thread_id: str | None = None,
    gmail_account: str = "default",
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
        # Step 0: Skip emails with empty body (e.g. inline image only, no text)
        _body_marker = "Body:"
        _body_idx = email_text.find(_body_marker)
        _body_content = email_text[_body_idx + len(_body_marker):].strip() if _body_idx >= 0 else email_text.strip()
        if not _body_content:
            logger.info("Skipping email with empty body (gmail_message_id=%s)", gmail_message_id)
            return "Пропущено: письмо без текста (возможно, только изображение/вложение)."

        # Step 0.5: Get conversation state + thread history for classifier context
        context_str, pre_state_record, last_order = build_classifier_context(gmail_thread_id, email_text, gmail_account=gmail_account)

        # Step 0.9 + 1: Deterministic parser or LLM classification
        state_dict = pre_state_record.get("state") if pre_state_record else None
        classification = run_classification(email_text, context_str, conversation_state=state_dict, last_order=last_order)

        # Fix 2B: Fallback override other->payment_received for legacy threads
        # without facts.payment_request_sent. Uses "use email below" as prepay-only
        # marker (postpay templates don't have it).
        if (
            classification.situation == "other"
            and not classification.needs_reply
            and state_dict
        ):
            from agents.classifier import _looks_like_payment_ack
            _facts_2b = state_dict.get("facts") or {}
            _we_said_2b = (state_dict.get("last_exchange") or {}).get("we_said", "")
            if (
                _we_said_2b
                and "use email below" in _we_said_2b.lower()
                and not _facts_2b.get("payment_confirmed")
                and _looks_like_payment_ack(email_text)
            ):
                _order_id_2b = _facts_2b.get("order_id")
                classification.situation = "payment_received"
                classification.needs_reply = True
                classification.dialog_intent = "confirms_payment"
                classification.followup_to = "payment_info"
                if _order_id_2b and not classification.order_id:
                    classification.order_id = _order_id_2b
                logger.info(
                    "Pipeline override: other -> payment_received (legacy Zelle, order=%s) for %s",
                    _order_id_2b, classification.client_email,
                )

        # Business rule: certain situations ALWAYS need a reply,
        # regardless of LLM decision (e.g. "I sent it thanks" looks
        # like an acknowledgment but payment_received needs tracking/Zelle).
        _ALWAYS_REPLY_SITUATIONS = {"payment_received", "new_order", "oos_followup"}
        if (
            not classification.needs_reply
            and classification.situation in _ALWAYS_REPLY_SITUATIONS
        ):
            logger.info(
                "Override needs_reply=True for %s (%s)",
                classification.situation, classification.client_email,
            )
            classification.needs_reply = True

        logger.info(
            "Classified: email=%s, situation=%s, needs_reply=%s",
            classification.client_email, classification.situation, classification.needs_reply,
        )

        # Phase D: detect stale cycle + strong order signal → clean re-classify
        if pre_state_record:
            _old_state = (pre_state_record.get("state") or {})
            _old_status = _old_state.get("status", "new")
            if _old_status not in _ACTIVE_ORDER_STATUSES:
                _has_order_signal = (
                    classification.situation == "new_order"
                    or classification.order_items
                    or getattr(classification, "parser_used", False)
                )
                if _has_order_signal:
                    logger.info(
                        "Stale thread (status=%s) + order signal: "
                        "clean re-classify for %s",
                        _old_status, classification.client_email,
                    )
                    fresh = state_updater.empty_state()
                    # Don't seed with first-pass order_id — it may come
                    # from stale context. Let AUTO-{message_id} own it.
                    pre_state_record["state"] = fresh
                    state_dict = fresh

                    context_str, _, _ = build_classifier_context(
                        gmail_thread_id, email_text,
                        gmail_account=gmail_account,
                        override_state=fresh,
                        override_thread_history=[],
                        override_other_thread_states=[],
                    )
                    # Reuse last_order from first call — thread-independent
                    classification = run_classification(
                        email_text, context_str,
                        conversation_state=state_dict,
                        last_order=last_order,
                    )
                    logger.info(
                        "Re-classified after stale reset: "
                        "situation=%s for %s",
                        classification.situation,
                        classification.client_email,
                    )

        # Phase D: synthetic order_id from pre_state_record + gmail_message_id
        if (
            classification.situation == "new_order"
            and not (classification.order_id or "").strip()
            and gmail_thread_id
        ):
            _existing_state = (pre_state_record or {}).get("state") or {}
            _existing_status = _existing_state.get("status")
            _existing_oid = _existing_state.get("facts", {}).get("order_id")

            if _existing_oid and _existing_status in _ACTIVE_ORDER_STATUSES:
                auto_id = _existing_oid
            elif gmail_message_id:
                auto_id = f"AUTO-{gmail_message_id[-8:]}"
            else:
                auto_id = f"AUTO-{gmail_thread_id[-8:]}"

            classification.order_id = auto_id
            logger.info(
                "Auto-generated order_id=%s for %s",
                auto_id, classification.client_email,
            )

        # Step 2: Python processes (0 tokens — pure logic)
        result = process_classified_email(
            classification,
            gmail_message_id=gmail_message_id,
            gmail_thread_id=gmail_thread_id,
            gmail_account=gmail_account,
        )

        # Attach gmail_thread_id and gmail_account for downstream context building
        result["gmail_thread_id"] = gmail_thread_id
        result["gmail_account"] = gmail_account

        # Step 2.5: State Updater — update ConversationState
        result["conversation_state"] = _update_inbound_state(
            gmail_thread_id, email_text, classification, pre_state_record,
            result=result,
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

            # Phase 2 state enrichment (only for deterministic state path)
            if (
                state_updater._use_llm() != "true"
                and result.get("conversation_state")
            ):
                try:
                    state_updater._enrich_state_after_routing(
                        result["conversation_state"], result, classification,
                    )
                except Exception as e:
                    logger.error("State enrichment failed: %s", e, exc_info=True)

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

        # Step 3.9: Create Gmail draft in the same thread
        draft_reply = result.get("draft_reply") or ""
        _SKIP_PREFIXES = ("(", "—")  # system messages, not real replies
        if (
            result["needs_reply"]
            and draft_reply
            and not draft_reply.startswith(_SKIP_PREFIXES)
            and gmail_thread_id
        ):
            try:
                from tools.gmail import GmailClient

                subject = ""
                for line in email_text.split("\n"):
                    if line.lower().startswith("subject:"):
                        subject = line.split(":", 1)[1].strip()
                        break
                reply_subject = f"Re: {subject}" if subject else ""

                draft_html = result.get("draft_reply_html")
                draft_id = GmailClient(account=gmail_account).create_draft(
                    to=classification.client_email,
                    subject=reply_subject,
                    body=draft_html or draft_reply,
                    thread_id=gmail_thread_id,
                    html=bool(draft_html),
                )
                result["gmail_draft_id"] = draft_id
            except Exception as e:
                logger.error("Failed to create Gmail draft: %s", e, exc_info=True)

        # Step 3.95: Fulfillment — maks_sales increment (after successful draft)
        if result.get("gmail_draft_id"):
            # Save address snapshot before fulfillment so shipping job can find it
            _oid_pre = (getattr(classification, "order_id", None) or "").strip()
            _is_trusted_pre = (
                classification.situation == "new_order"
                or (classification.situation == "payment_received" and _oid_pre.startswith("PAY-"))
            )
            if (
                _oid_pre
                and classification.customer_street
                and classification.customer_city_state_zip
                and _is_trusted_pre
            ):
                try:
                    from db.shipping import save_order_shipping_address as _save_addr
                    _save_addr(
                        email=classification.client_email,
                        order_id=_oid_pre,
                        name=getattr(classification, "client_name", "") or client.get("name", ""),
                        street=classification.customer_street,
                        csz=classification.customer_city_state_zip,
                    )
                except Exception as _e:
                    logger.warning("Pre-fulfillment address save failed: %s", _e)

            from agents.handlers.fulfillment_trigger import try_fulfillment
            try_fulfillment(classification, result, gmail_message_id)

        # Step 4: Format the output
        logger.info(
            "Done: email=%s, template=%s, client_found=%s",
            classification.client_email, result["template_used"], result["client_found"],
        )
        formatted = format_result(result)

        # Step 5-7: Persist everything
        _persist_results(classification, result, gmail_thread_id, gmail_message_id, email_text, gmail_account=gmail_account)

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
