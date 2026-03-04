"""
Stock Sync
----------

Multi-warehouse orchestrator: reads Google Sheets, parses stock data,
validates, and saves to PostgreSQL.

Supports multiple warehouses (LA_MAKS, CHICAGO_MAX, MIAMI_MAKS), each with
its own spreadsheet. Sync runs sequentially for each warehouse every 5 min.

Usage:
    from tools.stock_sync import sync_stock_from_sheets
    results = sync_stock_from_sheets()  # returns {"status": ..., "warehouses": [...]}
"""

import json
import logging
import threading
from dataclasses import dataclass
from os import getenv

from db.memory import get_stock_summary, sync_stock
from db.sheet_config import load_sheet_config, save_sheet_config, is_config_stale
from tools.google_sheets import SheetsClient
from tools.stock_parser import ParseResult, parse_stock_with_config, records_to_dicts
from utils.telegram import send_telegram

logger = logging.getLogger(__name__)

_sheets_client: SheetsClient | None = None
_sync_lock = threading.Lock()

# Maximum allowed drop in item count (50%)
MAX_ITEM_DROP_RATIO = 0.5


# ---------------------------------------------------------------------------
# Warehouse configuration
# ---------------------------------------------------------------------------

@dataclass
class WarehouseConfig:
    """Configuration for a single warehouse."""

    name: str
    spreadsheet_id: str
    sheet_pattern: str


def _load_warehouse_configs() -> list[WarehouseConfig]:
    """Load warehouse configurations from environment.

    Supports two formats:
    1. JSON array in STOCK_WAREHOUSES env var (multi-warehouse)
    2. Legacy single-warehouse via STOCK_SPREADSHEET_ID + STOCK_WAREHOUSE_NAME
    """
    warehouses_json = getenv("STOCK_WAREHOUSES", "").strip()

    if warehouses_json:
        try:
            configs = json.loads(warehouses_json)
            result = [
                WarehouseConfig(
                    name=cfg["name"],
                    spreadsheet_id=cfg["spreadsheet_id"],
                    sheet_pattern=cfg.get("sheet_pattern", cfg["name"].replace("_", " ")),
                )
                for cfg in configs
            ]
            logger.info("Loaded %d warehouse configs from STOCK_WAREHOUSES", len(result))
            return result
        except (json.JSONDecodeError, KeyError) as e:
            logger.error("Invalid STOCK_WAREHOUSES JSON: %s", e)
            send_telegram(
                f"\U0001f6a8 <b>Invalid STOCK_WAREHOUSES config!</b>\n\n"
                f"<b>Error:</b> {e}\n\n"
                f"Check .env STOCK_WAREHOUSES JSON syntax."
            )
            return []

    # Legacy single-warehouse fallback
    spreadsheet_id = getenv("STOCK_SPREADSHEET_ID", "")
    if spreadsheet_id:
        warehouse_name = getenv("STOCK_WAREHOUSE_NAME", "LA_MAKS")
        return [WarehouseConfig(
            name=warehouse_name,
            spreadsheet_id=spreadsheet_id,
            sheet_pattern=warehouse_name.replace("_", " "),
        )]

    return []


def _get_client() -> SheetsClient:
    """Lazy singleton for SheetsClient."""
    global _sheets_client
    if _sheets_client is None:
        _sheets_client = SheetsClient()
    return _sheets_client


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_parse(
    result: ParseResult,
    warehouse: str,
    expected_sections: list[str],
) -> tuple[bool, str]:
    """Validate parse result before saving to DB.

    Dynamic validation: checks sections from LLM config, not hardcoded list.
    """
    if not result.records:
        return False, "No stock records parsed at all"

    # Check that expected sections were found
    found_set = set(result.sections_found)
    missing = [s for s in expected_sections if s not in found_set]
    if missing:
        return False, (
            f"Missing sections: {missing}. "
            f"Found: {result.sections_found}"
        )

    # Item count drop check vs previous sync
    prev = get_stock_summary(warehouse)
    if prev["total"] > 0:
        ratio = len(result.records) / prev["total"]
        if ratio < MAX_ITEM_DROP_RATIO:
            return False, (
                f"Item count dropped from {prev['total']} to {len(result.records)} "
                f"({ratio:.0%}). Possible parse error."
            )

    return True, "OK"


# ---------------------------------------------------------------------------
# Sync pipeline
# ---------------------------------------------------------------------------

