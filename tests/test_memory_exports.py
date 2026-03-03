"""Tests for db.memory re-exports."""

import db.memory as memory


def test_memory_exports_include_client_profile_functions():
    """New client-profile helpers should be exported for backward compatibility."""
    assert "get_client_profile" in memory.__all__
    assert "update_client_notes" in memory.__all__
    assert "update_client_summary" in memory.__all__


def test_memory_has_reexported_client_profile_functions():
    """Re-exported functions should be accessible as module attributes."""
    assert callable(memory.get_client_profile)
    assert callable(memory.update_client_notes)
    assert callable(memory.update_client_summary)


def test_memory_exports_include_calculate_order_price():
    """calculate_order_price should be exported via memory layer."""
    assert "calculate_order_price" in memory.__all__
    assert callable(memory.calculate_order_price)
