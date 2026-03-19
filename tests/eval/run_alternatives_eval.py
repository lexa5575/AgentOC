"""
Alternatives Quality Eval
--------------------------

For each OOS-able product in the catalog, calls select_best_alternatives()
and checks whether the suggested alternatives are from the same flavor_family.

Distinguishes between:
  - REAL BUG: same-family alternatives existed in stock but LLM chose wrong family
  - FORCED: no same-family alternatives in stock, fallback is expected

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

from db.models import ProductCatalog, StockItem, get_session
from db.stock import select_best_alternatives, get_product_type, _get_allowed_categories
from db.catalog import get_display_name
from db.product_resolver import resolve_product_to_catalog

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


def _get_available_same_family(
    oos_product: dict,
    all_products: list[dict],
) -> list[dict]:
    """Check if there are in-stock products with the same flavor_family.

    Returns list of available same-family products (excluding the OOS item itself).
    """
    oos_family = oos_product["flavor_family"]
    oos_name = oos_product["stock_name"].lower()
    oos_category = oos_product["category"]

    if not oos_family:
        return []

    # Get allowed categories for this product type
    product_type = get_product_type(oos_product["stock_name"])
    allowed_cats = _get_allowed_categories(product_type)

    session = get_session()
    try:
        # Find in-stock items with same flavor_family
        rows = session.query(StockItem, ProductCatalog.flavor_family, ProductCatalog.stock_name).outerjoin(
            ProductCatalog, StockItem.product_id == ProductCatalog.id,
        ).filter(
            StockItem.category.in_(allowed_cats),
            StockItem.quantity > 0,
            ProductCatalog.flavor_family == oos_family,
        ).all()

        available = []
        seen = set()
        for item, family, stock_name in rows:
            # Skip the OOS product itself
            if stock_name and stock_name.lower() == oos_name and item.category == oos_category:
                continue
            key = (stock_name, item.category)
            if key in seen:
                continue
            seen.add(key)
            available.append({
                "stock_name": stock_name,
                "category": item.category,
                "flavor_family": family,
                "quantity": item.quantity,
            })
        return available
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

    stats = {
        "total": 0, "ok": 0, "mismatch_bug": 0, "mismatch_forced": 0,
        "no_alts": 0, "unknown": 0,
    }
    real_bugs = []
    forced_mismatches = []

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
                print(f"   —  {display:<35} [{oos_family:<15}] → NO ALTERNATIVES")
            continue

        # Check if same-family alternatives exist in stock
        same_family_available = _get_available_same_family(p, products)
        has_same_family_in_stock = len(same_family_available) > 0

        row_mismatches = []
        alt_details = []
        for a in alts:
            alt_item = a["alternative"]
            alt_display = get_display_name(alt_item["product_name"], alt_item["category"])
            alt_family = alt_item.get("flavor_family")
            reason = a.get("reason", "?")
            match = _check_flavor_match(oos_family, alt_family)

            if match == "MISMATCH":
                row_mismatches.append(alt_display)
                if has_same_family_in_stock:
                    stats["mismatch_bug"] += 1
                else:
                    stats["mismatch_forced"] += 1
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

        is_bug = row_mismatches and has_same_family_in_stock
        is_forced = row_mismatches and not has_same_family_in_stock

        # Print row
        if row_mismatches or show_all:
            if is_bug:
                status = " BUG"
            elif is_forced:
                status = "FRCD"
            else:
                status = "  OK"

            avail_note = ""
            if row_mismatches:
                sf_count = len(same_family_available)
                if has_same_family_in_stock:
                    sf_names = ", ".join(
                        f"{s['stock_name']}({s['category']})"
                        for s in same_family_available[:5]
                    )
                    avail_note = f"  ← {sf_count} same-family in stock: {sf_names}"
                else:
                    avail_note = f"  ← 0 same-family in stock (forced fallback)"

            print(f"\n{status}  {display:<35} [{oos_family:<15}]{avail_note}")
            for ad in alt_details:
                match_icon = "✅" if ad["match"] == "OK" else "❌" if ad["match"] == "MISMATCH" else "❓"
                print(
                    f"       {match_icon} {ad['display']:<35} [{ad['family']:<15}] reason={ad['reason']}"
                )

            if is_bug:
                real_bugs.append({
                    "oos": display,
                    "oos_family": oos_family,
                    "alts": alt_details,
                    "same_family_available": same_family_available[:5],
                })
            elif is_forced:
                forced_mismatches.append({
                    "oos": display,
                    "oos_family": oos_family,
                    "alts": alt_details,
                })

    # Summary
    total_alts = stats["ok"] + stats["mismatch_bug"] + stats["mismatch_forced"] + stats["unknown"]
    print("\n" + "=" * 100)
    print(f"SUMMARY: {stats['total']} products tested, {total_alts} alternatives generated")
    print(f"  ✅ Family match:       {stats['ok']}")
    print(f"  🐛 Mismatch (BUG):    {stats['mismatch_bug']}  ← same-family WAS available but not chosen")
    print(f"  ⚠️  Mismatch (FORCED): {stats['mismatch_forced']}  ← no same-family in stock, fallback OK")
    print(f"  ❓ Unknown family:     {stats['unknown']}")
    print(f"  — No alternatives:    {stats['no_alts']} products")

    if real_bugs:
        print(f"\n{'=' * 100}")
        print(f"🐛 REAL BUGS ({len(real_bugs)} products — same-family was available but ignored):")
        for m in real_bugs:
            bad = [a for a in m["alts"] if a["match"] == "MISMATCH"]
            bad_str = ", ".join(f"{a['display']}[{a['family']}]" for a in bad)
            available_str = ", ".join(
                f"{s['stock_name']}({s['category']})" for s in m["same_family_available"]
            )
            print(f"  {m['oos']} [{m['oos_family']}] → got: {bad_str}")
            print(f"    should have used: {available_str}")

    if forced_mismatches:
        print(f"\n{'=' * 100}")
        print(f"⚠️  FORCED FALLBACKS ({len(forced_mismatches)} products — no same-family in stock):")
        for m in forced_mismatches:
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
