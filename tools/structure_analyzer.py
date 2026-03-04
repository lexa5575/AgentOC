"""
Structure Analyzer
------------------

Python reconnaissance + Pydantic schemas for LLM-generated sheet configs.

Scans a Google Sheets matrix BOTTOM-TO-UP to find active section markers,
detects seller headers (Farik/Maks/Никита), and builds compact "Structure Hints"
for the LLM analyzer.

Usage:
    from tools.structure_analyzer import detect_sections, build_structure_hints
    sections = detect_sections(matrix)
    hints = build_structure_hints("LA_MAKS", "LA MAKS FEB", matrix)
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic schemas for LLM-generated config
# ---------------------------------------------------------------------------


class SectionConfig(BaseModel):
    """Configuration for one section in the spreadsheet."""

    name: str                           # "KZ_TEREA", "ARMENIA", etc.
    marker_text: str = ""               # Exact text found in sheet
    type: Literal["marker", "prefix"]   # marker-based or prefix-based
    prefix: str | None = None           # For prefix type: "ONE", "STND", "PRIME"
    col_start: int                      # Zone start column (0-based, inclusive)
    col_end: int                        # Zone end column (0-based, exclusive)
    name_col: int                       # Absolute column index for product name
    remainder_col: int | None = None    # Absolute column for remainder qty
    maks_col: int | None = None         # Absolute column for Maks sales


class SheetStructureConfig(BaseModel):
    """Full structure config for one warehouse's spreadsheet."""

    warehouse: str
    spreadsheet_id: str
    sheet_name: str
    sections: list[SectionConfig]
    analyzed_at: datetime


# ---------------------------------------------------------------------------
# Constants for reconnaissance
# ---------------------------------------------------------------------------

# Known section marker patterns (case-insensitive substring match).
# Order matters: more specific patterns first to avoid partial matches.
KNOWN_MARKERS = [
    "KZ TEREA KZ",
    "KZ TEREA",
    "TEREA JAPAN",
    "TEREA EUROPE",
    "УНИКАЛЬНАЯ ТЕРЕА",
    "ARMENIA",
    "INDONESIA",
    "KZ HEETS",
]

# Seller header names (case-insensitive)
SELLER_NAMES = {"farik", "maks", "макс", "никита", "nikita"}

# Known prefix categories (no standalone marker row)
PREFIX_CATEGORIES = {"ONE", "STND", "PRIME"}

# Words that indicate a header row, not a product row
_HEADER_WORDS = {
    "farik", "maks", "макс", "никита", "nikita", "la maks",
    "chicago max", "chi maks", "mia", "arrived",
    "customer", "discount", "lost", "refund",
}

# How many sample product rows to collect per section
_SAMPLE_ROWS = 5


# ---------------------------------------------------------------------------
# Data structures for detected sections
# ---------------------------------------------------------------------------

@dataclass
class DetectedSection:
    """A section found by Python reconnaissance."""

    marker_text: str
    marker_row: int
    marker_col: int
    seller_headers: dict[str, tuple[int, int]] = field(default_factory=dict)
    warehouse_label: str | None = None
    warehouse_label_pos: tuple[int, int] | None = None
    sample_rows: list[list] = field(default_factory=list)
    sample_row_indices: list[int] = field(default_factory=list)
    is_prefix: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_cell(row: list, col: int) -> str:
    """Safely get cell value as string."""
    if col < len(row):
        val = row[col]
        return str(val).strip() if val is not None else ""
    return ""


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


def _is_header_row(cells: list) -> bool:
    """Check if row is a header (Farik/Maks/etc.), not a product row."""
    text = " ".join(str(c).strip() for c in cells if c).lower()
    return any(hw in text for hw in _HEADER_WORDS)


def _match_known_marker(cell_text: str) -> str | None:
    """Check if cell text matches a known section marker.

    Returns the matched marker pattern or None.
    """
    upper = cell_text.upper().strip()
    if not upper or len(upper) < 3:
        return None

    for marker in KNOWN_MARKERS:
        marker_upper = marker.upper()
        if upper == marker_upper or upper.startswith(marker_upper + " "):
            return marker

    return None


