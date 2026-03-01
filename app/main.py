"""
AgentOS
-------

The main entry point for AgentOS.

Run:
    python -m app.main
"""

import logging
import threading
import time
from os import getenv
from pathlib import Path

# Configure logging BEFORE any other imports that use loggers
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from agno.os import AgentOS

from agents.admin_agent import admin_agent
from agents.email_agent import email_agent
from agents.knowledge_agent import knowledge_agent
from agents.mcp_agent import mcp_agent
from db import get_postgres_db, init_default_data

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Initialize database tables and default data
# ---------------------------------------------------------------------------
init_default_data()

# ---------------------------------------------------------------------------
# Create AgentOS
# ---------------------------------------------------------------------------
agent_os = AgentOS(
    name="AgentOS",
    tracing=True,
    scheduler=True,
    db=get_postgres_db(),
    agents=[knowledge_agent, mcp_agent, email_agent, admin_agent],
    config=str(Path(__file__).parent / "config.yaml"),
    cors_allowed_origins=["https://ui.aleksei-chuprynin.cv"],
)

app = agent_os.get_app()

# ---------------------------------------------------------------------------
# Gmail Poller — background task + manual trigger endpoint
# ---------------------------------------------------------------------------
GMAIL_POLL_INTERVAL = 60  # seconds
STOCK_SYNC_INTERVAL = int(getenv("STOCK_SYNC_INTERVAL", "300"))  # seconds (5 min default)


def _gmail_poll_thread():
    """Background thread: poll Gmail every GMAIL_POLL_INTERVAL seconds."""
    from tools.gmail_poller import poll_gmail

    logger.info("Gmail poller thread started (interval=%ds)", GMAIL_POLL_INTERVAL)
    while True:
        try:
            count = poll_gmail()
            if count:
                logger.info("Gmail poll: %d messages processed", count)
        except Exception as e:
            logger.error("Gmail poll thread error: %s", e, exc_info=True)
        time.sleep(GMAIL_POLL_INTERVAL)


# Start Gmail poller as daemon thread (dies with main process)
if getenv("GMAIL_REFRESH_TOKEN", ""):
    threading.Thread(target=_gmail_poll_thread, daemon=True).start()
    logger.info("Gmail poller thread launched (every %ds)", GMAIL_POLL_INTERVAL)
else:
    logger.info("Gmail not configured, poller disabled")


@app.post("/api/gmail/poll")
async def trigger_gmail_poll():
    """Manual trigger for Gmail polling."""
    from tools.gmail_poller import poll_gmail

    count = poll_gmail()
    return {"processed": count}


# ---------------------------------------------------------------------------
# Stock Sync — background task + manual trigger endpoint
# ---------------------------------------------------------------------------

def _stock_sync_thread():
    """Background thread: sync stock every STOCK_SYNC_INTERVAL seconds."""
    from tools.stock_sync import sync_stock_from_sheets

    # Wait a bit on startup to let the app initialize
    time.sleep(10)
    logger.info("Stock sync thread started (interval=%ds)", STOCK_SYNC_INTERVAL)

    while True:
        try:
            result = sync_stock_from_sheets()
            if result.get("status") == "ok":
                logger.info(
                    "Stock sync: %d items (%d available)",
                    result.get("synced", 0), result.get("available", 0),
                )
        except Exception as e:
            logger.error("Stock sync thread error: %s", e, exc_info=True)
        time.sleep(STOCK_SYNC_INTERVAL)


# Start stock sync as daemon thread (dies with main process)
if getenv("STOCK_SPREADSHEET_ID", ""):
    threading.Thread(target=_stock_sync_thread, daemon=True).start()
    logger.info("Stock sync thread launched (every %ds)", STOCK_SYNC_INTERVAL)
else:
    logger.info("Stock sync not configured, disabled")


@app.post("/api/stock/sync")
async def trigger_stock_sync():
    """Manual trigger for stock synchronization."""
    from tools.stock_sync import sync_stock_from_sheets

    result = sync_stock_from_sheets()
    return result


if __name__ == "__main__":
    agent_os.serve(
        app="main:app",
        reload=getenv("RUNTIME_ENV", "prd") == "dev",
    )
