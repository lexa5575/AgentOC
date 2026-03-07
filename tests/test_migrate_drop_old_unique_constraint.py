"""Tests for Phase 9: drop old unique constraint uq_client_order_item.

Uses SQLite in-memory DB via the shared db_session fixture.
"""

from unittest.mock import MagicMock, call

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from db.models import ClientOrderItem
from scripts.migrate_drop_old_unique_constraint import (
    OLD_CONSTRAINT,
    PREREQ_INDEX,
    _constraint_exists,
    run_migration,
)


def _add_item(session, email, order_id, base_flavor, variant_id=None):
    """Insert a ClientOrderItem via raw SQL to bypass ORM constraint checks."""
    conn = session.get_bind().connect()
    conn.execute(text(
        "INSERT INTO client_order_items "
        "(client_email, order_id, product_name, base_flavor, product_type, quantity, variant_id) "
        "VALUES (:email, :order_id, :product_name, :base_flavor, 'stick', 1, :variant_id)"
    ), {
        "email": email.lower().strip(),
        "order_id": order_id,
        "product_name": base_flavor,
        "base_flavor": base_flavor,
        "variant_id": variant_id,
    })
    conn.commit()
    conn.close()


def _create_prereq_index(session):
    """Create the prerequisite uq_client_order_variant partial index."""
    conn = session.get_bind().connect()
    conn.execute(text(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {PREREQ_INDEX} "
        "ON client_order_items (client_email, order_id, variant_id) "
        "WHERE variant_id IS NOT NULL AND order_id IS NOT NULL"
    ))
    conn.commit()
    conn.close()


def _create_old_constraint(session):
    """Manually create old uq_client_order_item (removed from model in Phase 9.1).

    In SQLite, CREATE UNIQUE INDEX is functionally equivalent to a UNIQUE constraint
    and is detected by _constraint_exists().
    """
    conn = session.get_bind().connect()
    conn.execute(text(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {OLD_CONSTRAINT} "
        "ON client_order_items (client_email, order_id, base_flavor)"
    ))
    conn.commit()
    conn.close()


class TestModelMetadata:
    """Verify ORM model no longer declares the old constraint."""

    def test_no_uq_client_order_item_in_table_args(self):
        """[P9.1-T1] ClientOrderItem must not declare uq_client_order_item."""
        table_args = getattr(ClientOrderItem, "__table_args__", None)
        if table_args is None:
            return  # no table_args at all — OK
        # Check that no UniqueConstraint with the old name exists
        for arg in (table_args if isinstance(table_args, tuple) else (table_args,)):
            if hasattr(arg, "name"):
                assert arg.name != OLD_CONSTRAINT, (
                    f"uq_client_order_item still declared in ClientOrderItem.__table_args__"
                )


