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

## Standard column layout

Each section follows this pattern (column order may vary between warehouses):

  ARRIVED | [gap?] | PRODUCT_NAME | TOTAL | [gap?] | FARIK_SALES | MAKS_SALES | NIKITA_SALES | REMAINDER

The formula is: ARRIVED - FARIK - MAKS - NIKITA = REMAINDER
(TOTAL may or may not equal ARRIVED; ignore TOTAL for column identification.)

## How to identify columns — STRICT RULES

1. **name_col** — the column with TEXT (not numbers) in sample rows.
   - Look for values like 'Amber', 'Silver', 'T Mint', 'ONE Red'.
   - This is the ONLY column with text strings in product rows.

2. **maks_col** — MUST be the EXACT same column index as the "Maks" or "Макс" seller header.
   - If hints say Maks(86, 5) → maks_col=5. No exceptions.
   - If no Maks header found → set to null.

3. **remainder_col** — the column with the remaining stock after all seller sales.
   - CRITICAL: remainder_col MUST NOT equal any seller header column (Farik, Maks, or Nikita).
   - The remainder is the number left AFTER subtracting all seller sales from arrived quantity.

4. **col_start** / **col_end** — zone boundaries.
   - col_start = leftmost column used by this section.
   - col_end = rightmost column used + 1 (exclusive).

## Verification step (MANDATORY)

For EACH section, verify your column assignments using the arithmetic formula on sample rows:

  ARRIVED - value_at_farik_col - value_at_maks_col - value_at_nikita_col ≈ value_at_remainder_col

If the math doesn't add up, your column assignments are WRONG. Re-examine and fix them.

Example verification:
  Seller headers: Farik(86,3), Maks(86,5), Nikita(86,7)
  Sample: col0=25, col2='Amber', col3=36, col5=3, col7=32, col8=1

  → name_col=2 (text), maks_col=5 (Maks header), farik is col3, nikita is col7
  → Remaining candidate: col8 (not a seller column)
  → Verify: col0(ARRIVED)=25, col3(Farik)=36... wait, 36 > 25?
  → col3=36 is probably TOTAL, not Farik. Farik must be at col3? No — Farik header is at col3.
  → Re-check: ARRIVED might be col0=25 or col1 (empty). Actually col3=36 could be TOTAL.
  → The formula: 25 - (some Farik value) - 3 - 32 wouldn't work either.
  → Better: maybe ARRIVED is not shown, or TOTAL=36 and the formula uses TOTAL.
  → Key point: seller columns MUST match their header positions exactly.

When in doubt: trust the seller header positions over arithmetic guessing.

## Two common column orders

**Pattern A** (e.g., some warehouses):
  Name → Total → Farik → Maks → Nikita → Remainder

**Pattern B** (e.g., other warehouses):
  Name → Total → Farik → Maks → Remainder → Nikita

The seller header positions in the hints tell you which pattern this section uses.
Do NOT assume all sections in the same warehouse follow the same pattern.

## Section types

- **"marker"** — identified by a text marker row. Set prefix to null.
- **"prefix"** — identified by product name prefix (e.g., "ONE Red" → prefix "ONE").
  Set prefix to "ONE", "STND", or "PRIME".

## Section name rules

- UPPERCASE with underscores: "KZ_TEREA", "TEREA_JAPAN", "TEREA_EUROPE", "ARMENIA"
- УНИКАЛЬНАЯ ТЕРЕА → "УНИКАЛЬНАЯ_ТЕРЕА"
- INDONESIA → "INDONESIA"
- KZ HEETS → "KZ_HEETS"
- Prefix sections: use the prefix itself ("ONE", "STND", "PRIME")

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
