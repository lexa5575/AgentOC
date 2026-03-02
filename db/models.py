"""
Database Models
---------------

SQLAlchemy models for business data (clients, etc.).
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, UniqueConstraint, create_engine
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
    notes = Column(Text, default="")  # Manual operator notes
    llm_summary = Column(Text, default="")  # LLM-generated client summary
    summary_updated_at = Column(DateTime, nullable=True)
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
            "notes": self.notes or "",
            "llm_summary": self.llm_summary or "",
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
    gmail_thread_id = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "client_email": self.client_email,
            "direction": self.direction,
            "subject": self.subject,
            "body": self.body,
            "situation": self.situation,
            "gmail_thread_id": self.gmail_thread_id,
            "created_at": self.created_at,
        }


class GmailState(Base):
    __tablename__ = "gmail_state"

    id = Column(Integer, primary_key=True)
    last_history_id = Column(String, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ConversationState(Base):
    """Compact JSON state per Gmail thread — updated by State Updater LLM."""

    __tablename__ = "conversation_states"

    id = Column(Integer, primary_key=True, autoincrement=True)
    gmail_thread_id = Column(String, unique=True, nullable=False, index=True)
    client_email = Column(String, nullable=False, index=True)

    # Compact JSON state — updated by State Updater LLM
    state_json = Column(Text, default="{}")

    # Metadata
    message_count = Column(Integer, default=0)
    last_situation = Column(String, default="other")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "gmail_thread_id": self.gmail_thread_id,
            "client_email": self.client_email,
            "state_json": self.state_json,
            "message_count": self.message_count,
            "last_situation": self.last_situation,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class StockItem(Base):
    """Current stock levels per product per warehouse."""

    __tablename__ = "stock_items"
    __table_args__ = (
        UniqueConstraint("warehouse", "category", "product_name", name="uq_stock_item"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    warehouse = Column(String, nullable=False, index=True)
    category = Column(String, nullable=False)
    product_name = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False, default=0)
    is_fallback = Column(Boolean, default=False)
    source_row = Column(Integer, nullable=True)
    source_col = Column(Integer, nullable=True)
    synced_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "warehouse": self.warehouse,
            "category": self.category,
            "product_name": self.product_name,
            "quantity": self.quantity,
            "is_fallback": self.is_fallback,
            "source_row": self.source_row,
            "source_col": self.source_col,
            "synced_at": self.synced_at,
        }


class ClientOrderItem(Base):
    """Structured order items for customer preference tracking."""

    __tablename__ = "client_order_items"
    __table_args__ = (
        UniqueConstraint("client_email", "order_id", "base_flavor", name="uq_client_order_item"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_email = Column(String, nullable=False, index=True)
    order_id = Column(String, nullable=True)
    product_name = Column(String, nullable=False)
    base_flavor = Column(String, nullable=False)
    product_type = Column(String, nullable=False, default="stick")  # "stick" or "device"
    quantity = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)


class StockBackup(Base):
    """Previous valid stock snapshot (one backup for rollback)."""

    __tablename__ = "stock_backup"

    id = Column(Integer, primary_key=True, autoincrement=True)
    warehouse = Column(String, nullable=False)
    category = Column(String, nullable=False)
    product_name = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False, default=0)
    is_fallback = Column(Boolean, default=False)
    source_row = Column(Integer, nullable=True)
    source_col = Column(Integer, nullable=True)
    synced_at = Column(DateTime, nullable=True)


def get_session() -> Session:
    """Create a new database session."""
    return Session(engine)
