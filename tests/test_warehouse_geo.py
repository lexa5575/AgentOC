"""Tests for warehouse geographic routing."""

import pytest

from db.warehouse_geo import (
    DEFAULT_FALLBACK,
    _extract_state_code,
    resolve_warehouse_from_address,
)


class TestExtractStateCode:
    """State code extraction from various real address formats."""

    def test_standard_comma_format(self):
        assert _extract_state_code("Roseville, CA 95747") == "CA"

    def test_full_state_name(self):
        assert _extract_state_code("Los Angeles, California 90005") == "CA"

    def test_full_state_name_washington(self):
        assert _extract_state_code("Auburn, Washington 98092") == "WA"

    def test_abbreviated_state(self):
        assert _extract_state_code("Danvers, Mass 01923") == "MA"

    def test_no_comma_with_zip(self):
        assert _extract_state_code("Freedom PA 15042-1960") == "PA"

    def test_no_comma_no_zip4(self):
        assert _extract_state_code("West Columbia SC 29169") == "SC"

    def test_damascus_pa(self):
        assert _extract_state_code("Damascus, PA 18415") == "PA"

    def test_empty_string(self):
        assert _extract_state_code("") is None

    def test_none_input(self):
        assert _extract_state_code(None) is None

    def test_garbage(self):
        assert _extract_state_code("not an address at all") is None

    def test_state_name_no_zip(self):
        assert _extract_state_code("Portland, Oregon") == "OR"

    def test_comma_code_no_zip(self):
        """'Miami, FL' — comma + 2-letter code, no ZIP."""
        assert _extract_state_code("Miami, FL") == "FL"

    def test_comma_code_no_zip_ny(self):
        assert _extract_state_code("New York, NY") == "NY"

    def test_no_comma_no_zip_at_end(self):
        """'Houston TX' — 2-letter code at end, no comma, no ZIP."""
        assert _extract_state_code("Houston TX") == "TX"


class TestResolveWarehouseFromAddress:
    """Full warehouse priority resolution."""

    def test_california_la_first(self):
        result = resolve_warehouse_from_address("Los Angeles, CA 90001")
        assert result[0] == "LA_MAKS"
        assert len(result) == 3

    def test_florida_miami_first(self):
        result = resolve_warehouse_from_address("Miami, FL 33101")
        assert result[0] == "MIAMI_MAKS"

    def test_illinois_chicago_first(self):
        result = resolve_warehouse_from_address("Chicago, IL 60601")
        assert result[0] == "CHICAGO_MAX"

    def test_new_york_chicago_first_miami_second(self):
        result = resolve_warehouse_from_address("New York, NY 10001")
        assert result[0] == "CHICAGO_MAX"
        assert result[1] == "MIAMI_MAKS"
        assert result[2] == "LA_MAKS"

    def test_texas_miami_first(self):
        result = resolve_warehouse_from_address("Houston, TX 77001")
        assert result[0] == "MIAMI_MAKS"

    def test_empty_address_fallback(self):
        result = resolve_warehouse_from_address("")
        assert result == list(DEFAULT_FALLBACK)

    def test_garbage_address_fallback(self):
        result = resolve_warehouse_from_address("invalid data 123")
        assert result == list(DEFAULT_FALLBACK)

    def test_returns_all_three_warehouses(self):
        result = resolve_warehouse_from_address("Seattle, WA 98101")
        assert set(result) == {"LA_MAKS", "CHICAGO_MAX", "MIAMI_MAKS"}

    def test_miami_no_zip_resolves(self):
        """'Miami, FL' (no ZIP) → MIAMI_MAKS first."""
        result = resolve_warehouse_from_address("Miami, FL")
        assert result[0] == "MIAMI_MAKS"

    def test_returns_new_list_not_reference(self):
        r1 = resolve_warehouse_from_address("Seattle, WA 98101")
        r2 = resolve_warehouse_from_address("Seattle, WA 98101")
        assert r1 is not r2
