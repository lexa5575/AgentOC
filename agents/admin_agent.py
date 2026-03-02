"""
Admin Agent
-----------

An agent for managing client data in PostgreSQL.
Add, edit, delete, and list clients through chat.

Run with test data:
    python -m agents.admin_agent
"""

from agno.agent import Agent
from agno.models.openai import OpenAIResponses

from db import get_postgres_db
from db.clients import (
    get_client_profile as db_get_client_profile,
    update_client_notes as db_update_client_notes,
)
from db.memory import (
    add_client as db_add_client,
    delete_client as db_delete_client,
    get_client as db_get_client,
    get_available_by_category as db_get_available_by_category,
    get_full_email_history as db_get_full_email_history,
    get_stock_summary as db_get_stock_summary,
    list_clients as db_list_clients,
    search_stock as db_search_stock,
    update_client as db_update_client,
)

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
agent_db = get_postgres_db()

# ---------------------------------------------------------------------------
# Tool functions for client management (format output for LLM)
# ---------------------------------------------------------------------------


def list_clients() -> str:
    """List all clients in the database.
    Returns a formatted table of all clients with their details.
    """
    clients = db_list_clients()
    if not clients:
        return "No clients in database."

    lines = [f"Total clients: {len(clients)}", ""]
    for c in clients:
        discount = ""
        d = c.get("discount_percent", 0)
        dl = c.get("discount_orders_left", 0)
        if d and dl:
            discount = f", discount: {d}% ({dl} orders left)"
        lines.append(
            f"- {c['email']} | {c['name']} | {c['payment_type']}"
            f" | zelle: {c.get('zelle_address') or 'none'}{discount}"
        )
    return "\n".join(lines)


def get_client(email: str) -> str:
    """Get detailed information about a specific client.

    Args:
        email: Client email address.

    Returns:
        Client details or 'not found' message.
    """
    client = db_get_client(email)
    if not client:
        return f"Client {email} not found."

    lines = [
        f"Email: {client['email']}",
        f"Name: {client['name']}",
        f"Payment Type: {client['payment_type']}",
        f"Zelle Address: {client.get('zelle_address') or 'none'}",
        f"Street: {client.get('street') or 'none'}",
        f"City/State/Zip: {client.get('city_state_zip') or 'none'}",
        f"Discount: {client.get('discount_percent', 0)}%",
        f"Discount Orders Left: {client.get('discount_orders_left', 0)}",
    ]
    return "\n".join(lines)


def add_client(
    email: str,
    name: str,
    payment_type: str,
    zelle_address: str = "",
    street: str = "",
    city_state_zip: str = "",
    discount_percent: int = 0,
    discount_orders_left: int = 0,
) -> str:
    """Add a new client to the database.

    Args:
        email: Client email address (must be unique).
        name: Client full name.
        payment_type: Must be 'prepay' or 'postpay'.
        zelle_address: Zelle payment address (optional).
        street: Street address (optional).
        city_state_zip: City, State Zip (optional).
        discount_percent: Discount percentage 0-100 (optional).
        discount_orders_left: How many orders get the discount (optional).

    Returns:
        Success or error message.
    """
    try:
        client = db_add_client(
            email=email,
            name=name,
            payment_type=payment_type,
            zelle_address=zelle_address,
            street=street,
            city_state_zip=city_state_zip,
            discount_percent=discount_percent,
            discount_orders_left=discount_orders_left,
        )
        return f"Client added: {client['email']} ({client['name']}, {client['payment_type']})"
    except ValueError as e:
        return f"Error: {e}"


def update_client(
    email: str,
    name: str = "",
    payment_type: str = "",
    zelle_address: str = "",
    street: str = "",
    city_state_zip: str = "",
    discount_percent: int = -1,
    discount_orders_left: int = -1,
) -> str:
    """Update an existing client's data. Only provided fields will be changed.

    Args:
        email: Client email to update (required).
        name: New name (leave empty to keep current).
        payment_type: New payment type - 'prepay' or 'postpay' (leave empty to keep current).
        zelle_address: New Zelle address (leave empty to keep current).
        street: New street address (leave empty to keep current).
        city_state_zip: New City, State Zip (leave empty to keep current).
        discount_percent: New discount percentage 0-100 (use -1 to keep current).
        discount_orders_left: New discount orders count (use -1 to keep current).

    Returns:
        Success or error message.
    """
    fields = {}
    if name:
        fields["name"] = name
    if payment_type:
        fields["payment_type"] = payment_type
    if zelle_address:
        fields["zelle_address"] = zelle_address
    if street:
        fields["street"] = street
    if city_state_zip:
        fields["city_state_zip"] = city_state_zip
    if discount_percent >= 0:
        fields["discount_percent"] = discount_percent
    if discount_orders_left >= 0:
        fields["discount_orders_left"] = discount_orders_left

    if not fields:
        return "No changes specified."

    try:
        result = db_update_client(email, **fields)
        if not result:
            return f"Error: client {email} not found."
        changes = ", ".join(f"{k}='{v}'" for k, v in fields.items())
        return f"Updated {email}: {changes}"
    except ValueError as e:
        return f"Error: {e}"


