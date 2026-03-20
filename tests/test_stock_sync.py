"""Tests for tools.stock_sync orchestrator paths."""

from types import SimpleNamespace

import tools.stock_sync as stock_sync
from tools.stock_parser import ParseResult


def _cfg(name: str) -> stock_sync.WarehouseConfig:
    return stock_sync.WarehouseConfig(
        name=name,
        spreadsheet_id=f"{name}-sheet-id",
        sheet_pattern=name,
    )


def test_sync_stock_from_sheets_not_configured(monkeypatch):
    monkeypatch.setattr(stock_sync, "_load_warehouse_configs", lambda: [])
    result = stock_sync.sync_stock_from_sheets()
    assert result["status"] == "skipped"
    assert result["reason"] == "not configured"
    assert result["warehouses"] == []


def test_sync_stock_from_sheets_already_running(monkeypatch):
    monkeypatch.setattr(stock_sync, "_load_warehouse_configs", lambda: [_cfg("LA_MAKS")])

    locked = stock_sync._sync_lock.acquire(blocking=False)
    assert locked
    try:
        result = stock_sync.sync_stock_from_sheets()
    finally:
        stock_sync._sync_lock.release()

    assert result["status"] == "skipped"
    assert result["reason"] == "already running"
    assert result["warehouses"] == []


def test_sync_stock_from_sheets_partial_status(monkeypatch):
    monkeypatch.setattr(stock_sync, "_load_warehouse_configs", lambda: [_cfg("A"), _cfg("B")])
    monkeypatch.setattr(
        stock_sync,
        "_sync_single_warehouse",
        lambda cfg: {"status": "ok", "warehouse": cfg.name}
        if cfg.name == "A"
        else {"status": "error", "warehouse": cfg.name, "reason": "boom"},
    )
    result = stock_sync.sync_stock_from_sheets()
    assert result["status"] == "partial"
    assert len(result["warehouses"]) == 2


def test_validate_parse_no_records():
    result = ParseResult(records=[], sections_found=[], sections_missing=[], warnings=[])
    ok, reason = stock_sync._validate_parse(result, "LA_MAKS", ["TEREA_EUROPE"])
    assert ok is False
    assert "No stock records parsed" in reason


def test_validate_parse_missing_sections(monkeypatch):
    monkeypatch.setattr(
        stock_sync,
        "get_stock_summary",
        lambda warehouse=None, bypass_active_filter=False: {"total": 0, "available": 0, "fallback": 0, "synced_at": None},
    )
    result = ParseResult(records=[object()], sections_found=["KZ_TEREA"], sections_missing=[], warnings=[])
    ok, reason = stock_sync._validate_parse(result, "LA_MAKS", ["KZ_TEREA", "ARMENIA"])
    assert ok is False
    assert "Missing sections" in reason


def test_validate_parse_drop_ratio(monkeypatch):
    monkeypatch.setattr(
        stock_sync,
        "get_stock_summary",
        lambda warehouse=None, bypass_active_filter=False: {"total": 100, "available": 50, "fallback": 0, "synced_at": None},
    )
    result = ParseResult(records=[object()] * 10, sections_found=["KZ_TEREA"], sections_missing=[], warnings=[])
    ok, reason = stock_sync._validate_parse(result, "LA_MAKS", ["KZ_TEREA"])
    assert ok is False
    assert "Item count dropped" in reason


def test_validate_parse_ok(monkeypatch):
    monkeypatch.setattr(
        stock_sync,
        "get_stock_summary",
        lambda warehouse=None, bypass_active_filter=False: {"total": 0, "available": 0, "fallback": 0, "synced_at": None},
    )
    result = ParseResult(records=[object()] * 3, sections_found=["KZ_TEREA"], sections_missing=[], warnings=[])
    ok, reason = stock_sync._validate_parse(result, "LA_MAKS", ["KZ_TEREA"])
    assert ok is True
    assert reason == "OK"


def test_sync_single_warehouse_validation_failed(monkeypatch):
    cfg = _cfg("LA_MAKS")

    class _DummyClient:
        def find_active_sheet(self, spreadsheet_id, warehouse_pattern):
            return "LA MAKS FEB"

        def read_sheet_values(self, spreadsheet_id, sheet_name):
            return [["cell"]]

    fake_config = SimpleNamespace(
        sheet_name="LA MAKS FEB",
        sections=[SimpleNamespace(name="KZ_TEREA")],
    )
    fake_parse = ParseResult(
        records=[object()],
        sections_found=["KZ_TEREA"],
        sections_missing=[],
        warnings=[],
    )
    validate_results = [(False, "first failure"), (False, "second failure")]
    tg_messages = []

    monkeypatch.setattr(stock_sync, "_get_client", lambda: _DummyClient())
    monkeypatch.setattr(stock_sync, "load_sheet_config", lambda warehouse: fake_config)
    monkeypatch.setattr(stock_sync, "has_structure_changed", lambda matrix, config: None)
    monkeypatch.setattr(stock_sync, "_run_llm_analysis", lambda wh_cfg, sheet_name, matrix: fake_config)
    monkeypatch.setattr(stock_sync, "parse_stock_with_config", lambda matrix, config: fake_parse)
    monkeypatch.setattr(stock_sync, "_validate_parse", lambda result, warehouse, expected: validate_results.pop(0))
    monkeypatch.setattr(stock_sync, "send_telegram", lambda msg: tg_messages.append(msg))

    def _unexpected_sync_stock(*args, **kwargs):
        raise AssertionError("sync_stock should not be called when validation fails")

    monkeypatch.setattr(stock_sync, "sync_stock", _unexpected_sync_stock)

    result = stock_sync._sync_single_warehouse(cfg)
    assert result["status"] == "validation_failed"
    assert result["warehouse"] == "LA_MAKS"
    assert result["reason"] == "second failure"
    assert len(tg_messages) == 1

