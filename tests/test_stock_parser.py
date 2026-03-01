"""
Test Stock Parser
-----------------

Unit tests for the 2D zone-based stock parser.
Mock data simulates the real Google Sheets layout from screenshots.

Run:
    python -m tests.test_stock_parser
"""

from tools.stock_parser import parse_stock, StockRecord


def _build_mock_matrix() -> list[list]:
    """Build a mock matrix simulating the real spreadsheet layout.

    Layout matches actual spreadsheet:
    - Left zone (cols 0-8): KZ TEREA, TEREA JAPAN, TEREA EUROPE
    - Middle zone (cols 9-17): ONE/STND/PRIME (by prefix, no marker), УНИКАЛЬНАЯ ТЕРЕА
    - Right zone (cols 17-26): ARMENIA

    ONE/STND/PRIME products have NO section marker — category is derived from
    product name prefix (e.g., "ONE Red" → category ONE).
    """
    rows = []

    # --- Rows 0-2: empty/headers ---
    rows.append([""] * 27)
    rows.append([""] * 27)
    rows.append([""] * 27)

    # --- Row 3: KZ TEREA marker + ARMENIA marker (NO "ONE" marker!) ---
    r = [""] * 27
    r[1] = "KZ TEREA"
    r[18] = "ARMENIA"
    rows.append(r)

    # --- Row 4: Sub-headers (all zones on same row) ---
    r = [""] * 27
    r[0] = "ARRIVED"
    r[3] = "Farik"; r[4] = "Maks"; r[5] = "Никита"
    r[12] = "Farik"; r[13] = "Maks"; r[14] = "Nikita"; r[17] = "ARRIVED"
    r[20] = "Farik"; r[21] = "Maks"; r[22] = "Никита"
    rows.append(r)

    # --- Row 6: KZ TEREA Amber | ONE Red | ARMENIA Amber ---
    r = [""] * 27
    r[1] = "Amber"; r[2] = 59; r[3] = 17; r[4] = 32; r[5] = 2; r[6] = 8
    r[10] = "ONE Red"; r[15] = 0
    r[18] = "Amber"; r[19] = 59; r[20] = 17; r[21] = 32; r[22] = 2; r[23] = 8
    rows.append(r)

    # --- Row 7: KZ TEREA Yellow | ONE Black | ARMENIA Yellow ---
    r = [""] * 27
    r[1] = "Yellow"; r[2] = 25; r[3] = 6; r[4] = 5; r[5] = 1; r[6] = 13
    r[10] = "ONE Black"; r[11] = 2; r[14] = 2; r[15] = 0
    r[18] = "Yellow"; r[19] = 14; r[23] = 4
    rows.append(r)

    # --- Row 8: KZ TEREA Silver | ONE Green | ARMENIA Silver ---
    r = [""] * 27
    r[1] = "Silver"; r[2] = 61; r[3] = 4; r[4] = 19; r[5] = 7; r[6] = 31
    r[10] = "ONE Green"; r[15] = 0
    r[18] = "Silver"; r[19] = 17; r[20] = 4; r[21] = 1; r[23] = 31
    rows.append(r)

    # --- Row 9: KZ TEREA Bronze ---
    r = [""] * 27
    r[1] = "Bronze"; r[2] = 0; r[6] = 0
    rows.append(r)

    # --- Row 10: empty (end of KZ TEREA and ONE) ---
    rows.append([""] * 27)

    # --- Row 11: middle zone STND header ---
    r = [""] * 27
    r[12] = "Farik"; r[13] = "Maks"; r[14] = "Nikita"
    rows.append(r)

    # --- Row 12-13: STND products ---
    r = [""] * 27
    r[10] = "STND Red"; r[11] = 0; r[15] = 0
    rows.append(r)

    r = [""] * 27
    r[10] = "STND Black"; r[11] = 0; r[15] = 0
    rows.append(r)

    # --- Row 14: empty ---
    rows.append([""] * 27)

    # --- Row 15: middle zone PRIME header ---
    r = [""] * 27
    r[12] = "Farik"; r[13] = "Maks"; r[14] = "Nikita"
    rows.append(r)

    # --- Row 16-17: PRIME products ---
    r = [""] * 27
    r[10] = "PRIME Black"; r[15] = 0
    rows.append(r)

    r = [""] * 27
    r[10] = "PRIME Gold"; r[11] = 1; r[14] = 1; r[15] = 0
    rows.append(r)

    # --- Row 18: empty ---
    rows.append([""] * 27)

    # --- Row 19: TEREA JAPAN marker | УНИКАЛЬНАЯ ТЕРЕА marker ---
    r = [""] * 27
    r[1] = "TEREA JAPAN"
    r[10] = "УНИКАЛЬНАЯ ТЕРЕА"; r[14] = "LA MAKS"
    rows.append(r)

    # --- Row 20: headers ---
    r = [""] * 27
    r[3] = "Farik"; r[4] = "Maks"; r[5] = "Никита"
    r[13] = "Farik"; r[14] = "Maks"; r[15] = "Nikita"
    rows.append(r)

    # --- Row 21: TEREA JAPAN T Regular | Warm Regular ---
    r = [""] * 27
    r[1] = "T Regular"; r[2] = 15; r[4] = 1; r[6] = 14
    r[9] = 8; r[10] = "Warm Regular"; r[12] = 10; r[16] = 10
    rows.append(r)

    # --- Row 22: T Mint | Black Ruby Menthol ---
    r = [""] * 27
    r[1] = "T Mint"; r[2] = 24; r[3] = 3; r[5] = 2; r[6] = 19
    r[9] = 18; r[10] = "Black Ruby Menthol"; r[12] = 20; r[15] = 1; r[16] = 19
    rows.append(r)

    # --- Row 23: T Black ---
    r = [""] * 27
    r[1] = "T Black"; r[2] = 30; r[3] = 3; r[6] = 27
    rows.append(r)

    # --- Row 24: empty ---
    rows.append([""] * 27)

    # --- Row 25: TEREA EUROPE marker ---
    r = [""] * 27
    r[1] = "TEREA EUROPE"
    rows.append(r)

    # --- Row 26: headers ---
    r = [""] * 27
    r[3] = "Farik"; r[4] = "Maks"; r[5] = "Никита"
    rows.append(r)

    # --- Row 27: TEREA EUROPE Amber ---
    r = [""] * 27
    r[1] = "Amber"; r[2] = 26; r[4] = 10; r[6] = 16
    rows.append(r)

    return rows


