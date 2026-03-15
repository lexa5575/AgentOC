"""Tests for region preference resolution module (db/region_preference.py)
and OrderItem.region_preference validator (agents/models.py).

Covers:
- apply_region_preference() narrowing logic
- _family_has_warehouse_stock() per-warehouse check
- _update_region_metadata() deterministic name overwrite
- OrderItem validator normalization (string, list, dedup, garbage)
- apply_thread_hint() thread-backed canonical narrowing
"""

import json
from unittest.mock import patch, MagicMock

from agents.models import OrderItem
from db.region_preference import apply_region_preference, apply_thread_hint


# ---------------------------------------------------------------------------
# Catalog fixtures
# ---------------------------------------------------------------------------

CATALOG = [
    {"id": 18, "category": "ARMENIA", "name_norm": "turquoise", "stock_name": "T Turquoise"},
    {"id": 39, "category": "KZ_TEREA", "name_norm": "turquoise", "stock_name": "T Turquoise"},
    {"id": 71, "category": "TEREA_EUROPE", "name_norm": "turquoise", "stock_name": "T Turquoise"},
    {"id": 80, "category": "TEREA_JAPAN", "name_norm": "silver", "stock_name": "T Silver"},
]


def _item(base_flavor="Turquoise", quantity=10, product_ids=None, pref=None, strict=False):
    """Helper to build an item dict for apply_region_preference."""
    return {
        "product_name": f"Terea {base_flavor}",
        "base_flavor": base_flavor,
        "quantity": quantity,
        "original_product_name": base_flavor,
        "product_ids": product_ids or [],
        "region_preference": pref,
        "strict_region": strict,
    }


# ---------------------------------------------------------------------------
# Mock stock helper
# ---------------------------------------------------------------------------

def _mock_stock(pid_stock_map):
    """Return a mock for search_stock_by_ids using pid→[(warehouse, qty)] map.

    Example: {71: [("NY", 5)], 18: [("LA", 20), ("NY", 50)]}
    """
    def _search(product_ids):
        results = []
        for pid in product_ids:
            for wh, qty in pid_stock_map.get(pid, []):
                results.append({
                    "product_id": pid,
                    "warehouse": wh,
                    "quantity": qty,
                })
        return results
    return _search


# ===================================================================
# A. Unit tests — apply_region_preference
# ===================================================================

class TestApplyRegionPreferenceNoOp:
    """Items without region_preference pass through unchanged."""

    def test_none_preference(self):
        items = [_item(product_ids=[18, 39, 71], pref=None)]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_ids"] == [18, 39, 71]

    def test_single_pid_no_preference(self):
        items = [_item(product_ids=[71], pref=None)]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_ids"] == [71]

    def test_single_pid_with_pref_no_metadata_overwrite(self):
        """Single pid already resolved → don't overwrite metadata even with pref."""
        item = _item(product_ids=[71], pref=["EU"])
        original_name = item["product_name"]
        result = apply_region_preference([item], catalog_entries=CATALOG)
        assert result[0]["product_ids"] == [71]
        assert result[0]["product_name"] == original_name  # unchanged

    def test_same_family_no_preference(self):
        items = [_item(product_ids=[18, 39], pref=None)]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_ids"] == [18, 39]

    def test_cross_family_no_preference_unchanged(self):
        """Without preference, cross-family stays cross-family (existing ambiguous behavior)."""
        items = [_item(product_ids=[18, 39, 71], pref=None)]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_ids"] == [18, 39, 71]


