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
from db.memory import (
    add_client as db_add_client,
    delete_client as db_delete_client,
    get_client as db_get_client,
    list_clients as db_list_clients,
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
        f"Discount: {client.get('discount_percent', 0)}%",
        f"Discount Orders Left: {client.get('discount_orders_left', 0)}",
    ]
    return "\n".join(lines)


def add_client(
    email: str,
    name: str,
    payment_type: str,
    zelle_address: str = "",
    discount_percent: int = 0,
    discount_orders_left: int = 0,
) -> str:
    """Add a new client to the database.

    Args:
        email: Client email address (must be unique).
        name: Client full name.
        payment_type: Must be 'prepay' or 'postpay'.
        zelle_address: Zelle payment address (optional).
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
    discount_percent: int = -1,
    discount_orders_left: int = -1,
) -> str:
    """Update an existing client's data. Only provided fields will be changed.

    Args:
        email: Client email to update (required).
        name: New name (leave empty to keep current).
        payment_type: New payment type - 'prepay' or 'postpay' (leave empty to keep current).
        zelle_address: New Zelle address (leave empty to keep current).
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
# Agent
# ---------------------------------------------------------------------------
admin_instructions = """\
You are a database administrator for shipmecarton.com.
You manage client data. You understand both Russian and English.

When a user asks you to manage clients, use the appropriate tool:
- list_clients: show all clients
- get_client: show details of one client
- add_client: add a new client
- update_client: change client data (payment_type, discount, zelle, name)
- delete_client: remove a client

RULES:
- payment_type can only be "prepay" or "postpay"
- discount_percent is 0-100
- Always confirm the action after completing it
- If the user says "prepay" or "предоплата", use payment_type="prepay"
- If the user says "postpay" or "постоплата" or "оплата после", use payment_type="postpay"
"""

admin_agent = Agent(
    id="admin-agent",
    name="Admin Agent",
    model=OpenAIResponses(id="gpt-5.2"),
    db=agent_db,
    instructions=admin_instructions,
    tools=[list_clients, get_client, add_client, update_client, delete_client],
    markdown=False,
)

# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    admin_agent.print_response("Show all clients", stream=True)
