"""
Stock Parser
------------

2D zone-based parser for Google Sheets stock data.
Sections are arranged in column zones (left, middle, right) and
may repeat vertically when the table is "transferred" weekly.
Always takes the LAST occurrence of each section marker (= current data).

Usage:
    from tools.stock_parser import parse_stock
    records = parse_stock(matrix)  # matrix from SheetsClient.read_sheet_values()
"""

import logging
import re
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class StockRecord:
    """One parsed stock item."""

    category: str       # "KZ_TEREA", "ARMENIA", etc.
    product_name: str   # "Amber", "T Mint", etc.
    quantity: int        # Remaining stock (can be negative = error signal)
    is_fallback: bool    # True if qty was calculated, not read from cell
    source_row: int      # 0-based row in sheet (for debug)
    source_col: int      # 0-based col of qty cell


@dataclass
class ParseResult:
    """Full result of a stock parse run."""

    records: list[StockRecord]
    sections_found: list[str]
    sections_missing: list[str]
    warnings: list[str]


# ---------------------------------------------------------------------------
# Zone configuration
# ---------------------------------------------------------------------------
# Each zone defines a column range and the sections expected within it.
# Column ranges are 0-based indices.

ZONE_CONFIG = {
    "left": {
        "col_range": (0, 9),        # columns A through I
        "sections": ["KZ TEREA", "TEREA JAPAN", "TEREA EUROPE"],
    },
    "middle": {
        "col_range": (9, 18),       # columns J through R
        "sections": ["УНИКАЛЬНАЯ ТЕРЕА"],
        # ONE/STND/PRIME have no section marker — detected by product name prefix.
        # Parsed via _parse_prefix_sections() from zone start to first marker.
        "prefix_sections": ["ONE", "STND", "PRIME"],
    },
    "right": {
        "col_range": (17, 27),      # columns R through AA
        "sections": ["ARMENIA"],
    },
}

# Words that indicate a header row (not a product row)
_HEADER_WORDS = {
    "farik", "maks", "никита", "nikita", "la maks", "arrived",
    "customer", "discount", "lost", "refund",
}

