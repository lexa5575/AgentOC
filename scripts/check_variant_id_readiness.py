"""Preflight readiness check for REQUIRE_VARIANT_ID=true (Phase 7).

Checks:
1. variant_id coverage (% of client_order_items with variant_id NOT NULL)
2. Duplicate groups that would violate uq_client_order_variant
3. Blocked/unresolved fulfillment events in last 24h
4. Stuck processing events

Usage:
    python scripts/check_variant_id_readiness.py
    python scripts/check_variant_id_readiness.py --min-coverage 90
    python scripts/check_variant_id_readiness.py --max-ambiguous-pct 5
"""

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

logger = logging.getLogger(__name__)


def run_readiness_check(
    *,
    min_coverage: float = 95.0,
    max_ambiguous_pct: float = 3.0,
    bind=None,
) -> dict:
    """Core readiness check — testable with injected engine.

    Args:
        min_coverage: Minimum % of rows with variant_id NOT NULL.
        max_ambiguous_pct: Maximum % of ambiguous rows allowed.
        bind: SQLAlchemy engine. Defaults to db.models.engine.

    Returns:
        Report dict with metrics and go/no-go decision.
    """
    if bind is None:
        from db.models import engine
        bind = engine

    report = {
        "total_rows": 0,
        "with_variant_rows": 0,
        "null_variant_rows": 0,
        "pct_with_variant": 0.0,
        "duplicate_groups_for_uq_client_order_variant": 0,
        "partial_unique_index_exists": False,
        "stuck_processing_events": 0,
        "blocked_ambiguous_last_24h": 0,
        "unresolved_strict_last_24h": 0,
        "go_no_go": False,
        "reasons": [],
        "thresholds": {
            "min_coverage": min_coverage,
            "max_ambiguous_pct": max_ambiguous_pct,
        },
    }

    dialect = bind.dialect.name

    with bind.connect() as conn:
        # 1. Coverage
        row = conn.execute(text(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN variant_id IS NOT NULL THEN 1 ELSE 0 END) AS with_variant "
            "FROM client_order_items"
        )).fetchone()

        total = row[0] or 0
        with_variant = row[1] or 0
        null_variant = total - with_variant

        report["total_rows"] = total
        report["with_variant_rows"] = with_variant
        report["null_variant_rows"] = null_variant
        report["pct_with_variant"] = (
            round(100.0 * with_variant / total, 2) if total > 0 else 0.0
        )

        # 2. Duplicate groups
        dupes = conn.execute(text(
            "SELECT COUNT(*) FROM ("
            "  SELECT client_email, order_id, variant_id "
            "  FROM client_order_items "
            "  WHERE variant_id IS NOT NULL AND order_id IS NOT NULL "
            "  GROUP BY client_email, order_id, variant_id "
            "  HAVING COUNT(*) > 1"
            ") AS dups"
        )).fetchone()
        report["duplicate_groups_for_uq_client_order_variant"] = dupes[0] if dupes else 0

        # 3. Partial unique index check
        report["partial_unique_index_exists"] = _index_exists(
            conn, dialect, "uq_client_order_variant",
        )

        # 4. Fulfillment events (last 24h) — only if table exists
        _check_fulfillment_events(conn, report, dialect)

    # Go/No-Go decision
    # Preserve reasons added by _check_fulfillment_events (e.g. fail-closed errors)
    reasons = list(report["reasons"])

    if report["pct_with_variant"] < min_coverage:
        reasons.append(
            f"coverage {report['pct_with_variant']}% < {min_coverage}% threshold"
        )

    if report["duplicate_groups_for_uq_client_order_variant"] > 0:
        reasons.append(
            f"{report['duplicate_groups_for_uq_client_order_variant']} duplicate groups "
            "for uq_client_order_variant"
        )

    if not report["partial_unique_index_exists"]:
        reasons.append(
            "missing uq_client_order_variant partial unique index"
        )

    if report["stuck_processing_events"] > 0:
        reasons.append(
            f"{report['stuck_processing_events']} stuck processing events"
        )

    # Ambiguous check: null_variant as % of total
    if total > 0:
        ambiguous_pct = round(100.0 * null_variant / total, 2)
        if ambiguous_pct > max_ambiguous_pct:
            reasons.append(
                f"null variant_id rate {ambiguous_pct}% > {max_ambiguous_pct}% threshold"
            )

    report["reasons"] = reasons
    report["go_no_go"] = len(reasons) == 0

    return report


