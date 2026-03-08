"""
Shared pytest fixtures — SQLite in-memory DB for fast isolated tests.
"""

import importlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from db.models import Base


@pytest.fixture(autouse=True)
def db_session(monkeypatch):
    """Create a fresh SQLite in-memory DB for every test.

    Patches get_session in all domain modules so they use this DB
    instead of PostgreSQL.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    def _get_session():
        return Session(engine)

    # Patch DB access only in modules that are importable in the current test context.
    # Some unittest-style suites inject lightweight stubs (e.g. fake `db` package) that
    # don't expose all submodules, so we skip missing targets instead of failing setup.
    for module_name in (
        "db.models",
        "db.clients",
        "db.email_history",
        "db.stock",
        "db.conversation_state",
        "db.product_resolver",
        "db.fulfillment",
        "db.catalog",
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        monkeypatch.setattr(module, "get_session", _get_session, raising=False)

    yield _get_session

    engine.dispose()
