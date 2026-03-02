"""Tests for db.clients module."""

import pytest

from db.clients import (
    add_client,
    decrement_discount,
    delete_client,
    get_client,
    list_clients,
    update_client,
)


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
