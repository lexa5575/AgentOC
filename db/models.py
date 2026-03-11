"""
Database Models
---------------

SQLAlchemy models for business data (clients, etc.).
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine
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
    street = Column(String, default="")
    city_state_zip = Column(String, default="")
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
            "street": self.street or "",
            "city_state_zip": self.city_state_zip or "",
            "discount_percent": self.discount_percent or 0,
            "discount_orders_left": self.discount_orders_left or 0,
            "notes": self.notes or "",
            "llm_summary": self.llm_summary or "",
            "summary_updated_at": self.summary_updated_at,
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
            "gmail_message_id": self.gmail_message_id,
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
    maks_sales = Column(Integer, nullable=False, default=0)
    is_fallback = Column(Boolean, default=False)
    source_row = Column(Integer, nullable=True)
    source_col = Column(Integer, nullable=True)
    product_id = Column(Integer, ForeignKey("product_catalog.id"), nullable=True, index=True)
    synced_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "warehouse": self.warehouse,
            "category": self.category,
            "product_name": self.product_name,
            "quantity": self.quantity,
            "maks_sales": self.maks_sales,
            "is_fallback": self.is_fallback,
            "source_row": self.source_row,
            "source_col": self.source_col,
            "product_id": self.product_id,
            "synced_at": self.synced_at,
        }


class ClientOrderItem(Base):
    """Structured order items for customer preference tracking."""

    __tablename__ = "client_order_items"
    # Phase 9: old UniqueConstraint("client_email", "order_id", "base_flavor",
    # name="uq_client_order_item") removed. Superseded by partial unique index
    # uq_client_order_variant (client_email, order_id, variant_id)
    # WHERE variant_id IS NOT NULL AND order_id IS NOT NULL.

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_email = Column(String, nullable=False, index=True)
    order_id = Column(String, nullable=True)
    product_name = Column(String, nullable=False)
    base_flavor = Column(String, nullable=False)
    product_type = Column(String, nullable=False, default="stick")  # "stick" or "device"
    quantity = Column(Integer, default=1)
    variant_id = Column(Integer, ForeignKey("product_catalog.id"), nullable=True, index=True)
    display_name_snapshot = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class StockBackup(Base):
    """Previous valid stock snapshot (one backup for rollback)."""

    __tablename__ = "stock_backup"

    id = Column(Integer, primary_key=True, autoincrement=True)
    warehouse = Column(String, nullable=False)
    category = Column(String, nullable=False)
    product_name = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False, default=0)
    maks_sales = Column(Integer, nullable=False, default=0)
    is_fallback = Column(Boolean, default=False)
    source_row = Column(Integer, nullable=True)
    source_col = Column(Integer, nullable=True)
    synced_at = Column(DateTime, nullable=True)


class ProductCatalog(Base):
    """Canonical product identity — one entry per unique product across all warehouses."""

    __tablename__ = "product_catalog"
    __table_args__ = (
        UniqueConstraint("category", "name_norm", name="uq_catalog_entry"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    category = Column(String, nullable=False, index=True)    # "TEREA_JAPAN"
    name_norm = Column(String, nullable=False)                # "t purple" (lower, trimmed)
    stock_name = Column(String, nullable=False)               # "T Purple" (original from sheet)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "category": self.category,
            "name_norm": self.name_norm,
            "stock_name": self.stock_name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class SheetConfig(Base):
    """LLM-generated sheet structure config per warehouse."""

    __tablename__ = "sheet_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    warehouse = Column(String, unique=True, nullable=False, index=True)
    config_json = Column(Text, nullable=False)
    analyzed_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class FulfillmentEvent(Base):
    """Tracks maks_sales increments for idempotency."""

    __tablename__ = "fulfillment_events"
    __table_args__ = (
        UniqueConstraint(
            "gmail_message_id", "trigger_type",
            name="uq_fulfillment_gmail_trigger",
        ),
        UniqueConstraint(
            "client_email", "order_id", "trigger_type",
            name="uq_fulfillment_email_order_trigger",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_email = Column(String, nullable=False, index=True)
    order_id = Column(String, nullable=True)
    gmail_message_id = Column(String, nullable=True)
    trigger_type = Column(String, nullable=False)   # "new_order_postpay" / "payment_received_prepay"
    status = Column(String, nullable=False)          # "updated" / "skipped_split" / etc.
    warehouse = Column(String, nullable=True)
    details_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class OrderShippingAddress(Base):
    """Shipping address snapshot, keyed to order. UPSERT on new_order emails.
    Address is copied into ShippingJob at creation and frozen there."""

    __tablename__ = "order_shipping_addresses"
    __table_args__ = (
        UniqueConstraint("client_email", "order_id", name="uq_order_shipping_addr"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_email = Column(String, nullable=False, index=True)
    order_id = Column(String, nullable=False)
    client_name = Column(String, nullable=False)
    street = Column(String, nullable=False)
    city_state_zip = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ShippingJob(Base):
    """Auto-fill job for PirateShip. Created after successful fulfillment."""

    __tablename__ = "shipping_jobs"
    __table_args__ = (
        UniqueConstraint("fulfillment_event_id", name="uq_shipping_fulfillment"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    fulfillment_event_id = Column(Integer, ForeignKey("fulfillment_events.id"), nullable=False)
    client_email = Column(String, nullable=False, index=True)
    order_id = Column(String, nullable=True)
    client_name = Column(String, nullable=False)
    street = Column(String, nullable=False)
    city = Column(String, nullable=False)
    state = Column(String, nullable=False)
    zipcode = Column(String, nullable=False)
    address_source = Column(String, nullable=False)  # "order_snapshot" or "client_record"
    warehouse = Column(String, nullable=False)
    items_json = Column(Text, nullable=False)
    package_type = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending")  # pending/claimed/filled/failed
    claim_token = Column(String, nullable=True)
    claimed_until = Column(DateTime, nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    claimed_at = Column(DateTime, nullable=True)
    filled_at = Column(DateTime, nullable=True)


def get_session() -> Session:
    """Create a new database session."""
    return Session(engine)
