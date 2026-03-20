"""Tests for db.warehouse_config — centralized warehouse configuration."""

import json
import pytest

from db.warehouse_config import (
    _reset_cache,
    get_active_warehouses,
    get_warehouse_configs,
    get_warehouse_spreadsheet_id,
    is_warehouse_active,
)


@pytest.fixture(autouse=True)
def reset_cache():
    """Clear config cache before/after each test."""
    _reset_cache()
    yield
    _reset_cache()


class TestGetActiveWarehouses:
    def test_from_stock_warehouses(self, monkeypatch):
        monkeypatch.setenv("STOCK_WAREHOUSES", json.dumps([
            {"name": "CHICAGO_MAX", "spreadsheet_id": "id1"},
            {"name": "MIAMI_MAKS", "spreadsheet_id": "id2"},
        ]))
        assert get_active_warehouses() == ["CHICAGO_MAX", "MIAMI_MAKS"]

    def test_invalid_json_fail_closed(self, monkeypatch):
        monkeypatch.setenv("STOCK_WAREHOUSES", "not valid json{}")
        result = get_active_warehouses()
        assert result == []

    def test_legacy_fallback(self, monkeypatch):
        monkeypatch.delenv("STOCK_WAREHOUSES", raising=False)
        monkeypatch.setenv("STOCK_WAREHOUSE_NAME", "LA_MAKS")
        monkeypatch.setenv("STOCK_SPREADSHEET_ID", "legacy_id")
        assert get_active_warehouses() == ["LA_MAKS"]

    def test_legacy_needs_both_envs(self, monkeypatch):
        monkeypatch.delenv("STOCK_WAREHOUSES", raising=False)
        monkeypatch.setenv("STOCK_WAREHOUSE_NAME", "LA_MAKS")
        monkeypatch.delenv("STOCK_SPREADSHEET_ID", raising=False)
        assert get_active_warehouses() == []

    def test_nothing_configured(self, monkeypatch):
        monkeypatch.delenv("STOCK_WAREHOUSES", raising=False)
        monkeypatch.delenv("STOCK_WAREHOUSE_NAME", raising=False)
        monkeypatch.delenv("STOCK_SPREADSHEET_ID", raising=False)
        assert get_active_warehouses() == []

    def test_stock_warehouses_overrides_legacy(self, monkeypatch):
        """When STOCK_WAREHOUSES is set, legacy vars are ignored."""
        monkeypatch.setenv("STOCK_WAREHOUSES", json.dumps([
            {"name": "CHICAGO_MAX", "spreadsheet_id": "id1"},
        ]))
        monkeypatch.setenv("STOCK_WAREHOUSE_NAME", "LA_MAKS")
        monkeypatch.setenv("STOCK_SPREADSHEET_ID", "legacy_id")
        assert get_active_warehouses() == ["CHICAGO_MAX"]


class TestGetWarehouseConfigs:
    def test_returns_full_configs(self, monkeypatch):
        monkeypatch.setenv("STOCK_WAREHOUSES", json.dumps([
            {"name": "LA_MAKS", "spreadsheet_id": "id_la", "sheet_pattern": "LA MAKS"},
        ]))
        configs = get_warehouse_configs()
        assert len(configs) == 1
        assert configs[0]["name"] == "LA_MAKS"
        assert configs[0]["spreadsheet_id"] == "id_la"
        assert configs[0]["sheet_pattern"] == "LA MAKS"

    def test_default_sheet_pattern(self, monkeypatch):
        monkeypatch.setenv("STOCK_WAREHOUSES", json.dumps([
            {"name": "LA_MAKS", "spreadsheet_id": "id_la"},
        ]))
        configs = get_warehouse_configs()
        assert configs[0]["sheet_pattern"] == "LA MAKS"


class TestGetWarehouseSpreadsheetId:
    def test_found(self, monkeypatch):
        monkeypatch.setenv("STOCK_WAREHOUSES", json.dumps([
            {"name": "LA_MAKS", "spreadsheet_id": "id_la"},
            {"name": "CHICAGO_MAX", "spreadsheet_id": "id_chi"},
        ]))
        assert get_warehouse_spreadsheet_id("LA_MAKS") == "id_la"
        assert get_warehouse_spreadsheet_id("CHICAGO_MAX") == "id_chi"

    def test_not_found(self, monkeypatch):
        monkeypatch.setenv("STOCK_WAREHOUSES", json.dumps([
            {"name": "LA_MAKS", "spreadsheet_id": "id_la"},
        ]))
        assert get_warehouse_spreadsheet_id("UNKNOWN") is None


class TestIsWarehouseActive:
    def test_active(self, monkeypatch):
        monkeypatch.setenv("STOCK_WAREHOUSES", json.dumps([
            {"name": "CHICAGO_MAX", "spreadsheet_id": "id1"},
        ]))
        assert is_warehouse_active("CHICAGO_MAX") is True

    def test_inactive(self, monkeypatch):
        monkeypatch.setenv("STOCK_WAREHOUSES", json.dumps([
            {"name": "CHICAGO_MAX", "spreadsheet_id": "id1"},
        ]))
        assert is_warehouse_active("LA_MAKS") is False


class TestCacheReset:
    def test_cache_persists(self, monkeypatch):
        monkeypatch.setenv("STOCK_WAREHOUSES", json.dumps([
            {"name": "A", "spreadsheet_id": "id_a"},
        ]))
        assert get_active_warehouses() == ["A"]
        # Change env without reset — cache persists
        monkeypatch.setenv("STOCK_WAREHOUSES", json.dumps([
            {"name": "B", "spreadsheet_id": "id_b"},
        ]))
        assert get_active_warehouses() == ["A"]

    def test_reset_clears_cache(self, monkeypatch):
        monkeypatch.setenv("STOCK_WAREHOUSES", json.dumps([
            {"name": "A", "spreadsheet_id": "id_a"},
        ]))
        assert get_active_warehouses() == ["A"]
        monkeypatch.setenv("STOCK_WAREHOUSES", json.dumps([
            {"name": "B", "spreadsheet_id": "id_b"},
        ]))
        _reset_cache()
        assert get_active_warehouses() == ["B"]