def test_basic_parsing():
    """Test that the parser correctly extracts stock data from mock matrix."""
    matrix = _build_mock_matrix()
    result = parse_stock(matrix)

    print(f"\nSections found: {result.sections_found}")
    print(f"Sections missing: {result.sections_missing}")
    print(f"Total records: {len(result.records)}")
    print(f"Warnings: {result.warnings}")
    print()

    # Build lookup: (category, product_name) -> quantity
    stock = {(r.category, r.product_name): r.quantity for r in result.records}

    for (cat, name), qty in sorted(stock.items()):
        print(f"  {cat:25s} | {name:25s} | qty={qty}")

    # --- Assertions ---
    print("\n--- Running assertions ---")

    # KZ TEREA
    assert stock[("KZ_TEREA", "Amber")] == 8, f"KZ TEREA Amber expected 8, got {stock.get(('KZ_TEREA', 'Amber'))}"
    assert stock[("KZ_TEREA", "Yellow")] == 13
    assert stock[("KZ_TEREA", "Silver")] == 31
    assert stock[("KZ_TEREA", "Bronze")] == 0

    # TEREA JAPAN
    assert stock[("TEREA_JAPAN", "T Regular")] == 14
    assert stock[("TEREA_JAPAN", "T Mint")] == 19
    assert stock[("TEREA_JAPAN", "T Black")] == 27

    # ARMENIA
    assert stock[("ARMENIA", "Amber")] == 8
    assert stock[("ARMENIA", "Yellow")] == 4
    assert stock[("ARMENIA", "Silver")] == 31

    # ONE (detected by prefix, no explicit marker)
    assert "ONE" in result.sections_found, "ONE should be detected by product prefix"
    assert stock[("ONE", "ONE Red")] == 0
    assert stock[("ONE", "ONE Black")] == 0
    assert stock[("ONE", "ONE Green")] == 0

    # STND (detected by prefix)
    assert "STND" in result.sections_found, "STND should be detected by product prefix"
    assert stock[("STND", "STND Red")] == 0
    assert stock[("STND", "STND Black")] == 0

    # PRIME (detected by prefix)
    assert "PRIME" in result.sections_found, "PRIME should be detected by product prefix"
    assert stock[("PRIME", "PRIME Black")] == 0
    assert stock[("PRIME", "PRIME Gold")] == 0

    # УНИКАЛЬНАЯ ТЕРЕА
    assert "УНИКАЛЬНАЯ ТЕРЕА" in result.sections_found
    assert stock[("УНИКАЛЬНАЯ_ТЕРЕА", "Black Ruby Menthol")] == 19
    assert stock[("УНИКАЛЬНАЯ_ТЕРЕА", "Warm Regular")] == 10

    # TEREA EUROPE
    assert stock[("TEREA_EUROPE", "Amber")] == 16

    # All sections should be found
    assert "KZ TEREA" in result.sections_found
    assert "TEREA JAPAN" in result.sections_found
    assert "ARMENIA" in result.sections_found
    assert "TEREA EUROPE" in result.sections_found

    print("\nAll assertions PASSED!")


