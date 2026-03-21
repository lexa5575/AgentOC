"""Tests for payment ack detection (Fix 1-5).

Covers:
- _looks_like_payment_ack() whitelist/reject logic
- Deterministic payment-ack override in classifier (Fix 2A)
- Pipeline post-LLM fallback (Fix 2B)
- State transitions (Fix 1, Fix 4b)
- _flatten_parts recursive walker (Fix 3a)
- Attachment formatting (Fix 3b)
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.smoke

from agents.classifier import _looks_like_payment_ack, _ACK_PHRASES


def _ensure_gmail_stubs():
    """Ensure googleapiclient stubs are in sys.modules for gmail imports."""
    stubs = {}
    for name in (
        "googleapiclient", "googleapiclient.discovery",
        "googleapiclient.discovery_cache",
        "google", "google.auth", "google.auth.transport",
        "google.auth.transport.requests",
        "google.oauth2", "google.oauth2.credentials",
    ):
        if name not in sys.modules:
            stubs[name] = sys.modules[name] = MagicMock()
    return stubs


# ═══════════════════════════════════════════════════════════════════════════
# _looks_like_payment_ack() unit tests
# ═══════════════════════════════════════════════════════════════════════════

class TestLooksLikePaymentAck:
    """N4-N7f: Whitelist ack detection."""

    def _email(self, body: str, attachments: str = "") -> str:
        parts = ["From: client@example.com", "Subject: Re: Order"]
        if attachments:
            parts.append(f"Attachments: {attachments}")
        parts.append(f"Body: {body}")
        return "\n".join(parts)

    # --- Positive matches ---

    def test_thank_you(self):
        assert _looks_like_payment_ack(self._email("Thank you"))

    def test_thanks(self):
        assert _looks_like_payment_ack(self._email("Thanks"))

    def test_paid(self):
        assert _looks_like_payment_ack(self._email("Paid"))

    def test_sent(self):
        assert _looks_like_payment_ack(self._email("Sent"))

    def test_payment_sent(self):
        assert _looks_like_payment_ack(self._email("Payment sent"))

    def test_thank_you_so_much(self):
        assert _looks_like_payment_ack(self._email("Thank you so much"))

    # N7d: Exact Basma case
    def test_thank_you_regards_basma(self):
        assert _looks_like_payment_ack(self._email("Thank you Regards, Basma Elajou"))

    # N7e: Generic name
    def test_thank_you_regards_john(self):
        assert _looks_like_payment_ack(self._email("Thank you Regards, John Smith"))

    # N7f: Generic closing
    def test_thanks_best_maria(self):
        assert _looks_like_payment_ack(self._email("Thanks, Best, Maria Lopez"))

    def test_sent_via_zelle(self):
        assert _looks_like_payment_ack(self._email("Sent via Zelle"))

    def test_i_paid(self):
        assert _looks_like_payment_ack(self._email("I paid"))

    # Image attachment + short ack
    def test_thank_you_with_image_attachment(self):
        assert _looks_like_payment_ack(
            self._email("Thank you", "screenshot.jpg (image/jpeg)")
        )

    # Body is only greeting/signature + image attachment
    def test_signature_only_with_image(self):
        assert _looks_like_payment_ack(
            self._email("Regards, John", "payment.png (image/png)")
        )

    # --- Surname collisions with product names (P1 fix) ---

    def test_thank_you_regards_john_green(self):
        """Green is a product name but here it's a surname in signature."""
        assert _looks_like_payment_ack(self._email("Thank you Regards, John Green"))

    def test_thank_you_regards_amber_stone(self):
        """Amber is a product name but here it's a first name in signature."""
        assert _looks_like_payment_ack(self._email("Thank you Regards, Amber Stone"))

    def test_thank_you_best_black(self):
        """Black is a product name but here it's a surname in signature."""
        assert _looks_like_payment_ack(self._email("Thank you Best, Tom Black"))

    # --- Negative: reject patterns ---

    # N7a: digit + product
    def test_reject_thanks_2_silver(self):
        assert not _looks_like_payment_ack(self._email("Thanks, 2 Silver please"))

    # N7b: product + digit
    def test_reject_done_amber_x3(self):
        assert not _looks_like_payment_ack(self._email("Done, Amber x3"))

    # N7c: digit + action
    def test_reject_paid_need_1_more_carton(self):
        assert not _looks_like_payment_ack(self._email("Paid, need 1 more carton"))

    # N5: question mark
    def test_reject_thanks_change_address(self):
        assert not _looks_like_payment_ack(self._email("Thanks, change address?"))

    # N6: "add" action word
    def test_reject_done_add_2_silver(self):
        assert not _looks_like_payment_ack(self._email("Done, add 2 Silver too"))

    # Long message → fallthrough
    def test_reject_long_message(self):
        long = "Thank you so much! " * 10
        assert not _looks_like_payment_ack(self._email(long))

    # Empty body → False (out of scope v1)
    def test_reject_empty_body(self):
        assert not _looks_like_payment_ack(self._email(""))

    # N7: Wrong status context (tested at classifier level, but body still valid)
    def test_pure_ack_returns_true(self):
        # Body-only check, status is checked by caller
        assert _looks_like_payment_ack(self._email("Thank you"))

    # Product names in body
    def test_reject_thanks_terea(self):
        assert not _looks_like_payment_ack(self._email("Thanks, Terea Silver"))

    def test_reject_tracking_question(self):
        assert not _looks_like_payment_ack(self._email("Paid, tracking?"))

    def test_reject_when_ship(self):
        assert not _looks_like_payment_ack(self._email("Thanks when will it ship"))


