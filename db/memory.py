"""
Memory Layer — re-exports for backward compatibility.
------------------------------------------------------

All business logic lives in domain modules:
  db.clients        — client CRUD
  db.email_history   — email history, Gmail state, Gmail thread search
  db.stock           — stock sync/search, order items, OOS alternatives
  db.catalog         — product catalog (canonical product identity)
"""

from db.catalog import (
    _enrich_display_name_with_region,
    ensure_catalog_entry,
    get_base_display_name,
    get_catalog_products,
    get_display_name,
    normalize_product_name,
)
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
    email_is_deferred,
    finalize_deferred,
    get_email_history,
    get_full_email_history,
    get_full_thread_history,
    get_gmail_state,
    get_gmail_thread_history,
    get_thread_history,
    save_email,
    set_gmail_state,
)
from db.product_resolver import resolve_order_items, resolve_product_to_catalog
from db.sheet_config import (
    delete_sheet_config,
    is_config_stale,
    load_sheet_config,
    save_sheet_config,
)
from db.stock import (
    calculate_order_price,
    check_stock_for_order,
    get_available_by_category,
    get_client_flavor_history,
    get_last_order,
    get_product_type,
    get_stock_summary,
    replace_order_items,
    save_order_items,
    search_stock,
    search_stock_by_ids,
    select_best_alternatives,
    sync_stock,
)

__all__ = [
    # catalog
    "ensure_catalog_entry",
    "get_base_display_name",
    "get_catalog_products",
    "get_display_name",
    "normalize_product_name",
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
    "email_is_deferred",
    "finalize_deferred",
    "get_email_history",
    "get_full_email_history",
    "get_full_thread_history",
    "get_gmail_state",
    "get_gmail_thread_history",
    "get_thread_history",
    "save_email",
    "set_gmail_state",
    # product resolver
    "resolve_order_items",
    "resolve_product_to_catalog",
    # sheet config
    "delete_sheet_config",
    "is_config_stale",
    "load_sheet_config",
    "save_sheet_config",
    # stock
    "search_stock_by_ids",
    "calculate_order_price",
    "check_stock_for_order",
    "get_available_by_category",
    "get_client_flavor_history",
    "get_last_order",
    "get_product_type",
    "get_stock_summary",
    "replace_order_items",
    "save_order_items",
    "search_stock",
    "select_best_alternatives",
    "sync_stock",
]
