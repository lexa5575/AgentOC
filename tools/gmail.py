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
from email.utils import parseaddr
from os import getenv

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


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
                    labels = msg.get("labelIds", [])
                    # Only inbox messages (skip sent, drafts, spam, trash)
                    if "INBOX" in labels and "SENT" not in labels:
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
