"""Tests for db.stock module."""

from unittest.mock import patch

from db.stock import has_ambiguous_variants, _has_ambiguous_variants
from db.stock import (
    CATEGORY_PRICES,
    _extract_variant_id,
    extract_variant_id,
    calculate_order_price,
    check_stock_for_order,
    get_available_by_category,
    get_client_flavor_history,
    get_product_type,
    get_stock_summary,
    replace_order_items,
    save_order_items,
    search_stock,
    select_best_alternatives,
    sync_stock,
)


def _get_product_ids(flavor: str, warehouse: str = "main") -> list[int]:
    """Look up product_ids from seeded stock by flavor name (for tests).

    Returns list of product_ids matching the flavor across all categories.
    Uses search_stock (which keeps ILIKE) to find entries.
    """
    entries = search_stock(flavor, warehouse=warehouse)
    ids = list({e["product_id"] for e in entries if e["product_id"] is not None})
    return ids


# ---------------------------------------------------------------------------
# get_product_type
# ---------------------------------------------------------------------------

def test_product_type_stick():
    assert get_product_type("Green") == "stick"
    assert get_product_type("Turquoise") == "stick"
    assert get_product_type("Silver") == "stick"


def test_product_type_device():
    assert get_product_type("ONE Green") == "device"
    assert get_product_type("STND Red") == "device"
    assert get_product_type("PRIME Black") == "device"


def test_product_type_device_no_color():
    """ONE/STND/PRIME without color = device."""
    assert get_product_type("ONE") == "device"
    assert get_product_type("STND") == "device"
    assert get_product_type("PRIME") == "device"


def test_product_type_case_insensitive():
    assert get_product_type("one green") == "device"
    assert get_product_type("  ONE Green  ") == "device"


# ---------------------------------------------------------------------------
# sync_stock
# ---------------------------------------------------------------------------

def _seed_stock():
    """Helper: seed stock items for testing."""
    items = [
        {"category": "TEREA_EUROPE", "product_name": "Turquoise", "quantity": 10},
        {"category": "TEREA_EUROPE", "product_name": "Green", "quantity": 5},
        {"category": "TEREA_EUROPE", "product_name": "Silver", "quantity": 0},
        {"category": "ARMENIA", "product_name": "Turquoise", "quantity": 3},
        {"category": "KZ_TEREA", "product_name": "Green", "quantity": 8},
        {"category": "TEREA_JAPAN", "product_name": "T Mint", "quantity": 5},
        {"category": "ONE", "product_name": "ONE Green", "quantity": 2},
    ]
    return sync_stock("main", items)


def test_sync_stock():
    count = _seed_stock()
    assert count == 7


def test_sync_stock_upsert():
    _seed_stock()
    # Re-sync with updated quantities
    items = [
        {"category": "TEREA_EUROPE", "product_name": "Turquoise", "quantity": 20},
        {"category": "TEREA_EUROPE", "product_name": "Green", "quantity": 0},
    ]
    count = sync_stock("main", items)
    assert count == 2

    # Turquoise updated, Green updated, Silver+Armenia+KZ deleted (stale)
    results = search_stock("Turquoise", warehouse="main")
    assert len(results) == 1
    assert results[0]["quantity"] == 20

    results = search_stock("Silver", warehouse="main")
    assert len(results) == 0


def test_sync_stock_separate_warehouses():
    sync_stock("main", [{"category": "TEREA_EUROPE", "product_name": "Green", "quantity": 5}])
    sync_stock("backup", [{"category": "TEREA_EUROPE", "product_name": "Green", "quantity": 10}])

    main = search_stock("Green", warehouse="main")
    backup = search_stock("Green", warehouse="backup")
    assert main[0]["quantity"] == 5
    assert backup[0]["quantity"] == 10


def test_sync_stock_assigns_product_id():
    sync_stock("main", [{"category": "TEREA_EUROPE", "product_name": "Green", "quantity": 5}])
    rows = search_stock("Green", warehouse="main")
    eu = next(r for r in rows if r["category"] == "TEREA_EUROPE" and r["product_name"] == "Green")
    assert eu["product_id"] is not None


def test_sync_stock_update_keeps_same_product_id():
    sync_stock("main", [{"category": "TEREA_EUROPE", "product_name": "Green", "quantity": 5}])
    first = next(
        r for r in search_stock("Green", warehouse="main")
        if r["category"] == "TEREA_EUROPE" and r["product_name"] == "Green"
    )
    first_id = first["product_id"]
    assert first_id is not None

    sync_stock("main", [{"category": "TEREA_EUROPE", "product_name": "Green", "quantity": 9}])
    second = next(
        r for r in search_stock("Green", warehouse="main")
        if r["category"] == "TEREA_EUROPE" and r["product_name"] == "Green"
    )
    assert second["product_id"] == first_id
    assert second["quantity"] == 9


