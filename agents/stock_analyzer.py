"""
Stock Structure Analyzer
------------------------

LLM agent that analyzes spreadsheet structure from Python reconnaissance hints.
Called once per day per warehouse, or when parsing validation fails.

Usage:
    from agents.stock_analyzer import analyze_structure
    config = analyze_structure("LA_MAKS", "1g7jy...", "LA MAKS FEB", matrix)
"""

import json
import logging
import re
from datetime import datetime

from agno.agent import Agent
from agno.models.openai import OpenAIResponses

from tools.structure_analyzer import (
    SectionConfig,
    SheetStructureConfig,
    build_structure_hints,
)

logger = logging.getLogger(__name__)


_INSTRUCTIONS = """\
You are a spreadsheet structure analyzer for a tobacco inventory tracking system.

You receive "Structure Hints" — detected sections from a Google Sheets inventory table.
Your job: determine the exact column layout for each section.

## What you receive

Each section in the hints has:
- A marker (text label like "KZ TEREA KZ", "ARMENIA")
- Seller header positions: Farik(row,col), Maks(row,col), Nikita(row,col)
- Sample product rows with absolute column indices: col2='Amber', col3=36, etc.

## STRICT RULES for column identification

All column indices are 0-based absolute. Use them directly from the hints.

1. **name_col** — the column with TEXT (not numbers) in sample rows.
   Look for values like 'Amber', 'Silver', 'T Mint', 'ONE Red'.

2. **maks_col** — MUST equal the EXACT column index from the Maks/Макс seller header.
   If hints say Maks(86, 5) → maks_col=5. No exceptions. If no Maks header → null.

3. **remainder_col** — the LAST numeric column in product rows that is NOT a seller column.
   MUST NOT equal any seller header column (Farik, Maks, or Nikita).
   This is the remaining stock after all seller sales are subtracted.

4. **col_start** / **col_end** — zone boundaries (exclusive end).
   IMPORTANT: Keep zones tight around the actual data. A section typically spans 7-9 columns.
   col_start = marker column or first data column.
   col_end = last data column + 1.
   Do NOT extend col_end far beyond the last number in sample rows.

## How to determine remainder_col

Look at the sample rows. Find all numeric columns. Cross-reference with seller headers:
- Column matching Farik header → Farik sales (skip)
- Column matching Maks header → Maks sales (skip)
- Column matching Nikita header → Nikita sales (skip)
- The remaining numeric column that is AFTER all seller columns → remainder_col

Example:
  Seller headers: Farik(86,3), Maks(86,5), Nikita(86,7)
  Sample: col0=25, col2='Amber', col3=36, col5=3, col7=32, col8=1
  → Seller columns: 3, 5, 7
  → col8=1 is NOT a seller column and is the LAST number → remainder_col=8
  → name_col=2 (text), maks_col=5 (from header)

## Section types

- **"marker"** — identified by a text marker row. Set prefix to null.
- **"prefix"** — identified by product name prefix (e.g., "ONE Red" → prefix "ONE").
  Set prefix to "ONE", "STND", or "PRIME".

## Section name rules

- UPPERCASE with underscores: "KZ_TEREA", "TEREA_JAPAN", "TEREA_EUROPE", "ARMENIA"
- УНИКАЛЬНАЯ ТЕРЕА → "УНИКАЛЬНАЯ_ТЕРЕА"
- INDONESIA → "INDONESIA", KZ HEETS → "KZ_HEETS"
- Prefix sections: use the prefix ("ONE", "STND", "PRIME")

## Response format

Return ONLY a JSON object (no markdown, no explanation, no code fences):

{"sections": [{"name": "KZ_TEREA", "marker_text": "KZ TEREA KZ", "type": "marker", "prefix": null, "col_start": 0, "col_end": 9, "name_col": 2, "remainder_col": 8, "maks_col": 5}]}
"""


def analyze_structure(
    warehouse_name: str,
    spreadsheet_id: str,
    sheet_name: str,
    matrix: list[list],
) -> SheetStructureConfig | None:
    """Run LLM structure analysis on a sheet matrix.

    Args:
        warehouse_name: Warehouse identifier (e.g., "LA_MAKS").
        spreadsheet_id: Google Sheets spreadsheet ID.
        sheet_name: Active sheet/tab name.
        matrix: 2D list of cell values from Sheets API.

    Returns:
        SheetStructureConfig on success, None on failure.
    """
    hints = build_structure_hints(warehouse_name, sheet_name, matrix)

    if not hints.strip():
        logger.warning("No structure hints for %s — empty matrix?", warehouse_name)
        return None

    prompt = (
        "Analyze the following spreadsheet structure and return the column configuration.\n\n"
        f"{hints}\n\n"
        "Return ONLY the JSON object with \"sections\" array. No markdown, no explanation."
    )

    agent = Agent(
        id="stock-structure-analyzer",
        name="Stock Structure Analyzer",
        model=OpenAIResponses(id="gpt-5.2"),
        instructions=_INSTRUCTIONS,
        markdown=False,
    )

    try:
        response = agent.run(prompt)
        raw = response.content.strip()

        # Strip markdown code fences if present
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        raw = raw.strip()

        data = json.loads(raw)

        # Validate sections via Pydantic, skip invalid ones
        sections = []
        for s in data.get("sections", []):
            # Fix common LLM issues: null for required fields
            if s.get("marker_text") is None:
                s["marker_text"] = s.get("name", "")
            if s.get("name_col") is None:
                logger.warning("Skipping section '%s': name_col is null", s.get("name"))
                continue
            if s.get("col_start") is None or s.get("col_end") is None:
                logger.warning("Skipping section '%s': col boundaries are null", s.get("name"))
                continue
            try:
                sections.append(SectionConfig(**s))
            except Exception as e:
                logger.warning("Skipping invalid section '%s': %s", s.get("name"), e)
                continue

        # Deduplicate by section name (keep first occurrence)
        seen_names: set[str] = set()
        unique_sections: list[SectionConfig] = []
        for s in sections:
            if s.name in seen_names:
                logger.warning("Duplicate section name '%s' — skipping (marker='%s')", s.name, s.marker_text)
                continue
            seen_names.add(s.name)
            unique_sections.append(s)
        sections = unique_sections

        if not sections:
            logger.warning("LLM returned no valid sections for %s", warehouse_name)
            return None

        config = SheetStructureConfig(
            warehouse=warehouse_name,
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            sections=sections,
            analyzed_at=datetime.utcnow(),
        )

        for s in sections:
            logger.info(
                "  Section '%s': type=%s, cols=%d-%d, name_col=%d, remainder_col=%s, maks_col=%s",
                s.name, s.type, s.col_start, s.col_end, s.name_col,
                s.remainder_col, s.maks_col,
            )

        logger.info(
            "Structure analysis complete for %s: %d sections (%s)",
            warehouse_name,
            len(sections),
            ", ".join(s.name for s in sections),
        )
        return config

    except json.JSONDecodeError as e:
        logger.error("LLM returned invalid JSON for %s: %s\nRaw: %s", warehouse_name, e, raw[:500])
        return None
    except Exception as e:
        logger.error("Structure analysis failed for %s: %s", warehouse_name, e, exc_info=True)
        return None