def _has_product_rows_below(matrix: list[list], row: int, col: int) -> bool:
    """Check if there are product-like rows below a potential marker.

    A product row has at least one text cell and one numeric cell.
    """
    count = 0
    for r in range(row + 1, min(row + 6, len(matrix))):
        if r >= len(matrix):
            break
        cells = matrix[r]
        has_text = False
        has_number = False
        for c in cells:
            c_str = str(c).strip() if c is not None else ""
            if not c_str:
                continue
            if _is_number(c):
                has_number = True
            elif len(c_str) >= 2:
                has_text = True
        if has_text and has_number:
            count += 1
        elif _is_header_row(cells):
            continue  # Skip header rows, keep looking
        elif not any(str(c).strip() for c in cells if c is not None):
            break  # Empty row = stop
    return count >= 2


# ---------------------------------------------------------------------------
# Section detection (bottom-up)
# ---------------------------------------------------------------------------

def _find_seller_headers(
    matrix: list[list],
    marker_row: int,
    scan_col_start: int,
    scan_col_end: int,
) -> dict[str, tuple[int, int]]:
    """Find seller header names (Farik, Maks, Никита) near a marker.

    Searches rows marker_row-1, marker_row, marker_row+1 within
    the column range around the marker.
    """
    found: dict[str, tuple[int, int]] = {}
    col_start = max(0, scan_col_start - 2)
    col_end = min(scan_col_end + 10, 30)

    for r in range(max(0, marker_row - 1), min(marker_row + 2, len(matrix))):
        row = matrix[r]
        for c in range(col_start, min(col_end, len(row))):
            val = _get_cell(row, c).lower()
            if val in SELLER_NAMES:
                # Normalize to canonical name
                canonical = val
                if val in ("maks", "макс"):
                    canonical = "Maks"
                elif val in ("никита", "nikita"):
                    canonical = "Nikita"
                elif val == "farik":
                    canonical = "Farik"
                if canonical not in found:
                    found[canonical] = (r, c)

    return found


def _find_warehouse_label(
    matrix: list[list],
    marker_row: int,
    seller_cols: list[int],
) -> tuple[str, tuple[int, int]] | None:
    """Find warehouse label (LA MAKS, MIA, CHICAGO MAX, etc.) near sellers.

    Looks 1-2 rows above the marker in seller column positions.
    """
    if not seller_cols:
        return None

    min_col = min(seller_cols)
    max_col = max(seller_cols)

    for r in range(max(0, marker_row - 2), marker_row):
        row = matrix[r] if r < len(matrix) else []
        for c in range(max(0, min_col - 2), min(max_col + 3, len(row))):
            val = _get_cell(row, c)
            if not val or _is_number(val):
                continue
            val_lower = val.lower()
            # Skip seller names themselves
            if val_lower in SELLER_NAMES:
                continue
            # Check if it looks like a warehouse label (short text, not a product)
            if 2 <= len(val) <= 20 and not _is_number(val):
                return val, (r, c)

    return None


def _extract_sample_rows(
    matrix: list[list],
    marker_row: int,
    col_start: int,
    col_end: int,
) -> tuple[list[list], list[int]]:
    """Extract sample product rows below a marker for LLM hints.

    Skips header rows, collects up to _SAMPLE_ROWS product rows.
    """
    samples = []
    indices = []
    col_end_safe = min(col_end + 4, 30)  # Extra cols for context

    for r in range(marker_row + 1, min(marker_row + 20, len(matrix))):
        row = matrix[r] if r < len(matrix) else []

        # Extract sub-row
        subrow = []
        for c in range(max(0, col_start - 2), col_end_safe):
            subrow.append(_get_cell(row, c) if c < len(row) else "")

        # Skip empty rows
        if not any(v for v in subrow):
            break

        # Skip header rows
        if _is_header_row(subrow):
            continue

        samples.append(subrow)
        indices.append(r)

        if len(samples) >= _SAMPLE_ROWS:
            break

    return samples, indices