def test_sync_stock_same_category_name_same_product_id_across_warehouses():
    sync_stock("main", [{"category": "TEREA_EUROPE", "product_name": "Green", "quantity": 5}])
    sync_stock("backup", [{"category": "TEREA_EUROPE", "product_name": "Green", "quantity": 10}])

    main_green = next(
        r for r in search_stock("Green", warehouse="main")
        if r["category"] == "TEREA_EUROPE" and r["product_name"] == "Green"
    )
    backup_green = next(
        r for r in search_stock("Green", warehouse="backup")
        if r["category"] == "TEREA_EUROPE" and r["product_name"] == "Green"
    )
    assert main_green["product_id"] is not None
    assert main_green["product_id"] == backup_green["product_id"]


def test_sync_stock_same_name_different_category_different_product_id():
    sync_stock("main", [
        {"category": "TEREA_EUROPE", "product_name": "Green", "quantity": 5},
        {"category": "ARMENIA", "product_name": "Green", "quantity": 7},
    ])
    rows = [r for r in search_stock("Green", warehouse="main") if r["product_name"] == "Green"]
    by_cat = {r["category"]: r for r in rows}
    assert by_cat["TEREA_EUROPE"]["product_id"] is not None
    assert by_cat["ARMENIA"]["product_id"] is not None
    assert by_cat["TEREA_EUROPE"]["product_id"] != by_cat["ARMENIA"]["product_id"]


def test_sync_stock_normalized_name_reuses_catalog_entry():
    sync_stock("main", [{"category": "TEREA_EUROPE", "product_name": "Green", "quantity": 5}])
    sync_stock("backup", [{"category": "TEREA_EUROPE", "product_name": "  green  ", "quantity": 6}])

    main_green = next(
        r for r in search_stock("Green", warehouse="main")
        if r["category"] == "TEREA_EUROPE" and r["product_name"] == "Green"
    )
    backup_green = next(
        r for r in search_stock("green", warehouse="backup")
        if r["category"] == "TEREA_EUROPE" and r["product_name"].strip().lower() == "green"
    )
    assert main_green["product_id"] == backup_green["product_id"]


# ---------------------------------------------------------------------------
# search_stock
# ---------------------------------------------------------------------------

def test_search_stock():
    _seed_stock()
    results = search_stock("Turquoise")
    assert len(results) == 2  # TEREA_EUROPE + ARMENIA


def test_search_stock_case_insensitive():
    _seed_stock()
    results = search_stock("turquoise")
    assert len(results) == 2


def test_search_stock_no_match():
    _seed_stock()
    assert search_stock("Purple") == []


def test_search_stock_warehouse_filter():
    _seed_stock()
    results = search_stock("Green", warehouse="main")
    # TEREA_EUROPE "Green" + KZ_TEREA "Green" + ONE "ONE Green" (substring match)
    assert len(results) == 3


# ---------------------------------------------------------------------------
# get_available_by_category
# ---------------------------------------------------------------------------

def test_get_available_by_category():
    _seed_stock()
    available = get_available_by_category("TEREA_EUROPE")
    # Silver has qty=0, should be excluded
    names = [a["product_name"] for a in available]
    assert "Turquoise" in names
    assert "Green" in names
    assert "Silver" not in names


def test_get_available_by_category_empty():
    _seed_stock()
    assert get_available_by_category("NONEXISTENT") == []


# ---------------------------------------------------------------------------
# get_stock_summary
# ---------------------------------------------------------------------------

def test_get_stock_summary():
    _seed_stock()
    summary = get_stock_summary()
    assert summary["total"] == 7
    assert summary["available"] == 6  # Silver has qty=0
    assert summary["synced_at"] is not None


def test_get_stock_summary_empty():
    summary = get_stock_summary()
    assert summary["total"] == 0
    assert summary["synced_at"] is None


def test_get_stock_summary_warehouse():
    _seed_stock()
    summary = get_stock_summary(warehouse="main")
    assert summary["total"] == 7


# ---------------------------------------------------------------------------
# check_stock_for_order
# ---------------------------------------------------------------------------

def test_check_stock_sufficient():
    _seed_stock()
    result = check_stock_for_order([
        {"base_flavor": "Green", "quantity": 3, "product_name": "Tera Green",
         "product_ids": _get_product_ids("Green")},
    ])
    assert result["all_in_stock"] is True
    assert result["items"][0]["is_sufficient"] is True
    assert result["items"][0]["total_available"] >= 3


