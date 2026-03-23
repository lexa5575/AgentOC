"""
Gmail API Client
----------------

Wrapper over Gmail API for reading inbox messages.
Auth via refresh_token from environment variables (no credentials.json in prod).

Usage:
    from tools.gmail import GmailClient
    client = GmailClient()
    messages = client.get_new_messages(after_history_id="12345")
"""

import base64
import logging
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import parseaddr
from os import getenv

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]

# Gmail category labels to skip (only process PRIMARY inbox)
_SKIP_LABELS = {
    "CATEGORY_PROMOTIONS",
    "CATEGORY_SOCIAL",
    "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
    "SPAM",
    "TRASH",
}

# Named accounts: account_name → env var suffix for refresh token
# "default" → GMAIL_REFRESH_TOKEN, "tilda" → GMAIL_REFRESH_TOKEN_TILDA
GMAIL_ACCOUNTS = {
    "default": "",
    "tilda": "_TILDA",
}


class GmailClient:
    """Gmail API client using refresh_token from env.

    Supports multiple accounts via the `account` parameter:
    - "default" → uses GMAIL_REFRESH_TOKEN (getorderstick@gmail.com)
    - "tilda"   → uses GMAIL_REFRESH_TOKEN_TILDA (iqostilda2@gmail.com)
    """

    def __init__(self, account: str = "default"):
        self._service = None
        self._account = account

    @property
    def account(self) -> str:
        return self._account

    def _get_service(self):
        """Lazy-init Gmail API service."""
        if self._service:
            return self._service

        suffix = GMAIL_ACCOUNTS.get(self._account, "")
        client_id = getenv("GMAIL_CLIENT_ID", "")
        client_secret = getenv("GMAIL_CLIENT_SECRET", "")
        refresh_token = getenv(f"GMAIL_REFRESH_TOKEN{suffix}", "")

        if not all([client_id, client_secret, refresh_token]):
            raise RuntimeError(
                f"Gmail account '{self._account}' not configured. "
                f"Set GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, "
                f"GMAIL_REFRESH_TOKEN{suffix} in .env"
            )

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
        )
        creds.refresh(Request())

        self._service = build("gmail", "v1", credentials=creds)
        return self._service

    def get_current_history_id(self) -> str:
        """Get current history_id from Gmail profile (for initial setup)."""
        service = self._get_service()
        profile = service.users().getProfile(userId="me").execute()
        return profile["historyId"]

    def get_new_messages(self, after_history_id: str) -> list[dict]:
        """Fetch new inbox messages since the given history_id.

        Returns list of dicts: [{msg_id, history_id}, ...]
        """
        service = self._get_service()
        messages = []
        page_token = None

        while True:
            try:
                params = {
                    "userId": "me",
                    "startHistoryId": after_history_id,
                    "historyTypes": ["messageAdded"],
                    "labelId": "INBOX",
                }
                if page_token:
                    params["pageToken"] = page_token

                result = service.users().history().list(**params).execute()
            except Exception as e:
                error_str = str(e)
                if "404" in error_str or "historyId" in error_str.lower():
                    logger.warning(
                        "History ID %s expired, resetting. Error: %s",
                        after_history_id, e,
                    )
                    return []
                raise

            history_id = result.get("historyId", after_history_id)

            for record in result.get("history", []):
                for msg_added in record.get("messagesAdded", []):
                    msg = msg_added["message"]
                    labels = set(msg.get("labelIds", []))
                    # Only primary inbox (skip sent, promotions, social, etc.)
                    if "INBOX" in labels and "SENT" not in labels and not labels & _SKIP_LABELS:
                        messages.append({
                            "msg_id": msg["id"],
                            "history_id": history_id,
                            "thread_id": msg.get("threadId"),
                        })

            page_token = result.get("nextPageToken")
            if not page_token:
                break

        # Deduplicate by msg_id (same message can appear in multiple history records)
        seen = set()
        unique = []
        for m in messages:
            if m["msg_id"] not in seen:
                seen.add(m["msg_id"])
                unique.append(m)

        return unique

    def list_unread_inbox(self, max_results: int = 10) -> list[dict]:
        """Fetch unread PRIMARY inbox messages (for initial catch-up).

        Skips Promotions, Social, Updates, Forums categories.
        Returns list of dicts: [{msg_id}, ...]
        """
        service = self._get_service()
        result = service.users().messages().list(
            userId="me",
            q="is:unread in:inbox category:primary",
            maxResults=max_results,
        ).execute()

        messages = result.get("messages", [])
        return [{"msg_id": m["id"]} for m in messages]

    def get_message(self, msg_id: str) -> dict:
        """Fetch and parse a single Gmail message.

        Returns:
            {from, reply_to, subject, body, gmail_message_id, gmail_thread_id, created_at}
        """
        service = self._get_service()
        msg = service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()

        headers = {h["name"].lower(): h["value"] for h in msg["payload"]["headers"]}

        from_raw = headers.get("from", "")
        _, from_email = parseaddr(from_raw)
        reply_to_raw = headers.get("reply-to", "")
        _, reply_to = parseaddr(reply_to_raw)

        subject = headers.get("subject", "")

        body = self._extract_body(msg["payload"])

        # Prefer Gmail internalDate (epoch ms, UTC); fallback to now UTC.
        created_at = datetime.now(timezone.utc)
        internal_ms = msg.get("internalDate")
        if internal_ms:
            try:
                created_at = datetime.fromtimestamp(int(internal_ms) / 1000, tz=timezone.utc)
            except Exception:
                pass

        return {
            "from": from_email or from_raw,
            "from_raw": from_raw,
            "reply_to": reply_to,
            "subject": subject,
            "body": body,
            "attachments": self._extract_attachments_meta(msg["payload"]),
            "gmail_message_id": msg_id,
            "gmail_thread_id": msg.get("threadId"),
            "created_at": created_at,
        }

    def fetch_thread(self, thread_id: str, max_messages: int | None = None) -> list[dict]:
        """Fetch messages in a Gmail thread by threadId.

        Uses threads().get() — single API call for entire thread.
        Returns list of dicts matching get_email_history() format, sorted oldest-first.

        Direction is determined by Gmail SENT label (works for all accounts
        including tilda/iqostilda2). Timestamp uses internalDate (same as
        get_message) for deterministic cross-method comparison.
        """
        service = self._get_service()
        try:
            thread = service.users().threads().get(
                userId="me", id=thread_id, format="full",
            ).execute()
        except Exception as e:
            logger.error("Failed to fetch thread %s: %s", thread_id, e)
            return []

        raw_messages = thread.get("messages", [])
        if max_messages is not None and max_messages > 0:
            raw_messages = raw_messages[-max_messages:]

        messages = []
        for msg in raw_messages:
            headers = {h["name"].lower(): h["value"] for h in msg["payload"]["headers"]}
            label_ids = msg.get("labelIds", [])

            from_raw = headers.get("from", "")
            _, from_email = parseaddr(from_raw)

            # Direction: SENT label is authoritative (works for all accounts)
            is_outbound = "SENT" in label_ids
            direction = "outbound" if is_outbound else "inbound"

            # Timestamp: use internalDate (epoch ms, same as get_message)
            created_at = datetime.now(timezone.utc)
            internal_ms = msg.get("internalDate")
            if internal_ms:
                try:
                    created_at = datetime.fromtimestamp(
                        int(internal_ms) / 1000, tz=timezone.utc,
                    )
                except Exception:
                    pass

            # Determine client email (the non-us address)
            reply_to = headers.get("reply-to", "")
            _, reply_to_email = parseaddr(reply_to)
            if is_outbound:
                # For outbound messages, client is in To or Reply-To
                to_raw = headers.get("to", "")
                _, to_email = parseaddr(to_raw)
                client_email = to_email or reply_to_email or ""
            else:
                client_email = from_email

            messages.append({
                "client_email": client_email,
                "direction": direction,
                "subject": headers.get("subject", ""),
                "body": self._extract_body(msg["payload"]),
                "situation": "unknown",
                "created_at": created_at,
                "gmail_message_id": msg.get("id", ""),
            })

        messages.sort(key=lambda m: m["created_at"])
        return messages

    def check_thread_after_message(
        self,
        thread_id: str,
        after_msg_id: str,
        after_timestamp: "datetime",
    ) -> dict:
        """Check thread for newer messages after a given inbound.

        Single Gmail API call via fetch_thread(). Returns:
            {
                "has_newer_outbound": bool,
                "has_newer_inbound": bool,
                "latest_newer_inbound_msg_id": str | None,
            }

        Uses Gmail-only data (NOT local DB) for deterministic detection
        of manual replies the operator sent outside the automation.
        Direction detected via SENT label (works for all accounts).
        """
        thread_msgs = self.fetch_thread(thread_id)

        has_newer_outbound = False
        has_newer_inbound = False
        latest_newer_inbound_msg_id = None
        latest_newer_inbound_ts = None

        for msg in thread_msgs:
            msg_ts = msg.get("created_at")
            msg_id = msg.get("gmail_message_id", "")
            if not msg_ts or msg_ts <= after_timestamp:
                continue
            if msg_id == after_msg_id:
                continue
            if msg["direction"] == "outbound":
                has_newer_outbound = True
            elif msg["direction"] == "inbound":
                has_newer_inbound = True
                if latest_newer_inbound_ts is None or msg_ts > latest_newer_inbound_ts:
                    latest_newer_inbound_ts = msg_ts
                    latest_newer_inbound_msg_id = msg_id

        return {
            "has_newer_outbound": has_newer_outbound,
            "has_newer_inbound": has_newer_inbound,
            "latest_newer_inbound_msg_id": latest_newer_inbound_msg_id,
        }

    def search_thread_history(self, client_email: str, max_results: int = 10) -> list[dict]:
        """Search Gmail for recent conversation with a client.

        Returns list of dicts sorted oldest-first:
        [{client_email, direction, subject, body, created_at}, ...]

        Format matches get_email_history() output so format_email_history() works.
        """
        from email.utils import parsedate_to_datetime

        service = self._get_service()

        try:
            result = service.users().messages().list(
                userId="me",
                q=f"from:{client_email} OR to:{client_email}",
                maxResults=max_results,
            ).execute()
        except Exception as e:
            logger.error("Gmail search failed for %s: %s", client_email, e)
            return []

        msg_ids = [m["id"] for m in result.get("messages", [])]
        if not msg_ids:
            return []

        history = []
        for msg_id in msg_ids:
            try:
                # Single API call per message (format=full includes headers + body)
                raw = service.users().messages().get(
                    userId="me", id=msg_id, format="full",
                ).execute()

                headers = {h["name"].lower(): h["value"] for h in raw["payload"]["headers"]}

                # Parse sender
                from_raw = headers.get("from", "")
                _, from_email = parseaddr(from_raw)
                direction = "inbound" if from_email.lower() == client_email.lower().strip() else "outbound"

                # Parse date
                created_at = datetime.now(timezone.utc)
                if headers.get("date"):
                    try:
                        created_at = parsedate_to_datetime(headers["date"])
                    except Exception:
                        pass

                history.append({
                    "client_email": client_email,
                    "direction": direction,
                    "subject": headers.get("subject", ""),
                    "body": self._extract_body(raw["payload"]),
                    "situation": "unknown",
                    "created_at": created_at,
                    "gmail_message_id": msg_id,
                })
            except Exception as e:
                logger.error("Failed to fetch message %s: %s", msg_id, e)

        # Sort oldest first (Gmail returns newest first)
        history.sort(key=lambda m: m["created_at"])
        return history

    def search_unread_from(self, sender_email: str, max_results: int = 5) -> list[dict]:
        """Search for unread PRIMARY inbox messages from a specific sender.

        Returns list of dicts: [{msg_id}, ...] (newest first).
        """
        service = self._get_service()
        try:
            result = service.users().messages().list(
                userId="me",
                q=f"from:{sender_email} is:unread in:inbox category:primary",
                maxResults=max_results,
            ).execute()
        except Exception as e:
            logger.error("Gmail search failed for %s: %s", sender_email, e)
            return []

        return [{"msg_id": m["id"]} for m in result.get("messages", [])]

    def search_unread_order_notifications(
        self, client_email: str, max_results: int = 5
    ) -> list[dict]:
        """Search for unread order notifications mentioning this client email.

        Website orders arrive from order@shipmecarton.com with the client's
        email in the body. This finds those unread notifications.

        Returns list of dicts: [{msg_id}, ...] (newest first).
        """
        service = self._get_service()
        try:
            result = service.users().messages().list(
                userId="me",
                q=(
                    f"from:(order@shipmecarton.com OR noreply@shipmecarton.com) "
                    f"{client_email} is:unread"
                ),
                maxResults=max_results,
            ).execute()
        except Exception as e:
            logger.error(
                "Gmail unread order notification search failed for %s: %s",
                client_email, e,
            )
            return []

        return [{"msg_id": m["id"]} for m in result.get("messages", [])]

    def search_order_notifications(
        self,
        client_email: str,
        max_results: int = 50,
    ) -> list[dict]:
        """Search Gmail for order notifications mentioning this client.

        Uses query: 'from:(order@shipmecarton.com OR noreply@shipmecarton.com) {client_email}'
        Gmail full-text indexes the email body (which contains 'Email: client@...')
        so this finds order notifications even when the client is only in
        Reply-To or body — not in the To: header.

        Returns list of message dicts from get_message() (from, reply_to,
        subject, body, gmail_message_id).
        """
        service = self._get_service()
        try:
            result = service.users().messages().list(
                userId="me",
                q=f"from:(order@shipmecarton.com OR noreply@shipmecarton.com) {client_email}",
                maxResults=max_results,
            ).execute()
        except Exception as e:
            logger.error(
                "Gmail order notification search failed for %s: %s",
                client_email, e,
            )
            return []

        msg_ids = [m["id"] for m in result.get("messages", [])]
        messages = []
        for msg_id in msg_ids:
            try:
                msg = self.get_message(msg_id)
                messages.append(msg)
            except Exception as e:
                logger.error("Failed to fetch order notification %s: %s", msg_id, e)

        logger.info(
            "Gmail order notifications for %s: %d found",
            client_email, len(messages),
        )
        return messages

    def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        html: bool = False,
    ) -> str:
        """Create a Gmail draft in the specified thread.

        Args:
            to: Recipient email address.
            subject: Email subject (typically "Re: ...").
            body: Plain text or HTML body of the reply.
            thread_id: Gmail thread ID to attach the draft to.
            html: If True, body is treated as HTML.

        Returns:
            Gmail draft ID.
        """
        service = self._get_service()

        message = MIMEText(body, "html" if html else "plain")
        message["to"] = to
        message["subject"] = subject

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

        draft_body: dict = {"message": {"raw": raw}}
        if thread_id:
            draft_body["message"]["threadId"] = thread_id

        draft = (
            service.users()
            .drafts()
            .create(userId="me", body=draft_body)
            .execute()
        )

        draft_id = draft["id"]
        logger.info("Gmail draft created: draft_id=%s, thread=%s, to=%s", draft_id, thread_id, to)
        return draft_id

    @staticmethod
    def _flatten_parts(payload: dict) -> list[dict]:
        """Recursively flatten all MIME parts from a Gmail payload.

        Returns [payload] itself for leaf payloads (no children).
        """
        children = payload.get("parts", [])
        if not children:
            return [payload]
        parts = []
        for part in children:
            if part.get("mimeType", "").startswith("multipart/"):
                parts.extend(GmailClient._flatten_parts(part))
            else:
                parts.append(part)
        return parts

    @staticmethod
    def _extract_attachments_meta(payload: dict) -> list[dict]:
        """Extract attachment metadata (filename, MIME type) from payload."""
        attachments = []
        for part in GmailClient._flatten_parts(payload):
            filename = part.get("filename", "")
            mime = part.get("mimeType", "")
            if mime in ("text/plain", "text/html") and not filename:
                continue
            if filename or mime.startswith("image/"):
                attachments.append({"filename": filename or "(inline)", "mime_type": mime})
        return attachments

    def _extract_body(self, payload: dict) -> str:
        """Extract plain text body from Gmail message payload.

        Prefers text/plain but falls back to HTML→text conversion when
        text/plain is missing or just a stub ("does not support HTML").
        """
        # Collect text/plain and text/html from all parts (recursive)
        plain_text = None
        html_text = None

        all_parts = self._flatten_parts(payload)

        for part in all_parts:
            mime = part.get("mimeType", "")
            data = part.get("body", {}).get("data")
            if not data:
                continue
            if mime == "text/plain" and plain_text is None:
                plain_text = self._decode_base64(data)
            elif mime == "text/html" and html_text is None:
                html_text = self._decode_base64(data)

        # Use text/plain if it has real content (not just a stub)
        if plain_text and "does not support html" not in plain_text.lower() and len(plain_text.strip()) > 20:
            return plain_text

        # Otherwise convert HTML to plain text
        if html_text:
            return self._html_to_text(html_text)

        # Return whatever we have
        return plain_text or ""

    @staticmethod
    def _html_to_text(html: str) -> str:
        """Convert HTML email body to readable plain text."""
        try:
            from lxml.html import fromstring, tostring
            doc = fromstring(html)

            # Remove script/style elements
            for el in doc.iter("script", "style"):
                el.drop_tree()

            text = doc.text_content()

            # Clean up whitespace: collapse multiple blank lines
            import re
            text = re.sub(r"\n{3,}", "\n\n", text)
            return text.strip()
        except Exception:
            # Last resort: strip tags with regex
            import re
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text)
            return text.strip()

    @staticmethod
    def _decode_base64(data: str) -> str:
        """Decode Gmail's URL-safe base64 encoded data."""
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
