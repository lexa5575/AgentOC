"""
Stock Sync
----------

Orchestrator: reads Google Sheets, parses stock data, validates,
and saves to PostgreSQL. Follows the same pattern as gmail_poller.py.

Usage:
    from tools.stock_sync import sync_stock_from_sheets
    result = sync_stock_from_sheets()  # returns sync summary dict
"""

import logging
import threading
from os import getenv

from db.memory import get_stock_summary, sync_stock
from tools.google_sheets import SheetsClient
from tools.stock_parser import ParseResult, parse_stock, records_to_dicts
from utils.telegram import send_telegram

logger = logging.getLogger(__name__)

_sheets_client: SheetsClient | None = None
_sync_lock = threading.Lock()

# Minimum required sections for a valid parse (out of 8 total)
MIN_SECTIONS_REQUIRED = 4
# Maximum allowed drop in item count (50%)
MAX_ITEM_DROP_RATIO = 0.5


def _get_client() -> SheetsClient:
    """Lazy singleton for SheetsClient."""
    global _sheets_client
    if _sheets_client is None:
        _sheets_client = SheetsClient()
    return _sheets_client


def _validate_parse(
    result: ParseResult, warehouse: str,
) -> tuple[bool, str]:
    """Validate parse result before saving to DB.

    Returns (is_valid, reason).
    """
    # Check minimum sections found
    if len(result.sections_found) < MIN_SECTIONS_REQUIRED:
        return False, (
            f"Only {len(result.sections_found)}/{MIN_SECTIONS_REQUIRED} "
            f"required sections found: {result.sections_found}. "
            f"Missing: {result.sections_missing}"
        )

    # Check we have at least some records
    if not result.records:
        return False, "No stock records parsed at all"

    # Check item count didn't drop dramatically vs previous sync
    prev = get_stock_summary(warehouse)
    if prev["total"] > 0:
        ratio = len(result.records) / prev["total"]
        if ratio < MAX_ITEM_DROP_RATIO:
            return False, (
                f"Item count dropped from {prev['total']} to {len(result.records)} "
                f"({ratio:.0%}). Possible parse error."
            )

    return True, "OK"


def sync_stock_from_sheets() -> dict:
    """Full sync pipeline: read → parse → validate → save.

    Thread-safe: only one sync can run at a time.
    Returns summary dict.
    """
    spreadsheet_id = getenv("STOCK_SPREADSHEET_ID", "")
    warehouse = getenv("STOCK_WAREHOUSE_NAME", "LA_MAKS")

    if not spreadsheet_id:
        logger.debug("Stock sync not configured (no STOCK_SPREADSHEET_ID)")
        return {"status": "skipped", "reason": "not configured"}

    if not _sync_lock.acquire(blocking=False):
        logger.info("Stock sync already running, skipping")
        return {"status": "skipped", "reason": "already running"}

    try:
        return _sync_locked(spreadsheet_id, warehouse)
    finally:
        _sync_lock.release()


def _sync_locked(spreadsheet_id: str, warehouse: str) -> dict:
    """Internal sync logic (must be called under _sync_lock)."""
    try:
        client = _get_client()

        # Step 1: Find active sheet
        sheet_name = client.find_active_sheet(spreadsheet_id)
        logger.info("Stock sync: using sheet '%s'", sheet_name)

        # Step 2: Read all values
        matrix = client.read_sheet_values(spreadsheet_id, sheet_name)
        if not matrix:
            logger.warning("Stock sync: empty sheet '%s'", sheet_name)
            return {"status": "error", "reason": "empty sheet"}

        # Step 3: Parse
        result = parse_stock(matrix)

        # Step 4: Validate
        is_valid, reason = _validate_parse(result, warehouse)

        if not is_valid:
            logger.error("Stock sync validation FAILED: %s", reason)
            send_telegram(
                f"\U0001f6a8 <b>Stock sync validation failed!</b>\n\n"
                f"<b>Warehouse:</b> {warehouse}\n"
                f"<b>Sheet:</b> {sheet_name}\n"
                f"<b>Reason:</b> {reason}\n\n"
                f"Previous data preserved. Check the spreadsheet."
            )
            return {"status": "validation_failed", "reason": reason}

        # Step 5: Save to DB
        items = records_to_dicts(result.records)
        count = sync_stock(warehouse, items)

        # Step 6: Log & alert on warnings
        available = sum(1 for r in result.records if r.quantity > 0)
        fallback = sum(1 for r in result.records if r.is_fallback)

        summary = {
            "status": "ok",
            "warehouse": warehouse,
            "sheet": sheet_name,
            "synced": count,
            "available": available,
            "fallback": fallback,
            "sections_found": result.sections_found,
            "sections_missing": result.sections_missing,
            "warnings": len(result.warnings),
        }

        logger.info(
            "Stock sync OK: %d items (%d available, %d fallback, %d warnings)",
            count, available, fallback, len(result.warnings),
        )

        # Send consistency warnings to Telegram
        if result.warnings:
            warnings_text = "\n".join(
                f"• {w}" for w in result.warnings[:10]  # Limit to 10
            )
            send_telegram(
                f"\u26a0\ufe0f <b>Stock sync warnings</b>\n\n"
                f"<b>Warehouse:</b> {warehouse}\n"
                f"<b>Warnings ({len(result.warnings)}):</b>\n"
                f"<pre>{warnings_text}</pre>"
            )

        return summary

    except Exception as e:
        logger.error("Stock sync failed: %s", e, exc_info=True)
        send_telegram(
            f"\U0001f6a8 <b>Stock sync error!</b>\n\n"
            f"<b>Warehouse:</b> {warehouse}\n"
            f"<b>Error:</b> {e}\n\n"
            f"Check container logs."
        )
        return {"status": "error", "reason": str(e)}