# ═══════════════════════════════════════════════════════════════════════════
# _flatten_parts() tests (Fix 3a)
# ═══════════════════════════════════════════════════════════════════════════

def _can_import_gmail():
    try:
        _ensure_gmail_stubs()
        from tools.gmail import GmailClient  # noqa: F401
        return True
    except ImportError:
        return False


_skip_no_gmail = pytest.mark.skipif(
    not _can_import_gmail(),
    reason="googleapiclient not available (test in Docker)",
)


@_skip_no_gmail
class TestFlattenParts:

    def _get_gmail_client(self):
        from tools.gmail import GmailClient
        return GmailClient

    def test_leaf_payload_no_parts(self):
        """N15: Leaf payload returns [payload]."""
        GmailClient = self._get_gmail_client()
        payload = {"mimeType": "text/plain", "body": {"data": "abc"}}
        result = GmailClient._flatten_parts(payload)
        assert result == [payload]

    def test_simple_multipart(self):
        GmailClient = self._get_gmail_client()
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": "abc"}},
                {"mimeType": "image/jpeg", "filename": "photo.jpg"},
            ],
        }
        result = GmailClient._flatten_parts(payload)
        assert len(result) == 2

    def test_3_levels_deep(self):
        """N14: 3+ levels nesting."""
        GmailClient = self._get_gmail_client()
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "multipart/related",
                            "parts": [
                                {"mimeType": "text/html", "body": {"data": "html"}},
                                {"mimeType": "image/png", "filename": "inline.png"},
                            ],
                        },
                        {"mimeType": "text/plain", "body": {"data": "plain"}},
                    ],
                },
                {"mimeType": "image/jpeg", "filename": "attachment.jpg"},
            ],
        }
        result = GmailClient._flatten_parts(payload)
        mimes = [p["mimeType"] for p in result]
        assert "text/html" in mimes
        assert "text/plain" in mimes
        assert "image/png" in mimes
        assert "image/jpeg" in mimes
        assert len(result) == 4


# ═══════════════════════════════════════════════════════════════════════════
# _extract_attachments_meta() tests (Fix 3a)
# ═══════════════════════════════════════════════════════════════════════════

@_skip_no_gmail
class TestExtractAttachmentsMeta:

    def _get_gmail_client(self):
        from tools.gmail import GmailClient
        return GmailClient

    def test_no_attachments(self):
        GmailClient = self._get_gmail_client()
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": "abc"}},
                {"mimeType": "text/html", "body": {"data": "<p>abc</p>"}},
            ],
        }
        assert GmailClient._extract_attachments_meta(payload) == []

    def test_image_attachment(self):
        GmailClient = self._get_gmail_client()
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": "abc"}},
                {"mimeType": "image/jpeg", "filename": "payment.jpg", "body": {"size": 12345}},
            ],
        }
        attachments = GmailClient._extract_attachments_meta(payload)
        assert len(attachments) == 1
        assert attachments[0]["filename"] == "payment.jpg"
        assert attachments[0]["mime_type"] == "image/jpeg"

    def test_inline_image_no_filename(self):
        GmailClient = self._get_gmail_client()
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": "abc"}},
                {"mimeType": "image/png", "filename": "", "body": {"size": 5000}},
            ],
        }
        attachments = GmailClient._extract_attachments_meta(payload)
        assert len(attachments) == 1
        assert attachments[0]["filename"] == "(inline)"