def detect_sections(matrix: list[list]) -> list[DetectedSection]:
    """Scan matrix BOTTOM-TO-UP to find all section markers.

    First found (from bottom) = current/active data.
    Markers that appear multiple times: only the bottom-most is kept.

    Also detects "unknown" sections via the pattern:
    text cell → nearby Farik/Maks/Никита → product rows below.
    """
    if not matrix:
        return []

    found_markers: dict[str, DetectedSection] = {}

    # Scan bottom-to-top for known markers
    for row_idx in range(len(matrix) - 1, -1, -1):
        row = matrix[row_idx]
        for col_idx in range(len(row)):
            cell = _get_cell(row, col_idx)
            if not cell:
                continue

            marker = _match_known_marker(cell)
            if marker is None:
                continue

            # Already found this marker (from a lower row) — skip
            if marker in found_markers:
                continue

            # Skip if a more/less specific variant already found
            # e.g., skip "KZ TEREA" if "KZ TEREA KZ" already exists
            already_covered = False
            for existing in found_markers:
                if existing.startswith(marker) or marker.startswith(existing):
                    already_covered = True
                    break
            if already_covered:
                continue

            # Verify it has product rows below
            if not _has_product_rows_below(matrix, row_idx, col_idx):
                continue

            # Find seller headers nearby
            sellers = _find_seller_headers(matrix, row_idx, col_idx, col_idx + 10)

            # Find warehouse label
            seller_cols = [c for _, c in sellers.values()]
            wh_label = _find_warehouse_label(matrix, row_idx, seller_cols)

            # Extract sample rows
            samples, sample_indices = _extract_sample_rows(
                matrix, row_idx, col_idx, col_idx + 10,
            )

            section = DetectedSection(
                marker_text=cell,
                marker_row=row_idx,
                marker_col=col_idx,
                seller_headers=sellers,
                warehouse_label=wh_label[0] if wh_label else None,
                warehouse_label_pos=wh_label[1] if wh_label else None,
                sample_rows=samples,
                sample_row_indices=sample_indices,
            )

            found_markers[marker] = section
            logger.debug(
                "Detected section '%s' at row=%d col=%d (sellers=%s)",
                marker, row_idx, col_idx, list(sellers.keys()),
            )

    return list(found_markers.values())


def detect_prefix_sections(
    matrix: list[list],
    marker_sections: list[DetectedSection],
) -> list[DetectedSection]:
    """Detect ONE/STND/PRIME sections (no standalone marker row).

    Scans bottom-to-up for rows with product names starting with known
    prefixes ("ONE ", "STND ", "PRIME "). Groups by prefix.
    """
    if not matrix:
        return []

    # Determine columns already used by marker sections
    marker_cols = set()
    for sec in marker_sections:
        for c in range(max(0, sec.marker_col - 2), sec.marker_col + 12):
            marker_cols.add(c)

    found_prefixes: dict[str, DetectedSection] = {}

    for row_idx in range(len(matrix) - 1, -1, -1):
        row = matrix[row_idx]
        for col_idx in range(len(row)):
            cell = _get_cell(row, col_idx)
            if not cell or _is_number(cell):
                continue

            # Check if cell starts with a known prefix
            cell_upper = cell.upper()
            matched_prefix = None
            for prefix in PREFIX_CATEGORIES:
                if cell_upper.startswith(prefix + " "):
                    matched_prefix = prefix
                    break

            if matched_prefix is None:
                continue

            # Already found this prefix — skip (bottom-up = first is freshest)
            if matched_prefix in found_prefixes:
                continue

            # Find seller headers nearby
            sellers = _find_seller_headers(matrix, row_idx, col_idx, col_idx + 10)

            # Collect a few sample rows around this one
            # Look for the "block start" — go up until we find a header or empty
            block_start = row_idx
            for r in range(row_idx - 1, max(row_idx - 15, -1), -1):
                r_row = matrix[r] if r < len(matrix) else []
                r_cell = _get_cell(r_row, col_idx)
                if not r_cell:
                    break
                r_upper = r_cell.upper()
                if any(r_upper.startswith(p + " ") for p in PREFIX_CATEGORIES):
                    block_start = r
                elif _is_header_row(r_row):
                    break
                else:
                    break

            # Collect samples from block_start
            samples = []
            sample_indices = []
            for r in range(block_start, min(block_start + 10, len(matrix))):
                r_row = matrix[r] if r < len(matrix) else []
                r_cell = _get_cell(r_row, col_idx)
                if not r_cell:
                    break
                r_upper = r_cell.upper()
                if any(r_upper.startswith(p + " ") for p in PREFIX_CATEGORIES):
                    subrow = []
                    for c in range(max(0, col_idx - 2), min(col_idx + 12, 30)):
                        subrow.append(_get_cell(r_row, c) if c < len(r_row) else "")
                    samples.append(subrow)
                    sample_indices.append(r)

            if not samples:
                continue

            # If no sellers found for this prefix, look at the nearest marker section's sellers
            if not sellers and marker_sections:
                # Find closest marker section by column distance
                closest = min(
                    marker_sections,
                    key=lambda s: abs(s.marker_col - col_idx),
                )
                # Only borrow sellers if in same general area
                if abs(closest.marker_col - col_idx) < 15:
                    sellers = closest.seller_headers

            found_prefixes[matched_prefix] = DetectedSection(
                marker_text=matched_prefix,
                marker_row=block_start,
                marker_col=col_idx,
                seller_headers=sellers,
                sample_rows=samples,
                sample_row_indices=sample_indices,
                is_prefix=True,
            )

            logger.debug(
                "Detected prefix section '%s' at row=%d col=%d (%d samples)",
                matched_prefix, block_start, col_idx, len(samples),
            )

    return list(found_prefixes.values())