def test_check_stock_insufficient():
    _seed_stock()
    result = check_stock_for_order([
        {"base_flavor": "Silver", "quantity": 5, "product_name": "Tera Silver",
         "product_ids": _get_product_ids("Silver")},
    ])
    assert result["all_in_stock"] is False
    assert len(result["insufficient_items"]) == 1
    assert result["insufficient_items"][0]["base_flavor"] == "Silver"


def test_check_stock_device_vs_stick():
    """Devices and sticks search in different categories."""
    _seed_stock()
    # "ONE Green" should only search device categories, finding qty=2
    result = check_stock_for_order([
        {"base_flavor": "ONE Green", "quantity": 1, "product_name": "ONE Green device",
         "product_ids": _get_product_ids("ONE Green")},
    ])
    assert result["all_in_stock"] is True
    assert result["items"][0]["total_available"] == 2


# ---------------------------------------------------------------------------
# save_order_items + get_client_flavor_history
# ---------------------------------------------------------------------------

def test_save_and_get_order_history():
    saved = save_order_items("buyer@example.com", "ORD-1", [
        {"product_name": "Tera Green EU", "base_flavor": "Green", "quantity": 2},
        {"product_name": "Tera Silver EU", "base_flavor": "Silver", "quantity": 1},
    ])
    assert saved == 2

    history = get_client_flavor_history("buyer@example.com")
    assert len(history) == 2
    flavors = [h["base_flavor"] for h in history]
    assert "Green" in flavors
    assert "Silver" in flavors


def test_order_history_ranked_by_frequency():
    save_order_items("freq@example.com", "O1", [
        {"product_name": "Tera Green", "base_flavor": "Green", "quantity": 1},
    ])
    save_order_items("freq@example.com", "O2", [
        {"product_name": "Tera Green", "base_flavor": "Green", "quantity": 1},
    ])
    save_order_items("freq@example.com", "O3", [
        {"product_name": "Tera Silver", "base_flavor": "Silver", "quantity": 1},
    ])
    history = get_client_flavor_history("freq@example.com")
    assert history[0]["base_flavor"] == "Green"
    assert history[0]["order_count"] == 2
    assert history[1]["base_flavor"] == "Silver"
    assert history[1]["order_count"] == 1


def test_order_history_filter_by_product_type():
    save_order_items("mix@example.com", "O1", [
        {"product_name": "Tera Green", "base_flavor": "Green", "quantity": 1},
        {"product_name": "ONE Green device", "base_flavor": "ONE Green", "quantity": 1},
    ])
    sticks = get_client_flavor_history("mix@example.com", product_type="stick")
    devices = get_client_flavor_history("mix@example.com", product_type="device")
    assert len(sticks) == 1
    assert sticks[0]["base_flavor"] == "Green"
    assert len(devices) == 1
    assert devices[0]["base_flavor"] == "ONE Green"


def test_save_order_items_skip_duplicates(db_session):
    """Duplicate (email, order_id, variant_id) → skipped via uq_client_order_variant."""
    from sqlalchemy import text
    session = db_session()
    conn = session.get_bind().connect()
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_client_order_variant "
        "ON client_order_items (client_email, order_id, variant_id) "
        "WHERE variant_id IS NOT NULL AND order_id IS NOT NULL"
    ))
    conn.commit()
    conn.close()

    save_order_items("dup@example.com", "O1", [
        {"product_name": "Tera Green", "base_flavor": "Green", "quantity": 1,
         "variant_id": 10},
    ])
    saved = save_order_items("dup@example.com", "O1", [
        {"product_name": "Tera Green", "base_flavor": "Green", "quantity": 1,
         "variant_id": 10},
    ])
    assert saved == 0


# ---------------------------------------------------------------------------
# select_best_alternatives (LLM-backed, mock get_llm_alternatives)
# ---------------------------------------------------------------------------

_PATCH_LLM = "agents.alternatives.get_llm_alternatives"

# Helper: a minimal stock item dict returned by the mock LLM
def _llm_item(product_name: str, category: str, qty: int = 5) -> dict:
    return {"product_name": product_name, "category": category, "quantity": qty,
            "warehouse": "main", "is_fallback": False, "synced_at": None}


def test_alternatives_fallback_excludes_oos_flavor():
    """Fallback never includes the OOS flavor (LLM returns empty → fallback used)."""
    _seed_stock()
    with patch(_PATCH_LLM, return_value=[]):
        result = select_best_alternatives("buyer@example.com", "Turquoise")
    assert len(result["alternatives"]) > 0
    for alt in result["alternatives"]:
        assert "turquoise" not in alt["alternative"]["product_name"].lower()
    assert result["alternatives"][0]["reason"] == "fallback"


