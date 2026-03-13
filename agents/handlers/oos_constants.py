"""Shared constants for OOS handling, safe to import from any layer."""

# Sources trusted for persistence and fulfillment (plan §3)
TRUSTED_SOURCES: frozenset[str] = frozenset({"thread_extraction", "pending_oos"})
