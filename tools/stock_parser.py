"""
Stock Parser
------------

Config-driven parser for Google Sheets stock data.
Uses LLM-generated SheetStructureConfig to determine column layout per section.
Scans bottom-to-up for section markers (= current data after weekly transfers).

Usage:
    from tools.stock_parser import parse_stock_with_config
    result = parse_stock_with_config(matrix, config)
"""

import logging
import re
from dataclasses import dataclass, asdict

from tools.structure_analyzer import SectionConfig, SheetStructureConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class StockRecord:
    """One parsed stock item."""

    category: str       # "KZ_TEREA", "ARMENIA", etc.
    product_name: str   # "Amber", "T Mint", etc.
    quantity: int        # Remaining stock (clamped to 0 if negative)
    maks_sales: int      # Maks sales count
    is_fallback: bool    # True if qty was from last-number heuristic
    source_row: int      # 0-based row in sheet
    source_col: int      # 0-based col of qty cell


@dataclass
class ParseResult:
    """Full result of a stock parse run."""

    records: list[StockRecord]
    sections_found: list[str]
    sections_missing: list[str]
    warnings: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Words that indicate a header row (not a product row)
_HEADER_WORDS = {
    "farik", "maks", "макс", "никита", "nikita", "la maks",
    "chicago max", "chi maks", "mia", "arrived",
    "customer", "discount", "lost", "refund",
}


def _is_number(val) -> bool:
    """Check if a value is numeric."""
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
        try:
            return int(float(val.replace(",", "")))
        except (ValueError, TypeError):
            return 0
    return 0


def _normalize_name(name: str) -> str:
    """Normalize product name: strip, collapse whitespace."""
    return re.sub(r"\s+", " ", str(name).strip())


def _get_cell(row: list, col: int):
    """Safely get cell value, returning '' for out-of-range."""
    if col < len(row):
        return row[col]
    return ""


def _is_header_row(cells: list) -> bool:
    """Check if a row is a header (Farik/Maks/Nikita etc.), not a product."""
    text = " ".join(str(c).strip() for c in cells if c).lower()
    return any(hw in text for hw in _HEADER_WORDS)


def _last_number_in_range(row: list, col_start: int, col_end: int) -> tuple[int, int]:
    """Find the last numeric value in a column range.

    Returns (value, col_index).
    """
    last_val = 0
    last_col = col_start
    for i in range(col_start, min(col_end, len(row))):
        cell = row[i] if i < len(row) else ""
        if _is_number(cell) and str(cell).strip():
            last_val = _to_int(cell)
            last_col = i
    return last_val, last_col


# ---------------------------------------------------------------------------
# Bottom-up marker search
# ---------------------------------------------------------------------------

def _find_marker_bottom_up(
    matrix: list[list],
    marker_text: str,
    col_start: int,
    col_end: int,
) -> int | None:
    """Find the bottom-most occurrence of a marker text in a column range.

    Scans from bottom to top. First match = current data.
    Returns row index or None.
    """
    marker_upper = marker_text.upper().strip()

    for row_idx in range(len(matrix) - 1, -1, -1):
        row = matrix[row_idx]
        for col_idx in range(col_start, min(col_end, len(row))):
            cell = str(_get_cell(row, col_idx)).strip()
            if not cell:
                continue
            cell_upper = cell.upper()
            if cell_upper == marker_upper or cell_upper.startswith(marker_upper + " "):
                return row_idx

    return None


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------