def test_alternatives_none_available():
    """Empty stock → none_available returned, LLM never called."""
    with patch(_PATCH_LLM) as mock_llm:
        result = select_best_alternatives("any@example.com", "Green")
    assert result["alternatives"] == []
    assert result["reason"] == "none_available"
    mock_llm.assert_not_called()


def test_alternatives_max_options():
    """Fallback respects max_options limit."""
    _seed_stock()
    with patch(_PATCH_LLM, return_value=[]):
        result = select_best_alternatives("x@example.com", "Purple", max_options=1)
    assert len(result["alternatives"]) <= 1


def test_alternatives_llm_picks_used():
    """LLM-returned items appear in result with reason='llm'."""
    _seed_stock()
    green = _llm_item("Green", "TEREA_EUROPE")
    with patch(_PATCH_LLM, return_value=[green]):
        result = select_best_alternatives("buyer@example.com", "Turquoise")
    assert len(result["alternatives"]) == 1
    assert result["alternatives"][0]["reason"] == "llm"
    assert result["alternatives"][0]["alternative"]["product_name"] == "Green"
    assert result["reason"] == "llm"


def test_alternatives_llm_fallback_when_empty():
    """When LLM returns empty list, fallback (quantity-based) is used."""
    _seed_stock()
    with patch(_PATCH_LLM, return_value=[]):
        result = select_best_alternatives("buyer@example.com", "Turquoise")
    assert len(result["alternatives"]) > 0
    assert all(a["reason"] == "fallback" for a in result["alternatives"])


def test_alternatives_excluded_products_respected():
    """excluded_products prevents those items from appearing in result."""
    _seed_stock()
    green = _llm_item("Green", "TEREA_EUROPE")
    with patch(_PATCH_LLM, return_value=[green]):
        result = select_best_alternatives(
            "buyer@example.com", "Turquoise", excluded_products={"Green"}
        )
    # Green excluded — fallback should be used and Green not present
    for alt in result["alternatives"]:
        assert alt["alternative"]["product_name"] != "Green"


def test_alternatives_oos_flavor_not_offered_to_llm():
    """OOS flavor is excluded from the stock list passed to LLM."""
    _seed_stock()
    with patch(_PATCH_LLM, return_value=[]) as mock_llm:
        select_best_alternatives("buyer@example.com", "Turquoise")
    mock_llm.assert_called_once()
    available = mock_llm.call_args.kwargs.get("available_items", [])
    for item in available:
        assert "turquoise" not in item["product_name"].lower(), (
            f"OOS flavor Turquoise should not be in LLM stock list: {item}"
        )


def test_alternatives_max_options_with_llm():
    """max_options respected when LLM returns more items than requested."""
    _seed_stock()
    items = [
        _llm_item("Green", "TEREA_EUROPE"),
        _llm_item("Silver", "TEREA_EUROPE", qty=3),
        _llm_item("Green", "ARMENIA", qty=3),
    ]
    with patch(_PATCH_LLM, return_value=items):
        result = select_best_alternatives("buyer@example.com", "Turquoise", max_options=2)
    assert len(result["alternatives"]) <= 2


# ---------------------------------------------------------------------------
# calculate_order_price
# ---------------------------------------------------------------------------

def test_calculate_price_sticks():
    """Standard sticks: $110 each."""
    _seed_stock()
    stock = check_stock_for_order([
        {"base_flavor": "Green", "quantity": 2, "product_name": "Tera Green",
         "product_ids": _get_product_ids("Green")},
    ])
    price = calculate_order_price(stock["items"])
    assert price == 220.0


def test_calculate_price_device():
    """ONE device: $99."""
    _seed_stock()
    stock = check_stock_for_order([
        {"base_flavor": "ONE Green", "quantity": 1, "product_name": "ONE Green",
         "product_ids": _get_product_ids("ONE Green")},
    ])
    price = calculate_order_price(stock["items"])
    assert price == 99.0


def test_calculate_price_mixed():
    """Sticks + device in one order."""
    _seed_stock()
    stock = check_stock_for_order([
        {"base_flavor": "Green", "quantity": 2, "product_name": "Tera Green",
         "product_ids": _get_product_ids("Green")},
        {"base_flavor": "ONE Green", "quantity": 1, "product_name": "ONE Green",
         "product_ids": _get_product_ids("ONE Green")},
    ])
    price = calculate_order_price(stock["items"])
    assert price == 319.0  # $110x2 + $99x1


def test_calculate_price_japan():
    """Japan sticks: $115 each."""
    _seed_stock()
    stock = check_stock_for_order([
        {"base_flavor": "T Mint", "quantity": 3, "product_name": "T Mint",
         "product_ids": _get_product_ids("T Mint")},
    ])
    price = calculate_order_price(stock["items"])
    assert price == 345.0  # $115 x 3