class TestSoftPreferenceWithStock:
    """Soft preference: try families in order, pick first with stock."""

    @patch("db.region_preference.search_stock_by_ids")
    def test_eu_has_stock(self, mock_search):
        mock_search.side_effect = _mock_stock({71: [("NY", 20)]})
        items = [_item(product_ids=[18, 39, 71], pref=["EU", "ME"])]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_ids"] == [71]

    @patch("db.region_preference.search_stock_by_ids")
    def test_eu_oos_me_has_stock(self, mock_search):
        mock_search.side_effect = _mock_stock({
            71: [("NY", 2)],  # EU: not enough
            18: [("LA", 65)],  # ARMENIA: enough
        })
        items = [_item(product_ids=[18, 39, 71], pref=["EU", "ME"])]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        # ME family = ARMENIA + KZ_TEREA
        assert set(result[0]["product_ids"]) == {18, 39}

    @patch("db.region_preference.search_stock_by_ids")
    def test_both_oos_falls_to_first_pref(self, mock_search):
        """Both EU and ME OOS → first preferred (EU) → downstream OOS flow."""
        mock_search.side_effect = _mock_stock({})  # nothing in stock
        items = [_item(product_ids=[18, 39, 71], pref=["EU", "ME"])]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_ids"] == [71]
        # original_product_name should have EU context
        assert "EU" in result[0]["original_product_name"]

    @patch("db.region_preference.search_stock_by_ids")
    def test_preference_overrides_history(self, mock_search):
        """Explicit preference picks family even when another has more stock."""
        mock_search.side_effect = _mock_stock({
            71: [("NY", 15)],  # EU: enough
            18: [("LA", 100)],  # ME: much more but not preferred
        })
        items = [_item(product_ids=[18, 39, 71], pref=["EU"])]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_ids"] == [71]


class TestStrictPreference:
    """Strict: only first preferred family, regardless of stock."""

    def test_strict_eu_has_stock(self):
        items = [_item(product_ids=[18, 39, 71], pref=["EU"], strict=True)]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_ids"] == [71]

    def test_strict_eu_oos_no_fallback(self):
        """Strict EU OOS → product_ids=[71] → downstream OOS flow, NOT ambiguous."""
        items = [_item(product_ids=[18, 39, 71], pref=["EU"], strict=True)]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_ids"] == [71]
        assert "EU" in result[0]["original_product_name"]

    def test_strict_japan_no_pids(self):
        """Strict Japan but no Japan Turquoise → empty pids → OOS flow."""
        items = [_item(product_ids=[18, 39, 71], pref=["JAPAN"], strict=True)]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_ids"] == []
        assert "Japan" in result[0]["product_name"]


class TestMetadataUpdate:
    """_update_region_metadata deterministically overwrites names."""

    def test_display_metadata_after_narrowing(self):
        items = [_item(product_ids=[18, 39, 71], pref=["ME"], strict=True)]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert "ME" in result[0]["original_product_name"]
        assert result[0].get("display_name") is not None
        assert result[0]["display_name"] != ""

    def test_strict_eu_oos_original_name_has_suffix(self):
        items = [_item(product_ids=[18, 39, 71], pref=["EU"], strict=True)]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["original_product_name"] == "Turquoise EU"

    def test_japan_zero_pids_synthesized(self):
        items = [_item(base_flavor="Turquoise", product_ids=[18, 39, 71], pref=["JAPAN"], strict=True)]
        result = apply_region_preference(items, catalog_entries=CATALOG)
        assert result[0]["product_name"] == "Terea Turquoise Japan"
        assert result[0]["display_name"] == "Terea Turquoise Japan"
        assert result[0]["original_product_name"] == "Turquoise Japan"

    def test_non_terea_brand_preserved(self):
        """Non-Terea brand (e.g. ONE Green) keeps brand prefix in fallback."""
        item = {
            "product_name": "ONE Green",
            "base_flavor": "Green",
            "quantity": 5,
            "original_product_name": "ONE Green",
            "product_ids": [18, 39, 71],  # cross-family
            "region_preference": ["JAPAN"],
            "strict_region": True,
        }
        result = apply_region_preference([item], catalog_entries=CATALOG)
        # JAPAN has no pids → fallback synthesis from "ONE Green" + " Japan"
        assert result[0]["product_ids"] == []
        assert result[0]["product_name"] == "ONE Green Japan"
        assert "Terea" not in result[0]["product_name"]

    def test_stale_display_name_overwritten(self):
        """Old generic display_name gets overwritten after apply."""
        item = _item(product_ids=[18, 39, 71], pref=["ME"], strict=True)
        item["display_name"] = "Terea Turquoise"  # stale generic
        result = apply_region_preference([item], catalog_entries=CATALOG)
        assert result[0]["display_name"] != "Terea Turquoise"
        assert "ME" in result[0]["display_name"] or "Middle East" in result[0]["display_name"]