# ---------------------------------------------------------------------------
# Structure Hints builder (for LLM)
# ---------------------------------------------------------------------------

def build_structure_hints(
    warehouse_name: str,
    sheet_name: str,
    matrix: list[list],
) -> str:
    """Build a compact text description of detected structure for LLM.

    Returns a text string (~1-2K tokens) with all detected sections,
    their coordinates, seller headers, and sample product rows.
    """
    marker_sections = detect_sections(matrix)
    prefix_sections = detect_prefix_sections(matrix, marker_sections)
    all_sections = marker_sections + prefix_sections

    if not all_sections:
        logger.warning("No sections detected in matrix for %s", warehouse_name)
        return ""

    lines = [
        f"Warehouse: {warehouse_name}",
        f"Sheet: {sheet_name}",
        f"Matrix size: {len(matrix)} rows x {max(len(r) for r in matrix) if matrix else 0} cols",
        "",
    ]

    for sec in all_sections:
        sec_type = "Prefix Section" if sec.is_prefix else "Section"
        lines.append(f'=== Detected {sec_type}: "{sec.marker_text}" ===')
        lines.append(f"Marker at row={sec.marker_row}, col={sec.marker_col}")

        if sec.seller_headers:
            headers_str = ", ".join(
                f"{name}({r},{c})" for name, (r, c) in sec.seller_headers.items()
            )
            lines.append(f"Nearby seller headers: {headers_str}")

        if sec.warehouse_label:
            r, c = sec.warehouse_label_pos
            lines.append(f'Label above sellers: "{sec.warehouse_label}" at ({r},{c})')

        if sec.sample_rows:
            # Show absolute column indices for clarity
            abs_col_start = max(0, sec.marker_col - 2)
            lines.append(f"Sample rows below marker (absolute column indices):")
            for idx, sample in zip(sec.sample_row_indices, sec.sample_rows):
                # Format as col{N}=value for non-empty cells
                parts = []
                for i, v in enumerate(sample):
                    if v:
                        abs_col = abs_col_start + i
                        parts.append(f"col{abs_col}={v!r}")
                lines.append(f"  Row {idx}: {', '.join(parts) if parts else '(empty)'}")

        lines.append("")

    result = "\n".join(lines)

    logger.info(
        "Built structure hints for %s: %d marker sections, %d prefix sections, %d chars",
        warehouse_name, len(marker_sections), len(prefix_sections), len(result),
    )

    return result
