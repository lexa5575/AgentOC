"""Tests for db.catalog module."""

from db.catalog import ensure_catalog_entries, ensure_catalog_entry, normalize_product_name
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

