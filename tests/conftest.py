"""
Shared pytest fixtures — SQLite in-memory DB for fast isolated tests.
"""

import importlib
import json
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from db.models import Base
from db.warehouse_config import _reset_cache

# Snapshot the real db.region_family at conftest import time (before any
# stub test can replace it). Used by _restore_region_family fixture.
import db.region_family as _REAL_REGION_FAMILY


@pytest.fixture(autouse=True)
def db_session(monkeypatch):
    """Create a fresh SQLite in-memory DB for every test.

    Patches get_session in all domain modules so they use this DB
    instead of PostgreSQL.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    def _get_session():
        return Session(engine)

    # Patch DB access only in modules that are importable in the current test context.
    # Some unittest-style suites inject lightweight stubs (e.g. fake `db` package) that
    # don't expose all submodules, so we skip missing targets instead of failing setup.
    for module_name in (
        "db.models",
        "db.clients",
        "db.email_history",
        "db.stock",
        "db.conversation_state",
        "db.product_resolver",
        "db.fulfillment",
        "db.fulfillment_events",
        "db.catalog",
        "db.shipping",
        "db.stock_sync",
        "db.order_items",
        "db.stock_search",
        "db.alternatives",
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        monkeypatch.setattr(module, "get_session", _get_session, raising=False)

    yield _get_session

    engine.dispose()


@pytest.fixture(autouse=True)
def _restore_region_family():
    """Restore real db.region_family if a test stub replaced it.

    Several test files replace sys.modules["db.region_family"] with
    types.ModuleType stubs that have empty REGION_FAMILIES. This fixture
    restores the real module from the snapshot taken at conftest import.
    Safe even when sys.modules["db"] is also a stub (no reimport needed).
    """
    yield
    # After each test, ensure real module is back in sys.modules
    current = sys.modules.get("db.region_family")
    if current is not _REAL_REGION_FAMILY:
        sys.modules["db.region_family"] = _REAL_REGION_FAMILY


@pytest.fixture(autouse=True)
def active_warehouses(monkeypatch):
    """Set all 3 warehouses as active by default in tests.

    Individual tests can override STOCK_WAREHOUSES via monkeypatch + _reset_cache().
    """
    monkeypatch.setenv("STOCK_WAREHOUSES", json.dumps([
        {"name": "LA_MAKS", "spreadsheet_id": "test_la"},
        {"name": "CHICAGO_MAX", "spreadsheet_id": "test_chi"},
        {"name": "MIAMI_MAKS", "spreadsheet_id": "test_mia"},
        # Synthetic names used by test_stock.py and other test suites
        {"name": "main", "spreadsheet_id": "test_main"},
        {"name": "backup", "spreadsheet_id": "test_backup"},
        {"name": "wh_region", "spreadsheet_id": "test_wh_region"},
    ]))
    _reset_cache()
    yield
    _reset_cache()
