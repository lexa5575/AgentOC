"""
Email Agent
-----------

Agent wiring for the email processing pipeline.

`classify_and_process` is imported from agents.pipeline and re-exported
so tools/gmail_poller.py can continue to use:
    from agents.email_agent import classify_and_process

`classifier_agent` is imported here so test suites can patch
    self.email_agent.classifier_agent.run
until Phase 4 migrates those patches to self.agents_classifier.
"""

import logging

from agno.agent import Agent
from agno.models.openai import OpenAIResponses

from agents.classifier import classifier_agent  # noqa: F401 — test patching compatibility
from agents.pipeline import classify_and_process  # noqa: F401 — re-export for gmail_poller
from db import get_postgres_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
agent_db = get_postgres_db()

# ---------------------------------------------------------------------------
# Main Email Agent (orchestrates the workflow, visible in AgentOS UI)
# ---------------------------------------------------------------------------
email_agent_instructions = """\
You are an email processing assistant for shipmecarton.com.

When a user gives you an email to process:
1. Call the `classify_and_process` tool.
2. Copy the tool output to the user EXACTLY as-is. Do not change a single character.

ABSOLUTE RULES:
- Copy the ENTIRE tool output verbatim — every line, every symbol, every space.
- Do NOT rephrase, summarize, reformat, or restructure the output.
- Do NOT add greetings, commentary, or explanations before or after.
- Do NOT change "===" separators to other formatting.
- Do NOT merge lines or split lines differently.
- The tool output IS your response. Nothing more, nothing less.
"""

email_agent = Agent(
    id="email-agent",
    name="Email Agent",
    model=OpenAIResponses(id="gpt-5.2"),
    db=agent_db,
    instructions=email_agent_instructions,
    tools=[classify_and_process],
    enable_agentic_memory=True,
    add_datetime_to_context=True,
    add_history_to_context=True,
    read_chat_history=True,
    num_history_runs=5,
    markdown=False,
)
