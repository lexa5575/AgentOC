"""
Warehouse Configuration
-----------------------

Single source of truth for active warehouses.
Reads STOCK_WAREHOUSES env var (JSON array) with legacy fallback.

Fail-closed: invalid JSON or missing config → no active warehouses.
"""

import json
import logging
from os import getenv

logger = logging.getLogger(__name__)

_cache: dict | None = None


def _load() -> dict:
    """Parse warehouse config once, cache at module level.

    Priority:
    1. STOCK_WAREHOUSES set + valid JSON → use exclusively
    2. STOCK_WAREHOUSES set + invalid JSON → [] (fail-closed) + log error
    3. STOCK_WAREHOUSES not set + both legacy envs → single warehouse
    4. Nothing configured → []
    """
    global _cache
    if _cache is not None:
        return _cache

    warehouses_json = getenv("STOCK_WAREHOUSES", "").strip()

    if warehouses_json:
        try:
            configs = json.loads(warehouses_json)
            result = {
                "names": [cfg["name"] for cfg in configs],
                "configs": [
                    {
                        "name": cfg["name"],
                        "spreadsheet_id": cfg["spreadsheet_id"],
                        "sheet_pattern": cfg.get(
                            "sheet_pattern", cfg["name"].replace("_", " ")
                        ),
                    }
                    for cfg in configs
                ],
            }
            _cache = result
            return result
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.error("Invalid STOCK_WAREHOUSES JSON: %s — fail-closed", e)
            _cache = {"names": [], "configs": [], "parse_error": str(e)}
            return _cache

    # Legacy single-warehouse fallback (only when STOCK_WAREHOUSES not set)
    spreadsheet_id = getenv("STOCK_SPREADSHEET_ID", "").strip()
    warehouse_name = getenv("STOCK_WAREHOUSE_NAME", "").strip()
    if spreadsheet_id and warehouse_name:
        _cache = {
            "names": [warehouse_name],
            "configs": [
                {
                    "name": warehouse_name,
                    "spreadsheet_id": spreadsheet_id,
                    "sheet_pattern": warehouse_name.replace("_", " "),
                }
            ],
        }
        return _cache

    _cache = {"names": [], "configs": []}
    return _cache


def get_active_warehouses() -> list[str]:
    """List of active warehouse names. Empty = nothing active (fail-closed)."""
    return list(_load()["names"])


def get_warehouse_configs() -> list[dict]:
    """Full configs: [{"name":..., "spreadsheet_id":..., "sheet_pattern":...}]."""
    return list(_load()["configs"])


def get_warehouse_spreadsheet_id(warehouse: str) -> str | None:
    """Lookup spreadsheet_id by warehouse name."""
    for cfg in _load()["configs"]:
        if cfg["name"] == warehouse:
            return cfg["spreadsheet_id"]
    return None


def is_warehouse_active(warehouse: str) -> bool:
    """Check if a specific warehouse is currently active."""
    return warehouse in _load()["names"]


def _reset_cache():
    """For tests: clear cached config after monkeypatch.setenv."""
    global _cache
    _cache = None