def test_calculate_price_unmatched_returns_none():
    """Unmatched item → strict None (no product_ids)."""
    _seed_stock()
    stock = check_stock_for_order([
        {"base_flavor": "NonExistent", "quantity": 1, "product_name": "???"},
    ])
    price = calculate_order_price(stock["items"])
    assert price is None


def test_calculate_price_empty():
    """Empty or None input → None."""
    assert calculate_order_price([]) is None
    assert calculate_order_price(None) is None


def test_calculate_price_ambiguous_categories():
    """Entries from different price groups → None (safety)."""
    items = [{
        "base_flavor": "Weird",
        "ordered_qty": 1,
        "stock_entries": [
            {"category": "KZ_TEREA", "product_name": "Weird", "quantity": 5},
            {"category": "TEREA_JAPAN", "product_name": "Weird", "quantity": 3},
        ],
    }]
    assert calculate_order_price(items) is None


# ---------------------------------------------------------------------------
# replace_order_items (Plan 8C)
# ---------------------------------------------------------------------------

def test_replace_removes_stale_flavors():
    """[8C.1] Replace deletes old flavors that are not in the new set."""
    save_order_items("replace@example.com", "R1", [
        {"product_name": "Tera Amber", "base_flavor": "Amber", "quantity": 2},
        {"product_name": "Tera Green", "base_flavor": "Green", "quantity": 1},
    ])
    # Replace with completely different set
    result = replace_order_items("replace@example.com", "R1", [
        {"product_name": "Tera Silver", "base_flavor": "Silver", "quantity": 3},
        {"product_name": "Tera Bronze", "base_flavor": "Bronze", "quantity": 1},
    ])
    assert result == 2

    # Verify: only Silver + Bronze remain
    history = get_client_flavor_history("replace@example.com")
    flavors = {h["base_flavor"] for h in history}
    assert flavors == {"Silver", "Bronze"}


def test_replace_updates_qty_via_replacement(db_session):
    """[8C.2] Replace updates quantity by deleting old row and inserting new."""
    save_order_items("rqty@example.com", "R2", [
        {"product_name": "Tera Green", "base_flavor": "Green", "quantity": 2},
    ])
    replace_order_items("rqty@example.com", "R2", [
        {"product_name": "Tera Green", "base_flavor": "Green", "quantity": 5},
    ])

    from db.models import ClientOrderItem
    session = db_session()
    try:
        row = session.query(ClientOrderItem).filter_by(
            client_email="rqty@example.com", order_id="R2", base_flavor="Green",
        ).one()
        assert row.quantity == 5
    finally:
        session.close()


def test_replace_atomic_on_error(monkeypatch):
    """[8C.3] Error mid-insert rolls back everything — old items preserved."""
    from db.models import ClientOrderItem as _ClientOrderItem
    from db import stock as stock_module

    save_order_items("atomic@example.com", "R3", [
        {"product_name": "Tera Green", "base_flavor": "Green", "quantity": 1},
    ])

    original_get_session = stock_module.get_session

    def patched_get_session():
        session = original_get_session()
        _original_add = session.add
        count = {"n": 0}

        def failing_add(obj):
            if isinstance(obj, _ClientOrderItem):
                count["n"] += 1
                if count["n"] == 2:
                    raise RuntimeError("simulated insert failure on 2nd item")
            return _original_add(obj)

        session.add = failing_add
        return session

    monkeypatch.setattr(stock_module, "get_session", patched_get_session)

    result = replace_order_items("atomic@example.com", "R3", [
        {"product_name": "Tera Silver", "base_flavor": "Silver", "quantity": 1},
        {"product_name": "Tera Bronze", "base_flavor": "Bronze", "quantity": 1},
    ])
    assert result == 0  # error → 0

    # Restore original get_session to verify old data
    monkeypatch.setattr(stock_module, "get_session", original_get_session)

    history = get_client_flavor_history("atomic@example.com")
    flavors = {h["base_flavor"] for h in history}
    assert "Green" in flavors  # old data preserved
    assert "Silver" not in flavors  # new data NOT written
    assert "Bronze" not in flavors


def test_replace_guard_empty_list():
    """[8C.4] Empty order_items → no deletion, return 0."""
    save_order_items("guard@example.com", "R4", [
        {"product_name": "Tera Green", "base_flavor": "Green", "quantity": 1},
    ])
    result = replace_order_items("guard@example.com", "R4", [])
    assert result == 0

    # Verify original items still exist
    history = get_client_flavor_history("guard@example.com")
    assert len(history) == 1
    assert history[0]["base_flavor"] == "Green"


