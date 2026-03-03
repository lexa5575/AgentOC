"""Tests for db.stock module."""

from db.stock import (
    CATEGORY_PRICES,
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
        {"base_flavor": "Green", "quantity": 3, "product_name": "Tera Green"},
    ])
    assert result["all_in_stock"] is True
    assert result["items"][0]["is_sufficient"] is True
    assert result["items"][0]["total_available"] >= 3


def test_check_stock_insufficient():
    _seed_stock()
    result = check_stock_for_order([
        {"base_flavor": "Silver", "quantity": 5, "product_name": "Tera Silver"},
    ])
    assert result["all_in_stock"] is False
    assert len(result["insufficient_items"]) == 1
    assert result["insufficient_items"][0]["base_flavor"] == "Silver"


def test_check_stock_device_vs_stick():
    """Devices and sticks search in different categories."""
    _seed_stock()
    # "ONE Green" should only search device categories, finding qty=2
    result = check_stock_for_order([
        {"base_flavor": "ONE Green", "quantity": 1, "product_name": "ONE Green device"},
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


def test_save_order_items_skip_duplicates():
    save_order_items("dup@example.com", "O1", [
        {"product_name": "Tera Green", "base_flavor": "Green", "quantity": 1},
    ])
    saved = save_order_items("dup@example.com", "O1", [
        {"product_name": "Tera Green", "base_flavor": "Green", "quantity": 1},
    ])
    assert saved == 0


# ---------------------------------------------------------------------------
# select_best_alternatives
# ---------------------------------------------------------------------------

def test_alternatives_fallback_excludes_oos_flavor():
    """Fallback never includes the same flavor that is out of stock."""
    _seed_stock()
    # No history for buyer — should get fallback, but NOT Turquoise itself
    result = select_best_alternatives("buyer@example.com", "Turquoise")
    assert len(result["alternatives"]) > 0
    for alt in result["alternatives"]:
        assert "turquoise" not in alt["alternative"]["product_name"].lower()
    assert result["alternatives"][0]["reason"] == "fallback"


def test_alternatives_from_history():
    """Priority 1: flavors from customer order history."""
    _seed_stock()
    # Give buyer history with Green
    save_order_items("hist@example.com", "O1", [
        {"product_name": "Tera Green", "base_flavor": "Green", "quantity": 1},
    ])
    # Ask for alternative to Purple (not in stock at all)
    result = select_best_alternatives("hist@example.com", "Purple", max_options=3)
    alts = result["alternatives"]
    # Should find Green from history
    history_alts = [a for a in alts if a["reason"] == "history"]
    assert len(history_alts) > 0


def test_alternatives_history_excludes_oos_flavor():
    """History-based alternatives skip the flavor that is out of stock."""
    _seed_stock()
    # Client ordered both Green and Turquoise before
    save_order_items("skip@example.com", "O1", [
        {"product_name": "Tera Green", "base_flavor": "Green", "quantity": 1},
        {"product_name": "Tera Turquoise", "base_flavor": "Turquoise", "quantity": 1},
    ])
    # OOS flavor = Green → alternatives should NOT include Green
    result = select_best_alternatives("skip@example.com", "Green", max_options=3)
    for alt in result["alternatives"]:
        assert "green" not in alt["alternative"]["product_name"].lower()


def test_alternatives_none_available():
    """No stock at all → empty alternatives."""
    result = select_best_alternatives("any@example.com", "Green")
    assert result["alternatives"] == []
    assert result["reason"] == "none_available"


def test_alternatives_max_options():
    _seed_stock()
    result = select_best_alternatives("x@example.com", "Purple", max_options=1)
    assert len(result["alternatives"]) <= 1


# ---------------------------------------------------------------------------
# calculate_order_price
# ---------------------------------------------------------------------------

def test_calculate_price_sticks():
    """Standard sticks: $110 each."""
    _seed_stock()
    stock = check_stock_for_order([
        {"base_flavor": "Green", "quantity": 2, "product_name": "Tera Green"},
    ])
    price = calculate_order_price(stock["items"])
    assert price == 220.0


def test_calculate_price_device():
    """ONE device: $99."""
    _seed_stock()
    stock = check_stock_for_order([
        {"base_flavor": "ONE Green", "quantity": 1, "product_name": "ONE Green"},
    ])
    price = calculate_order_price(stock["items"])
    assert price == 99.0


def test_calculate_price_mixed():
    """Sticks + device in one order."""
    _seed_stock()
    stock = check_stock_for_order([
        {"base_flavor": "Green", "quantity": 2, "product_name": "Tera Green"},
        {"base_flavor": "ONE Green", "quantity": 1, "product_name": "ONE Green"},
    ])
    price = calculate_order_price(stock["items"])
    assert price == 319.0  # $110x2 + $99x1


def test_calculate_price_japan():
    """Japan sticks: $115 each."""
    _seed_stock()
    stock = check_stock_for_order([
        {"base_flavor": "T Mint", "quantity": 3, "product_name": "T Mint"},
    ])
    price = calculate_order_price(stock["items"])
    assert price == 345.0  # $115 x 3


def test_calculate_price_unmatched_returns_none():
    """Unmatched item → strict None."""
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
