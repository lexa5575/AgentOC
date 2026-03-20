"""Tests for OOS agreement region override logic."""

import pytest

from agents.handlers.oos_agreement import (
    _build_confirmed_item,
    _match_alternative_from_text,
    _resolve_oos_agreement,
)
from db.region_family import extract_region_from_text, get_family_suffix


# ── Region helpers ───────────────────────────────────────────────────

class TestExtractRegionFromText:
    def test_eu_long(self):
        assert extract_region_from_text("2X Tera Blue made in Europe") == "EU"

    def test_eu_suffix_short(self):
        assert extract_region_from_text("Blue EU please") == "EU"

    def test_eu_prefix(self):
        assert extract_region_from_text("EU Blue") == "EU"

    def test_eu_long_sentence(self):
        assert extract_region_from_text("I want Blue made in Europe please") == "EU"

    def test_me_suffix(self):
        assert extract_region_from_text("Blue ME") == "ME"

    def test_me_long(self):
        assert extract_region_from_text("Silver made in Middle East") == "ME"

    def test_japan(self):
        assert extract_region_from_text("Smooth Japan") == "JAPAN"

    def test_european(self):
        assert extract_region_from_text("I want European Blue") == "EU"

    def test_none(self):
        assert extract_region_from_text("yes Blue please") is None

    def test_plain_flavor(self):
        assert extract_region_from_text("Blue") is None


class TestGetFamilySuffix:
    def test_eu(self):
        assert get_family_suffix("EU") == "EU"

    def test_japan(self):
        assert get_family_suffix("JAPAN") == "Japan"

    def test_me(self):
        assert get_family_suffix("ME") == "ME"

    def test_unknown(self):
        assert get_family_suffix("UNKNOWN") is None


# ── Match alternative from text ──────────────────────────────────────

class TestMatchAlternativeFromText:
    def test_single_match(self):
        alts = [
            {"product_name": "BLUE", "category": "KZ_TEREA"},
            {"product_name": "Silver", "category": "ARMENIA"},
        ]
        result = _match_alternative_from_text("I want Blue please", alts)
        assert result is not None
        assert result["product_name"] == "BLUE"

    def test_no_match(self):
        alts = [{"product_name": "BLUE", "category": "KZ_TEREA"}]
        result = _match_alternative_from_text("I want Amber please", alts)
        assert result is None

    def test_multi_match_no_region_returns_none(self):
        """Two alts with same base name, no region → can't disambiguate."""
        alts = [
            {"product_name": "BLUE", "category": "KZ_TEREA"},
            {"product_name": "BLUE", "category": "TEREA_EUROPE"},
        ]
        result = _match_alternative_from_text("I want Blue", alts)
        assert result is None

    def test_multi_match_region_disambiguates(self):
        """Two alts same name + customer says 'EU' → picks EU alt."""
        alts = [
            {"product_name": "BLUE", "category": "KZ_TEREA"},
            {"product_name": "BLUE", "category": "TEREA_EUROPE"},
        ]
        result = _match_alternative_from_text(
            "I want Blue", alts, customer_region="EU",
        )
        assert result is not None
        assert result["category"] == "TEREA_EUROPE"


# ── Build confirmed item ─────────────────────────────────────────────

class TestBuildConfirmedItem:
    def test_customer_region_overrides(self):
        alt = {"product_name": "BLUE", "category": "KZ_TEREA"}
        item = _build_confirmed_item(alt, 2, "Blue made in Europe", "EU")
        assert item["product_name"] == "BLUE EU"
        assert item["region_preference"] == ["EU"]
        assert item["quantity"] == 2

    def test_no_region_uses_suggestion(self):
        alt = {"product_name": "BLUE", "category": "KZ_TEREA"}
        item = _build_confirmed_item(alt, 2, "yes Blue please", None)
        assert item["product_name"] == "BLUE ME"
        assert "region_preference" not in item

    def test_japan_region(self):
        alt = {"product_name": "Smooth", "category": "TEREA_JAPAN"}
        item = _build_confirmed_item(alt, 1, "Smooth made in Japan", "JAPAN")
        assert item["product_name"] == "Smooth Japan"
        assert item["region_preference"] == ["JAPAN"]


# ── Resolve OOS agreement ────────────────────────────────────────────

class TestResolveOOSAgreement:
    def _make_result(self, alts, in_stock=None):
        """Build a result dict with pending_oos_resolution state."""
        return {
            "conversation_state": {
                "facts": {
                    "pending_oos_resolution": {
                        "in_stock_items": in_stock or [],
                        "items": [{
                            "base_flavor": "Oasis",
                            "available_qty": 0,
                            "requested_qty": 2,
                        }],
                        "alternatives": {
                            "Oasis": {"alternatives": alts},
                        },
                    }
                }
            }
        }

    def test_single_alt_customer_region_override(self):
        """Single KZ alt + customer says 'made in Europe' → EU suffix + region_preference."""
        result = self._make_result([
            {"product_name": "BLUE", "category": "KZ_TEREA"},
        ])
        confirmed, status = _resolve_oos_agreement(
            result, "I'll do 2X Tera Blue made in Europe please",
        )
        assert status == "ok"
        assert len(confirmed) == 1
        assert confirmed[0]["product_name"] == "BLUE EU"
        assert confirmed[0]["region_preference"] == ["EU"]

    def test_multi_alt_customer_region_override(self):
        """Multiple alts + customer says 'made in Europe' → EU matched."""
        result = self._make_result([
            {"product_name": "BLUE", "category": "KZ_TEREA"},
            {"product_name": "Silver", "category": "ARMENIA"},
        ])
        confirmed, status = _resolve_oos_agreement(
            result, "I'll take Blue made in Europe",
        )
        assert status == "ok"
        assert confirmed[0]["product_name"] == "BLUE EU"
        assert confirmed[0]["region_preference"] == ["EU"]

    def test_no_region_uses_suggestion_category(self):
        """No explicit region → uses suggestion's category suffix."""
        result = self._make_result([
            {"product_name": "BLUE", "category": "KZ_TEREA"},
        ])
        confirmed, status = _resolve_oos_agreement(result, "yes Blue please")
        assert status == "ok"
        assert confirmed[0]["product_name"] == "BLUE ME"
        assert "region_preference" not in confirmed[0]

    def test_no_data(self):
        confirmed, status = _resolve_oos_agreement({}, "text")
        assert status == "no_data"

    def test_no_alternatives(self):
        result = self._make_result([])
        confirmed, status = _resolve_oos_agreement(result, "Blue")
        assert status == "no_alternatives"
