"""
AgentOS
-------

The main entry point for AgentOS.

Run:
    python -m app.main
"""

import asyncio
import logging
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


async def _gmail_poll_loop():
    """Background loop: poll Gmail every GMAIL_POLL_INTERVAL seconds."""
    from tools.gmail_poller import poll_gmail

    logger.info("Gmail poller loop started (interval=%ds)", GMAIL_POLL_INTERVAL)
    while True:
        try:
            logger.info("Gmail poll cycle starting...")
            count = poll_gmail()
            logger.info("Gmail poll cycle done: %d messages processed", count)
        except Exception as e:
            logger.error("Gmail poll loop error: %s", e, exc_info=True)
        await asyncio.sleep(GMAIL_POLL_INTERVAL)


@app.on_event("startup")
async def start_gmail_poller():
    """Start Gmail polling if configured."""
    if getenv("GMAIL_REFRESH_TOKEN", ""):
        asyncio.create_task(_gmail_poll_loop())
        logger.info("Gmail poller scheduled (every %ds)", GMAIL_POLL_INTERVAL)
    else:
        logger.info("Gmail not configured, poller disabled")


@app.post("/api/gmail/poll")
async def trigger_gmail_poll():
    """Manual trigger for Gmail polling."""
    from tools.gmail_poller import poll_gmail

    count = poll_gmail()
    return {"processed": count}


if __name__ == "__main__":
    agent_os.serve(
        app="main:app",
        reload=getenv("RUNTIME_ENV", "prd") == "dev",
    )