# ===================================================================
# B. Validator tests — OrderItem.region_preference
# ===================================================================

class TestRegionPreferenceValidator:
    """OrderItem.normalize_region_preference validator."""

    def test_lowercase_normalized(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference=["eu", "me"])
        assert oi.region_preference == ["EU", "ME"]

    def test_japan_alias(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference=["japan"])
        assert oi.region_preference == ["JAPAN"]

    def test_invalid_dropped(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference=["blah", "eu"])
        assert oi.region_preference == ["EU"]

    def test_all_invalid_becomes_none(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference=["unknown"])
        assert oi.region_preference is None

    def test_null_stays_none(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference=None)
        assert oi.region_preference is None

    def test_absent_defaults_none(self):
        oi = OrderItem(product_name="X", base_flavor="X")
        assert oi.region_preference is None

    def test_string_input_wrapped(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference="eu")
        assert oi.region_preference == ["EU"]

    def test_duplicates_removed(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference=["EU", "eu", "EU"])
        assert oi.region_preference == ["EU"]

    def test_int_input_returns_none(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference=123)
        assert oi.region_preference is None

    def test_bool_input_returns_none(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference=True)
        assert oi.region_preference is None

    def test_dict_input_returns_none(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference={"region": "EU"})
        assert oi.region_preference is None

    def test_europe_alias(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference=["europe"])
        assert oi.region_preference == ["EU"]

    def test_order_preserved(self):
        oi = OrderItem(product_name="X", base_flavor="X", region_preference=["me", "eu", "japan"])
        assert oi.region_preference == ["ME", "EU", "JAPAN"]


# ===================================================================
# C. Classifier parsing test — run_classification → OrderItem with region fields
# ===================================================================

class TestClassifierParsingRegionFields:
    """Verify that run_classification() correctly parses region_preference
    and strict_region from LLM JSON output into OrderItem fields."""

    @patch("agents.classifier.classifier_agent")
    @patch("agents.classifier.try_parse_order", return_value=None)
    @patch("agents.classifier.clean_email_body", side_effect=lambda x: x)
    def test_region_fields_parsed_from_llm_json(self, _clean, _parse, mock_agent):
        """LLM returns JSON with region_preference → OrderItem has normalized fields."""
        llm_json = json.dumps({
            "needs_reply": True,
            "situation": "new_order",
            "client_email": "test@example.com",
            "order_items": [
                {
                    "product_name": "Turquoise",
                    "base_flavor": "Turquoise",
                    "quantity": 10,
                    "region_preference": ["eu", "me"],
                    "strict_region": False,
                }
            ],
        })
        mock_agent.run.return_value = MagicMock(content=llm_json)

        from agents.classifier import run_classification
        result = run_classification("fake email", "")

        assert result.order_items is not None
        assert len(result.order_items) == 1
        oi = result.order_items[0]
        assert oi.region_preference == ["EU", "ME"]
        assert oi.strict_region is False

    @patch("agents.classifier.classifier_agent")
    @patch("agents.classifier.try_parse_order", return_value=None)
    @patch("agents.classifier.clean_email_body", side_effect=lambda x: x)
    def test_null_region_fields_parsed(self, _clean, _parse, mock_agent):
        """LLM returns null region_preference → OrderItem has None."""
        llm_json = json.dumps({
            "needs_reply": True,
            "situation": "new_order",
            "client_email": "test@example.com",
            "order_items": [
                {
                    "product_name": "Silver",
                    "base_flavor": "Silver",
                    "quantity": 5,
                    "region_preference": None,
                }
            ],
        })
        mock_agent.run.return_value = MagicMock(content=llm_json)

        from agents.classifier import run_classification
        result = run_classification("fake email", "")

        oi = result.order_items[0]
        assert oi.region_preference is None
        assert oi.strict_region is False  # default

    @patch("agents.classifier.classifier_agent")
    @patch("agents.classifier.try_parse_order", return_value=None)
    @patch("agents.classifier.clean_email_body", side_effect=lambda x: x)
    def test_missing_region_fields_use_defaults(self, _clean, _parse, mock_agent):
        """LLM omits region fields entirely → defaults (None, False)."""
        llm_json = json.dumps({
            "needs_reply": True,
            "situation": "new_order",
            "client_email": "test@example.com",
            "order_items": [
                {
                    "product_name": "Green",
                    "base_flavor": "Green",
                    "quantity": 2,
                }
            ],
        })
        mock_agent.run.return_value = MagicMock(content=llm_json)

        from agents.classifier import run_classification
        result = run_classification("fake email", "")

        oi = result.order_items[0]
        assert oi.region_preference is None
        assert oi.strict_region is False