def delete_client(email: str) -> str:
    """Delete a client from the database.

    Args:
        email: Client email to delete.

    Returns:
        Success or error message.
    """
    if db_delete_client(email):
        return f"Deleted client: {email}"
    return f"Error: client {email} not found."


# ---------------------------------------------------------------------------
# Tool functions for stock queries
# ---------------------------------------------------------------------------


def check_stock(query: str) -> str:
    """Search for a product in stock by name (partial match).

    Args:
        query: Product name or part of it (e.g., "Amber", "ONE Red", "T Mint").

    Returns:
        Matching products with quantities.
    """
    items = db_search_stock(query)
    if not items:
        return f"No products found matching '{query}'."

    lines = [f"Found {len(items)} product(s) matching '{query}':", ""]
    for item in items:
        status = "IN STOCK" if item["quantity"] > 0 else "OUT OF STOCK"
        lines.append(
            f"- {item['category']} | {item['product_name']} | "
            f"qty: {item['quantity']} | {status}"
        )
    return "\n".join(lines)


def stock_by_category(category: str) -> str:
    """Get all available (in stock) products in a category.

    Args:
        category: Category name (e.g., "KZ_TEREA", "TEREA_JAPAN", "ONE",
                  "STND", "PRIME", "ARMENIA", "TEREA_EUROPE", "УНИКАЛЬНАЯ_ТЕРЕА").

    Returns:
        Available products in the category.
    """
    items = db_get_available_by_category(category)
    if not items:
        return f"No available products in category '{category}'."

    lines = [f"Available in '{category}': {len(items)} product(s)", ""]
    for item in items:
        lines.append(f"- {item['product_name']} | qty: {item['quantity']}")
    return "\n".join(lines)


def email_history(client_email: str) -> str:
    """Show conversation history with a client (local DB + Gmail).

    Args:
        client_email: Client email address.

    Returns:
        Formatted conversation history or 'no history' message.
    """
    history = db_get_full_email_history(client_email, max_results=30)

    if not history:
        return f"No conversation history found for {client_email}."

    lines = [f"Conversation history with {client_email}: {len(history)} message(s)", ""]
    for msg in history:
        ts = msg["created_at"].strftime("%Y-%m-%d %H:%M") if msg.get("created_at") else "unknown"
        direction = "CLIENT WROTE" if msg["direction"] == "inbound" else "WE SENT"
        subject = msg.get("subject", "")
        body = msg.get("body", "")
        if len(body) > 400:
            body = body[:400] + "..."

        lines.append(f"[{direction}] {ts} | {subject}")
        lines.append(body)
        lines.append("---")

    return "\n".join(lines)


def client_profile(email: str) -> str:
    """Get full client profile with order stats, favorite flavors, and summary.

    Args:
        email: Client email address.

    Returns:
        Detailed client profile or 'not found' message.
    """
    profile = db_get_client_profile(email)
    if not profile:
        return f"Client {email} not found."

    lines = [
        f"Email: {profile['email']}",
        f"Name: {profile['name']}",
        f"Payment Type: {profile['payment_type']}",
        f"Zelle Address: {profile.get('zelle_address') or 'none'}",
        f"Street: {profile.get('street') or 'none'}",
        f"City/State/Zip: {profile.get('city_state_zip') or 'none'}",
        f"Discount: {profile.get('discount_percent', 0)}%"
        + (f" ({profile.get('discount_orders_left', 0)} orders left)" if profile.get('discount_percent') else ""),
        f"Total Orders: {profile.get('total_orders', 0)}",
        f"Favorite Flavors: {', '.join(profile.get('favorite_flavors', [])) or 'none'}",
        f"Active: {'yes' if profile.get('is_active') else 'no'}",
        f"Last Interaction: {profile.get('last_interaction') or 'never'}",
    ]
    if profile.get("notes"):
        lines.append(f"Notes: {profile['notes']}")
    if profile.get("llm_summary"):
        lines.append(f"Summary: {profile['llm_summary']}")
    return "\n".join(lines)


