"""Tests for Phase 6.5: partial unique index migration.

Uses SQLite in-memory DB via the shared db_session fixture.
"""

import pytest
from sqlalchemy import text

from db.models import ClientOrderItem
from scripts.migrate_variant_unique_index import INDEX_NAME, run_migration


def _add_item(session, email, order_id, base_flavor, variant_id=None):
    """Insert a ClientOrderItem (minimal fields)."""
    item = ClientOrderItem(
        client_email=email.lower().strip(),
        order_id=order_id,
        product_name=base_flavor,
        base_flavor=base_flavor,
        product_type="stick",
        quantity=1,
        variant_id=variant_id,
    )
    session.add(item)
    session.flush()
    return item


class TestMigrateVariantUniqueIndex:

    def test_clean_data_creates_index(self, db_session):
        """[P6.5-T1] Index created on clean data."""
        session = db_session()
        _add_item(session, "a@example.com", "#1", "Silver", variant_id=10)
        _add_item(session, "b@example.com", "#2", "Bronze", variant_id=20)
        session.commit()

        engine = session.get_bind()
        report = run_migration(create=True, bind=engine)

        assert report["status"] == "created"
        assert report["index_exists_after"] is True
        assert report["duplicates"] == []

    def test_idempotent_rerun(self, db_session):
        """[P6.5-T2] Re-run does not fail when index already exists."""
        session = db_session()
        _add_item(session, "a@example.com", "#1", "Silver", variant_id=10)
        session.commit()

        engine = session.get_bind()

        # First run
        r1 = run_migration(create=True, bind=engine)
        assert r1["status"] == "created"

        # Second run
        r2 = run_migration(create=True, bind=engine)
        assert r2["status"] == "already_exists"
        assert r2["index_exists_after"] is True

    def test_duplicates_block_creation(self, db_session):
        """[P6.5-T3] Duplicates prevent index creation."""
        session = db_session()
        # Two rows with same (email, order_id, variant_id) — duplicate
        # Must bypass ORM unique constraint on (email, order_id, base_flavor)
        # by using different base_flavor values
        _add_item(session, "a@example.com", "#1", "Silver", variant_id=10)
        _add_item(session, "a@example.com", "#1", "Silver EU", variant_id=10)
        session.commit()

        engine = session.get_bind()
        report = run_migration(create=True, bind=engine)

        assert report["status"] == "blocked_duplicates"
        assert report["index_exists_after"] is False
        assert len(report["duplicates"]) == 1
        assert report["duplicates"][0]["count"] == 2

    def test_null_values_not_duplicates(self, db_session):
        """[P6.5-T4] NULL variant_id / NULL order_id rows don't block index."""
        session = db_session()
        # Same email+order but variant_id=NULL — not covered by partial index
        _add_item(session, "a@example.com", "#1", "Silver", variant_id=None)
        _add_item(session, "a@example.com", "#1", "Bronze", variant_id=None)
        # Same email+variant but order_id=NULL — not covered
        _add_item(session, "a@example.com", None, "Gold", variant_id=10)
        _add_item(session, "b@example.com", None, "Gold", variant_id=10)
        # One valid row
        _add_item(session, "c@example.com", "#2", "Teak", variant_id=20)
        session.commit()

        engine = session.get_bind()
        report = run_migration(create=True, bind=engine)

        assert report["status"] == "created"
        assert report["duplicates"] == []
        assert report["index_exists_after"] is True

    def test_rollback_drops_index(self, db_session):
        """[P6.5-T5] Rollback drops only uq_client_order_variant."""
        session = db_session()
        _add_item(session, "a@example.com", "#1", "Silver", variant_id=10)
        session.commit()

        engine = session.get_bind()

        # Create index
        r1 = run_migration(create=True, bind=engine)
        assert r1["status"] == "created"

        # Rollback
        r2 = run_migration(rollback=True, bind=engine)
        assert r2["status"] == "dropped"
        assert r2["index_exists_after"] is False

    def test_rollback_noop_when_no_index(self, db_session):
        """Rollback on missing index returns noop, no error."""
        session = db_session()
        engine = session.get_bind()

        report = run_migration(rollback=True, bind=engine)
        assert report["status"] == "noop"

    def test_check_only_no_ddl(self, db_session):
        """check_only reports duplicates but does not create index."""
        session = db_session()
        _add_item(session, "a@example.com", "#1", "Silver", variant_id=10)
        session.commit()

        engine = session.get_bind()
        report = run_migration(check_only=True, bind=engine)

        assert report["status"] == "clean"
        assert report["index_exists_after"] is False