# ═══════════════════════════════════════════════════════════════════════════
# _format_email_text() attachment tests (Fix 3b)
# ═══════════════════════════════════════════════════════════════════════════

@_skip_no_gmail
class TestFormatEmailTextAttachments:

    def test_with_attachments(self):
        """N13: Attachments appear in formatted text."""
        from tools.gmail_poller import _format_email_text
        msg = {
            "from": "client@example.com",
            "subject": "Re: Order",
            "body": "Thank you",
            "attachments": [
                {"filename": "payment.jpg", "mime_type": "image/jpeg"},
            ],
        }
        text = _format_email_text(msg)
        assert "Attachments: payment.jpg (image/jpeg)" in text
        assert text.index("Attachments:") < text.index("Body:")

    def test_without_attachments(self):
        from tools.gmail_poller import _format_email_text
        msg = {
            "from": "client@example.com",
            "subject": "Re: Order",
            "body": "Hello",
        }
        text = _format_email_text(msg)
        assert "Attachments:" not in text


# ═══════════════════════════════════════════════════════════════════════════
# format_combined_email_text() attachment tests (Fix 3b)
# ═══════════════════════════════════════════════════════════════════════════

class TestFormatCombinedEmailTextAttachments:

    def test_merged_thread_with_attachments(self):
        """N17: Merged-thread path includes attachments."""
        from datetime import datetime, timezone
        from agents.formatters import format_combined_email_text

        candidates = [
            {
                "msg": {
                    "from": "client@example.com",
                    "from_raw": "Client <client@example.com>",
                    "subject": "Re: Order",
                    "body": "Hello",
                },
                "created_at": datetime(2026, 3, 20, 10, 0, tzinfo=timezone.utc),
            },
            {
                "msg": {
                    "from": "client@example.com",
                    "from_raw": "Client <client@example.com>",
                    "subject": "Re: Order",
                    "body": "Thank you",
                    "attachments": [
                        {"filename": "receipt.png", "mime_type": "image/png"},
                    ],
                },
                "created_at": datetime(2026, 3, 20, 11, 0, tzinfo=timezone.utc),
            },
        ]
        text = format_combined_email_text(candidates)
        assert "[Attachments: receipt.png (image/png)]" in text

    def test_merged_thread_no_attachments(self):
        from datetime import datetime, timezone
        from agents.formatters import format_combined_email_text

        candidates = [
            {
                "msg": {
                    "from_raw": "Client <c@example.com>",
                    "subject": "Re: Order",
                    "body": "Hello",
                },
                "created_at": datetime(2026, 3, 20, 10, 0, tzinfo=timezone.utc),
            },
        ]
        text = format_combined_email_text(candidates)
        assert "Attachments:" not in text


# ═══════════════════════════════════════════════════════════════════════════
# State updater preserve-list test (Fix 5)
# ═══════════════════════════════════════════════════════════════════════════

class TestStateUpdaterPreserveFlags:

    def test_flags_in_empty_state(self):
        """payment_request_sent and payment_confirmed exist in empty state."""
        from agents.state_updater import _empty_state
        state = _empty_state()
        assert "payment_request_sent" in state["facts"]
        assert "payment_confirmed" in state["facts"]

    def test_flags_preserved_in_derive_facts(self):
        """N16: Flags survive _derive_facts re-derivation."""
        from agents.state_updater import _derive_facts
        from agents.models import EmailClassification

        current_facts = {
            "order_id": "12345",
            "ordered_items": ["Green x2"],
            "payment_request_sent": True,
            "payment_confirmed": None,
        }
        classification = EmailClassification(
            needs_reply=True,
            situation="other",
            client_email="test@example.com",
        )
        result = {"client_data": {}}
        facts = _derive_facts(current_facts, classification, "12345", None, result)
        assert facts.get("payment_request_sent") is True
        assert facts.get("payment_confirmed") is None


