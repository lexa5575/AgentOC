"""
Email Agent — manual test runner.

Seeds test clients + order history, then runs the email agent
against sample emails covering all major scenarios.

Usage:
    python -m tests.test_email_agent
"""

from agents.email_agent import email_agent, save_order_items
from db.memory import add_client, get_client, get_client_flavor_history

# ---------------------------------------------------------------------------
# Seed test data (idempotent)
# ---------------------------------------------------------------------------
_TEST_CLIENTS = [
    {"email": "client1@example.com", "name": "Test Client One", "payment_type": "prepay",
     "zelle_address": "pay@example.com"},
    {"email": "client2@example.com", "name": "Test Client Two", "payment_type": "postpay"},
    {"email": "client3@example.com", "name": "Test Client Three", "payment_type": "prepay",
     "zelle_address": "pay3@example.com", "discount_percent": 5, "discount_orders_left": 3},
]

_TEST_ORDERS = [
    ("client1@example.com", "23000", [
        {"product_name": "Tera Green made in Middle East", "base_flavor": "Green", "quantity": 2},
    ]),
    ("client1@example.com", "23100", [
        {"product_name": "Tera Green made in Middle East", "base_flavor": "Green", "quantity": 1},
    ]),
    ("client1@example.com", "23200", [
        {"product_name": "Tera Silver EU", "base_flavor": "Silver", "quantity": 1},
    ]),
]

_TESTS = [
    (
        "PREPAY client (template)",
        "Process this email:\n\n"
        "From: noreply@shipmecarton.com\n"
        "Reply-To: client1@example.com\n"
        "Subject: Shipmecarton - Order 23432\n\n"
        "Payment amount: $220.00\n"
        "Order ID: 23432\n"
        "Firstname: Test Client One\n"
        "Street address1: 123 Main St\n"
        "Town/City: Springfield\n"
        "State: Illinois\n"
        "Postcode/Zip: 62701\n"
        "Email: client1@example.com",
    ),
    (
        "DISCOUNT client 5% (template)",
        "Process this email:\n\n"
        "From: noreply@shipmecarton.com\n"
        "Reply-To: client3@example.com\n"
        "Subject: Shipmecarton - Order 23600\n\n"
        "Payment amount: $200.00\n"
        "Order ID: 23600\n"
        "Firstname: Test Client Three\n"
        "Email: client3@example.com",
    ),
    (
        "POSTPAY client (template)",
        "Process this email:\n\n"
        "From: noreply@shipmecarton.com\n"
        "Reply-To: client2@example.com\n"
        "Subject: Shipmecarton - Order 23551\n\n"
        "Payment amount: $180.00\n"
        "Order ID: 23551\n"
        "Firstname: Test Client Two\n"
        "Street address1: 456 Oak Ave\n"
        "Town/City: Chicago\n"
        "State: Illinois\n"
        "Postcode/Zip: 60601\n"
        "Email: client2@example.com",
    ),
    (
        "OUT OF STOCK — Turquoise (AI fallback with alternatives)",
        "Process this email:\n\n"
        "From: noreply@shipmecarton.com\n"
        "Reply-To: client1@example.com\n"
        "Subject: Shipmecarton - Order 23700\n\n"
        "# Image Name Price Qnt Amount\n"
        "1 Tera Turquoise EU $95.00 3 $285.00\n\n"
        "Payment amount: $285.00\n"
        "Order ID: 23700\n"
        "Firstname: Test Client One\n"
        "Street address1: 123 Main St\n"
        "Town/City: Springfield\n"
        "State: Illinois\n"
        "Postcode/Zip: 62701\n"
        "Email: client1@example.com",
    ),
    (
        "TRACKING question (AI fallback)",
        "Process this email:\n\n"
        "From: client2@example.com\n"
        "Subject: Re: Order 23551\n"
        "Body: Hey, when will my order be shipped? I need it by Friday.",
    ),
    (
        "THANK YOU (no reply needed)",
        "Process this email:\n\n"
        "From: client2@example.com\n"
        "Subject: Re: Order 23551\n"
        "Body: Thank you so much!",
    ),
]


def main():
    # Seed clients
    for tc in _TEST_CLIENTS:
        if not get_client(tc["email"]):
            add_client(**tc)
            print(f"  Created test client: {tc['email']}")

    # Seed order history
    existing_history = get_client_flavor_history("client1@example.com")
    if not existing_history:
        for email, order_id, items in _TEST_ORDERS:
            save_order_items(email, order_id, items)
        print("  Seeded order history for client1@example.com (Green x2, Silver x1)")

    # Run tests
    for name, prompt in _TESTS:
        print("\n" + "=" * 60)
        print(f"TEST: {name}")
        print("=" * 60)
        email_agent.print_response(prompt, stream=True)


if __name__ == "__main__":
    main()
