"""Tests for db.clients module."""

import pytest

from db.clients import (
    add_client,
    decrement_discount,
    delete_client,
    get_client,
    get_client_profile,
    list_clients,
    update_client,
    update_client_notes,
    update_client_summary,
)
from db.models import ClientOrderItem, EmailHistory, get_session


def test_add_and_get_client():
    client = add_client("TEST@Example.com", "Test User", "prepay", zelle_address="z@test.com")
    assert client["email"] == "test@example.com"
    assert client["name"] == "Test User"
    assert client["payment_type"] == "prepay"
    assert client["zelle_address"] == "z@test.com"

    found = get_client("TEST@Example.com")
    assert found is not None
    assert found["email"] == "test@example.com"


def test_get_client_not_found():
    assert get_client("nobody@example.com") is None


def test_add_client_duplicate_raises():
    add_client("dup@example.com", "First", "prepay")
    with pytest.raises(ValueError, match="already exists"):
        add_client("dup@example.com", "Second", "postpay")


def test_add_client_invalid_payment_type():
    with pytest.raises(ValueError, match="prepay"):
        add_client("bad@example.com", "Bad", "credit_card")


def test_add_client_invalid_discount():
    with pytest.raises(ValueError, match="0-100"):
        add_client("bad@example.com", "Bad", "prepay", discount_percent=150)


def test_list_clients():
    add_client("b@example.com", "Bob", "prepay")
    add_client("a@example.com", "Alice", "postpay")
    clients = list_clients()
    assert len(clients) == 2
    assert clients[0]["name"] == "Alice"
    assert clients[1]["name"] == "Bob"


def test_update_client():
    add_client("upd@example.com", "Old Name", "prepay")
    updated = update_client("upd@example.com", name="New Name", payment_type="postpay")
    assert updated["name"] == "New Name"
    assert updated["payment_type"] == "postpay"


def test_update_client_not_found():
    assert update_client("ghost@example.com", name="X") is None


def test_update_client_invalid_payment():
    add_client("val@example.com", "Val", "prepay")
    with pytest.raises(ValueError):
        update_client("val@example.com", payment_type="bitcoin")


def test_update_client_ignores_unknown_fields():
    add_client("ign@example.com", "Ign", "prepay")
    updated = update_client("ign@example.com", name="New", hacker_field="drop table")
    assert updated["name"] == "New"


def test_delete_client():
    add_client("del@example.com", "Del", "prepay")
    assert delete_client("del@example.com") is True
    assert get_client("del@example.com") is None


def test_delete_client_not_found():
    assert delete_client("ghost@example.com") is False


def test_decrement_discount():
    add_client(
        "disc@example.com", "Disc", "prepay",
        discount_percent=10, discount_orders_left=2,
    )
    decrement_discount("disc@example.com")
    client = get_client("disc@example.com")
    assert client["discount_orders_left"] == 1
    assert client["discount_percent"] == 10


def test_decrement_discount_resets_at_zero():
    add_client(
        "disc0@example.com", "Disc0", "prepay",
        discount_percent=10, discount_orders_left=1,
    )
    decrement_discount("disc0@example.com")
    client = get_client("disc0@example.com")
    assert client["discount_orders_left"] == 0
    assert client["discount_percent"] == 0


def test_decrement_discount_no_discount():
    add_client("nodis@example.com", "No", "prepay")
    decrement_discount("nodis@example.com")
    client = get_client("nodis@example.com")
    assert client["discount_orders_left"] == 0
    assert client["discount_percent"] == 0


# ---------------------------------------------------------------------------
# Phase 4: Client Profile tests
# ---------------------------------------------------------------------------
def _seed_order_items(db_session, email: str):
    """Seed order items for profile tests."""
    session = db_session()
    try:
        items = [
            ClientOrderItem(
                client_email=email, order_id="ORD-001",
                product_name="Green Stick", base_flavor="Green",
                product_type="stick", quantity=3,
            ),
            ClientOrderItem(
                client_email=email, order_id="ORD-001",
                product_name="Silver Stick", base_flavor="Silver",
                product_type="stick", quantity=2,
            ),
            ClientOrderItem(
                client_email=email, order_id="ORD-002",
                product_name="Green Stick", base_flavor="Green",
                product_type="stick", quantity=1,
            ),
        ]
        session.add_all(items)
        session.commit()
    finally:
        session.close()


def _seed_email_history(db_session, email: str):
    """Seed email history for profile tests."""
    from datetime import datetime, timedelta
    session = db_session()
    try:
        entries = [
            EmailHistory(
                client_email=email, direction="inbound",
                subject="Order", body="I want to order",
                created_at=datetime.utcnow() - timedelta(days=10),
            ),
            EmailHistory(
                client_email=email, direction="outbound",
                subject="Re: Order", body="Sure, here is your order",
                created_at=datetime.utcnow() - timedelta(days=9),
            ),
        ]
        session.add_all(entries)
        session.commit()
    finally:
        session.close()


