"""
Google Sheets Client
--------------------

Read-only access to Google Sheets via OAuth refresh token.
Auth pattern matches tools/gmail.py (same env var style).

Usage:
    from tools.google_sheets import SheetsClient
    client = SheetsClient()
    values = client.read_sheet_values(spreadsheet_id, "LA MAKS FEB")
"""

import json
import logging
import re
import time
from os import getenv

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# Retry settings for transient network errors (timeouts, connection resets)
MAX_RETRIES = 2
RETRY_DELAY = 3  # seconds

_RETRYABLE = (TimeoutError, ConnectionError, OSError)


def _retry(func, *args, **kwargs):
    """Execute func with retry on transient network errors."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return func(*args, **kwargs)
        except _RETRYABLE as e:
            if attempt == MAX_RETRIES:
                raise
            logger.warning(
                "Transient error (attempt %d/%d): %s. Retrying in %ds...",
                attempt, MAX_RETRIES, e, RETRY_DELAY,
            )
            time.sleep(RETRY_DELAY)


class SheetsClient:
    """Google Sheets API client using OAuth refresh_token from env."""

    def __init__(self):
        self._service = None

    def _get_service(self):
        """Lazy-init Sheets API service."""
        if self._service:
            return self._service

        client_id = getenv("SHEETS_CLIENT_ID", "")
        client_secret = getenv("SHEETS_CLIENT_SECRET", "")
        refresh_token = getenv("SHEETS_REFRESH_TOKEN", "")

        if not all([client_id, client_secret, refresh_token]):
            raise RuntimeError(
                "Google Sheets not configured. Set SHEETS_CLIENT_ID, "
                "SHEETS_CLIENT_SECRET, SHEETS_REFRESH_TOKEN in .env"
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

        self._service = build("sheets", "v4", credentials=creds)
        logger.info("Google Sheets API service initialized (OAuth)")
        return self._service

    def get_sheet_names(self, spreadsheet_id: str) -> list[str]:
        """Get all sheet/tab names in a spreadsheet."""
        service = self._get_service()
        req = service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties.title",
        )
        meta = _retry(req.execute)

        names = [s["properties"]["title"] for s in meta.get("sheets", [])]
        logger.info("Spreadsheet %s has tabs: %s", spreadsheet_id, names)
        return names

    def find_active_sheet(
        self, spreadsheet_id: str, warehouse_pattern: str | None = None,
    ) -> str:
        """Find the active (current) sheet name.

        Priority:
        1. Explicit STOCK_SHEET_NAME env var
        2. Regex: tab matching warehouse_pattern without "N/A"
        3. Fallback: first tab without "N/A" prefix

        Args:
            spreadsheet_id: The spreadsheet to search.
            warehouse_pattern: Pattern to match in tab names (e.g., "LA MAKS").
                             Falls back to STOCK_WAREHOUSE_NAME env var if None.
        """
        explicit = getenv("STOCK_SHEET_NAME", "").strip()
        if explicit:
            logger.info("Using explicit sheet name: %s", explicit)
            return explicit

        names = self.get_sheet_names(spreadsheet_id)

        if warehouse_pattern is None:
            warehouse_pattern = getenv("STOCK_WAREHOUSE_NAME", "LA MAKS").replace("_", " ")

        # Priority 2: match warehouse pattern (e.g., "LA MAKS FEB") without "N/A"
        pattern = re.compile(
            rf"^(?!N/A).*{re.escape(warehouse_pattern)}",
            re.IGNORECASE,
        )
        for name in names:
            if pattern.match(name.strip()):
                logger.info("Found active sheet by pattern: %s", name)
                return name

        # Priority 3: first tab without "N/A"
        for name in names:
            if not name.strip().upper().startswith("N/A"):
                logger.info("Fallback: using first non-N/A sheet: %s", name)
                return name

        raise RuntimeError(
            f"No active sheet found in spreadsheet {spreadsheet_id}. "
            f"All tabs: {names}"
        )

    def read_sheet_values(
        self, spreadsheet_id: str, sheet_name: str,
    ) -> list[list[str]]:
        """Read all values from a sheet as a 2D string matrix.

        Returns list of rows, each row is a list of cell values (strings).
        Empty trailing cells are omitted by the API, so rows may vary in length.
        """
        service = self._get_service()
        req = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=sheet_name,
            valueRenderOption="UNFORMATTED_VALUE",
        )
        result = _retry(req.execute)

        values = result.get("values", [])
        logger.info(
            "Read %d rows from '%s' in spreadsheet %s",
            len(values), sheet_name, spreadsheet_id,
        )
        return values