def test_replace_guard_empty_order_id():
    """[8C.4b] None/empty order_id → return 0, no operation."""
    assert replace_order_items("x@example.com", None, [
        {"product_name": "X", "base_flavor": "X", "quantity": 1},
    ]) == 0
    assert replace_order_items("x@example.com", "", [
        {"product_name": "X", "base_flavor": "X", "quantity": 1},
    ]) == 0
    assert replace_order_items("x@example.com", "  ", [
        {"product_name": "X", "base_flavor": "X", "quantity": 1},
    ]) == 0


# ---------------------------------------------------------------------------
# save_order_items savepoint fix (Plan 8C.5)
# ---------------------------------------------------------------------------

def test_savepoint_preserves_previous_inserts_despite_duplicate(db_session):
    """[8C.5] Insert A, dup A, insert B → both A and B saved (not just B).

    Phase 9.1: dedup now via uq_client_order_variant (variant_id), not base_flavor.
    """
    from sqlalchemy import text
    session = db_session()
    conn = session.get_bind().connect()
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_client_order_variant "
        "ON client_order_items (client_email, order_id, variant_id) "
        "WHERE variant_id IS NOT NULL AND order_id IS NOT NULL"
    ))
    conn.commit()
    conn.close()

    # Insert A first time
    save_order_items("sp@example.com", "SP1", [
        {"product_name": "Tera Green", "base_flavor": "Green", "quantity": 1,
         "variant_id": 10},
    ])
    # Now save [Green (dup variant_id=10), Silver (new variant_id=20)]
    # Green should be skipped, Silver saved, Green NOT rolled back
    saved = save_order_items("sp@example.com", "SP1", [
        {"product_name": "Tera Green", "base_flavor": "Green", "quantity": 1,
         "variant_id": 10},
        {"product_name": "Tera Silver", "base_flavor": "Silver", "quantity": 1,
         "variant_id": 20},
    ])
    assert saved == 1  # only Silver is new

    history = get_client_flavor_history("sp@example.com")
    flavors = {h["base_flavor"] for h in history}
    assert flavors == {"Green", "Silver"}  # both must exist


# ---------------------------------------------------------------------------
# Cross-region substitution prevention (Region Safety hotfix)
# ---------------------------------------------------------------------------

def test_eu_requested_kz_available_not_in_stock():
    """[RS] EU qty=0, KZ qty>0 — when product_ids filter to EU only, all_in_stock=False.

    This prevents silent cross-region substitution: if a customer agreed to
    EU Silver but only KZ Silver is available, the system must NOT confirm
    the order as "in stock".
    """
    # Seed: Silver in TEREA_EUROPE (qty=0) + KZ_TEREA (qty=50)
    sync_stock("wh_region", [
        {"category": "TEREA_EUROPE", "product_name": "Silver", "quantity": 0},
        {"category": "KZ_TEREA", "product_name": "Silver", "quantity": 50},
    ])

    # Get EU product_id from the stock entries themselves
    eu_entries = search_stock("Silver", warehouse="wh_region")
    eu_stock = [e for e in eu_entries if e["category"] == "TEREA_EUROPE"]
    assert len(eu_stock) == 1, "EU Silver stock entry should exist"
    eu_product_id = eu_stock[0]["product_id"]
    assert eu_product_id is not None, "EU Silver should have product_id"

    # Check stock with EU-only product_ids (simulating region-filtered resolver)
    result = check_stock_for_order([
        {"base_flavor": "Silver", "quantity": 3, "product_name": "Silver",
         "product_ids": [eu_product_id]},
    ])
    assert result["all_in_stock"] is False, (
        "EU Silver qty=0 must NOT be satisfied by KZ Silver qty=50"
    )
    assert result["items"][0]["total_available"] == 0


# ---------------------------------------------------------------------------
# Phase 2: _extract_variant_id + variant_id persistence
# ---------------------------------------------------------------------------

def test_extract_variant_id_single():
    """[T1] Single product_id → return it."""
    assert extract_variant_id([42]) == 42


def test_extract_variant_id_multi_cross_family_returns_none():
    """[T2] Multiple cross-family product_ids → None (ambiguous)."""
    # Cross-family: ARMENIA(17) + TEREA_EUROPE(10) → None
    catalog = [
        {"id": 10, "category": "TEREA_EUROPE", "name_norm": "silver"},
        {"id": 17, "category": "ARMENIA", "name_norm": "silver"},
    ]
    assert extract_variant_id([10, 17], catalog_entries=catalog) is None


def test_extract_variant_id_same_family_returns_preferred():
    """Same-family multi-match → preferred id (ARMENIA for ME)."""
    catalog = [
        {"id": 17, "category": "ARMENIA", "name_norm": "silver"},
        {"id": 24, "category": "KZ_TEREA", "name_norm": "silver"},
    ]
    assert extract_variant_id([17, 24], catalog_entries=catalog) == 17
    assert extract_variant_id([24, 17], catalog_entries=catalog) == 17


