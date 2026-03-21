"""Tests for repeat order / reorder detection.

Covers:
- _looks_like_reorder() exact-match detection (Layer 1)
- _body_has_reorder_hint() substring gate (Layer 2)
- _build_order_items_from_last_order() region resolution
- get_last_order() DB query
- format_client_order_context() formatting
- compose_classifier_context() with client_order_context param
"""

import pytest

pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# _looks_like_reorder — Layer 1 exact match
# ---------------------------------------------------------------------------

class TestLooksLikeReorder:
    """Deterministic reorder detection for pure reorder messages."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from agents.classifier import _looks_like_reorder
        self._fn = _looks_like_reorder

    def _email(self, body: str) -> str:
        return f"From: test@example.com\nBody: {body}"

    # --- Positive ---

    def test_same_order(self):
        assert self._fn(self._email("same order")) is True

    def test_same_order_please(self):
        assert self._fn(self._email("same order please")) is True

    def test_can_i_please_have_the_same_order(self):
        assert self._fn(self._email("Can I please have the same order?")) is True

    def test_the_usual(self):
        assert self._fn(self._email("the usual")) is True

    def test_as_usual(self):
        assert self._fn(self._email("as usual")) is True

    def test_russian_kak_obychno(self):
        assert self._fn(self._email("как обычно")) is True

    def test_repeat_order(self):
        assert self._fn(self._email("repeat order")) is True

    def test_with_greeting_and_signature(self):
        body = "Hey, same order please\nBest regards"
        assert self._fn(self._email(body)) is True

    # --- Negative ---

    def test_long_body_modification(self):
        body = "same order but add 2 blue please and also 3 silver if you have and maybe some green ones too and whatever else is in stock"
        assert self._fn(self._email(body)) is False

    def test_explicit_product(self):
        assert self._fn(self._email("send me 4 green")) is False

    def test_thanks(self):
        assert self._fn(self._email("thanks!")) is False

    def test_same_thing_not_reorder(self):
        """'same thing' is too ambiguous — could be non-order context."""
        assert self._fn(self._email("same thing")) is False

    def test_same_one_not_reorder(self):
        """'same one' is too ambiguous — could refer to a problem."""
        assert self._fn(self._email("same one")) is False

    def test_same_ones_not_reorder(self):
        assert self._fn(self._email("same ones")) is False

    def test_empty_body(self):
        assert self._fn("From: t@e.com\nBody: ") is False


# ---------------------------------------------------------------------------
# _body_has_reorder_hint — Layer 2 substring gate
# ---------------------------------------------------------------------------

class TestBodyHasReorderHint:
    """Substring hint detection for Layer 2 context injection gate."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from agents.classifier import _body_has_reorder_hint
        self._fn = _body_has_reorder_hint

    def _email(self, body: str) -> str:
        return f"From: test@example.com\nBody: {body}"

    # --- Positive: order-intent anchored ---

    def test_same_order_modification(self):
        assert self._fn(self._email("same order but add 2 blue")) is True

    def test_can_i_have_the_same(self):
        assert self._fn(self._email("can I have the same but add 2 blue")) is True

    def test_can_i_please_have_the_same(self):
        assert self._fn(self._email("can I please have the same but without amber")) is True

    def test_same_please(self):
        assert self._fn(self._email("same please but add blue")) is True

    def test_send_me_the_same(self):
        assert self._fn(self._email("send me the same but 3 boxes")) is True

    def test_ill_have_the_same(self):
        assert self._fn(self._email("I'll have the same but without amber")) is True

    def test_id_like_the_same(self):
        assert self._fn(self._email("I'd like the same order")) is True

    def test_as_usual(self):
        assert self._fn(self._email("as usual please")) is True

    def test_repeat_order(self):
        assert self._fn(self._email("repeat order and add silver")) is True

    def test_the_usual_but_without(self):
        assert self._fn(self._email("the usual but without amber")) is True

    def test_the_usual_but_add(self):
        assert self._fn(self._email("the usual but add 2 blue")) is True

    def test_russian_kak_obychno(self):
        assert self._fn(self._email("как обычно только добавь синий")) is True

    # --- Negative: no false positives ---

    def test_explicit_product(self):
        assert self._fn(self._email("send me 4 green")) is False

    def test_tracking_question(self):
        assert self._fn(self._email("where is my tracking?")) is False

    def test_repeat_tracking(self):
        """'repeat' alone must not match — requires 'repeat order'."""
        assert self._fn(self._email("repeat the tracking number")) is False

    def test_russian_repeat_tracking(self):
        assert self._fn(self._email("повторите трекинг")) is False

    def test_same_problem(self):
        """'I have the same' requires 'can I have the same'."""
        assert self._fn(self._email("I have the same problem")) is False

    def test_same_thing_tracking(self):
        assert self._fn(self._email("the same thing happened with tracking again")) is False

    def test_same_one_damaged(self):
        assert self._fn(self._email("same one arrived damaged")) is False

    def test_usual_address(self):
        """'my usual' requires 'my usual order'."""
        assert self._fn(self._email("my usual address is still wrong")) is False

    def test_want_same_thing(self):
        assert self._fn(self._email("I want the same thing")) is False

    def test_the_usual_tracking_issue(self):
        """'the usual' in hint regex requires 'the usual order'."""
        assert self._fn(self._email("the usual tracking issue again")) is False