# ===================================================================
# D. Thread hint tests — apply_thread_hint
# ===================================================================

# Cross-family catalog: Yellow in ARMENIA(16), KZ_TEREA(23), TEREA_EUROPE(69), TEREA_JAPAN(80)
THREAD_CATALOG = [
    {"id": 16, "category": "ARMENIA", "name_norm": "yellow", "stock_name": "Yellow"},
    {"id": 23, "category": "KZ_TEREA", "name_norm": "yellow", "stock_name": "Yellow"},
    {"id": 69, "category": "TEREA_EUROPE", "name_norm": "yellow", "stock_name": "Yellow"},
    {"id": 80, "category": "TEREA_JAPAN", "name_norm": "yellow", "stock_name": "Yellow"},
]

# ME-only pids (same family)
ME_ONLY_CATALOG = [
    {"id": 16, "category": "ARMENIA", "name_norm": "yellow", "stock_name": "Yellow"},
    {"id": 23, "category": "KZ_TEREA", "name_norm": "yellow", "stock_name": "Yellow"},
]


def _thread_item(base_flavor="Yellow", product_ids=None, pref=None):
    """Helper to build an item dict for apply_thread_hint."""
    return {
        "product_name": f"Terea {base_flavor}",
        "base_flavor": base_flavor,
        "quantity": 2,
        "original_product_name": base_flavor,
        "product_ids": product_ids or [16, 23, 69],
        "region_preference": pref,
    }


def _msg(body, direction="outbound"):
    return {"body": body, "direction": direction}


class TestThreadHintFullDisplay:
    """Tier 1: full catalog display name matching."""

    def test_terea_yellow_me(self):
        items = [_thread_item()]
        msgs = [_msg("Your order: 2 x Terea Yellow ME")]
        result = apply_thread_hint(items, msgs, THREAD_CATALOG)
        # Narrowed to ME family (ARMENIA + KZ_TEREA)
        assert set(result[0]["product_ids"]) == {16, 23}

    def test_terea_yellow_eu(self):
        items = [_thread_item()]
        msgs = [_msg("We have Terea Yellow EU in stock")]
        result = apply_thread_hint(items, msgs, THREAD_CATALOG)
        assert result[0]["product_ids"] == [69]

    def test_terea_yellow_made_in_japan(self):
        items = [_thread_item(product_ids=[16, 23, 69, 80])]
        msgs = [_msg("Terea Yellow made in Japan is available")]
        result = apply_thread_hint(items, msgs, THREAD_CATALOG)
        assert result[0]["product_ids"] == [80]


class TestThreadHintBroadAlias:
    """Tier 2: broad alias matching."""

    def test_yellow_middle_east(self):
        items = [_thread_item()]
        msgs = [_msg("Yellow Middle East version")]
        result = apply_thread_hint(items, msgs, THREAD_CATALOG)
        assert set(result[0]["product_ids"]) == {16, 23}


class TestThreadHintConflict:
    """Conflicting hints in same message → not narrowed."""

    def test_both_me_and_eu_in_one_msg(self):
        items = [_thread_item()]
        msgs = [_msg("Terea Yellow ME or Terea Yellow EU?")]
        result = apply_thread_hint(items, msgs, THREAD_CATALOG)
        # Not narrowed — both ME and EU in same msg
        assert set(result[0]["product_ids"]) == {16, 23, 69}


class TestThreadHintNoMatch:
    """No hints in thread → not narrowed."""

    def test_no_region_hints(self):
        items = [_thread_item()]
        msgs = [_msg("Thanks for your order!"), _msg("Payment received")]
        result = apply_thread_hint(items, msgs, THREAD_CATALOG)
        assert set(result[0]["product_ids"]) == {16, 23, 69}


