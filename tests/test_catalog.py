"""Tests for db.catalog module."""

from db.catalog import (
    ensure_catalog_entries,
    ensure_catalog_entry,
    get_base_display_name,
    get_display_name,
    normalize_product_name,
)
from db.models import ProductCatalog


def test_normalize_product_name():
    assert normalize_product_name("  T  Purple  ") == "t purple"
    assert normalize_product_name("GREEN") == "green"
    assert normalize_product_name("Green\tEU") == "green eu"


def test_ensure_catalog_entry_idempotent(db_session):
    session = db_session()
    try:
        pid1 = ensure_catalog_entry(session, "TEREA_EUROPE", "Green")
        pid2 = ensure_catalog_entry(session, "TEREA_EUROPE", "  green  ")
        session.commit()

        assert pid1 == pid2
        rows = session.query(ProductCatalog).all()
        assert len(rows) == 1
        assert rows[0].category == "TEREA_EUROPE"
        assert rows[0].name_norm == "green"
        assert rows[0].stock_name == "Green"
    finally:
        session.close()


def test_ensure_catalog_entry_same_name_different_category(db_session):
    session = db_session()
    try:
        eu_id = ensure_catalog_entry(session, "TEREA_EUROPE", "Green")
        arm_id = ensure_catalog_entry(session, "ARMENIA", "Green")
        session.commit()

        assert eu_id != arm_id
        assert session.query(ProductCatalog).count() == 2
    finally:
        session.close()


def test_ensure_catalog_entries_batch_dedups(db_session):
    session = db_session()
    try:
        items = [
            {"category": "TEREA_EUROPE", "product_name": "Green"},
            {"category": "TEREA_EUROPE", "product_name": "  green  "},
            {"category": "TEREA_EUROPE", "product_name": "Silver"},
            {"category": "ARMENIA", "product_name": "Green"},
            {"category": "ARMENIA", "product_name": "GREEN"},
        ]
        created = ensure_catalog_entries(session, items)
        session.commit()

        assert created == 3  # EU/Green, EU/Silver, ARMENIA/Green
        assert session.query(ProductCatalog).count() == 3
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Display name tests (no DB needed)
# ---------------------------------------------------------------------------

class TestGetDisplayName:
    """Test get_display_name: stock_name + category → customer-friendly name."""

    # --- Standard DB names (raw stock_name from sheets) ---

    def test_japan_t_prefix(self):
        assert get_display_name("T Purple", "TEREA_JAPAN") == "Terea Purple made in Japan"

    def test_japan_no_t_prefix(self):
        assert get_display_name("Fusion Menthol", "УНИКАЛЬНАЯ_ТЕРЕА") == "Terea Fusion Menthol made in Japan"

    def test_europe(self):
        assert get_display_name("Green", "TEREA_EUROPE") == "Terea Green EU"

    def test_armenia(self):
        assert get_display_name("Purple", "ARMENIA") == "Terea Purple ME"

    def test_kz_terea(self):
        assert get_display_name("Silver", "KZ_TEREA") == "Terea Silver ME"

    def test_device_passthrough(self):
        assert get_display_name("ONE Green", "ONE") == "ONE Green"

    def test_device_standalone(self):
        assert get_display_name("STND", "STND") == "STND"

    # --- Idempotency: already-decorated names ---

    def test_already_decorated_europe(self):
        """'Tera Green EU' should NOT become 'Terea Tera Green EU EU'."""
        assert get_display_name("Tera Green EU", "TEREA_EUROPE") == "Terea Green EU"

    def test_already_decorated_terea_prefix(self):
        assert get_display_name("Terea Silver EU", "TEREA_EUROPE") == "Terea Silver EU"

    def test_already_decorated_japan(self):
        assert get_display_name("Terea Purple made in Japan", "TEREA_JAPAN") == "Terea Purple made in Japan"

    def test_already_decorated_me(self):
        assert get_display_name("Tera Turquoise ME", "ARMENIA") == "Terea Turquoise ME"

    # --- Unknown category falls through ---

    def test_unknown_category_passthrough(self):
        assert get_display_name("Mystery", "UNKNOWN") == "Mystery"


class TestGetBaseDisplayName:
    """Test get_base_display_name: stock_name → generic customer-friendly name."""

    def test_plain_name(self):
        assert get_base_display_name("Silver") == "Terea Silver"

    def test_t_prefix_stripped(self):
        assert get_base_display_name("T Purple") == "Terea Purple"

    def test_device_passthrough(self):
        assert get_base_display_name("ONE Green") == "ONE Green"

    def test_device_standalone(self):
        assert get_base_display_name("PRIME") == "PRIME"

    # --- Idempotency ---

    def test_already_decorated_with_region(self):
        """'Tera Turquoise EU' should become 'Terea Turquoise', not 'Terea Tera Turquoise EU'."""
        assert get_base_display_name("Tera Turquoise EU") == "Terea Turquoise"

    def test_already_terea_prefix(self):
        assert get_base_display_name("Terea Silver") == "Terea Silver"

    def test_already_full_japan(self):
        assert get_base_display_name("Terea Purple made in Japan") == "Terea Purple"