def _parse_marker_section(
    matrix: list[list],
    cfg: SectionConfig,
) -> tuple[list[StockRecord], list[str], bool]:
    """Parse a marker-based section using config.

    Returns (records, warnings, was_found).
    """
    marker_row = _find_marker_bottom_up(matrix, cfg.marker_text, cfg.col_start, cfg.col_end)
    if marker_row is None:
        return [], [f"Marker '{cfg.marker_text}' not found in cols {cfg.col_start}-{cfg.col_end}"], False

    records = []
    warnings = []
    category = cfg.name

    for row_idx in range(marker_row + 1, len(matrix)):
        row = matrix[row_idx] if row_idx < len(matrix) else []

        # Extract sub-row for the zone
        subrow = []
        for i in range(cfg.col_start, min(cfg.col_end, len(row))):
            subrow.append(row[i] if i < len(row) else "")

        # Empty row = end of section
        if not any(str(c).strip() for c in subrow):
            break

        # Skip header rows
        if _is_header_row(subrow):
            continue

        # Get product name
        name_cell = _get_cell(row, cfg.name_col)
        name_str = str(name_cell).strip() if name_cell is not None else ""
        if not name_str or _is_number(name_cell):
            continue

        product_name = _normalize_name(name_str)

        # Get quantity
        quantity = 0
        source_col = cfg.name_col
        is_fallback = False

        if cfg.remainder_col is not None:
            qty_cell = _get_cell(row, cfg.remainder_col)
            if _is_number(qty_cell) and str(qty_cell).strip():
                quantity = _to_int(qty_cell)
                source_col = cfg.remainder_col
            else:
                # Fallback to last-number heuristic
                quantity, source_col = _last_number_in_range(row, cfg.col_start, cfg.col_end)
                is_fallback = True
        else:
            quantity, source_col = _last_number_in_range(row, cfg.col_start, cfg.col_end)
            is_fallback = True

        # Get maks_sales
        maks_sales = 0
        if cfg.maks_col is not None:
            maks_cell = _get_cell(row, cfg.maks_col)
            if _is_number(maks_cell) and str(maks_cell).strip():
                maks_sales = _to_int(maks_cell)

        # Clamp negative quantities to 0
        if quantity < 0:
            warnings.append(
                f"{category}/{product_name}: negative qty {quantity} clamped to 0 (row {row_idx + 1})"
            )
            quantity = 0

        records.append(StockRecord(
            category=category,
            product_name=product_name,
            quantity=quantity,
            maks_sales=maks_sales,
            is_fallback=is_fallback,
            source_row=row_idx,
            source_col=source_col,
        ))

    logger.info(
        "Parsed section '%s': %d products (marker at row %d)",
        cfg.name, len(records), marker_row + 1,
    )

    return records, warnings, True


def _parse_prefix_section(
    matrix: list[list],
    cfg: SectionConfig,
) -> tuple[list[StockRecord], list[str], bool]:
    """Parse a prefix-based section (ONE/STND/PRIME) using config.

    Scans the zone bottom-up for product names starting with the prefix.
    Groups products and takes the bottom-most block.
    """
    if not cfg.prefix:
        return [], [f"Prefix section '{cfg.name}' has no prefix defined"], False

    prefix_upper = cfg.prefix.upper() + " "

    # Scan bottom-up to find the last block of prefix products
    last_seen: dict[str, StockRecord] = {}
    found_any = False

    # Find the bottom-most prefix product
    bottom_row = None
    for row_idx in range(len(matrix) - 1, -1, -1):
        row = matrix[row_idx] if row_idx < len(matrix) else []
        name_cell = _get_cell(row, cfg.name_col)
        name_str = str(name_cell).strip() if name_cell is not None else ""
        if name_str and name_str.upper().startswith(prefix_upper):
            bottom_row = row_idx
            break

    if bottom_row is None:
        return [], [], False

    # Go up from bottom_row to find the start of the block
    block_start = bottom_row
    for r in range(bottom_row - 1, max(bottom_row - 30, -1), -1):
        row = matrix[r] if r < len(matrix) else []
        subrow = []
        for i in range(cfg.col_start, min(cfg.col_end, len(row))):
            subrow.append(row[i] if i < len(row) else "")

        if not any(str(c).strip() for c in subrow):
            break
        if _is_header_row(subrow):
            break

        name_cell = _get_cell(row, cfg.name_col)
        name_str = str(name_cell).strip() if name_cell is not None else ""
        # Check if this row has ANY prefix product (not just ours)
        has_prefix = False
        for p in ("ONE", "STND", "PRIME"):
            if name_str.upper().startswith(p + " "):
                has_prefix = True
                break
        if has_prefix:
            block_start = r
        else:
            break

    # Now parse forward from block_start, collecting only our prefix
    warnings = []
    for row_idx in range(block_start, min(bottom_row + 10, len(matrix))):
        row = matrix[row_idx] if row_idx < len(matrix) else []

        subrow = []
        for i in range(cfg.col_start, min(cfg.col_end, len(row))):
            subrow.append(row[i] if i < len(row) else "")

        if not any(str(c).strip() for c in subrow):
            break
        if _is_header_row(subrow):
            continue

        name_cell = _get_cell(row, cfg.name_col)
        name_str = str(name_cell).strip() if name_cell is not None else ""
        if not name_str or _is_number(name_cell):
            continue
        if not name_str.upper().startswith(prefix_upper):
            continue

        product_name = _normalize_name(name_str)
        found_any = True

        # Get quantity
        quantity = 0
        source_col = cfg.name_col
        is_fallback = False

        if cfg.remainder_col is not None:
            qty_cell = _get_cell(row, cfg.remainder_col)
            if _is_number(qty_cell) and str(qty_cell).strip():
                quantity = _to_int(qty_cell)
                source_col = cfg.remainder_col
            else:
                quantity, source_col = _last_number_in_range(row, cfg.col_start, cfg.col_end)
                is_fallback = True
        else:
            quantity, source_col = _last_number_in_range(row, cfg.col_start, cfg.col_end)
            is_fallback = True

        # Get maks_sales
        maks_sales = 0
        if cfg.maks_col is not None:
            maks_cell = _get_cell(row, cfg.maks_col)
            if _is_number(maks_cell) and str(maks_cell).strip():
                maks_sales = _to_int(maks_cell)

        # Clamp
        if quantity < 0:
            warnings.append(
                f"{cfg.name}/{product_name}: negative qty {quantity} clamped to 0 (row {row_idx + 1})"
            )
            quantity = 0

        # Last occurrence wins (overwrites if duplicate)
        last_seen[product_name] = StockRecord(
            category=cfg.name,
            product_name=product_name,
            quantity=quantity,
            maks_sales=maks_sales,
            is_fallback=is_fallback,
            source_row=row_idx,
            source_col=source_col,
        )

    records = list(last_seen.values())

    if found_any:
        logger.info(
            "Parsed prefix section '%s': %d products (block rows %d-%d)",
            cfg.name, len(records), block_start + 1, bottom_row + 1,
        )

    return records, warnings, found_any


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------