class TestPgConstraintQuery:
    """Verify PostgreSQL constraint check is table-scoped."""

    def test_pg_query_includes_table_scope(self):
        """[P9.1-T2] PG path joins pg_class to scope by table name."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None

        _constraint_exists(mock_conn, "postgresql")

        # Extract the SQL string passed to execute
        sql_arg = mock_conn.execute.call_args[0][0]
        sql_text = str(sql_arg)
        assert "pg_class" in sql_text
        assert "client_order_items" in sql_text


class TestMigrateDropOldUniqueConstraint:

    # ---------------------------------------------------------------
    # check-only
    # ---------------------------------------------------------------

    def test_check_only_shows_constraint_exists(self, db_session):
        """[P9-T1] check-only correctly detects old constraint presence."""
        session = db_session()
        _create_old_constraint(session)
        engine = session.get_bind()

        report = run_migration(check_only=True, bind=engine)

        assert report["status"] == "check_complete"
        assert report["old_constraint_existed"] is True
        assert report["old_constraint_exists_after"] is True
        assert report["prereq_index_exists"] is False

    def test_check_only_shows_prereq_when_present(self, db_session):
        """[P9-T2] check-only detects prerequisite index."""
        session = db_session()
        _create_old_constraint(session)
        _create_prereq_index(session)
        engine = session.get_bind()

        report = run_migration(check_only=True, bind=engine)

        assert report["prereq_index_exists"] is True
        assert report["old_constraint_existed"] is True

    # ---------------------------------------------------------------
    # execute
    # ---------------------------------------------------------------

    def test_execute_blocks_without_prereq(self, db_session):
        """[P9-T3] Execute blocked when uq_client_order_variant missing."""
        session = db_session()
        _create_old_constraint(session)
        engine = session.get_bind()

        report = run_migration(execute=True, bind=engine)

        assert report["status"] == "blocked"
        assert PREREQ_INDEX in report["reasons"][0]
        assert report["old_constraint_exists_after"] is True

    def test_execute_drops_constraint(self, db_session):
        """[P9-T4] Execute drops old constraint when prerequisites met."""
        session = db_session()
        _add_item(session, "a@example.com", "#1", "Silver", variant_id=10)
        _create_old_constraint(session)
        _create_prereq_index(session)
        engine = session.get_bind()

        report = run_migration(execute=True, bind=engine)

        assert report["status"] == "dropped"
        assert report["old_constraint_existed"] is True
        assert report["old_constraint_exists_after"] is False

    def test_execute_idempotent_noop(self, db_session):
        """[P9-T5] Re-run after drop returns noop."""
        session = db_session()
        _create_old_constraint(session)
        _create_prereq_index(session)
        engine = session.get_bind()

        r1 = run_migration(execute=True, bind=engine)
        assert r1["status"] == "dropped"

        r2 = run_migration(execute=True, bind=engine)
        assert r2["status"] == "noop"

    # ---------------------------------------------------------------
    # rollback
    # ---------------------------------------------------------------

    def test_rollback_restores_constraint(self, db_session):
        """[P9-T6] Rollback re-adds constraint after drop."""
        session = db_session()
        _add_item(session, "a@example.com", "#1", "Silver", variant_id=10)
        _create_old_constraint(session)
        _create_prereq_index(session)
        engine = session.get_bind()

        # Drop
        r1 = run_migration(execute=True, bind=engine)
        assert r1["status"] == "dropped"

        # Rollback
        r2 = run_migration(rollback=True, bind=engine)
        assert r2["status"] == "restored"
        assert r2["old_constraint_exists_after"] is True

    def test_rollback_blocks_on_old_key_duplicates(self, db_session):
        """[P9-T7] Rollback blocked when old-key duplicates exist."""
        session = db_session()
        _create_old_constraint(session)
        _create_prereq_index(session)
        engine = session.get_bind()

        # Drop constraint first
        r1 = run_migration(execute=True, bind=engine)
        assert r1["status"] == "dropped"

        # Insert rows that duplicate on (email, order_id, base_flavor)
        # but differ on variant_id — allowed after drop
        _add_item(session, "a@example.com", "#1", "Silver", variant_id=10)
        _add_item(session, "a@example.com", "#1", "Silver", variant_id=20)

        # Rollback should be blocked
        r2 = run_migration(rollback=True, bind=engine)
        assert r2["status"] == "blocked"
        assert len(r2["duplicates"]) == 1
        assert r2["duplicates"][0]["count"] == 2
        assert "duplicate" in r2["reasons"][0]

    def test_rollback_noop_when_constraint_exists(self, db_session):
        """[P9-T8] Rollback on existing constraint returns noop."""
        session = db_session()
        _create_old_constraint(session)
        engine = session.get_bind()

        report = run_migration(rollback=True, bind=engine)
        assert report["status"] == "noop"

    # ---------------------------------------------------------------
    # DB behavior verification
    # ---------------------------------------------------------------

    def test_same_base_flavor_different_variant_id_after_drop(self, db_session):
        """[P9-T9] After drop: same (email, order_id, base_flavor) with
        different variant_id is allowed.

        Confirms the Phase 9 goal: Silver EU + Silver ME in same order.
        """
        session = db_session()
        _create_old_constraint(session)
        _create_prereq_index(session)
        engine = session.get_bind()

        # Drop old constraint
        r = run_migration(execute=True, bind=engine)
        assert r["status"] == "dropped"

        # Insert: same email, order, base_flavor — different variant_id
        # (Silver EU id=10, Silver ME id=20)
        _add_item(session, "client@example.com", "#100", "Silver", variant_id=10)
        _add_item(session, "client@example.com", "#100", "Silver", variant_id=20)

        # Verify both rows exist
        conn = engine.connect()
        rows = conn.execute(text(
            "SELECT variant_id FROM client_order_items "
            "WHERE client_email = 'client@example.com' AND order_id = '#100' "
            "AND base_flavor = 'Silver' "
            "ORDER BY variant_id"
        )).fetchall()
        conn.close()

        assert len(rows) == 2
        assert rows[0][0] == 10
        assert rows[1][0] == 20

    def test_prereq_index_still_prevents_true_duplicates(self, db_session):
        """[P9-T10] After drop: uq_client_order_variant still blocks
        true duplicates (same email + order + variant_id)."""
        session = db_session()
        _create_old_constraint(session)
        _create_prereq_index(session)
        engine = session.get_bind()

        r = run_migration(execute=True, bind=engine)
        assert r["status"] == "dropped"

        # First insert
        _add_item(session, "client@example.com", "#100", "Silver", variant_id=10)

        # Same (email, order, variant_id) → blocked by uq_client_order_variant
        with pytest.raises(IntegrityError):
            _add_item(session, "client@example.com", "#100", "Silver EU", variant_id=10)
