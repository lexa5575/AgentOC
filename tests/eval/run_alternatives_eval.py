"""
Alternatives Quality Eval
--------------------------

For each OOS-able product in the catalog, calls select_best_alternatives()
and checks whether the suggested alternatives are from the same flavor_family.

Usage (on server):
    docker exec agentos-api python -m tests.eval.run_alternatives_eval

    # Specific flavor:
    docker exec agentos-api python -m tests.eval.run_alternatives_eval --flavor Bronze

    # With client context:
    docker exec agentos-api python -m tests.eval.run_alternatives_eval --flavor Bronze --client test@example.com
"""

import argparse
import logging
import sys
from collections import defaultdict

from db.models import ProductCatalog, StockItem, get_session
from db.stock import select_best_alternatives
from db.catalog import get_display_name

logging.basicConfig(level=logging.WARNING, format="%(levelname)-8s %(message)s")
logger = logging.getLogger(__name__)


def _get_all_products() -> list[dict]:
    """Get all products from catalog with their flavor_family."""
    session = get_session()
    try:
        rows = session.query(ProductCatalog).order_by(
            ProductCatalog.category, ProductCatalog.stock_name
        ).all()
        return [
            {
                "id": r.id,
                "stock_name": r.stock_name,
                "category": r.category,
                "flavor_family": r.flavor_family,
                "name_norm": r.name_norm,
            }
            for r in rows
        ]
    finally:
        session.close()


def _get_in_stock_product_ids() -> set[int]:
    """Get product_ids that are currently in stock."""
    session = get_session()
    try:
        rows = session.query(StockItem.product_id).filter(
            StockItem.quantity > 0,
            StockItem.product_id.isnot(None),
        ).distinct().all()
        return {r[0] for r in rows}
    finally:
        session.close()


def _check_flavor_match(oos_family: str | None, alt_family: str | None) -> str:
    """Check if alternative's flavor family matches OOS product."""
    if not oos_family or not alt_family:
        return "?"
    if oos_family == alt_family:
        return "OK"
    return "MISMATCH"


def run_eval(
    flavor_filter: str | None = None,
    client_email: str = "test@example.com",
    show_all: bool = False,
):
    products = _get_all_products()
    in_stock_ids = _get_in_stock_product_ids()

    # Group products by (stock_name, category) for unique entries
    seen = set()
    test_products = []
    for p in products:
        key = (p["stock_name"].lower(), p["category"])
        if key in seen:
            continue
        seen.add(key)

        # Skip devices
        if p["category"] in ("ONE", "STND", "PRIME"):
            continue

        if flavor_filter and flavor_filter.lower() not in p["stock_name"].lower():
            continue

        test_products.append(p)

    print(f"\nTesting {len(test_products)} products | client: {client_email}")
    print("=" * 100)

    stats = {"total": 0, "ok": 0, "mismatch": 0, "no_alts": 0, "unknown": 0}
    mismatches = []

    for p in test_products:
        display = get_display_name(p["stock_name"], p["category"])
        oos_family = p["flavor_family"]

        result = select_best_alternatives(
            client_email=client_email,
            base_flavor=p["stock_name"],
            original_product_name=display,
            client_summary="",
            warehouse=None,
        )

        alts = result.get("alternatives", [])
        stats["total"] += 1

        if not alts:
            stats["no_alts"] += 1
            if show_all:
                print(f"  {'—':>2}  {display:<35} [{oos_family:<15}] → NO ALTERNATIVES")
            continue

        row_has_mismatch = False
        alt_details = []
        for a in alts:
            alt_item = a["alternative"]
            alt_display = get_display_name(alt_item["product_name"], alt_item["category"])
            alt_family = alt_item.get("flavor_family")
            reason = a.get("reason", "?")
            match = _check_flavor_match(oos_family, alt_family)

            if match == "MISMATCH":
                row_has_mismatch = True
                stats["mismatch"] += 1
            elif match == "OK":
                stats["ok"] += 1
            else:
                stats["unknown"] += 1

            alt_details.append({
                "display": alt_display,
                "family": alt_family,
                "reason": reason,
                "match": match,
            })

        # Print row
        if row_has_mismatch or show_all:
            status = "FAIL" if row_has_mismatch else "  OK"
            print(f"\n{status}  {display:<35} [{oos_family:<15}]")
            for ad in alt_details:
                match_icon = "✅" if ad["match"] == "OK" else "❌" if ad["match"] == "MISMATCH" else "❓"
                print(
                    f"       {match_icon} {ad['display']:<35} [{ad['family']:<15}] reason={ad['reason']}"
                )

            if row_has_mismatch:
                mismatches.append({
                    "oos": display,
                    "oos_family": oos_family,
                    "alts": alt_details,
                })

    # Summary
    total_alts = stats["ok"] + stats["mismatch"] + stats["unknown"]
    print("\n" + "=" * 100)
    print(f"SUMMARY: {stats['total']} products tested")
    print(f"  Alternatives generated: {total_alts} total")
    print(f"  ✅ Family match:    {stats['ok']}")
    print(f"  ❌ Family mismatch: {stats['mismatch']}")
    print(f"  ❓ Unknown family:  {stats['unknown']}")
    print(f"  — No alternatives: {stats['no_alts']} products")

    if mismatches:
        print(f"\n{'=' * 100}")
        print(f"MISMATCHES ({len(mismatches)} products with wrong flavor family):")
        for m in mismatches:
            bad = [a for a in m["alts"] if a["match"] == "MISMATCH"]
            bad_str = ", ".join(f"{a['display']}[{a['family']}]" for a in bad)
            print(f"  {m['oos']} [{m['oos_family']}] → {bad_str}")

    return stats


def main():
    parser = argparse.ArgumentParser(description="Eval alternatives quality")
    parser.add_argument("--flavor", type=str, default=None,
                        help="Filter by flavor name (e.g. Bronze, Amber)")
    parser.add_argument("--client", type=str, default="test@example.com",
                        help="Client email for history context")
    parser.add_argument("--all", action="store_true",
                        help="Show all results, not just mismatches")
    args = parser.parse_args()

    run_eval(
        flavor_filter=args.flavor,
        client_email=args.client,
        show_all=args.all,
    )


if __name__ == "__main__":
    main()
