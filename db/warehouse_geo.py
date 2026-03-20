"""
Warehouse geographic routing.

Maps US client addresses to warehouse priority lists based on proximity.
"""

import logging
import re

logger = logging.getLogger(__name__)

# ── State code → nearest warehouse (all 50 states + DC) ──────────────

STATE_TO_WAREHOUSE: dict[str, str] = {
    # West Coast / Mountain → LA_MAKS
    "CA": "LA_MAKS", "NV": "LA_MAKS", "AZ": "LA_MAKS", "OR": "LA_MAKS",
    "WA": "LA_MAKS", "UT": "LA_MAKS", "CO": "LA_MAKS", "NM": "LA_MAKS",
    "ID": "LA_MAKS", "MT": "LA_MAKS", "WY": "LA_MAKS", "HI": "LA_MAKS",
    "AK": "LA_MAKS",
    # Midwest / Great Plains → CHICAGO_MAX
    "IL": "CHICAGO_MAX", "IN": "CHICAGO_MAX", "WI": "CHICAGO_MAX",
    "MI": "CHICAGO_MAX", "OH": "CHICAGO_MAX", "MN": "CHICAGO_MAX",
    "IA": "CHICAGO_MAX", "MO": "CHICAGO_MAX", "KS": "CHICAGO_MAX",
    "NE": "CHICAGO_MAX", "SD": "CHICAGO_MAX", "ND": "CHICAGO_MAX",
    "KY": "CHICAGO_MAX", "WV": "CHICAGO_MAX",
    # Northeast → CHICAGO_MAX (closer than Miami)
    "NY": "CHICAGO_MAX", "PA": "CHICAGO_MAX", "NJ": "CHICAGO_MAX",
    "CT": "CHICAGO_MAX", "MA": "CHICAGO_MAX", "RI": "CHICAGO_MAX",
    "VT": "CHICAGO_MAX", "NH": "CHICAGO_MAX", "ME": "CHICAGO_MAX",
    "DE": "CHICAGO_MAX", "MD": "CHICAGO_MAX", "DC": "CHICAGO_MAX",
    # Southeast → MIAMI_MAKS
    "FL": "MIAMI_MAKS", "GA": "MIAMI_MAKS", "AL": "MIAMI_MAKS",
    "SC": "MIAMI_MAKS", "NC": "MIAMI_MAKS", "TN": "MIAMI_MAKS",
    "MS": "MIAMI_MAKS", "LA": "MIAMI_MAKS", "AR": "MIAMI_MAKS",
    "VA": "MIAMI_MAKS",
    # Texas → MIAMI_MAKS (slightly closer)
    "TX": "MIAMI_MAKS", "OK": "MIAMI_MAKS",
}

# ── Proximity fallback order per home warehouse ──────────────────────

WAREHOUSE_PROXIMITY: dict[str, list[str]] = {
    "LA_MAKS":     ["LA_MAKS", "CHICAGO_MAX", "MIAMI_MAKS"],
    "CHICAGO_MAX": ["CHICAGO_MAX", "MIAMI_MAKS", "LA_MAKS"],
    "MIAMI_MAKS":  ["MIAMI_MAKS", "CHICAGO_MAX", "LA_MAKS"],
}

DEFAULT_FALLBACK = ["LA_MAKS", "CHICAGO_MAX", "MIAMI_MAKS"]

# ── Full name / abbreviation → 2-letter code ────────────────────────

STATE_NAME_TO_CODE: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
    "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
    # Common abbreviations found in real addresses
    "mass": "MA", "conn": "CT", "penn": "PA", "penna": "PA",
    "wash": "WA", "mich": "MI", "minn": "MN", "wisc": "WI",
    "tenn": "TN", "miss": "MS", "okla": "OK", "ore": "OR",
    "colo": "CO", "calif": "CA", "ariz": "AZ", "nev": "NV",
    "nebr": "NE", "kans": "KS",
    "ala": "AL", "ark": "AR", "fla": "FL", "ind": "IN",
    "mont": "MT", "wyo": "WY",
}


