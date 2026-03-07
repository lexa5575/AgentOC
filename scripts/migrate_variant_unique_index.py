"""Add partial unique index uq_client_order_variant on client_order_items.

Phase 6.5 of the variant_id-first migration.

Creates:
    UNIQUE INDEX uq_client_order_variant
    ON client_order_items (client_email, order_id, variant_id)
    WHERE variant_id IS NOT NULL AND order_id IS NOT NULL

Does NOT touch the old uq_client_order_item constraint (that's Phase 8).

Usage:
    # Check for duplicates only (no DDL):
    python scripts/migrate_variant_unique_index.py --check-only

    # Create index (blocks if duplicates found):
    python scripts/migrate_variant_unique_index.py

    # Rollback (drop index):
    python scripts/migrate_variant_unique_index.py --rollback
"""

import argparse
import logging
import sys

from sqlalchemy import text

logger = logging.getLogger(__name__)

INDEX_NAME = "uq_client_order_variant"


def _index_exists(conn, dialect_name: str) -> bool:
    """Check if uq_client_order_variant index exists."""
    if dialect_name == "sqlite":
        row = conn.execute(text(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name=:name"
        ), {"name": INDEX_NAME}).fetchone()
    else:
        # PostgreSQL
        row = conn.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE indexname = :name"
        ), {"name": INDEX_NAME}).fetchone()
    return row is not None


def _find_duplicates(conn) -> list[dict]:
    """Find duplicate (client_email, order_id, variant_id) rows.

    Only considers rows where both variant_id and order_id are NOT NULL.
    Returns list of dicts with group info.
    """
    rows = conn.execute(text("""
        SELECT client_email, order_id, variant_id, COUNT(*) AS cnt
        FROM client_order_items
        WHERE variant_id IS NOT NULL AND order_id IS NOT NULL
        GROUP BY client_email, order_id, variant_id
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC
    """)).fetchall()

    return [
        {
            "client_email": r[0],
            "order_id": r[1],
            "variant_id": r[2],
            "count": r[3],
        }
        for r in rows
    ]


def run_migration(
    *,
    create: bool = True,
    rollback: bool = False,
    check_only: bool = False,
    bind=None,
) -> dict:
    """Core migration logic — testable with injected engine.

    Args:
        create: Create the index (default True).
        rollback: Drop the index instead of creating.
        check_only: Only check for duplicates, no DDL.
        bind: SQLAlchemy engine. Defaults to db.models.engine.

    Returns:
        Report dict with status, duplicates info, etc.
    """
    if bind is None:
        from db.models import engine
        bind = engine

    report = {
        "action": "check_only" if check_only else ("rollback" if rollback else "create"),
        "index_name": INDEX_NAME,
        "index_existed": False,
        "index_exists_after": False,
        "duplicates": [],
        "status": "ok",
    }

    with bind.connect() as conn:
        dialect = bind.dialect.name
        report["index_existed"] = _index_exists(conn, dialect)

        if check_only:
            dupes = _find_duplicates(conn)
            report["duplicates"] = dupes
            report["status"] = "duplicates_found" if dupes else "clean"
            report["index_exists_after"] = report["index_existed"]
            return report

        if rollback:
            if not report["index_existed"]:
                logger.info("Index %s does not exist, nothing to drop", INDEX_NAME)
                report["status"] = "noop"
            else:
                conn.execute(text(f"DROP INDEX IF EXISTS {INDEX_NAME}"))
                conn.commit()
                logger.info("Index %s dropped", INDEX_NAME)
                report["status"] = "dropped"
            report["index_exists_after"] = _index_exists(conn, dialect)
            return report

        # Create path
        if report["index_existed"]:
            logger.info("Index %s already exists, skipping", INDEX_NAME)
            report["status"] = "already_exists"
            report["index_exists_after"] = True
            return report

        # Check duplicates before creating
        dupes = _find_duplicates(conn)
        if dupes:
            report["duplicates"] = dupes
            report["status"] = "blocked_duplicates"
            report["index_exists_after"] = False
            logger.error(
                "Cannot create index: %d duplicate group(s) found. "
                "Resolve duplicates first.",
                len(dupes),
            )
            for d in dupes[:5]:
                logger.error(
                    "  dup: email=%s order=%s variant=%s count=%d",
                    d["client_email"], d["order_id"],
                    d["variant_id"], d["count"],
                )
            return report

        # Create the partial unique index
        conn.execute(text(
            f"CREATE UNIQUE INDEX {INDEX_NAME} "
            "ON client_order_items (client_email, order_id, variant_id) "
            "WHERE variant_id IS NOT NULL AND order_id IS NOT NULL"
        ))
        conn.commit()
        logger.info("Index %s created", INDEX_NAME)

        report["status"] = "created"
        report["index_exists_after"] = _index_exists(conn, dialect)

    return report


def main():
    parser = argparse.ArgumentParser(
        description="Add partial unique index uq_client_order_variant.",
    )
    parser.add_argument(
        "--check-only", action="store_true",
        help="Only check for duplicates, no DDL.",
    )
    parser.add_argument(
        "--rollback", action="store_true",
        help="Drop the index instead of creating.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    report = run_migration(
        create=not args.rollback,
        rollback=args.rollback,
        check_only=args.check_only,
    )

    import json
    print(json.dumps(report, indent=2, default=str))

    if report["status"] == "blocked_duplicates":
        sys.exit(1)


if __name__ == "__main__":
    main()