def test_last_occurrence():
    """Test that parser takes the LAST occurrence of a section marker."""
    matrix = _build_mock_matrix()

    # Add an OLD (archived) KZ TEREA block at the very beginning
    old_block = [[""] * 27 for _ in range(6)]
    old_block[0][1] = "KZ TEREA"
    old_block[1][3] = "Farik"; old_block[1][4] = "Maks"
    old_block[2][1] = "Amber"; old_block[2][2] = 100; old_block[2][6] = 99
    old_block[3][1] = "Yellow"; old_block[3][2] = 100; old_block[3][6] = 88

    full_matrix = old_block + matrix

    result = parse_stock(full_matrix)
    stock = {(r.category, r.product_name): r.quantity for r in result.records}

    print("\n--- Last occurrence test ---")
    print(f"KZ TEREA Amber: {stock.get(('KZ_TEREA', 'Amber'))}")
    print(f"KZ TEREA Yellow: {stock.get(('KZ_TEREA', 'Yellow'))}")

    assert stock[("KZ_TEREA", "Amber")] == 8, "Should use last occurrence, not old block"
    assert stock[("KZ_TEREA", "Yellow")] == 13

    print("Last occurrence test PASSED!")


def test_last_number_is_remainder():
    """Test that the last numeric value in a row is always treated as the remainder.

    Covers rows with varying numbers of filled cells (some sales columns empty).
    """
    rows = []
    for _ in range(3):
        rows.append([""] * 10)

    # Section marker
    r = [""] * 10
    r[1] = "KZ TEREA"
    rows.append(r)

    # Header
    r = [""] * 10
    r[3] = "Farik"; r[4] = "Maks"; r[5] = "Никита"
    rows.append(r)

    # 5 numbers: ARRIVED + 3 sales + REMAINDER → last = 8
    r = [""] * 10
    r[1] = "Amber"; r[2] = 59; r[3] = 17; r[4] = 32; r[5] = 2; r[6] = 8
    rows.append(r)

    # 4 numbers (one sale empty): ARRIVED + 2 sales + REMAINDER → last = 19
    r = [""] * 10
    r[1] = "Yellow"; r[2] = 24; r[3] = 3; r[5] = 2; r[6] = 19
    rows.append(r)

    # 1 number only → just take it
    r = [""] * 10
    r[1] = "Bronze"; r[6] = 0
    rows.append(r)

    result = parse_stock(rows)
    records_by_name = {r.product_name: r for r in result.records}

    print("\n--- Last number test ---")
    for name, rec in sorted(records_by_name.items()):
        print(f"  {name}: qty={rec.quantity}")

    # Amber: last number is 8 (remainder after all sales)
    assert records_by_name["Amber"].quantity == 8

    # Yellow: last number is 19 (remainder, one sale column empty)
    assert records_by_name["Yellow"].quantity == 19

    # Bronze: only number is 0
    assert records_by_name["Bronze"].quantity == 0

    print("Last number test PASSED!")


if __name__ == "__main__":
    test_basic_parsing()
    test_last_occurrence()
    test_last_number_is_remainder()
    print("\n=== ALL TESTS PASSED ===")