# ═══════════════════════════════════════════════════════════════════════════
# Integration tests: run_classification (Fix 2A) + _persist_results (Fix 1, 4b)
# ═══════════════════════════════════════════════════════════════════════════

class TestClassifierPaymentOverride:
    """Fix 2A: run_classification() deterministic override."""

    def test_awaiting_payment_thank_you_returns_payment_received(self):
        """Full run_classification call with awaiting_payment state."""
        from agents.classifier import run_classification
        from unittest.mock import patch as _patch

        state = {
            "status": "awaiting_payment",
            "facts": {
                "order_id": "99999",
                "payment_request_sent": True,
                "payment_confirmed": None,
            },
            "last_exchange": {"we_said": "Zelle instructions sent"},
        }
        email = (
            "From: client@example.com\n"
            "Subject: Re: Order\n"
            "Body: Thank you"
        )
        # Should return deterministically without calling LLM
        result = run_classification(email, "", conversation_state=state)
        assert result.situation == "payment_received"
        assert result.order_id == "99999"
        assert result.needs_reply is True
        assert result.dialog_intent == "confirms_payment"

    def test_awaiting_payment_long_msg_falls_through_to_llm(self):
        """Long message with question → falls through to LLM."""
        from agents.classifier import run_classification
        from unittest.mock import patch as _patch, MagicMock as _MagicMock
        import types as _types

        state = {
            "status": "awaiting_payment",
            "facts": {
                "order_id": "99999",
                "payment_request_sent": True,
            },
            "last_exchange": {"we_said": "Zelle instructions"},
        }
        email = (
            "From: client@example.com\n"
            "Subject: Re: Order\n"
            "Body: Thank you! Can I also add 3 Silver EU to my order?"
        )
        # This should NOT be caught by deterministic override → will call LLM
        # We mock the LLM to avoid real API call
        fake_response = _types.SimpleNamespace(
            content='{"needs_reply": true, "situation": "new_order", "client_email": "client@example.com"}'
        )
        from agents.classifier import classifier_agent
        with _patch.object(classifier_agent, "run", return_value=fake_response):
            result = run_classification(email, "", conversation_state=state)
        # Should have gone to LLM, not deterministic
        assert result.situation == "new_order"

    def test_payment_confirmed_flag_blocks_repeat(self):
        """After payment_confirmed=True, "thanks" doesn't trigger override."""
        from agents.classifier import run_classification
        from unittest.mock import patch as _patch
        import types as _types

        state = {
            "status": "awaiting_payment",  # hasn't transitioned yet
            "facts": {
                "order_id": "99999",
                "payment_request_sent": True,
                "payment_confirmed": True,  # already confirmed
            },
            "last_exchange": {"we_said": "We received your payment"},
        }
        email = (
            "From: client@example.com\n"
            "Subject: Re: Order\n"
            "Body: Thanks"
        )
        fake_response = _types.SimpleNamespace(
            content='{"needs_reply": false, "situation": "other", "client_email": "client@example.com"}'
        )
        from agents.classifier import classifier_agent
        with _patch.object(classifier_agent, "run", return_value=fake_response):
            result = run_classification(email, "", conversation_state=state)
        # Should fall through to LLM, which returns "other"
        assert result.situation == "other"


