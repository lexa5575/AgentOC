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
from email.utils import parseaddr
from os import getenv

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Gmail category labels to skip (only process PRIMARY inbox)
_SKIP_LABELS = {
    "CATEGORY_PROMOTIONS",
    "CATEGORY_SOCIAL",
    "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
    "SPAM",
    "TRASH",
}


class GmailClient:
    """Gmail API client using refresh_token from env."""

    def __init__(self):
        self._service = None

    def _get_service(self):
        """Lazy-init Gmail API service."""
        if self._service:
            return self._service

        client_id = getenv("GMAIL_CLIENT_ID", "")
        client_secret = getenv("GMAIL_CLIENT_SECRET", "")
        refresh_token = getenv("GMAIL_REFRESH_TOKEN", "")

        if not all([client_id, client_secret, refresh_token]):
            raise RuntimeError(
                "Gmail not configured. Set GMAIL_CLIENT_ID, "
                "GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN in .env"
            )

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
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

        Returns: {from, reply_to, subject, body, gmail_message_id}
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

        return {
            "from": from_email or from_raw,
            "from_raw": from_raw,
            "reply_to": reply_to,
            "subject": subject,
            "body": body,
            "gmail_message_id": msg_id,
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
                })
            except Exception as e:
                logger.error("Failed to fetch message %s: %s", msg_id, e)

        # Sort oldest first (Gmail returns newest first)
        history.sort(key=lambda m: m["created_at"])
        return history

    def _extract_body(self, payload: dict) -> str:
        """Extract plain text body from Gmail message payload."""
        # Simple message with body directly
        if payload.get("body", {}).get("data"):
            return self._decode_base64(payload["body"]["data"])

        # Multipart message — find text/plain
        for part in payload.get("parts", []):
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                return self._decode_base64(part["body"]["data"])

        # Nested multipart (e.g., multipart/alternative inside multipart/mixed)
        for part in payload.get("parts", []):
            if part.get("mimeType", "").startswith("multipart/"):
                for sub in part.get("parts", []):
                    if sub.get("mimeType") == "text/plain" and sub.get("body", {}).get("data"):
                        return self._decode_base64(sub["body"]["data"])

        # Fallback: try text/html
        for part in payload.get("parts", []):
            if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
                return self._decode_base64(part["body"]["data"])

        return ""

    @staticmethod
    def _decode_base64(data: str) -> str:
        """Decode Gmail's URL-safe base64 encoded data."""
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