class TestThreadHintSkips:
    """Items that should be skipped by apply_thread_hint."""

    def test_skip_when_region_preference_set(self):
        items = [_thread_item(pref=["EU"])]
        msgs = [_msg("2 x Terea Yellow ME")]
        result = apply_thread_hint(items, msgs, THREAD_CATALOG)
        # region_preference already set → no change
        assert set(result[0]["product_ids"]) == {16, 23, 69}

    def test_skip_single_product_id(self):
        items = [_thread_item(product_ids=[16])]
        msgs = [_msg("2 x Terea Yellow EU")]
        result = apply_thread_hint(items, msgs, THREAD_CATALOG)
        assert result[0]["product_ids"] == [16]

    def test_skip_same_family_pids(self):
        items = [_thread_item(product_ids=[16, 23])]  # both ME family
        msgs = [_msg("2 x Terea Yellow EU")]
        result = apply_thread_hint(items, msgs, ME_ONLY_CATALOG)
        assert result[0]["product_ids"] == [16, 23]


class TestThreadHintWordBoundary:
    """Word boundary prevents false matches."""

    def test_menthol_me_no_false_match(self):
        """'Terea Yellow Menthol ME' should NOT match for base_flavor='Yellow'."""
        items = [_thread_item()]
        msgs = [_msg("2 x Terea Yellow Menthol ME")]
        result = apply_thread_hint(items, msgs, THREAD_CATALOG)
        # "terea yellow me" doesn't match "terea yellow menthol me" due to word boundary
        assert set(result[0]["product_ids"]) == {16, 23, 69}


class TestThreadHintTierPrecedence:
    """Tier 1 (full display) beats Tier 3 (short) across messages."""

    def test_tier1_in_older_beats_tier3_in_newer(self):
        items = [_thread_item()]
        # Older msg has Tier 1 ME match, newer msg has Tier 3 EU match
        msgs = [
            _msg("confirmed: 2 x Terea Yellow ME"),   # older (first in list)
            _msg("yellow eu is also available"),        # newer (last in list)
        ]
        result = apply_thread_hint(items, msgs, THREAD_CATALOG)
        # Tier 1 (full display) scans globally first → ME wins
        assert set(result[0]["product_ids"]) == {16, 23}


class TestThreadHintQuotedTextStripped:
    """Quoted text is stripped before matching."""

    def test_inbound_with_quoted_eu_resolves_me(self):
        """Inbound body says 'Yellow Middle East' while quoted block has 'Terea Yellow EU'."""
        items = [_thread_item()]
        inbound_body = (
            "Yes, yellow middle east please\n\n"
            "On Mar 12, 2026 support@shop.com wrote:\n"
            "> We have Terea Yellow EU and Terea Yellow ME available"
        )
        msgs = [_msg(inbound_body, direction="inbound")]
        result = apply_thread_hint(items, msgs, THREAD_CATALOG)
        # After stripping quoted text, only "yellow middle east" remains
        # Broad alias "yellow middle east" matches ME
        assert set(result[0]["product_ids"]) == {16, 23}


class TestThreadHintPunctuationNormalization:
    """Punctuation in messages is normalized to spaces before matching."""

    def test_comma_separated_region(self):
        """'yellow, middle east please' → comma stripped → broad alias matches."""
        items = [_thread_item()]
        msgs = [_msg("yellow, middle east please")]
        result = apply_thread_hint(items, msgs, THREAD_CATALOG)
        assert set(result[0]["product_ids"]) == {16, 23}

    def test_dash_separated_short_form(self):
        """'Terea Yellow - ME' → dash stripped → short form matches."""
        items = [_thread_item()]
        msgs = [_msg("Terea Yellow - ME")]
        result = apply_thread_hint(items, msgs, THREAD_CATALOG)
        assert set(result[0]["product_ids"]) == {16, 23}

    def test_cross_flavor_region_no_false_match(self):
        """'Green european is ok, not yellow' must NOT narrow Yellow to EU."""
        items = [_thread_item()]  # Yellow item
        msgs = [_msg("Green european is ok, not yellow")]
        result = apply_thread_hint(items, msgs, THREAD_CATALOG)
        # Not narrowed — "european" is about Green, not Yellow
        assert set(result[0]["product_ids"]) == {16, 23, 69}


