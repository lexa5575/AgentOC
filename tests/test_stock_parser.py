"""
Test Stock Parser
-----------------

Unit tests for the config-driven stock parser and Python reconnaissance.
Mock data simulates the real Google Sheets layout from screenshots.

Run:
    python -m tests.test_stock_parser
"""

import pytest

pytestmark = pytest.mark.domain_stock


from datetime import datetime

from tools.stock_parser import parse_stock_with_config, StockRecord
from tools.structure_analyzer import (
    SectionConfig,
    SheetStructureConfig,
    detect_sections,
    detect_prefix_sections,
)


def _build_mock_matrix() -> list[list]:
    """Build a mock matrix simulating the real spreadsheet layout.

    Layout matches actual spreadsheet:
    - Left zone (cols 0-8): KZ TEREA, TEREA JAPAN, TEREA EUROPE
    - Middle zone (cols 9-16): ONE/STND/PRIME (by prefix), УНИКАЛЬНАЯ ТЕРЕА
    - Right zone (cols 17-26): ARMENIA
    """
    rows = []

    # --- Rows 0-2: empty ---
    rows.append([""] * 27)
    rows.append([""] * 27)
    rows.append([""] * 27)

    # --- Row 3: KZ TEREA marker + LA MAKS header (middle) + ARMENIA marker ---
    r = [""] * 27
    r[1] = "KZ TEREA"
    r[13] = "LA MAKS"
    r[18] = "ARMENIA"
    rows.append(r)

    # --- Row 4: Sub-headers (all zones on same row) ---
    r = [""] * 27
    r[0] = "ARRIVED"
    r[3] = "Farik"; r[4] = "Maks"; r[5] = "Никита"
    r[12] = "Farik"; r[13] = "Maks"; r[14] = "Nikita"; r[17] = "ARRIVED"
    r[20] = "Farik"; r[21] = "Maks"; r[22] = "Никита"
    rows.append(r)

    # --- Row 5: KZ TEREA Amber | ONE Red | ARMENIA Amber ---
    r = [""] * 27
    r[1] = "Amber"; r[2] = 59; r[3] = 17; r[4] = 32; r[5] = 2; r[6] = 8
    r[10] = "ONE Red"; r[15] = 0
    r[18] = "Amber"; r[19] = 59; r[20] = 17; r[21] = 32; r[22] = 2; r[23] = 8
    rows.append(r)

    # --- Row 6: KZ TEREA Yellow | ONE Black | ARMENIA Yellow ---
    r = [""] * 27
    r[1] = "Yellow"; r[2] = 25; r[3] = 6; r[4] = 5; r[5] = 1; r[6] = 13
    r[10] = "ONE Black"; r[11] = 2; r[14] = 2; r[15] = 0
    r[18] = "Yellow"; r[19] = 14; r[23] = 4
    rows.append(r)

    # --- Row 7: KZ TEREA Silver | ONE Green | ARMENIA Silver ---
    r = [""] * 27
    r[1] = "Silver"; r[2] = 61; r[3] = 4; r[4] = 19; r[5] = 7; r[6] = 31
    r[10] = "ONE Green"; r[15] = 0
    r[18] = "Silver"; r[19] = 17; r[20] = 4; r[21] = 1; r[23] = 31
    rows.append(r)

    # --- Row 8: KZ TEREA Bronze ---
    r = [""] * 27
    r[1] = "Bronze"; r[2] = 0; r[6] = 0
    rows.append(r)

    # --- Row 9: empty (end of KZ TEREA and ONE) ---
    rows.append([""] * 27)

    # --- Row 10: middle zone STND header ---
    r = [""] * 27
    r[12] = "Farik"; r[13] = "Maks"; r[14] = "Nikita"
    rows.append(r)

    # --- Row 11-12: STND products ---
    r = [""] * 27
    r[10] = "STND Red"; r[11] = 0; r[15] = 0
    rows.append(r)

    r = [""] * 27
    r[10] = "STND Black"; r[11] = 0; r[15] = 0
    rows.append(r)

    # --- Row 13: empty ---
    rows.append([""] * 27)

    # --- Row 14: middle zone PRIME header ---
    r = [""] * 27
    r[12] = "Farik"; r[13] = "Maks"; r[14] = "Nikita"
    rows.append(r)

    # --- Row 15-16: PRIME products ---
    r = [""] * 27
    r[10] = "PRIME Black"; r[15] = 0
    rows.append(r)

    r = [""] * 27
    r[10] = "PRIME Gold"; r[11] = 1; r[14] = 1; r[15] = 0
    rows.append(r)

    # --- Row 17: empty ---
    rows.append([""] * 27)

    # --- Row 18: TEREA JAPAN marker | УНИКАЛЬНАЯ ТЕРЕА marker ---
    r = [""] * 27
    r[1] = "TEREA JAPAN"
    r[10] = "УНИКАЛЬНАЯ ТЕРЕА"; r[14] = "LA MAKS"
    rows.append(r)

    # --- Row 19: headers ---
    r = [""] * 27
    r[3] = "Farik"; r[4] = "Maks"; r[5] = "Никита"
    r[13] = "Farik"; r[14] = "Maks"; r[15] = "Nikita"
    rows.append(r)

    # --- Row 20: TEREA JAPAN T Regular | Warm Regular ---
    r = [""] * 27
    r[1] = "T Regular"; r[2] = 15; r[4] = 1; r[6] = 14
    r[9] = 8; r[10] = "Warm Regular"; r[12] = 10; r[16] = 10
    rows.append(r)

    # --- Row 21: T Mint | Black Ruby Menthol ---
    r = [""] * 27
    r[1] = "T Mint"; r[2] = 24; r[3] = 3; r[5] = 2; r[6] = 19
    r[9] = 18; r[10] = "Black Ruby Menthol"; r[12] = 20; r[15] = 1; r[16] = 19
    rows.append(r)

    # --- Row 22: T Black ---
    r = [""] * 27
    r[1] = "T Black"; r[2] = 30; r[3] = 3; r[6] = 27
    rows.append(r)

    # --- Row 23: empty ---
    rows.append([""] * 27)

    # --- Row 24: TEREA EUROPE marker ---
    r = [""] * 27
    r[1] = "TEREA EUROPE"
    rows.append(r)

    # --- Row 25: headers ---
    r = [""] * 27
    r[3] = "Farik"; r[4] = "Maks"; r[5] = "Никита"
    rows.append(r)

    # --- Row 26: TEREA EUROPE Amber ---
    r = [""] * 27
    r[1] = "Amber"; r[2] = 26; r[4] = 10; r[6] = 16
    rows.append(r)

    # --- Row 27: TEREA EUROPE Yellow ---
    r = [""] * 27
    r[1] = "Yellow"; r[2] = 12; r[4] = 3; r[6] = 9
    rows.append(r)

    return rows