def test_extract_variant_id_empty():
    """[T3] Empty or None → None."""
    assert extract_variant_id([]) is None
    assert extract_variant_id(None) is None


def test_extract_variant_id_backward_compat_alias():
    """[T3b] _extract_variant_id alias works identically."""
    assert _extract_variant_id([42]) == 42
    assert _extract_variant_id is extract_variant_id


def test_save_order_items_persists_variant_id(db_session):
    """[T6] variant_id + display_name_snapshot written to DB."""
    saved = save_order_items("vid@example.com", "V1", [
        {
            "product_name": "Tera Green EU",
            "base_flavor": "Green",
            "quantity": 2,
            "variant_id": 42,
            "display_name_snapshot": "Terea Green EU",
        },
    ])
    assert saved == 1

    from db.models import ClientOrderItem
    session = db_session()
    try:
        row = session.query(ClientOrderItem).filter_by(
            client_email="vid@example.com", order_id="V1",
        ).one()
        assert row.variant_id == 42
        assert row.display_name_snapshot == "Terea Green EU"
    finally:
        session.close()


def test_save_order_items_null_variant_when_missing(db_session):
    """[T7] No variant_id in item dict → NULL in DB (backward compat)."""
    save_order_items("novid@example.com", "V2", [
        {"product_name": "Tera Silver", "base_flavor": "Silver", "quantity": 1},
    ])

    from db.models import ClientOrderItem
    session = db_session()
    try:
        row = session.query(ClientOrderItem).filter_by(
            client_email="novid@example.com", order_id="V2",
        ).one()
        assert row.variant_id is None
        assert row.display_name_snapshot is None
    finally:
        session.close()


def test_save_order_items_skips_when_order_id_missing():
    """[T8] order_id=None → skip save, return 0."""
    saved = save_order_items("noid@example.com", None, [
        {"product_name": "X", "base_flavor": "X", "quantity": 1},
    ])
    assert saved == 0

    history = get_client_flavor_history("noid@example.com")
    assert len(history) == 0


def test_save_order_items_skips_empty_order_id():
    """[T8b] Empty/whitespace order_id → skip save."""
    assert save_order_items("noid@example.com", "", [
        {"product_name": "X", "base_flavor": "X", "quantity": 1},
    ]) == 0
    assert save_order_items("noid@example.com", "   ", [
        {"product_name": "X", "base_flavor": "X", "quantity": 1},
    ]) == 0


def test_replace_order_items_persists_variant_fields(db_session):
    """[T9] replace_order_items stores variant_id + display_name_snapshot."""
    save_order_items("rvid@example.com", "RV1", [
        {"product_name": "Old", "base_flavor": "Old", "quantity": 1},
    ])
    replace_order_items("rvid@example.com", "RV1", [
        {
            "product_name": "Tera Bronze EU",
            "base_flavor": "Bronze",
            "quantity": 3,
            "variant_id": 55,
            "display_name_snapshot": "Terea Bronze EU",
        },
    ])

    from db.models import ClientOrderItem
    session = db_session()
    try:
        row = session.query(ClientOrderItem).filter_by(
            client_email="rvid@example.com", order_id="RV1",
        ).one()
        assert row.variant_id == 55
        assert row.display_name_snapshot == "Terea Bronze EU"
        assert row.base_flavor == "Bronze"
        assert row.quantity == 3
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Phase 3: has_ambiguous_variants
# ---------------------------------------------------------------------------

def test_has_ambiguous_variants_detects_cross_family():
    """[P3-T4] Cross-family multi-match items are ambiguous."""
    catalog = [
        {"id": 17, "category": "ARMENIA", "name_norm": "silver"},
        {"id": 10, "category": "TEREA_EUROPE", "name_norm": "silver"},
        {"id": 7, "category": "TEREA_EUROPE", "name_norm": "green"},
        {"id": 52, "category": "ARMENIA", "name_norm": "bronze"},
    ]
    items = [
        {"base_flavor": "Bronze", "product_ids": [52]},
        {"base_flavor": "Silver", "product_ids": [17, 10]},  # ARMENIA + EU = cross-family
        {"base_flavor": "Green", "product_ids": [7]},
    ]
    result = has_ambiguous_variants(items, catalog_entries=catalog)
    assert result == ["Silver"]


def test_has_ambiguous_variants_same_family_not_ambiguous():
    """Same-family multi-match (ARMENIA + KZ_TEREA) is NOT ambiguous."""
    catalog = [
        {"id": 17, "category": "ARMENIA", "name_norm": "silver"},
        {"id": 24, "category": "KZ_TEREA", "name_norm": "silver"},
    ]
    items = [
        {"base_flavor": "Silver", "product_ids": [17, 24]},
    ]
    result = has_ambiguous_variants(items, catalog_entries=catalog)
    assert result == []