def test_get_client_profile_basic(db_session):
    """Profile returns client fields + computed stats."""
    add_client("prof@example.com", "Profile User", "postpay", zelle_address="p@z.com")
    _seed_order_items(db_session, "prof@example.com")
    _seed_email_history(db_session, "prof@example.com")

    profile = get_client_profile("prof@example.com")

    assert profile is not None
    assert profile["name"] == "Profile User"
    assert profile["payment_type"] == "postpay"
    assert profile["total_orders"] == 2  # ORD-001 and ORD-002
    assert len(profile["favorite_flavors"]) == 2
    assert "Green (4x)" in profile["favorite_flavors"]  # 3 + 1
    assert "Silver (2x)" in profile["favorite_flavors"]
    assert profile["is_active"] is True  # email within 90 days
    assert profile["last_interaction"] is not None


def test_get_client_profile_no_orders(db_session):
    """Profile works for client with no orders."""
    add_client("empty@example.com", "Empty", "prepay")

    profile = get_client_profile("empty@example.com")

    assert profile is not None
    assert profile["total_orders"] == 0
    assert profile["favorite_flavors"] == []
    assert profile["is_active"] is False
    assert profile["last_interaction"] is None


def test_get_client_profile_not_found():
    """Profile returns None for unknown client."""
    assert get_client_profile("ghost@example.com") is None


def test_get_client_profile_includes_notes_and_summary(db_session):
    """Profile includes notes and llm_summary fields."""
    add_client("noted@example.com", "Noted", "prepay")
    update_client_notes("noted@example.com", "VIP client, handle with care")
    update_client_summary("noted@example.com", "Frequent buyer, likes Green")

    profile = get_client_profile("noted@example.com")

    assert profile["notes"] == "VIP client, handle with care"
    assert profile["llm_summary"] == "Frequent buyer, likes Green"


def test_update_client_notes():
    """update_client_notes sets notes field."""
    add_client("note@example.com", "Note", "prepay")

    assert update_client_notes("note@example.com", "Always ships Monday") is True
    client = get_client("note@example.com")
    assert client["notes"] == "Always ships Monday"


def test_update_client_notes_not_found():
    """update_client_notes returns False for missing client."""
    assert update_client_notes("ghost@example.com", "nope") is False


def test_update_client_summary():
    """update_client_summary sets llm_summary field."""
    add_client("sum@example.com", "Sum", "postpay")

    assert update_client_summary("sum@example.com", "Loyal customer") is True
    client = get_client("sum@example.com")
    assert client["llm_summary"] == "Loyal customer"


def test_update_client_summary_not_found():
    """update_client_summary returns False for missing client."""
    assert update_client_summary("ghost@example.com", "nope") is False


def test_to_dict_includes_summary_updated_at():
    """Client.to_dict() includes summary_updated_at field."""
    add_client("ts@example.com", "TS", "prepay")
    profile = get_client_profile("ts@example.com")
    assert "summary_updated_at" in profile
    assert profile["summary_updated_at"] is None  # Never generated yet

    update_client_summary("ts@example.com", "Some summary")
    profile = get_client_profile("ts@example.com")
    assert profile["summary_updated_at"] is not None


# ---------------------------------------------------------------------------
# Client address tests
# ---------------------------------------------------------------------------

def test_add_client_with_address():
    """add_client stores street and city_state_zip."""
    client = add_client(
        "addr@example.com", "Addr User", "postpay",
        street="123 Main St", city_state_zip="Chicago, IL 60601",
    )
    assert client["street"] == "123 Main St"
    assert client["city_state_zip"] == "Chicago, IL 60601"

    found = get_client("addr@example.com")
    assert found["street"] == "123 Main St"
    assert found["city_state_zip"] == "Chicago, IL 60601"


def test_update_client_address():
    """update_client can update street and city_state_zip."""
    add_client("upaddr@example.com", "UpAddr", "prepay")
    updated = update_client("upaddr@example.com", street="456 Oak Ave", city_state_zip="Miami, FL 33101")
    assert updated["street"] == "456 Oak Ave"
    assert updated["city_state_zip"] == "Miami, FL 33101"


def test_to_dict_includes_address():
    """Client.to_dict() includes street and city_state_zip fields."""
    add_client("dictaddr@example.com", "DictAddr", "prepay")
    client = get_client("dictaddr@example.com")
    assert "street" in client
    assert "city_state_zip" in client
    assert client["street"] == ""
    assert client["city_state_zip"] == ""