def _build_mock_config() -> SheetStructureConfig:
    """Build a SheetStructureConfig matching the mock matrix layout."""
    return SheetStructureConfig(
        warehouse="LA_MAKS",
        spreadsheet_id="test-id",
        sheet_name="LA MAKS FEB",
        analyzed_at=datetime.utcnow(),
        sections=[
            SectionConfig(
                name="KZ_TEREA",
                marker_text="KZ TEREA",
                type="marker",
                col_start=0,
                col_end=9,
                name_col=1,
                remainder_col=6,
                maks_col=4,
            ),
            SectionConfig(
                name="TEREA_JAPAN",
                marker_text="TEREA JAPAN",
                type="marker",
                col_start=0,
                col_end=9,
                name_col=1,
                remainder_col=6,
                maks_col=4,
            ),
            SectionConfig(
                name="TEREA_EUROPE",
                marker_text="TEREA EUROPE",
                type="marker",
                col_start=0,
                col_end=9,
                name_col=1,
                remainder_col=6,
                maks_col=4,
            ),
            SectionConfig(
                name="ARMENIA",
                marker_text="ARMENIA",
                type="marker",
                col_start=17,
                col_end=27,
                name_col=18,
                remainder_col=23,
                maks_col=21,
            ),
            SectionConfig(
                name="УНИКАЛЬНАЯ_ТЕРЕА",
                marker_text="УНИКАЛЬНАЯ ТЕРЕА",
                type="marker",
                col_start=9,
                col_end=17,
                name_col=10,
                remainder_col=16,
                maks_col=14,
            ),
            SectionConfig(
                name="ONE",
                marker_text="ONE",
                type="prefix",
                prefix="ONE",
                col_start=9,
                col_end=17,
                name_col=10,
                remainder_col=15,
                maks_col=13,
            ),
            SectionConfig(
                name="STND",
                marker_text="STND",
                type="prefix",
                prefix="STND",
                col_start=9,
                col_end=17,
                name_col=10,
                remainder_col=15,
                maks_col=13,
            ),
            SectionConfig(
                name="PRIME",
                marker_text="PRIME",
                type="prefix",
                prefix="PRIME",
                col_start=9,
                col_end=17,
                name_col=10,
                remainder_col=15,
                maks_col=13,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Tests: config-driven parser
# ---------------------------------------------------------------------------


def test_parse_with_config():
    """Test that parse_stock_with_config correctly extracts stock data."""
    matrix = _build_mock_matrix()
    config = _build_mock_config()
    result = parse_stock_with_config(matrix, config)

    print(f"\nSections found: {result.sections_found}")
    print(f"Sections missing: {result.sections_missing}")
    print(f"Total records: {len(result.records)}")
    print(f"Warnings: {result.warnings}")
    print()

    stock = {(r.category, r.product_name): r for r in result.records}

    for (cat, name), rec in sorted(stock.items()):
        print(f"  {cat:25s} | {name:25s} | qty={rec.quantity:4d} | maks={rec.maks_sales:4d}")

    print("\n--- Running assertions ---")

    # KZ TEREA
    assert stock[("KZ_TEREA", "Amber")].quantity == 8
    assert stock[("KZ_TEREA", "Yellow")].quantity == 13
    assert stock[("KZ_TEREA", "Silver")].quantity == 31
    assert stock[("KZ_TEREA", "Bronze")].quantity == 0

    # TEREA JAPAN
    assert stock[("TEREA_JAPAN", "T Regular")].quantity == 14
    assert stock[("TEREA_JAPAN", "T Mint")].quantity == 19
    assert stock[("TEREA_JAPAN", "T Black")].quantity == 27

    # TEREA EUROPE
    assert stock[("TEREA_EUROPE", "Amber")].quantity == 16
    assert stock[("TEREA_EUROPE", "Yellow")].quantity == 9

    # ARMENIA
    assert stock[("ARMENIA", "Amber")].quantity == 8
    assert stock[("ARMENIA", "Yellow")].quantity == 4
    assert stock[("ARMENIA", "Silver")].quantity == 31

    # УНИКАЛЬНАЯ ТЕРЕА
    assert stock[("УНИКАЛЬНАЯ_ТЕРЕА", "Warm Regular")].quantity == 10
    assert stock[("УНИКАЛЬНАЯ_ТЕРЕА", "Black Ruby Menthol")].quantity == 19

    # ONE (prefix sections)
    assert stock[("ONE", "ONE Red")].quantity == 0
    assert stock[("ONE", "ONE Black")].quantity == 0
    assert stock[("ONE", "ONE Green")].quantity == 0

    # STND
    assert stock[("STND", "STND Red")].quantity == 0
    assert stock[("STND", "STND Black")].quantity == 0

    # PRIME
    assert stock[("PRIME", "PRIME Black")].quantity == 0
    assert stock[("PRIME", "PRIME Gold")].quantity == 0

    # All 8 sections found
    assert len(result.sections_found) == 8, f"Expected 8 sections, got {result.sections_found}"
    assert not result.sections_missing, f"Missing sections: {result.sections_missing}"

    print("All quantity assertions PASSED!")


def test_maks_sales():
    """Test maks_sales extraction from configured maks_col."""
    matrix = _build_mock_matrix()
    config = _build_mock_config()
    result = parse_stock_with_config(matrix, config)

    stock = {(r.category, r.product_name): r for r in result.records}

    print("\n--- Maks sales test ---")

    # KZ TEREA: maks_col=4
    assert stock[("KZ_TEREA", "Amber")].maks_sales == 32
    assert stock[("KZ_TEREA", "Yellow")].maks_sales == 5
    assert stock[("KZ_TEREA", "Silver")].maks_sales == 19
    assert stock[("KZ_TEREA", "Bronze")].maks_sales == 0

    # ARMENIA: maks_col=21
    assert stock[("ARMENIA", "Amber")].maks_sales == 32
    assert stock[("ARMENIA", "Yellow")].maks_sales == 0  # col 21 not set for this row
    assert stock[("ARMENIA", "Silver")].maks_sales == 1

    # TEREA JAPAN: maks_col=4
    assert stock[("TEREA_JAPAN", "T Regular")].maks_sales == 1
    assert stock[("TEREA_JAPAN", "T Mint")].maks_sales == 0  # col 4 empty
    assert stock[("TEREA_JAPAN", "T Black")].maks_sales == 0

    # TEREA EUROPE: maks_col=4
    assert stock[("TEREA_EUROPE", "Amber")].maks_sales == 10
    assert stock[("TEREA_EUROPE", "Yellow")].maks_sales == 3

    print("Maks sales assertions PASSED!")


def test_last_occurrence():
    """Test that parser takes the LAST (bottom-most) marker occurrence."""
    matrix = _build_mock_matrix()
    config = _build_mock_config()

    # Add an OLD (archived) KZ TEREA block at the very beginning
    old_block = [[""] * 27 for _ in range(6)]
    old_block[0][1] = "KZ TEREA"
    old_block[1][3] = "Farik"; old_block[1][4] = "Maks"
    old_block[2][1] = "Amber"; old_block[2][2] = 100; old_block[2][6] = 99
    old_block[3][1] = "Yellow"; old_block[3][2] = 100; old_block[3][6] = 88

    full_matrix = old_block + matrix

    result = parse_stock_with_config(full_matrix, config)
    stock = {(r.category, r.product_name): r for r in result.records}

    print("\n--- Last occurrence test ---")
    print(f"KZ TEREA Amber: {stock[('KZ_TEREA', 'Amber')].quantity}")
    print(f"KZ TEREA Yellow: {stock[('KZ_TEREA', 'Yellow')].quantity}")

    # Should use the bottom-most (current) data, NOT the old block
    assert stock[("KZ_TEREA", "Amber")].quantity == 8, "Should use last occurrence"
    assert stock[("KZ_TEREA", "Yellow")].quantity == 13

    print("Last occurrence test PASSED!")


def test_negative_clamp():
    """Test that negative quantities are clamped to 0."""
    rows = [[""] * 10 for _ in range(3)]

    r = [""] * 10
    r[1] = "KZ TEREA"
    rows.append(r)

    r = [""] * 10
    r[3] = "Farik"; r[4] = "Maks"; r[5] = "Никита"
    rows.append(r)

    # Product with negative remainder
    r = [""] * 10
    r[1] = "Amber"; r[2] = 59; r[4] = 32; r[6] = -5
    rows.append(r)

    config = SheetStructureConfig(
        warehouse="TEST",
        spreadsheet_id="test-id",
        sheet_name="Test",
        analyzed_at=datetime.utcnow(),
        sections=[
            SectionConfig(
                name="KZ_TEREA",
                marker_text="KZ TEREA",
                type="marker",
                col_start=0,
                col_end=9,
                name_col=1,
                remainder_col=6,
                maks_col=4,
            ),
        ],
    )

    result = parse_stock_with_config(rows, config)

    print("\n--- Negative clamp test ---")
    assert result.records[0].quantity == 0, "Negative should be clamped to 0"
    assert result.records[0].maks_sales == 32
    assert len(result.warnings) == 1
    assert "clamped" in result.warnings[0].lower()

    print("Negative clamp test PASSED!")


def test_fallback_last_number():
    """Test fallback to last-number heuristic when remainder_col is None."""
    rows = [[""] * 10 for _ in range(3)]

    r = [""] * 10
    r[1] = "KZ TEREA"
    rows.append(r)

    r = [""] * 10
    r[3] = "Farik"; r[4] = "Maks"; r[5] = "Никита"
    rows.append(r)

    # Full row: ARRIVED=59, Farik=17, Maks=32, Nikita=2, remainder=8
    r = [""] * 10
    r[1] = "Amber"; r[2] = 59; r[3] = 17; r[4] = 32; r[5] = 2; r[6] = 8
    rows.append(r)

    # Sparse row: missing some sales
    r = [""] * 10
    r[1] = "Yellow"; r[2] = 24; r[3] = 3; r[5] = 2; r[6] = 19
    rows.append(r)

    # remainder_col=None → fallback to last number in zone
    config = SheetStructureConfig(
        warehouse="TEST",
        spreadsheet_id="test-id",
        sheet_name="Test",
        analyzed_at=datetime.utcnow(),
        sections=[
            SectionConfig(
                name="KZ_TEREA",
                marker_text="KZ TEREA",
                type="marker",
                col_start=0,
                col_end=9,
                name_col=1,
                remainder_col=None,
                maks_col=4,
            ),
        ],
    )

    result = parse_stock_with_config(rows, config)
    records = {r.product_name: r for r in result.records}

    print("\n--- Fallback last number test ---")
    for name, rec in sorted(records.items()):
        print(f"  {name}: qty={rec.quantity}, fallback={rec.is_fallback}")

    assert records["Amber"].quantity == 8  # Last number in cols 0-8
    assert records["Yellow"].quantity == 19
    assert records["Amber"].is_fallback is True
    assert records["Yellow"].is_fallback is True

    print("Fallback last number test PASSED!")


# ---------------------------------------------------------------------------
# Tests: Python reconnaissance (detect_sections)
# ---------------------------------------------------------------------------


def test_detect_sections():
    """Test that detect_sections finds all marker-based sections."""
    matrix = _build_mock_matrix()
    sections = detect_sections(matrix)

    print("\n--- Detect sections test ---")
    marker_names = {s.marker_text for s in sections}
    print(f"Found markers: {marker_names}")

    for s in sections:
        print(
            f"  '{s.marker_text}' at row={s.marker_row}, col={s.marker_col}, "
            f"sellers={list(s.seller_headers.keys())}, "
            f"samples={len(s.sample_rows)}"
        )

    # All 5 marker-based sections
    expected = {"KZ TEREA", "TEREA JAPAN", "TEREA EUROPE", "ARMENIA", "УНИКАЛЬНАЯ ТЕРЕА"}
    assert marker_names == expected, f"Expected {expected}, got {marker_names}"

    # Check seller headers detected for KZ TEREA
    kz = next(s for s in sections if s.marker_text == "KZ TEREA")
    assert "Maks" in kz.seller_headers, "Maks should be detected near KZ TEREA"
    assert "Farik" in kz.seller_headers
    assert len(kz.sample_rows) >= 2, "Should have sample product rows"

    # Check seller headers detected for ARMENIA
    arm = next(s for s in sections if s.marker_text == "ARMENIA")
    assert "Maks" in arm.seller_headers

    print("Detect sections test PASSED!")


def test_detect_prefix_sections():
    """Test that detect_prefix_sections finds ONE/STND/PRIME."""
    matrix = _build_mock_matrix()
    marker_sections = detect_sections(matrix)
    prefix_sections = detect_prefix_sections(matrix, marker_sections)

    print("\n--- Detect prefix sections test ---")
    prefix_names = {s.marker_text for s in prefix_sections}
    print(f"Found prefixes: {prefix_names}")

    for s in prefix_sections:
        print(
            f"  '{s.marker_text}' at row={s.marker_row}, col={s.marker_col}, "
            f"is_prefix={s.is_prefix}, samples={len(s.sample_rows)}"
        )

    assert "ONE" in prefix_names
    assert "STND" in prefix_names
    assert "PRIME" in prefix_names

    one = next(s for s in prefix_sections if s.marker_text == "ONE")
    assert one.is_prefix is True
    assert len(one.sample_rows) >= 2

    print("Detect prefix sections test PASSED!")


if __name__ == "__main__":
    test_parse_with_config()
    test_maks_sales()
    test_last_occurrence()
    test_negative_clamp()
    test_fallback_last_number()
    test_detect_sections()
    test_detect_prefix_sections()
    print("\n=== ALL TESTS PASSED ===")