# ---------------------------------------------------------------------------
# format_client_order_context
# ---------------------------------------------------------------------------

class TestFormatClientOrderContext:

    @pytest.fixture(autouse=True)
    def _import(self):
        from agents.formatters import format_client_order_context
        self._fn = format_client_order_context

    def test_both(self):
        last = {"items": [{"product_name": "Terea Green", "display_name_snapshot": "Terea Green EU", "quantity": 2}]}
        result = self._fn(last, "Regular customer")
        assert "Last order:" in result
        assert "Terea Green EU x2" in result
        assert "Profile: Regular customer" in result

    def test_summary_only(self):
        result = self._fn(None, "Regular customer")
        assert "Profile:" in result
        assert "Last order:" not in result

    def test_last_order_only(self):
        last = {"items": [{"product_name": "Terea Silver", "display_name_snapshot": None, "quantity": 1}]}
        result = self._fn(last, None)
        assert "Last order:" in result
        assert "Terea Silver x1" in result
        assert "Profile:" not in result

    def test_none_none(self):
        assert self._fn(None, None) is None


# ---------------------------------------------------------------------------
# compose_classifier_context — new param
# ---------------------------------------------------------------------------

class TestComposeClassifierContextOrderParam:

    def test_with_context(self):
        from agents.formatters import compose_classifier_context
        result = compose_classifier_context(client_order_context="--- CLIENT ORDER HISTORY ---\nLast order: Green x2")
        assert "CLIENT ORDER HISTORY" in result

    def test_without_context(self):
        from agents.formatters import compose_classifier_context
        result = compose_classifier_context(client_order_context=None)
        assert "CLIENT ORDER HISTORY" not in result


# ---------------------------------------------------------------------------
# Template/pricing regression: parser_used must be False
# ---------------------------------------------------------------------------

class TestReorderParserUsedFalse:
    """Deterministic reorder must NOT set parser_used=True."""

    def test_reorder_classification_parser_used_false(self):
        from agents.classifier import _looks_like_reorder, _build_order_items_from_last_order, _derive_items_text
        from agents.models import EmailClassification

        email = "From: test@example.com\nBody: same order please"
        assert _looks_like_reorder(email) is True

        last_order = {
            "order_id": "AUTO-test123",
            "items": [
                {"product_name": "Terea Green", "base_flavor": "Green",
                 "quantity": 2, "variant_id": None, "display_name_snapshot": "Terea Green ME"}
            ],
        }
        items = _build_order_items_from_last_order(last_order)
        cls = EmailClassification(
            needs_reply=True,
            situation="new_order",
            client_email="test@example.com",
            order_items=items,
            items=_derive_items_text(items),
        )
        assert cls.parser_used is False
        assert cls.situation == "new_order"
        assert len(cls.order_items) == 1
        assert cls.order_items[0].base_flavor == "Green"
        assert cls.order_items[0].region_preference == ["ME"]


# ---------------------------------------------------------------------------
# _build_order_items_from_last_order — region resolution
# ---------------------------------------------------------------------------

class TestBuildOrderItemsRegion:

    @pytest.fixture(autouse=True)
    def _import(self):
        from agents.classifier import _build_order_items_from_last_order
        self._fn = _build_order_items_from_last_order

    def test_region_from_display_name(self):
        last = {"items": [
            {"product_name": "Terea Green ME", "base_flavor": "Green",
             "quantity": 3, "variant_id": None, "display_name_snapshot": "Terea Green ME"},
        ]}
        items = self._fn(last)
        assert items[0].region_preference == ["ME"]

    def test_no_region(self):
        last = {"items": [
            {"product_name": "Green", "base_flavor": "Green",
             "quantity": 1, "variant_id": None, "display_name_snapshot": None},
        ]}
        items = self._fn(last)
        assert items[0].region_preference is None

    def test_multiple_items_stable_order(self):
        last = {"items": [
            {"product_name": "Terea Green EU", "base_flavor": "Green",
             "quantity": 2, "variant_id": None, "display_name_snapshot": "Terea Green EU"},
            {"product_name": "Terea Silver ME", "base_flavor": "Silver",
             "quantity": 1, "variant_id": None, "display_name_snapshot": "Terea Silver ME"},
        ]}
        items = self._fn(last)
        assert len(items) == 2
        assert items[0].base_flavor == "Green"
        assert items[1].base_flavor == "Silver"
