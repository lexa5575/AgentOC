"""
AgentOS
-------

The main entry point for AgentOS.

Run:
    python -m app.main
"""

from os import getenv
from pathlib import Path

from agno.os import AgentOS

from agents.admin_agent import admin_agent
from agents.email_agent import email_agent
from agents.knowledge_agent import knowledge_agent
from agents.mcp_agent import mcp_agent
from db import get_postgres_db, init_default_data

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

if __name__ == "__main__":
    agent_os.serve(
        app="main:app",
        reload=getenv("RUNTIME_ENV", "prd") == "dev",
    )