def update_notes(email: str, notes: str) -> str:
    """Set or update manual operator notes for a client.

    Args:
        email: Client email address.
        notes: New notes text (replaces existing notes).

    Returns:
        Success or error message.
    """
    if db_update_client_notes(email, notes):
        return f"Notes updated for {email}."
    return f"Error: client {email} not found."


def refresh_client_summary(email: str) -> str:
    """Generate or refresh LLM summary for a client based on email history.

    Args:
        email: Client email address.

    Returns:
        Generated summary or error message.
    """
    from agents.client_profiler import generate_client_summary

    summary = generate_client_summary(email)
    if summary:
        return f"Summary updated for {email}:\n{summary}"
    return f"Could not generate summary for {email} (no email history or error)."


def stock_summary() -> str:
    """Get overall stock summary: total items, available, last sync time.

    Returns:
        Stock statistics.
    """
    summary = db_get_stock_summary()
    return (
        f"Stock summary:\n"
        f"- Total products: {summary['total']}\n"
        f"- In stock (qty > 0): {summary['available']}\n"
        f"- Fallback calculations: {summary['fallback']}\n"
        f"- Last synced: {summary['synced_at'] or 'never'}"
    )


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
admin_instructions = """\
You are a database administrator for shipmecarton.com.
You manage client data and check product stock. You understand Russian and English.

=== CLIENT DATA FIELDS ===

Every client record has these fields:
- email (required, unique identifier)
- name (required)
- payment_type (required): "prepay" or "postpay"
- zelle_address: Zelle payment email/phone
- street: shipping street (e.g. "123 Main St")
- city_state_zip: shipping city/state/zip (e.g. "Miami, FL 33101")
- discount_percent: 0-100
- discount_orders_left: number of discounted orders remaining

street and city_state_zip are CRITICAL fields — our email system uses them \
to auto-fill shipping addresses in reply templates. They must be saved via \
add_client or update_client parameters, never via update_notes.

=== WORKFLOW: ADD NEW CLIENT ===

When asked to add a client (with or without email to research):
1. Call email_history to find conversation history
2. From the history extract: name, payment type, zelle, shipping address
3. Call add_client with ALL extracted data:
   - street="..." and city_state_zip="..." if address was found
   - zelle_address="..." if Zelle info was found
4. Confirm what was saved

=== WORKFLOW: UPDATE CLIENT ADDRESS ===

When given an address to save:
1. Call update_client with street="..." and city_state_zip="..."
2. Do NOT use update_notes for address data

=== TOOLS ===

Client data:
- list_clients: all clients (compact list)
- get_client: one client details
- add_client: create new client (pass street, city_state_zip if known)
- update_client: change any field (pass street, city_state_zip to update address)
- delete_client: remove a client

Client intelligence:
- client_profile: full profile with order stats, favorite flavors, AI summary
- email_history: conversation history (local DB + Gmail)
- refresh_client_summary: regenerate AI summary from email history

Operator notes (for human commentary ONLY, not for client data):
- update_notes: set short operator labels like "VIP", "проблемный клиент"

Stock:
- check_stock: search by product name (e.g. "Amber", "ONE Red")
- stock_by_category: available products in a category
- stock_summary: overall statistics

Stock categories: KZ_TEREA, TEREA_JAPAN, TEREA_EUROPE, ONE, STND, PRIME, УНИКАЛЬНАЯ_ТЕРЕА, ARMENIA

=== RULES ===

- payment_type: "prepay" (предоплата) or "postpay" (постоплата)
- Always confirm the completed action to the user
- Stock answers: always show quantity and status (in stock / out of stock)
"""

admin_agent = Agent(
    id="admin-agent",
    name="Admin Agent",
    model=OpenAIResponses(id="gpt-5.2"),
    db=agent_db,
    instructions=admin_instructions,
    tools=[
        list_clients, get_client, client_profile, add_client, update_client, delete_client,
        update_notes, refresh_client_summary,
        email_history,
        check_stock, stock_by_category, stock_summary,
    ],
    markdown=False,
)

# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    admin_agent.print_response("Show all clients", stream=True)
