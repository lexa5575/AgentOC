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

You receive "Structure Hints" — a compact description of detected sections
in a Google Sheets inventory table. Each section has:
- A marker (text label like "KZ TEREA KZ", "ARMENIA", "INDONESIA")
- Nearby seller header positions (Farik, Maks, Никита)
- Sample product rows below the marker

Your job: determine the column layout for each section.

## Column types

For each section, identify these columns (0-based absolute indices):

1. **name_col** — column containing product names (text like "Amber", "Silver", "T Mint")
   - This is the column with text (not numbers) in sample rows.
   - For prefix sections (ONE/STND/PRIME), names include the prefix: "ONE Red", "STND Black".

2. **remainder_col** — column containing remaining stock quantity
   - Usually the LAST column with numbers in each product row.
   - Represents: ARRIVED - Farik - Maks - Nikita = remainder.
   - If you can clearly identify it, set it. Otherwise set to null.

3. **maks_col** — column containing Maks sales data
   - This is the column directly under the "Maks" or "Макс" seller header.
   - Use the seller_headers positions from hints.
   - If no Maks header found, set to null.

4. **col_start** / **col_end** — zone boundaries
   - col_start = leftmost column used by this section (marker col or 2 cols before first data).
   - col_end = rightmost column used + 1 (exclusive).

## Section types

- **"marker"** — section identified by a text marker row (e.g., "KZ TEREA KZ", "ARMENIA").
  Set prefix to null.
- **"prefix"** — section identified by product name prefix (e.g., "ONE Red" → section "ONE").
  Set prefix to the prefix string (e.g., "ONE", "STND", "PRIME").

## Section name rules

- Use UPPERCASE with underscores: "KZ_TEREA", "TEREA_JAPAN", "TEREA_EUROPE", "ARMENIA"
- For УНИКАЛЬНАЯ ТЕРЕА: use "УНИКАЛЬНАЯ_ТЕРЕА"
- For INDONESIA: use "INDONESIA"
- For KZ HEETS: use "KZ_HEETS"
- For prefix sections: use the prefix itself ("ONE", "STND", "PRIME")

## How to determine columns from sample rows

Look at the sample rows. Example:
  Row 90: ['25', '', 'Amber', '36', '', '3', '', '32', '1']

- '25' at position 0 = ARRIVED number
- '' at position 1 = empty
- 'Amber' at position 2 = product name → name_col
- '36' at position 3 = total quantity
- '' at position 4 = empty
- '3' at position 5 = Farik sales (check seller headers)
- '' at position 6 = empty
- '32' at position 7 = Maks sales (check seller headers) → maks_col
- '1' at position 8 = Nikita or remainder

Cross-reference with seller header positions to confirm which column is Maks.
The remainder is typically the last number in the row, after all seller columns.

IMPORTANT: The sample row indices shown (like "Row 90") are just for context.
The column positions in the sample arrays start from col_start-2 (shown in hints).
You must map sample array positions back to absolute column indices.

## Response format

Return ONLY a JSON object:
{
  "sections": [
    {
      "name": "KZ_TEREA",
      "marker_text": "KZ TEREA KZ",
      "type": "marker",
      "prefix": null,
      "col_start": 0,
      "col_end": 9,
      "name_col": 2,
      "remainder_col": 8,
      "maks_col": 6
    }
  ]
}

No markdown, no explanation, no code fences. ONLY the JSON object.
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
