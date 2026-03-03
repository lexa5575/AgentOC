"""
Memory Layer — re-exports for backward compatibility.
------------------------------------------------------

All business logic lives in domain modules:
  db.clients        — client CRUD
  db.email_history   — email history, Gmail state, Gmail thread search
  db.stock           — stock sync/search, order items, OOS alternatives
"""

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
from db.email_history import (
    email_already_processed,
    get_email_history,
    get_full_email_history,
    get_full_thread_history,
    get_gmail_state,
    get_gmail_thread_history,
    get_thread_history,
    save_email,
    set_gmail_state,
)
from db.stock import (
    calculate_order_price,
    check_stock_for_order,
    get_available_by_category,
    get_client_flavor_history,
    get_product_type,
    get_stock_summary,
    save_order_items,
    search_stock,
    select_best_alternatives,
    sync_stock,
)

__all__ = [
    # clients
    "add_client",
    "decrement_discount",
    "delete_client",
    "get_client",
    "get_client_profile",
    "list_clients",
    "update_client",
    "update_client_notes",
    "update_client_summary",
    # email
    "email_already_processed",
    "get_email_history",
    "get_full_email_history",
    "get_full_thread_history",
    "get_gmail_state",
    "get_gmail_thread_history",
    "get_thread_history",
    "save_email",
    "set_gmail_state",
    # stock
    "calculate_order_price",
    "check_stock_for_order",
    "get_available_by_category",
    "get_client_flavor_history",
    "get_product_type",
    "get_stock_summary",
    "save_order_items",
    "search_stock",
    "select_best_alternatives",
    "sync_stock",
]
