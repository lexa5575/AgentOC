"""Tests for db.conversation_state module."""

import json

from db.conversation_state import (
    delete_state,
    get_client_states,
    get_state,
    save_state,
)


def test_save_and_get_state():
    """Test saving and retrieving conversation state."""
    thread_id = "test_thread_001"
    client_email = "state_test@example.com"
    
    state = {
        "status": "new",
        "topic": "new_order",
        "facts": {"order_id": "#12345"},
        "promises": [],
        "last_exchange": {"we_said": None, "they_said": "Order placed"},
        "open_questions": [],
        "summary": "Test order",
    }
    
    save_state(
        gmail_thread_id=thread_id,
        client_email=client_email,
        state_json=state,
        situation="new_order",
    )
    
    # Retrieve and verify
    result = get_state(thread_id)
    assert result is not None
    assert result["gmail_thread_id"] == thread_id
    assert result["client_email"] == client_email
    assert result["last_situation"] == "new_order"
    assert result["message_count"] == 1
    
    # Check parsed state
    assert result["state"]["status"] == "new"
    assert result["state"]["topic"] == "new_order"
    assert result["state"]["facts"]["order_id"] == "#12345"


def test_update_existing_state():
    """Test updating an existing conversation state."""
    thread_id = "test_thread_002"
    client_email = "update_test@example.com"
    
    # Save initial state
    save_state(
        gmail_thread_id=thread_id,
        client_email=client_email,
        state_json={"status": "new"},
        situation="new_order",
    )
    
    # Update state
    save_state(
        gmail_thread_id=thread_id,
        client_email=client_email,
        state_json={"status": "awaiting_payment"},
        situation="payment_question",
    )
    
    # Verify update
    result = get_state(thread_id)
    assert result is not None
    assert result["state"]["status"] == "awaiting_payment"
    assert result["last_situation"] == "payment_question"
    assert result["message_count"] == 2


def test_state_json_as_string():
    """Test saving state as JSON string."""
    thread_id = "test_thread_003"
    client_email = "json_string@example.com"
    
    state_dict = {"status": "shipped", "tracking": "9400111"}
    state_json = json.dumps(state_dict)
    
    save_state(
        gmail_thread_id=thread_id,
        client_email=client_email,
        state_json=state_json,
        situation="tracking",
    )
    
    result = get_state(thread_id)
    assert result is not None
    assert result["state"]["status"] == "shipped"
    assert result["state"]["tracking"] == "9400111"


def test_get_state_not_found():
    """Test getting a non-existent state."""
    result = get_state("nonexistent_thread_id")
    assert result is None


def test_get_client_states():
    """Test getting all states for a client."""
    client_email = "multi_thread@example.com"
    
    # Create multiple threads for same client
    for i in range(3):
        save_state(
            gmail_thread_id=f"client_thread_{i:03d}",
            client_email=client_email,
            state_json={"thread_num": i},
            situation="other",
        )
    
    # Get all states
    states = get_client_states(client_email, limit=10)
    assert len(states) >= 3
    
    # Should be ordered by updated_at desc (most recent first)
    thread_ids = [s["gmail_thread_id"] for s in states[:3]]
    assert "client_thread_002" in thread_ids


def test_get_client_states_limit():
    """Test limit on get_client_states."""
    client_email = "limited@example.com"
    
    for i in range(5):
        save_state(
            gmail_thread_id=f"limit_thread_{i:03d}",
            client_email=client_email,
            state_json={"num": i},
            situation="other",
        )
    
    # Limit to 2
    states = get_client_states(client_email, limit=2)
    assert len(states) == 2


def test_delete_state():
    """Test deleting a conversation state."""
    thread_id = "delete_test_thread"
    
    save_state(
        gmail_thread_id=thread_id,
        client_email="delete@example.com",
        state_json={"status": "test"},
        situation="other",
    )
    
    # Verify exists
    assert get_state(thread_id) is not None
    
    # Delete
    result = delete_state(thread_id)
    assert result is True
    
    # Verify deleted
    assert get_state(thread_id) is None


def test_delete_state_not_found():
    """Test deleting a non-existent state."""
    result = delete_state("nonexistent_delete_thread")
    assert result is False


def test_email_case_insensitive():
    """Test that client email is normalized to lowercase."""
    thread_id = "case_test_thread"
    
    save_state(
        gmail_thread_id=thread_id,
        client_email="UPPERCASE@Example.COM",
        state_json={"test": True},
        situation="other",
    )
    
    result = get_state(thread_id)
    assert result is not None
    assert result["client_email"] == "uppercase@example.com"