# Product name prefixes that define categories (no standalone section marker)
_PREFIX_CATEGORIES = {"ONE", "STND", "PRIME"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_number(val) -> bool:
    """Check if a value is numeric (int or float from Sheets API)."""
    if isinstance(val, (int, float)):
        return True
    if isinstance(val, str):
        try:
            float(val.replace(",", ""))
            return True
        except (ValueError, TypeError):
            return False
    return False


def _to_int(val) -> int:
    """Convert a numeric value to int."""
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        return int(float(val.replace(",", "")))
    return 0


def _normalize_name(name: str) -> str:
    """Normalize product name: strip, collapse whitespace."""
    return re.sub(r"\s+", " ", str(name).strip())


def _is_header_row(cells: list) -> bool:
    """Check if a row is a header (Farik/Maks/Nikita etc.), not a product."""
    text = " ".join(str(c).strip() for c in cells if c).lower()
    return any(hw in text for hw in _HEADER_WORDS)


def _get_cell(row: list, col: int):
    """Safely get cell value, returning '' for out-of-range."""
    if col < len(row):
        return row[col]
    return ""


def _extract_subrow(row: list, col_start: int, col_end: int) -> list:
    """Extract a sub-row for a zone's column range."""
    result = []
    for i in range(col_start, min(col_end, len(row))):
        result.append(row[i] if i < len(row) else "")
    return result


# ---------------------------------------------------------------------------
# Zone parsing
# ---------------------------------------------------------------------------

def _find_section_markers(
    matrix: list[list], col_start: int, col_end: int, section_names: list[str],
) -> dict[str, int]:
    """Find the LAST occurrence of each section marker within a column zone.

    Returns {section_name: row_index} for found sections.
    """
    found: dict[str, int] = {}

    for row_idx, row in enumerate(matrix):
        # Check cells within the zone's columns
        for col_idx in range(col_start, min(col_end, len(row))):
            cell = str(_get_cell(row, col_idx)).strip()
            if not cell:
                continue

            cell_upper = cell.upper()

            for section in section_names:
                section_upper = section.upper()
                # Match: cell equals marker or starts with marker + space
                if cell_upper == section_upper or cell_upper.startswith(section_upper + " "):
                    found[section] = row_idx

    return found


def _parse_section_products(
    matrix: list[list],
    marker_row: int,
    col_start: int,
    col_end: int,
    next_marker_rows: list[int],
    section_name: str,
) -> tuple[list[StockRecord], list[str]]:
    """Parse product rows below a section marker.

    Reads rows from marker_row+1 until:
    - Empty row (all cells in zone are empty)
    - Another marker row
    - End of matrix

    Returns (records, warnings).
    """
    records = []
    warnings = []

    # Determine where to stop: next marker in this zone, or end of matrix
    stop_row = len(matrix)
    for mr in sorted(next_marker_rows):
        if mr > marker_row:
            stop_row = mr
            break

    category = section_name.upper().replace(" ", "_")

    for row_idx in range(marker_row + 1, stop_row):
        if row_idx >= len(matrix):
            break

        subrow = _extract_subrow(matrix[row_idx], col_start, col_end)

        # Skip completely empty rows
        if not any(str(c).strip() for c in subrow):
            break  # Empty row = end of section

        # Skip header rows
        if _is_header_row(subrow):
            continue

        # Find product name (first non-empty text cell that isn't just a number)
        product_name = ""
        for cell in subrow:
            cell_str = str(cell).strip()
            if cell_str and not _is_number(cell):
                product_name = _normalize_name(cell_str)
                break

        if not product_name:
            continue  # Row with only numbers or empty text — skip

        # Find all numeric values in the row
        numeric_values = []
        numeric_positions = []
        for i, cell in enumerate(subrow):
            if _is_number(cell) and str(cell).strip():
                numeric_values.append(_to_int(cell))
                numeric_positions.append(col_start + i)

        if not numeric_values:
            continue  # No numbers at all — skip

        # Last numeric value = remaining quantity.
        # This is the most reliable approach: the remainder column is always
        # the rightmost filled number in a product row.
        quantity = numeric_values[-1]
        source_col = numeric_positions[-1]
        is_fallback = False

        # Consistency check when we have enough data (5+ numbers =
        # ARRIVED + 3 sales + REMAINDER)
        if len(numeric_values) >= 5:
            arrived = numeric_values[0]
            sales_sum = sum(numeric_values[1:-1])
            calculated = arrived - sales_sum
            if abs(quantity - calculated) > 2:
                warnings.append(
                    f"{section_name}/{product_name}: remainder={quantity} "
                    f"but ARRIVED({arrived}) - sales({sales_sum}) = {calculated} "
                    f"(row {row_idx + 1})"
                )

        records.append(StockRecord(
            category=category,
            product_name=product_name,
            quantity=quantity,
            is_fallback=is_fallback,
            source_row=row_idx,
            source_col=source_col,
        ))

    return records, warnings


def _parse_prefix_sections(
    matrix: list[list],
    col_start: int,
    col_end: int,
    prefixes: list[str],
    stop_row: int,
) -> tuple[list[StockRecord], list[str], list[str]]:
    """Parse products whose name starts with a known prefix (ONE, STND, PRIME).

    Scans ALL rows in the zone up to stop_row. Keeps the LAST occurrence
    of each product (handles weekly transfers where old data stays above).

    Returns (records, found_prefixes, warnings).
    """
    prefix_upper = {p.upper() for p in prefixes}

    # Scan all rows, keeping last occurrence of each (category, product_name)
    last_seen: dict[tuple[str, str], StockRecord] = {}
    found_prefixes = set()

    for row_idx in range(min(stop_row, len(matrix))):
        subrow = _extract_subrow(matrix[row_idx], col_start, col_end)

        if not any(str(c).strip() for c in subrow):
            continue
        if _is_header_row(subrow):
            continue

        # Find product name (first non-numeric text cell)
        product_name = ""
        for cell in subrow:
            cell_str = str(cell).strip()
            if cell_str and not _is_number(cell):
                product_name = _normalize_name(cell_str)
                break

        if not product_name:
            continue

        # Determine category from prefix
        category = None
        for p in prefixes:
            if product_name.upper().startswith(p.upper() + " "):
                category = p.upper()
                found_prefixes.add(p)
                break

        if not category:
            continue

        # Find numeric values
        numeric_values = []
        numeric_positions = []
        for i, cell in enumerate(subrow):
            if _is_number(cell) and str(cell).strip():
                numeric_values.append(_to_int(cell))
                numeric_positions.append(col_start + i)

        if not numeric_values:
            continue

        quantity = numeric_values[-1]
        source_col = numeric_positions[-1]

        # Last occurrence wins (overwrites earlier transfers)
        last_seen[(category, product_name)] = StockRecord(
            category=category,
            product_name=product_name,
            quantity=quantity,
            is_fallback=False,
            source_row=row_idx,
            source_col=source_col,
        )

    return list(last_seen.values()), list(found_prefixes), []


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------

def parse_stock(matrix: list[list]) -> ParseResult:
    """Parse a full sheet matrix into stock records.

    Uses 2D zone-based parsing: each zone (left, middle, right) is processed
    independently. Within each zone, the LAST occurrence of each section
    marker is used (= current data after weekly transfers).

    Args:
        matrix: 2D list of cell values from Google Sheets API.

    Returns:
        ParseResult with records, found/missing sections, and warnings.
    """
    all_records: list[StockRecord] = []
    all_warnings: list[str] = []
    sections_found: list[str] = []
    sections_missing: list[str] = []

    for zone_name, zone_cfg in ZONE_CONFIG.items():
        col_start, col_end = zone_cfg["col_range"]
        section_names = zone_cfg["sections"]

        # Find last occurrence of each marker in this zone
        markers = _find_section_markers(matrix, col_start, col_end, section_names)

        # Track found/missing
        for section in section_names:
            if section in markers:
                sections_found.append(section)
            else:
                sections_missing.append(section)
                logger.warning(
                    "Section '%s' not found in zone '%s' (cols %d-%d)",
                    section, zone_name, col_start, col_end,
                )

        # All marker rows in this zone (for stop detection)
        all_marker_rows = list(markers.values())

        # Handle prefix-based sections (ONE/STND/PRIME — no standalone markers)
        prefix_sections = zone_cfg.get("prefix_sections", [])
        if prefix_sections:
            # Stop row = first marker in this zone (e.g., УНИКАЛЬНАЯ ТЕРЕА)
            stop_row = min(markers.values()) if markers else len(matrix)
            records, found_prefixes, warnings = _parse_prefix_sections(
                matrix, col_start, col_end, prefix_sections, stop_row,
            )
            all_records.extend(records)
            all_warnings.extend(warnings)
            for p in prefix_sections:
                if p in found_prefixes:
                    sections_found.append(p)
                    logger.info(
                        "Parsed prefix section '%s': %d products (zone=%s)",
                        p, sum(1 for r in records if r.category == p.upper()),
                        zone_name,
                    )
                else:
                    sections_missing.append(p)
                    logger.warning(
                        "Prefix section '%s' not found in zone '%s' (cols %d-%d)",
                        p, zone_name, col_start, col_end,
                    )

        # Parse products for each found marker-based section
        for section, marker_row in markers.items():
            records, warnings = _parse_section_products(
                matrix=matrix,
                marker_row=marker_row,
                col_start=col_start,
                col_end=col_end,
                next_marker_rows=all_marker_rows,
                section_name=section,
            )
            all_records.extend(records)
            all_warnings.extend(warnings)

            logger.info(
                "Parsed section '%s': %d products (zone=%s, row=%d)",
                section, len(records), zone_name, marker_row + 1,
            )

    # Summary log
    available = sum(1 for r in all_records if r.quantity > 0)
    fallback = sum(1 for r in all_records if r.is_fallback)
    logger.info(
        "Parse complete: %d items (%d available, %d fallback, %d warnings)",
        len(all_records), available, fallback, len(all_warnings),
    )

    if all_warnings:
        for w in all_warnings:
            logger.warning("Stock consistency: %s", w)

    return ParseResult(
        records=all_records,
        sections_found=sections_found,
        sections_missing=sections_missing,
        warnings=all_warnings,
    )


def records_to_dicts(records: list[StockRecord]) -> list[dict]:
    """Convert StockRecord list to list of dicts for db.memory.sync_stock()."""
    return [asdict(r) for r in records]
