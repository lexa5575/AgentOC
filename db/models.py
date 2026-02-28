"""
Database Models
---------------

SQLAlchemy models for business data (clients, etc.).
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Session

from db.url import db_url

engine = create_engine(db_url)


class Base(DeclarativeBase):
    pass


class Client(Base):
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)
    payment_type = Column(String, nullable=False)  # "prepay" or "postpay"
    zelle_address = Column(String, default="")
    discount_percent = Column(Integer, default=0)
    discount_orders_left = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        """Convert to dict (same format as old TEST_CLIENTS values)."""
        return {
            "email": self.email,
            "name": self.name,
            "payment_type": self.payment_type,
            "zelle_address": self.zelle_address or "",
            "discount_percent": self.discount_percent or 0,
            "discount_orders_left": self.discount_orders_left or 0,
        }


class EmailHistory(Base):
    __tablename__ = "email_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_email = Column(String, nullable=False, index=True)
    direction = Column(String, nullable=False)  # "inbound" or "outbound"
    subject = Column(String, default="")
    body = Column(String, default="")
    situation = Column(String, default="other")
    gmail_message_id = Column(String, nullable=True, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "client_email": self.client_email,
            "direction": self.direction,
            "subject": self.subject,
            "body": self.body,
            "situation": self.situation,
            "created_at": self.created_at,
        }


class GmailState(Base):
    __tablename__ = "gmail_state"

    id = Column(Integer, primary_key=True)
    last_history_id = Column(String, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def get_session() -> Session:
    """Create a new database session."""
    return Session(engine)