def parse_stock_with_config(
    matrix: list[list],
    config: SheetStructureConfig,
) -> ParseResult:
    """Parse stock using LLM-generated config.

    For each section in config:
    1. Find the section marker (bottom-up) or prefix-based products.
    2. Extract product rows below the marker.
    3. Use config columns for name, quantity, maks_sales.

    Args:
        matrix: 2D list of cell values from Google Sheets API.
        config: SheetStructureConfig from LLM analysis.

    Returns:
        ParseResult with records, found/missing sections, and warnings.
    """
    all_records: list[StockRecord] = []
    all_warnings: list[str] = []
    sections_found: list[str] = []
    sections_missing: list[str] = []

    for section_cfg in config.sections:
        if section_cfg.type == "marker":
            records, warnings, found = _parse_marker_section(matrix, section_cfg)
        elif section_cfg.type == "prefix":
            records, warnings, found = _parse_prefix_section(matrix, section_cfg)
        else:
            logger.warning("Unknown section type '%s' for %s", section_cfg.type, section_cfg.name)
            continue

        all_records.extend(records)
        all_warnings.extend(warnings)

        if found:
            sections_found.append(section_cfg.name)
        else:
            sections_missing.append(section_cfg.name)

    # Summary
    available = sum(1 for r in all_records if r.quantity > 0)
    fallback = sum(1 for r in all_records if r.is_fallback)

    logger.info(
        "Parse complete: %d items (%d available, %d fallback, %d warnings, %d sections found, %d missing)",
        len(all_records), available, fallback, len(all_warnings),
        len(sections_found), len(sections_missing),
    )

    if all_warnings:
        for w in all_warnings:
            logger.warning("Stock: %s", w)

    return ParseResult(
        records=all_records,
        sections_found=sections_found,
        sections_missing=sections_missing,
        warnings=all_warnings,
    )


def records_to_dicts(records: list[StockRecord]) -> list[dict]:
    """Convert StockRecord list to list of dicts for db.stock.sync_stock()."""
    return [asdict(r) for r in records]
