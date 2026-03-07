"""Backfill variant_id + display_name_snapshot for legacy ClientOrderItem rows.

Phase 6 of the variant_id-first migration.
Only touches rows where variant_id IS NULL — never overwrites existing values.

Usage:
    # Dry run (default — no writes):
    python scripts/backfill_variant_id.py

    # Execute (writes to DB):
    python scripts/backfill_variant_id.py --execute

    # With report:
    python scripts/backfill_variant_id.py --execute --report backfill_report.json

    # Batch control:
    python scripts/backfill_variant_id.py --execute --batch-size 50 --offset 100 --limit 500
"""

import argparse
import json
import logging
import sys

logger = logging.getLogger(__name__)


def run_backfill(
    *,
    dry_run: bool = True,
    batch_size: int = 100,
    offset: int = 0,
    limit: int | None = None,
    session_factory=None,
    resolver=None,
) -> dict:
    """Core backfill logic — testable without CLI.

    Args:
        dry_run: If True, don't write to DB.
        batch_size: Commit every N rows.
        offset: Skip first N rows.
        limit: Process at most N rows (None = all).
        session_factory: Callable returning a SQLAlchemy Session.
            Defaults to db.models.get_session.
        resolver: Callable(name) -> ResolveResult.
            Defaults to db.product_resolver.resolve_product_to_catalog.

    Returns:
        Report dict with counters and problem rows.
    """
    if session_factory is None:
        from db.models import get_session
        session_factory = get_session
    if resolver is None:
        from db.product_resolver import resolve_product_to_catalog
        resolver = resolve_product_to_catalog

    from db.models import ClientOrderItem

    report = {
        "total": 0,
        "processed": 0,
        "resolved": 0,
        "ambiguous": 0,
        "unresolved": 0,
        "rows": [],
    }

    session = session_factory()
    try:
        # Query only NULL variant_id rows, ordered by id for determinism
        q = (
            session.query(ClientOrderItem)
            .filter(ClientOrderItem.variant_id.is_(None))
            .order_by(ClientOrderItem.id)
        )

        all_items = q.all()
        report["total"] = len(all_items)

        # Apply offset and limit
        items = all_items[offset:]
        if limit is not None:
            items = items[:limit]

        batch_count = 0

        for item in items:
            report["processed"] += 1

            # 1. Primary resolve: product_name
            resolved = resolver(item.product_name)
            usable = (
                resolved.confidence in ("exact", "high")
                and len(resolved.product_ids) == 1
            )

            # 2. Fallback resolve: base_flavor (only if primary failed)
            if not usable and item.base_flavor != item.product_name:
                resolved = resolver(item.base_flavor)
                usable = (
                    resolved.confidence in ("exact", "high")
                    and len(resolved.product_ids) == 1
                )

            if usable:
                # Single match — set variant_id
                report["resolved"] += 1
                if not dry_run:
                    item.variant_id = resolved.product_ids[0]
                    if resolved.display_name:
                        item.display_name_snapshot = resolved.display_name
            elif resolved.confidence in ("exact", "high") and len(resolved.product_ids) > 1:
                # Ambiguous
                report["ambiguous"] += 1
                report["rows"].append({
                    "id": item.id,
                    "client_email": item.client_email,
                    "order_id": item.order_id,
                    "product_name": item.product_name,
                    "base_flavor": item.base_flavor,
                    "reason": "ambiguous",
                    "candidate_ids": resolved.product_ids,
                })
            else:
                # Unresolved (low confidence or no matches)
                report["unresolved"] += 1
                report["rows"].append({
                    "id": item.id,
                    "client_email": item.client_email,
                    "order_id": item.order_id,
                    "product_name": item.product_name,
                    "base_flavor": item.base_flavor,
                    "reason": "unresolved",
                    "candidate_ids": resolved.product_ids,
                })

            batch_count += 1
            if batch_count >= batch_size and not dry_run:
                session.commit()
                logger.info(
                    "Backfill checkpoint: processed=%d resolved=%d "
                    "ambiguous=%d unresolved=%d",
                    report["processed"], report["resolved"],
                    report["ambiguous"], report["unresolved"],
                )
                batch_count = 0

        # Final commit for remaining rows
        if not dry_run:
            session.commit()

        logger.info(
            "Backfill %s: total=%d processed=%d resolved=%d "
            "ambiguous=%d unresolved=%d",
            "DRY RUN" if dry_run else "DONE",
            report["total"], report["processed"],
            report["resolved"], report["ambiguous"], report["unresolved"],
        )

    except Exception:
        if not dry_run:
            session.rollback()
        raise
    finally:
        session.close()

    return report


def main():
    parser = argparse.ArgumentParser(
        description="Backfill variant_id for legacy ClientOrderItem rows.",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually write to DB (default is dry-run).",
    )
    parser.add_argument(
        "--batch-size", type=int, default=100,
        help="Commit every N rows (default: 100).",
    )
    parser.add_argument(
        "--offset", type=int, default=0,
        help="Skip first N rows (default: 0).",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process at most N rows (default: all).",
    )
    parser.add_argument(
        "--report", type=str, default=None,
        help="Path to save JSON report.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    dry_run = not args.execute
    if dry_run:
        logger.info("DRY RUN mode — no DB writes")
    else:
        logger.info("EXECUTE mode — will write to DB")

    report = run_backfill(
        dry_run=dry_run,
        batch_size=args.batch_size,
        offset=args.offset,
        limit=args.limit,
    )

    print(json.dumps(report, indent=2, default=str))

    if args.report:
        with open(args.report, "w") as f:
            json.dump(report, f, indent=2, default=str)
        logger.info("Report saved to %s", args.report)

    # Exit code: 0 if no problems, 1 if ambiguous/unresolved exist
    if report["ambiguous"] > 0 or report["unresolved"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