def _index_exists(conn, dialect: str, index_name: str) -> bool:
    """Check if a named index exists (dialect-aware)."""
    if dialect == "sqlite":
        row = conn.execute(text(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name=:name"
        ), {"name": index_name}).fetchone()
    else:
        row = conn.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE indexname = :name"
        ), {"name": index_name}).fetchone()
    return row is not None


def _check_fulfillment_events(conn, report, dialect):
    """Check fulfillment_events table for stuck/blocked events.

    Handles case where table may not exist (e.g., fresh test DB).
    Unresolved strict count uses Python-side details_json parsing
    (dialect-agnostic — no SQL JSON functions).
    """
    from db.fulfillment import parse_details_json

    try:
        # Check table exists
        if dialect == "sqlite":
            exists = conn.execute(text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='fulfillment_events'"
            )).fetchone()
        else:
            exists = conn.execute(text(
                "SELECT tablename FROM pg_tables "
                "WHERE tablename = 'fulfillment_events'"
            )).fetchone()

        if not exists:
            return

        # Stuck processing
        stuck = conn.execute(text(
            "SELECT COUNT(*) FROM fulfillment_events WHERE status = 'processing'"
        )).fetchone()
        report["stuck_processing_events"] = stuck[0] if stuck else 0

        # Blocked ambiguous last 24h (total count)
        blocked = conn.execute(text(
            "SELECT COUNT(*) FROM fulfillment_events "
            "WHERE status = 'blocked_ambiguous_variant' "
            "AND created_at > :cutoff"
        ), {"cutoff": datetime.now(UTC) - timedelta(hours=24)}).fetchone()
        report["blocked_ambiguous_last_24h"] = blocked[0] if blocked else 0

        # Unresolved strict last 24h:
        # blocked_ambiguous_variant events with reason=unresolved_variant_strict
        # in details_json. Parse in Python (no SQL JSON functions).
        rows = conn.execute(text(
            "SELECT details_json FROM fulfillment_events "
            "WHERE status = 'blocked_ambiguous_variant' "
            "AND created_at > :cutoff"
        ), {"cutoff": datetime.now(UTC) - timedelta(hours=24)}).fetchall()

        unresolved_strict = 0
        for row in rows:
            parsed = parse_details_json(row[0])
            if parsed.get("reason") == "unresolved_variant_strict":
                unresolved_strict += 1
        report["unresolved_strict_last_24h"] = unresolved_strict

    except Exception as e:
        logger.warning("Could not check fulfillment_events: %s", e)
        report["reasons"].append(
            f"fulfillment_events check failed: {e}"
        )


def main():
    parser = argparse.ArgumentParser(
        description="Preflight readiness check for REQUIRE_VARIANT_ID=true.",
    )
    parser.add_argument(
        "--min-coverage", type=float, default=95.0,
        help="Minimum variant_id coverage %% (default: 95).",
    )
    parser.add_argument(
        "--max-ambiguous-pct", type=float, default=3.0,
        help="Maximum null variant_id %% (default: 3).",
    )
    parser.add_argument(
        "--json", action="store_true", default=True,
        help="Output JSON (default: true).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    report = run_readiness_check(
        min_coverage=args.min_coverage,
        max_ambiguous_pct=args.max_ambiguous_pct,
    )

    print(json.dumps(report, indent=2, default=str))

    sys.exit(0 if report["go_no_go"] else 1)


if __name__ == "__main__":
    main()