class TestThreadHintMetadataUpdated:
    """Metadata (product_name, display_name, original_product_name) updated after narrowing."""

    def test_metadata_updated_on_narrow(self):
        items = [_thread_item()]
        msgs = [_msg("2 x Terea Yellow ME")]
        result = apply_thread_hint(items, msgs, THREAD_CATALOG)
        assert "ME" in result[0].get("original_product_name", "")
        assert result[0].get("display_name") is not None


# ===================================================================
# E. Post-classification region inference — _infer_region_from_state
# ===================================================================

from agents.classifier import _infer_region_from_state, _parse_region_from_product_string


class TestParseRegionFromProductString:
    """Unit tests for _parse_region_from_product_string helper."""

    def test_eu_suffix(self):
        assert _parse_region_from_product_string("Terea Green EU x5") == ("EU", "Green")

    def test_me_suffix(self):
        assert _parse_region_from_product_string("Terea Silver ME x3") == ("ME", "Silver")

    def test_made_in_middle_east(self):
        assert _parse_region_from_product_string("Terea Green made in Middle East x2") == ("ME", "Green")

    def test_made_in_japan(self):
        assert _parse_region_from_product_string("Terea Purple made in Japan x1") == ("JAPAN", "Purple")

    def test_compound_flavor_eu(self):
        assert _parse_region_from_product_string("Terea Black Ruby Menthol EU x2") == ("EU", "Black Ruby Menthol")

    def test_compound_flavor_made_in_japan(self):
        assert _parse_region_from_product_string("Terea Fusion Menthol made in Japan x3") == ("JAPAN", "Fusion Menthol")

    def test_no_region(self):
        assert _parse_region_from_product_string("Silver") == (None, "Silver")

    def test_no_qty(self):
        assert _parse_region_from_product_string("Terea Bright Menthol EU") == ("EU", "Bright Menthol")


