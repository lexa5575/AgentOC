"""
Web Search Tool Layer
Abstraction over web search — swap provider without changing agents.
Currently uses DuckDuckGo (free, no API key).
"""

from agno.tools.duckduckgo import DuckDuckGoTools


def get_search_tools():
    """Return web search tools for agents."""
    return DuckDuckGoTools(
        enable_search=True,
        enable_news=False,
        fixed_max_results=3,
    )
