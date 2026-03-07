"""Drop old unique constraint uq_client_order_item on client_order_items.

Phase 9 of the variant_id-first migration.

Drops:
    CONSTRAINT uq_client_order_item UNIQUE (client_email, order_id, base_flavor)

Prerequisite: partial index uq_client_order_variant must already exist (Phase 6.5).

Usage:
    # Check only (no DDL):
    python scripts/migrate_drop_old_unique_constraint.py --check-only

    # Drop constraint (blocks if prerequisite index missing):
    python scripts/migrate_drop_old_unique_constraint.py

    # Rollback (re-add constraint; blocks if duplicates on old key):
    python scripts/migrate_drop_old_unique_constraint.py --rollback
"""

import argparse
import json
import logging
import sys

from sqlalchemy import text

logger = logging.getLogger(__name__)

OLD_CONSTRAINT = "uq_client_order_item"
PREREQ_INDEX = "uq_client_order_variant"


def _constraint_exists(conn, dialect: str) -> bool:
    """Check if old uq_client_order_item exists (dialect-aware).

    SQLite: inline CONSTRAINT is not a named index — check CREATE TABLE sql.
    Also check for a standalone index (created by rollback).
    PostgreSQL: check pg_constraint catalog.
    """
    if dialect == "sqlite":
        # Check inline constraint in CREATE TABLE sql
        row = conn.execute(text(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='client_order_items'"
        )).fetchone()
        if row and OLD_CONSTRAINT in (row[0] or ""):
            return True
        # Also check standalone index (created by rollback's CREATE UNIQUE INDEX)
        idx = conn.execute(text(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name=:name"
        ), {"name": OLD_CONSTRAINT}).fetchone()
        return idx is not None
    else:
        row = conn.execute(text(
            "SELECT c.conname FROM pg_constraint c "
            "JOIN pg_class r ON c.conrelid = r.oid "
            "WHERE c.conname = :name AND r.relname = 'client_order_items'"
        ), {"name": OLD_CONSTRAINT}).fetchone()
        return row is not None


def _prereq_index_exists(conn, dialect: str) -> bool:
    """Check if prerequisite partial index uq_client_order_variant exists."""
    if dialect == "sqlite":
        row = conn.execute(text(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name=:name"
        ), {"name": PREREQ_INDEX}).fetchone()
    else:
        row = conn.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE indexname = :name"
        ), {"name": PREREQ_INDEX}).fetchone()
    return row is not None


def _find_old_key_duplicates(conn) -> list[dict]:
    """Find duplicate (client_email, order_id, base_flavor) rows.

    Used before rollback to ensure the old constraint can be restored.
    """
    rows = conn.execute(text("""
        SELECT client_email, order_id, base_flavor, COUNT(*) AS cnt
        FROM client_order_items
        WHERE order_id IS NOT NULL
        GROUP BY client_email, order_id, base_flavor
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC
    """)).fetchall()

    return [
        {
            "client_email": r[0],
            "order_id": r[1],
            "base_flavor": r[2],
            "count": r[3],
        }
        for r in rows
    ]


def _drop_constraint(conn, dialect: str) -> None:
    """Drop the old constraint (dialect-aware).

    PostgreSQL: ALTER TABLE DROP CONSTRAINT.
    SQLite: DROP INDEX for standalone indexes; table recreation for inline constraints.
    """
    if dialect == "sqlite":
        # Check if constraint exists as standalone index (droppable directly)
        standalone = conn.execute(text(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name=:name"
        ), {"name": OLD_CONSTRAINT}).fetchone()

        if standalone:
            conn.execute(text(f"DROP INDEX {OLD_CONSTRAINT}"))
            return

        # Inline constraint in CREATE TABLE → table recreation.
        # Save existing user-created index definitions first.
        idx_rows = conn.execute(text(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='client_order_items' "
            "AND sql IS NOT NULL"
        )).fetchall()
        saved_indexes = [r[0] for r in idx_rows]

        row = conn.execute(text(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='client_order_items'"
        )).fetchone()
        original_sql = row[0]

        lines = original_sql.split("\n")
        filtered = [line for line in lines if OLD_CONSTRAINT not in line]
        new_sql = "\n".join(filtered)
        new_sql = new_sql.replace(",\n)", "\n)")
        new_sql = new_sql.replace("client_order_items", "client_order_items_new", 1)

        conn.execute(text(new_sql))
        conn.execute(text(
            "INSERT INTO client_order_items_new "
            "SELECT * FROM client_order_items"
        ))
        conn.execute(text("DROP TABLE client_order_items"))
        conn.execute(text(
            "ALTER TABLE client_order_items_new "
            "RENAME TO client_order_items"
        ))

        for idx_sql in saved_indexes:
            conn.execute(text(idx_sql))
    else:
        conn.execute(text(
            f"ALTER TABLE client_order_items DROP CONSTRAINT {OLD_CONSTRAINT}"
        ))