def test_has_ambiguous_variants_no_ambiguous():
    """[P3-T5] All single-match items → empty list."""
    items = [
        {"base_flavor": "Bronze", "product_ids": [52]},
        {"base_flavor": "Green", "product_ids": [7]},
    ]
    result = has_ambiguous_variants(items)
    assert result == []


def test_has_ambiguous_variants_empty_and_none():
    """Edge cases: empty product_ids, None, missing key → not ambiguous."""
    items = [
        {"base_flavor": "A", "product_ids": []},
        {"base_flavor": "B", "product_ids": None},
        {"base_flavor": "C"},
    ]
    result = has_ambiguous_variants(items)
    assert result == []


def test_has_ambiguous_variants_backward_compat_alias():
    """Backward compat alias _has_ambiguous_variants works."""
    assert _has_ambiguous_variants is has_ambiguous_variants


def test_extract_variant_id_cross_family_resolved_from_history(monkeypatch):
    """Cross-family ambiguity resolved by client order history."""
    import db.stock as _stock_mod
    catalog = [
        {"id": 21, "category": "TEREA_EUROPE", "name_norm": "teak"},
        {"id": 58, "category": "ARMENIA", "name_norm": "teak"},
    ]
    # Mock _resolve_variant_from_history to return 58 (ARMENIA/ME)
    monkeypatch.setattr(
        _stock_mod, "_resolve_variant_from_history",
        lambda email, pids: 58 if email == "client@example.com" else None,
    )
    result = extract_variant_id(
        [21, 58], catalog_entries=catalog, client_email="client@example.com",
    )
    assert result == 58


def test_extract_variant_id_cross_family_no_history(monkeypatch):
    """Cross-family ambiguity with no history → None."""
    import db.stock as _stock_mod
    catalog = [
        {"id": 21, "category": "TEREA_EUROPE", "name_norm": "teak"},
        {"id": 58, "category": "ARMENIA", "name_norm": "teak"},
    ]
    monkeypatch.setattr(
        _stock_mod, "_resolve_variant_from_history",
        lambda email, pids: None,
    )
    result = extract_variant_id(
        [21, 58], catalog_entries=catalog, client_email="client@example.com",
    )
    assert result is None


def test_has_ambiguous_variants_resolved_from_history(monkeypatch):
    """Cross-family items resolved by history → not ambiguous."""
    import db.stock as _stock_mod
    catalog = [
        {"id": 21, "category": "TEREA_EUROPE", "name_norm": "teak"},
        {"id": 58, "category": "ARMENIA", "name_norm": "teak"},
    ]
    items = [
        {"base_flavor": "Teak", "product_ids": [21, 58]},
    ]
    monkeypatch.setattr(
        _stock_mod, "_resolve_variant_from_history",
        lambda email, pids: 58,
    )
    result = has_ambiguous_variants(items, catalog_entries=catalog, client_email="client@example.com")
    assert result == []


def test_has_ambiguous_variants_no_history_still_ambiguous(monkeypatch):
    """Cross-family items without history → still ambiguous."""
    import db.stock as _stock_mod
    catalog = [
        {"id": 21, "category": "TEREA_EUROPE", "name_norm": "teak"},
        {"id": 58, "category": "ARMENIA", "name_norm": "teak"},
    ]
    items = [
        {"base_flavor": "Teak", "product_ids": [21, 58]},
    ]
    monkeypatch.setattr(
        _stock_mod, "_resolve_variant_from_history",
        lambda email, pids: None,
    )
    result = has_ambiguous_variants(items, catalog_entries=catalog, client_email="client@example.com")
    assert result == ["Teak"]


# ---------------------------------------------------------------------------
# Phase 8: ILIKE removal — negative tests
# ---------------------------------------------------------------------------

def test_check_stock_no_product_ids_returns_not_in_stock():
    """[P8-T1] Item without product_ids → total_available=0, is_sufficient=False."""
    _seed_stock()
    result = check_stock_for_order([
        {"base_flavor": "Green", "quantity": 1, "product_name": "Green"},
        # No product_ids → unresolved
    ])
    assert result["all_in_stock"] is False
    assert result["items"][0]["total_available"] == 0
    assert result["items"][0]["is_sufficient"] is False


def test_search_stock_still_uses_ilike():
    """[P8-T3] search_stock() still works with broad text queries (ILIKE preserved)."""
    _seed_stock()
    results = search_stock("Green")
    # Should find stock entries via ILIKE substring match
    assert len(results) >= 1
    names = [r["product_name"] for r in results]
    assert any("Green" in n for n in names)