class TestPersistResultsStateTransitions:
    """Fix 1 + 4b: _persist_results state transitions."""

    def _make_classification(self, situation="new_order", order_id="12345",
                             client_email="test@example.com"):
        from agents.models import EmailClassification
        return EmailClassification(
            needs_reply=True,
            situation=situation,
            client_email=client_email,
            order_id=order_id,
        )

    def _make_result(self, *, template_used=True, draft_reply="reply text",
                     payment_type="prepay", effective_situation=None,
                     stock_issue=None, template_situation=None):
        result = {
            "needs_reply": True,
            "draft_reply": draft_reply,
            "template_used": template_used,
            "client_found": True,
            "client_data": {"payment_type": payment_type},
            "conversation_state": {
                "status": "new",
                "facts": {"order_id": "12345"},
                "last_exchange": {},
                "open_questions": [],
            },
        }
        if effective_situation:
            result["effective_situation"] = effective_situation
        if stock_issue:
            result["stock_issue"] = stock_issue
        if template_situation:
            result["template_situation"] = template_situation
        return result

    def test_prepay_new_order_sets_awaiting_payment(self):
        """Fix 1: prepay new_order + template → awaiting_payment."""
        from agents.pipeline import _persist_results
        from unittest.mock import patch as _patch

        classification = self._make_classification()
        result = self._make_result()

        with _patch("agents.pipeline.save_email"), \
             _patch("agents.pipeline.save_state") as mock_save, \
             _patch("agents.pipeline.save_order_items", return_value=None), \
             _patch("agents.pipeline.update_client"):
            _persist_results(classification, result, "thread123", "msg123",
                             "From: x\nSubject: y\nBody: z")

        state = result["conversation_state"]
        assert state["status"] == "awaiting_payment"
        assert state["facts"]["payment_request_sent"] is True
        assert state["facts"]["payment_method"] == "Zelle"

    def test_postpay_new_order_no_transition(self):
        """Fix 1: postpay → status stays 'new'."""
        from agents.pipeline import _persist_results
        from unittest.mock import patch as _patch

        classification = self._make_classification()
        result = self._make_result(payment_type="postpay")

        with _patch("agents.pipeline.save_email"), \
             _patch("agents.pipeline.save_state"), \
             _patch("agents.pipeline.save_order_items", return_value=None), \
             _patch("agents.pipeline.update_client"):
            _persist_results(classification, result, "thread123", "msg123",
                             "From: x\nSubject: y\nBody: z")

        assert result["conversation_state"]["status"] == "new"

    def test_stock_issue_no_transition(self):
        """Fix 1: OOS template (stock_issue present) → no transition."""
        from agents.pipeline import _persist_results
        from unittest.mock import patch as _patch

        classification = self._make_classification()
        result = self._make_result(stock_issue={
            "stock_check": {"items": [], "insufficient_items": []},
        })

        with _patch("agents.pipeline.save_email"), \
             _patch("agents.pipeline.save_state"), \
             _patch("agents.pipeline.save_order_items", return_value=None), \
             _patch("agents.pipeline.update_client"):
            _persist_results(classification, result, "thread123", "msg123",
                             "From: x\nSubject: y\nBody: z")

        assert result["conversation_state"]["status"] == "new"

    def test_payment_received_template_sets_confirmed(self):
        """Fix 4b: payment_received template → payment_confirmed + pending_response."""
        from agents.pipeline import _persist_results
        from unittest.mock import patch as _patch

        classification = self._make_classification(situation="payment_received")
        result = self._make_result(
            template_situation="payment_received",
        )
        result["conversation_state"]["status"] = "awaiting_payment"

        with _patch("agents.pipeline.save_email"), \
             _patch("agents.pipeline.save_state"), \
             _patch("agents.pipeline.save_order_items", return_value=None), \
             _patch("agents.pipeline.update_client"):
            _persist_results(classification, result, "thread123", "msg123",
                             "From: x\nSubject: y\nBody: z")

        state = result["conversation_state"]
        assert state["facts"]["payment_confirmed"] is True
        assert state["status"] == "pending_response"

    def test_oos_agrees_template_no_confirmed(self):
        """Fix 4b: oos_agrees template → payment_confirmed NOT set."""
        from agents.pipeline import _persist_results
        from unittest.mock import patch as _patch

        classification = self._make_classification(situation="payment_received")
        result = self._make_result(
            template_situation="oos_agrees",
        )
        result["conversation_state"]["status"] = "awaiting_payment"

        with _patch("agents.pipeline.save_email"), \
             _patch("agents.pipeline.save_state"), \
             _patch("agents.pipeline.save_order_items", return_value=None), \
             _patch("agents.pipeline.update_client"):
            _persist_results(classification, result, "thread123", "msg123",
                             "From: x\nSubject: y\nBody: z")

        state = result["conversation_state"]
        assert state["facts"].get("payment_confirmed") is not True
        assert state["status"] == "awaiting_payment"  # unchanged