def _extract_state_code(text: str) -> str | None:
    """Extract a 2-letter US state code from address text.

    Strategies (tried in order):
    1. "City, ST 12345" — comma + 2-letter code + ZIP
    2. "City, Full Name 12345" — comma + full state name + ZIP
    3. "City ST 12345" — no comma, 2-letter code before ZIP
    4. "City, Full Name" — comma + full state name, no ZIP
    5. "City, ST" — comma + 2-letter code at end, no ZIP
    6. Word before ZIP without comma — "Freedom PA 15042"
    7. "City ST" — 2-letter code at end, no ZIP, no comma
    """
    if not text or not text.strip():
        return None

    t = text.strip()

    # Strategy 1: ", ST 12345"
    m = re.search(r",\s*([A-Z]{2})\s+\d{5}", t)
    if m:
        code = m.group(1).upper()
        if code in STATE_TO_WAREHOUSE:
            return code

    # Strategy 2: ", Full Name 12345" or ", Abbreviation 12345"
    m = re.search(r",\s*([A-Za-z][A-Za-z ]+?)\s+\d{5}", t)
    if m:
        name = m.group(1).strip().lower()
        code = STATE_NAME_TO_CODE.get(name)
        if code:
            return code

    # Strategy 3: no comma — "Word 12345" where Word is 2 letters
    m = re.search(r"\b([A-Z]{2})\s+\d{5}", t)
    if m:
        code = m.group(1).upper()
        if code in STATE_TO_WAREHOUSE:
            return code

    # Strategy 4: ", Full Name" at end (no ZIP)
    m = re.search(r",\s*([A-Za-z][A-Za-z ]+?)\s*$", t)
    if m:
        name = m.group(1).strip().lower()
        code = STATE_NAME_TO_CODE.get(name)
        if code:
            return code

    # Strategy 5: ", ST" at end (no ZIP) — e.g. "Miami, FL"
    m = re.search(r",\s*([A-Z]{2})\s*$", t)
    if m:
        code = m.group(1).upper()
        if code in STATE_TO_WAREHOUSE:
            return code

    # Strategy 6: word before ZIP, no comma — "Freedom PA 15042-1960"
    m = re.search(r"\b([A-Za-z]+)\s+\d{5}", t)
    if m:
        word = m.group(1)
        if len(word) == 2:
            code = word.upper()
            if code in STATE_TO_WAREHOUSE:
                return code
        code = STATE_NAME_TO_CODE.get(word.lower())
        if code:
            return code

    # Strategy 7: "City ST" at end, no ZIP, no comma — e.g. "Houston TX"
    m = re.search(r"\b([A-Z]{2})\s*$", t)
    if m:
        code = m.group(1).upper()
        if code in STATE_TO_WAREHOUSE:
            return code

    return None


def resolve_warehouse_from_address(
    city_state_zip: str,
    active_warehouses: list[str] | None = None,
) -> list[str]:
    """Return warehouse priority list based on client address.

    Args:
        city_state_zip: Client address string (e.g. "Roseville, CA 95747").
        active_warehouses: If provided, filter output to only include these
            warehouses. Static mappings are preserved — filtering happens
            at output time only.

    Returns:
        Ordered list of warehouse names, closest first.
        Falls back to DEFAULT_FALLBACK if address can't be parsed.
    """
    state_code = _extract_state_code(city_state_zip)
    if not state_code:
        logger.warning(
            "Could not parse state from address '%s', using default fallback",
            city_state_zip,
        )
        result = list(DEFAULT_FALLBACK)
        return _filter_active(result, active_warehouses)

    home_wh = STATE_TO_WAREHOUSE.get(state_code)
    if not home_wh:
        logger.warning(
            "State code %s not mapped to warehouse, using default fallback",
            state_code,
        )
        result = list(DEFAULT_FALLBACK)
        return _filter_active(result, active_warehouses)

    priority = WAREHOUSE_PROXIMITY.get(home_wh, DEFAULT_FALLBACK)
    logger.debug(
        "Address '%s' → state=%s → warehouse priority=%s",
        city_state_zip, state_code, priority,
    )
    result = list(priority)
    return _filter_active(result, active_warehouses)


def _filter_active(
    priority: list[str],
    active_warehouses: list[str] | None,
) -> list[str]:
    """Filter priority list to only include active warehouses."""
    if active_warehouses is None:
        return priority
    active_set = set(active_warehouses)
    filtered = [w for w in priority if w in active_set]
    if not filtered and priority:
        logger.warning(
            "All warehouses filtered out from priority %s (active=%s)",
            priority, active_warehouses,
        )
    return filtered
