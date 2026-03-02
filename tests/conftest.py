"""
Shared pytest fixtures — SQLite in-memory DB for fast isolated tests.
"""

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

    monkeypatch.setattr("db.clients.get_session", _get_session)
    monkeypatch.setattr("db.email_history.get_session", _get_session)
    monkeypatch.setattr("db.stock.get_session", _get_session)

    yield _get_session

    engine.dispose()