def _add_constraint(conn, dialect: str) -> None:
    """Re-add the old constraint (dialect-aware).

    PostgreSQL: ALTER TABLE ADD CONSTRAINT.
    SQLite: CREATE UNIQUE INDEX (functionally equivalent).
    """
    if dialect == "sqlite":
        conn.execute(text(
            f"CREATE UNIQUE INDEX {OLD_CONSTRAINT} "
            "ON client_order_items (client_email, order_id, base_flavor)"
        ))
    else:
        conn.execute(text(
            f"ALTER TABLE client_order_items "
            f"ADD CONSTRAINT {OLD_CONSTRAINT} "
            "UNIQUE (client_email, order_id, base_flavor)"
        ))


def run_migration(
    *,
    execute: bool = True,
    rollback: bool = False,
    check_only: bool = False,
    bind=None,
) -> dict:
    """Core migration logic — testable with injected engine.

    Args:
        execute: Drop the old constraint (default True).
        rollback: Re-add the old constraint instead.
        check_only: Only report status, no DDL.
        bind: SQLAlchemy engine. Defaults to db.models.engine.

    Returns:
        Report dict with status and diagnostic info.
    """
    if bind is None:
        from db.models import engine
        bind = engine

    report = {
        "action": "check_only" if check_only else ("rollback" if rollback else "execute"),
        "old_constraint": OLD_CONSTRAINT,
        "prereq_index": PREREQ_INDEX,
        "old_constraint_existed": False,
        "old_constraint_exists_after": False,
        "prereq_index_exists": False,
        "duplicates": [],
        "status": "ok",
        "reasons": [],
    }

    with bind.connect() as conn:
        dialect = bind.dialect.name
        report["old_constraint_existed"] = _constraint_exists(conn, dialect)
        report["prereq_index_exists"] = _prereq_index_exists(conn, dialect)

        if check_only:
            report["status"] = "check_complete"
            report["old_constraint_exists_after"] = report["old_constraint_existed"]
            return report

        if rollback:
            if report["old_constraint_existed"]:
                logger.info("Constraint %s already exists, nothing to add", OLD_CONSTRAINT)
                report["status"] = "noop"
                report["old_constraint_exists_after"] = True
                return report

            # Pre-check: duplicates on old key would block constraint creation
            dupes = _find_old_key_duplicates(conn)
            if dupes:
                report["duplicates"] = dupes
                report["status"] = "blocked"
                report["reasons"].append(
                    f"{len(dupes)} duplicate group(s) on old key "
                    "(client_email, order_id, base_flavor)"
                )
                report["old_constraint_exists_after"] = False
                logger.error(
                    "Cannot restore constraint: %d old-key duplicate group(s). "
                    "Resolve before rollback.",
                    len(dupes),
                )
                for d in dupes[:5]:
                    logger.error(
                        "  dup: email=%s order=%s flavor=%s count=%d",
                        d["client_email"], d["order_id"],
                        d["base_flavor"], d["count"],
                    )
                return report

            _add_constraint(conn, dialect)
            conn.commit()
            logger.info("Constraint %s restored", OLD_CONSTRAINT)
            report["status"] = "restored"
            report["old_constraint_exists_after"] = _constraint_exists(conn, dialect)
            return report

        # Execute path: drop constraint
        if not report["old_constraint_existed"]:
            logger.info("Constraint %s does not exist, nothing to drop", OLD_CONSTRAINT)
            report["status"] = "noop"
            report["old_constraint_exists_after"] = False
            return report

        if not report["prereq_index_exists"]:
            report["status"] = "blocked"
            report["reasons"].append(
                f"prerequisite index {PREREQ_INDEX} must exist before dropping "
                f"{OLD_CONSTRAINT}"
            )
            report["old_constraint_exists_after"] = True
            logger.error(
                "Cannot drop %s: prerequisite index %s is missing. "
                "Run Phase 6.5 migration first.",
                OLD_CONSTRAINT, PREREQ_INDEX,
            )
            return report

        _drop_constraint(conn, dialect)
        conn.commit()
        logger.info("Constraint %s dropped", OLD_CONSTRAINT)
        report["status"] = "dropped"
        report["old_constraint_exists_after"] = _constraint_exists(conn, dialect)

    return report


def main():
    parser = argparse.ArgumentParser(
        description="Drop old unique constraint uq_client_order_item (Phase 9).",
    )
    parser.add_argument(
        "--check-only", action="store_true",
        help="Only check status, no DDL.",
    )
    parser.add_argument(
        "--rollback", action="store_true",
        help="Re-add the old constraint instead of dropping.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    report = run_migration(
        execute=not args.rollback,
        rollback=args.rollback,
        check_only=args.check_only,
    )

    print(json.dumps(report, indent=2, default=str))

    if report["status"] == "blocked":
        sys.exit(1)


if __name__ == "__main__":
    main()