class TestInferRegionFromState:
    """Unit tests for _infer_region_from_state post-correction."""

    @staticmethod
    def _make_cls(situation, intent=None, items=None):
        """Create minimal classification-like object."""
        from agents.models import OrderItem
        order_items = [
            OrderItem(product_name=i.get("product_name", i["base_flavor"]),
                      base_flavor=i["base_flavor"], quantity=i.get("quantity", 1),
                      region_preference=i.get("region_preference"),
                      strict_region=i.get("strict_region", False))
            for i in (items or [])
        ]

        class Cls:
            pass

        c = Cls()
        c.situation = situation
        c.dialog_intent = intent
        c.order_items = order_items if order_items else None
        return c

    # --- Positive tests ---

    def test_payment_received_ordered_items_eu(self):
        cls = self._make_cls("payment_received", items=[{"base_flavor": "Green"}])
        state = {"facts": {"ordered_items": ["Terea Green EU x5"]}}
        _infer_region_from_state(cls, state)
        assert cls.order_items[0].region_preference == ["EU"]

    def test_payment_received_ordered_items_me(self):
        cls = self._make_cls("payment_received", items=[{"base_flavor": "Silver"}])
        state = {"facts": {"ordered_items": ["Terea Silver ME x3"]}}
        _infer_region_from_state(cls, state)
        assert cls.order_items[0].region_preference == ["ME"]

    def test_oos_agrees_pending_structured(self):
        cls = self._make_cls("oos_followup", "agrees_to_alternative",
                             items=[{"base_flavor": "Bright Menthol"}])
        state = {"facts": {"pending_oos_resolution": {
            "alternatives": [{"base_flavor": "Bright Menthol", "region_preference": ["EU"]}]
        }}}
        _infer_region_from_state(cls, state)
        assert cls.order_items[0].region_preference == ["EU"]

    def test_oos_agrees_offered_alternatives_string_fallback(self):
        cls = self._make_cls("oos_followup", "agrees_to_alternative",
                             items=[{"base_flavor": "Amber"}])
        state = {"facts": {"offered_alternatives": ["Terea Amber EU x3"]}}
        _infer_region_from_state(cls, state)
        assert cls.order_items[0].region_preference == ["EU"]

    def test_two_items_different_regions(self):
        cls = self._make_cls("payment_received", items=[
            {"base_flavor": "Black Ruby Menthol"},
            {"base_flavor": "Green"},
        ])
        state = {"facts": {"ordered_items": [
            "Terea Black Ruby Menthol EU x2",
            "Terea Green ME x1",
        ]}}
        _infer_region_from_state(cls, state)
        assert cls.order_items[0].region_preference == ["EU"]
        assert cls.order_items[1].region_preference == ["ME"]

    def test_made_in_japan_string(self):
        cls = self._make_cls("payment_received", items=[{"base_flavor": "Fusion Menthol"}])
        state = {"facts": {"ordered_items": ["Terea Fusion Menthol made in Japan x3"]}}
        _infer_region_from_state(cls, state)
        assert cls.order_items[0].region_preference == ["JAPAN"]

    # --- Negative tests ---

    def test_oos_declines_skipped(self):
        cls = self._make_cls("oos_followup", "declines_alternative",
                             items=[{"base_flavor": "Green"}])
        state = {"facts": {"offered_alternatives": ["Terea Green EU x3"]}}
        _infer_region_from_state(cls, state)
        assert cls.order_items[0].region_preference is None

    def test_oos_provides_info_skipped(self):
        cls = self._make_cls("oos_followup", "provides_info",
                             items=[{"base_flavor": "Green"}])
        state = {"facts": {"offered_alternatives": ["Terea Green EU x3"]}}
        _infer_region_from_state(cls, state)
        assert cls.order_items[0].region_preference is None

    def test_new_order_skipped(self):
        cls = self._make_cls("new_order", items=[{"base_flavor": "Green"}])
        state = {"facts": {"ordered_items": ["Terea Green EU x5"]}}
        _infer_region_from_state(cls, state)
        assert cls.order_items[0].region_preference is None

    def test_stock_question_skipped(self):
        cls = self._make_cls("stock_question", items=[{"base_flavor": "Green"}])
        state = {"facts": {"ordered_items": ["Terea Green EU x5"]}}
        _infer_region_from_state(cls, state)
        assert cls.order_items[0].region_preference is None

    def test_existing_region_not_overridden(self):
        cls = self._make_cls("payment_received",
                             items=[{"base_flavor": "Green", "region_preference": ["ME"]}])
        state = {"facts": {"ordered_items": ["Terea Green EU x5"]}}
        _infer_region_from_state(cls, state)
        assert cls.order_items[0].region_preference == ["ME"]  # NOT changed to EU

    def test_product_name_with_region_not_filled(self):
        cls = self._make_cls("payment_received",
                             items=[{"base_flavor": "Green", "product_name": "Green EU"}])
        state = {"facts": {"ordered_items": ["Terea Green EU x5"]}}
        _infer_region_from_state(cls, state)
        assert cls.order_items[0].region_preference is None  # product_name has region

    def test_strict_region_not_changed(self):
        cls = self._make_cls("payment_received",
                             items=[{"base_flavor": "Green", "strict_region": True}])
        state = {"facts": {"ordered_items": ["Terea Green EU x5"]}}
        _infer_region_from_state(cls, state)
        assert cls.order_items[0].strict_region is True  # unchanged

    def test_payment_uses_only_ordered_items(self):
        """payment_received must NOT use offered_alternatives."""
        cls = self._make_cls("payment_received", items=[{"base_flavor": "Amber"}])
        state = {"facts": {"offered_alternatives": ["Terea Amber EU x3"]}}
        _infer_region_from_state(cls, state)
        assert cls.order_items[0].region_preference is None  # not from offered_alternatives

    def test_oos_uses_only_offered_alternatives(self):
        """oos_followup+agrees must NOT use ordered_items for region."""
        cls = self._make_cls("oos_followup", "agrees_to_alternative",
                             items=[{"base_flavor": "Green"}])
        state = {"facts": {"ordered_items": ["Terea Green EU x5"]}}
        _infer_region_from_state(cls, state)
        assert cls.order_items[0].region_preference is None  # not from ordered_items