def sync_stock_from_sheets() -> dict:
    """Full sync pipeline for ALL configured warehouses.

    Thread-safe: only one sync can run at a time.
    Returns dict: {"status": "ok"|"skipped"|"error", "warehouses": [...]}.
    """
    configs = _load_warehouse_configs()
    if not configs:
        logger.debug("Stock sync not configured (no warehouses)")
        return {"status": "skipped", "reason": "not configured", "warehouses": []}

    if not _sync_lock.acquire(blocking=False):
        logger.info("Stock sync already running, skipping")
        return {"status": "skipped", "reason": "already running", "warehouses": []}

    try:
        warehouse_results = []
        for wh_cfg in configs:
            result = _sync_single_warehouse(wh_cfg)
            warehouse_results.append(result)

        # Overall status
        statuses = [r.get("status") for r in warehouse_results]
        if all(s == "ok" for s in statuses):
            overall = "ok"
        elif any(s == "ok" for s in statuses):
            overall = "partial"
        else:
            overall = "error"

        return {
            "status": overall,
            "warehouses": warehouse_results,
        }
    finally:
        _sync_lock.release()


def _sync_single_warehouse(wh_cfg: WarehouseConfig) -> dict:
    """Sync a single warehouse.

    Flow:
    1. Find active sheet
    2. Read matrix
    3. Load LLM config from DB (or generate if stale/missing)
    4. Parse using config
    5. Validate — on failure: re-analyze + retry once
    6. Save to DB
    """
    try:
        client = _get_client()

        # Step 1: Find active sheet
        sheet_name = client.find_active_sheet(
            wh_cfg.spreadsheet_id,
            warehouse_pattern=wh_cfg.sheet_pattern,
        )
        logger.info("Stock sync [%s]: using sheet '%s'", wh_cfg.name, sheet_name)

        # Step 2: Read matrix
        matrix = client.read_sheet_values(wh_cfg.spreadsheet_id, sheet_name)
        if not matrix:
            logger.warning("Stock sync [%s]: empty sheet '%s'", wh_cfg.name, sheet_name)
            return {"status": "error", "warehouse": wh_cfg.name, "reason": "empty sheet"}

        # Step 3: Load or generate config
        config = load_sheet_config(wh_cfg.name)

        need_analysis = (
            config is None
            or is_config_stale(config)
            or config.sheet_name != sheet_name
        )

        if need_analysis:
            reason = "missing" if config is None else "stale" if is_config_stale(config) else "sheet changed"
            logger.info("Generating config for %s (reason: %s)", wh_cfg.name, reason)
            config = _run_llm_analysis(wh_cfg, sheet_name, matrix)
            if not config:
                return {"status": "error", "warehouse": wh_cfg.name, "reason": "LLM analysis failed"}

        # Step 4: Parse
        result = parse_stock_with_config(matrix, config)

        # Step 5: Validate
        expected = [s.name for s in config.sections]
        is_valid, reason = _validate_parse(result, wh_cfg.name, expected)

        if not is_valid:
            # Re-analyze and retry once
            logger.warning(
                "Validation failed for %s: %s. Re-analyzing...",
                wh_cfg.name, reason,
            )
            config = _run_llm_analysis(wh_cfg, sheet_name, matrix)
            if config:
                result = parse_stock_with_config(matrix, config)
                expected = [s.name for s in config.sections]
                is_valid, reason = _validate_parse(result, wh_cfg.name, expected)

            if not is_valid:
                send_telegram(
                    f"\U0001f6a8 <b>Stock sync validation failed!</b>\n\n"
                    f"<b>Warehouse:</b> {wh_cfg.name}\n"
                    f"<b>Sheet:</b> {sheet_name}\n"
                    f"<b>Reason:</b> {reason}\n\n"
                    f"Previous data preserved."
                )
                return {
                    "status": "validation_failed",
                    "warehouse": wh_cfg.name,
                    "reason": reason,
                }

        # Step 6: Save to DB
        items = records_to_dicts(result.records)
        count = sync_stock(wh_cfg.name, items)

        available = sum(1 for r in result.records if r.quantity > 0)

        summary = {
            "status": "ok",
            "warehouse": wh_cfg.name,
            "sheet": sheet_name,
            "synced": count,
            "available": available,
            "sections_found": result.sections_found,
            "sections_missing": result.sections_missing,
            "warnings": len(result.warnings),
        }

        logger.info(
            "Stock sync [%s] OK: %d items (%d available, %d warnings)",
            wh_cfg.name, count, available, len(result.warnings),
        )

        return summary

    except Exception as e:
        logger.error("Stock sync failed for %s: %s", wh_cfg.name, e, exc_info=True)
        send_telegram(
            f"\U0001f6a8 <b>Stock sync error!</b>\n\n"
            f"<b>Warehouse:</b> {wh_cfg.name}\n"
            f"<b>Error:</b> {e}\n\n"
            f"Check container logs."
        )
        return {"status": "error", "warehouse": wh_cfg.name, "reason": str(e)}


def _run_llm_analysis(wh_cfg: WarehouseConfig, sheet_name: str, matrix: list[list]):
    """Run LLM structure analysis and save config to DB.

    Returns SheetStructureConfig or None.
    """
    from agents.stock_analyzer import analyze_structure

    config = analyze_structure(wh_cfg.name, wh_cfg.spreadsheet_id, sheet_name, matrix)
    if config:
        save_sheet_config(wh_cfg.name, config)
    return config
