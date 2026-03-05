"""
Context Builder
---------------

Central context assembly for ALL handler agents.
Loads client data, conversation state, email history, and policy YAML
into a structured prompt that handlers pass to their LLM agents.

Replaces the duplicated context-building code in each handler.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from agents.formatters import format_email_history
from db.clients import get_client_profile
from db.memory import get_full_email_history, get_full_thread_history

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Policy YAML loading
# ---------------------------------------------------------------------------
_POLICY_DIR = Path(__file__).resolve().parent.parent / "policy"

# Cache loaded YAML files (they don't change at runtime)
_policy_cache: dict[str, dict] = {}

# Which policy files to load for each situation
_SITUATION_POLICIES: dict[str, list[str]] = {
    "new_order": ["payment", "shipping", "tone", "hard_rules"],
    "price_question": ["payment", "tone", "hard_rules"],
    "tracking": ["tracking", "shipping", "tone", "hard_rules"],
    "payment_question": ["payment", "tone", "hard_rules"],
    "payment_received": ["payment", "tone", "hard_rules"],
    "discount_request": ["discounts", "tone", "hard_rules"],
    "shipping_timeline": ["shipping", "tone", "hard_rules"],
    "oos_followup": ["payment", "shipping", "tone", "hard_rules"],
    "stock_question": ["tone", "hard_rules"],
    "other": ["tone", "hard_rules"],
}


def _load_yaml(name: str) -> dict:
    """Load a single YAML policy file (cached)."""
    if name in _policy_cache:
        return _policy_cache[name]

    path = _POLICY_DIR / f"{name}.yaml"
    if not path.exists():
        logger.warning("Policy file not found: %s", path)
        return {}

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    _policy_cache[name] = data
    return data


def load_policy(situation: str) -> str:
    """Load and format relevant policy rules for a situation.

    Returns:
        Formatted policy text for inclusion in the prompt.
    """
    policy_names = _SITUATION_POLICIES.get(situation, ["tone", "hard_rules"])
    sections = []

    for name in policy_names:
        data = _load_yaml(name)
        if not data:
            continue

        lines = [f"[{data.get('name', name)}]"]
        priority = data.get("priority", "soft")
        if priority == "hard":
            lines.append("(MANDATORY — never violate)")

        # Add all rule lists
        for key, value in data.items():
            if key in ("name", "priority"):
                continue
            if isinstance(value, list):
                for rule in value:
                    lines.append(f"- {rule}")
            # Skip non-list values (they're metadata)

        sections.append("\n".join(lines))

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# EmailContext dataclass
# ---------------------------------------------------------------------------
@dataclass
class EmailContext:
    """All context needed by a handler agent to generate a reply."""

    # Current task
    situation: str
    email_text: str

    # Client data
    client_name: str = "unknown"
    client_found: bool = False
    payment_type: str = "unknown"
    zelle_address: str = ""
    discount_percent: int = 0
    discount_orders_left: int = 0

    # Client profile (cold memory)
    total_orders: int = 0
    favorite_flavors: list[str] = field(default_factory=list)
    is_active: bool = False
    notes: str = ""
    llm_summary: str = ""

    # Conversation state (from State Updater LLM)
    conversation_state: dict | None = None

    # Email history (formatted text)
    history_text: str = ""

    # Policy rules (formatted text)
    policy_rules: str = ""

    # Cross-thread context (states from other threads of same client)
    other_thread_states: list[dict] = field(default_factory=list)

    # Extra facts from result dict
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Build and format context
# ---------------------------------------------------------------------------
def build_context(
    classification,
    result: dict,
    email_text: str,
) -> EmailContext:
    """Central context assembly — one function for ALL handlers.

    Args:
        classification: EmailClassification object.
        result: Result dict from process_classified_email.
        email_text: Original email text.

    Returns:
        Populated EmailContext.
    """
    # Client data — use profile for enriched data, fall back to result
    client_email = result.get("client_email", "")
    client_name = result.get("client_name") or "unknown"
    client_found = result.get("client_found", False)
    payment_type = "unknown"
    zelle_address = ""
    discount_percent = 0
    discount_orders_left = 0
    total_orders = 0
    favorite_flavors = []
    is_active = False
    notes = ""
    llm_summary = ""

    if client_found and client_email:
        # Try enriched profile (includes stats from order history)
        profile = get_client_profile(client_email)
        if profile:
            client_name = profile.get("name", client_name)
            payment_type = profile.get("payment_type", "unknown")
            zelle_address = profile.get("zelle_address", "")
            discount_percent = profile.get("discount_percent", 0)
            discount_orders_left = profile.get("discount_orders_left", 0)
            total_orders = profile.get("total_orders", 0)
            favorite_flavors = profile.get("favorite_flavors", [])
            is_active = profile.get("is_active", False)
            notes = profile.get("notes", "")
            llm_summary = profile.get("llm_summary", "")
        elif result.get("client_data"):
            # Fallback to basic client data from result
            c = result["client_data"]
            client_name = c.get("name", client_name)
            payment_type = c.get("payment_type", "unknown")
            zelle_address = c.get("zelle_address", "")
            discount_percent = c.get("discount_percent", 0)
            discount_orders_left = c.get("discount_orders_left", 0)

    # Conversation state
    conversation_state = result.get("conversation_state")

    # Cross-thread context: other active threads for same client
    gmail_thread_id = result.get("gmail_thread_id")
    other_thread_states = []
    if client_email:
        try:
            from db.conversation_state import get_client_states
            all_states = get_client_states(client_email, limit=4)
            other_thread_states = [
                s for s in all_states
                if s.get("gmail_thread_id") != gmail_thread_id
            ]
        except Exception as e:
            logger.warning("Failed to load cross-thread states: %s", e)

    # Email history — prefer thread-specific when gmail_thread_id available
    if gmail_thread_id:
        history = get_full_thread_history(gmail_thread_id, max_results=10)
    else:
        history = get_full_email_history(client_email, max_results=10)
    history_text = format_email_history(history)

    # Policy rules
    situation = result.get("situation", "other")
    policy_rules = load_policy(situation)

    # Extra context (e.g. Tier 4: unresolved product names)
    extra = {}
    if result.get("unresolved_context"):
        extra["unresolved_context"] = result["unresolved_context"]

    return EmailContext(
        situation=situation,
        email_text=email_text,
        client_name=client_name,
        client_found=client_found,
        payment_type=payment_type,
        zelle_address=zelle_address,
        discount_percent=discount_percent,
        discount_orders_left=discount_orders_left,
        total_orders=total_orders,
        favorite_flavors=favorite_flavors,
        is_active=is_active,
        notes=notes,
        llm_summary=llm_summary,
        conversation_state=conversation_state,
        other_thread_states=other_thread_states,
        history_text=history_text,
        policy_rules=policy_rules,
        extra=extra,
    )


def format_context_for_prompt(ctx: EmailContext) -> str:
    """Format EmailContext into structured prompt sections.

    Returns:
        Ready-to-use prompt string with all context sections.
    """
    sections = []

    # === CLIENT PROFILE ===
    if ctx.client_found:
        client_lines = [
            "=== CLIENT PROFILE ===",
            f"Name: {ctx.client_name}",
            f"Payment type: {ctx.payment_type}",
        ]
        if ctx.zelle_address:
            client_lines.append(f"Zelle address: {ctx.zelle_address}")
        if ctx.discount_percent > 0 and ctx.discount_orders_left > 0:
            client_lines.append(
                f"Active discount: {ctx.discount_percent}% "
                f"for next {ctx.discount_orders_left} orders"
            )
        else:
            client_lines.append("Discount: none")
        if ctx.total_orders > 0:
            client_lines.append(f"Total orders: {ctx.total_orders}")
        if ctx.favorite_flavors:
            client_lines.append(f"Favorite flavors: {', '.join(ctx.favorite_flavors)}")
        status = "active" if ctx.is_active else "inactive"
        client_lines.append(f"Status: {status}")
        if ctx.notes:
            client_lines.append(f"Operator notes: {ctx.notes}")
        if ctx.llm_summary:
            client_lines.append(f"Summary: {ctx.llm_summary}")
        sections.append("\n".join(client_lines))
    else:
        sections.append("=== CLIENT PROFILE ===\nNEW CLIENT — not in our database")

    # === CONVERSATION STATE ===
    if ctx.conversation_state:
        state_json = json.dumps(ctx.conversation_state, ensure_ascii=False, indent=2)
        sections.append(f"=== CONVERSATION STATE ===\n{state_json}")

    # === OTHER ACTIVE THREADS ===
    if ctx.other_thread_states:
        other_lines = ["=== OTHER ACTIVE THREADS ==="]
        for s in ctx.other_thread_states[:3]:
            state = s.get("state", {})
            other_lines.append(f"Thread ({s.get('last_situation', '?')}):")
            if state.get("facts"):
                other_lines.append(f"  Facts: {json.dumps(state['facts'], ensure_ascii=False)}")
            if state.get("summary"):
                other_lines.append(f"  Summary: {state['summary']}")
        sections.append("\n".join(other_lines))

    # === CONVERSATION HISTORY ===
    if ctx.history_text:
        sections.append(ctx.history_text)

    # === UNRESOLVED PRODUCTS (Tier 4) ===
    if ctx.extra.get("unresolved_context"):
        sections.append(f"=== {ctx.extra['unresolved_context']}")

    # === POLICY RULES ===
    if ctx.policy_rules:
        sections.append(f"=== POLICY RULES ===\n{ctx.policy_rules}")

    # === CUSTOMER'S EMAIL ===
    sections.append(f"=== CUSTOMER'S EMAIL ===\n{ctx.email_text}")

    return "\n\n".join(sections)
