"""Tests for scripts.migrate_product_catalog."""

import scripts.migrate_product_catalog as migration
from db.models import ProductCatalog, StockItem


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self):
        self.executed: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, stmt):
        sql = str(stmt)
        self.executed.append(sql)
        if "information_schema.columns" in sql:
            return _FakeResult(None)  # column absent -> run ALTER
        if "FROM pg_indexes" in sql:
            return _FakeResult(None)  # index absent -> run CREATE INDEX
        return _FakeResult(None)

    def commit(self):
        return None


class _FakeEngine:
    def __init__(self):
        self.conn = _FakeConn()

    def connect(self):
        return self.conn


def test_migrate_product_catalog_backfills_product_ids(monkeypatch, db_session):
    # Seed existing stock rows without product_id.
    seed = db_session()
    try:
        seed.add_all([
            StockItem(warehouse="LA_MAKS", category="TEREA_EUROPE", product_name="Green", quantity=5),
            StockItem(warehouse="CHICAGO_MAX", category="TEREA_EUROPE", product_name=" green ", quantity=3),
            StockItem(warehouse="MIAMI_MAKS", category="ARMENIA", product_name="Green", quantity=2),
        ])
        seed.commit()
    finally:
        seed.close()

    fake_engine = _FakeEngine()
    monkeypatch.setattr(migration, "engine", fake_engine)
    monkeypatch.setattr(migration, "get_session", db_session)
    monkeypatch.setattr(migration.Base.metadata, "create_all", lambda eng: None)

    migration.migrate()

    verify = db_session()
    try:
        assert verify.query(StockItem).filter(StockItem.product_id.is_(None)).count() == 0

        # Two catalog entries expected:
        # 1) TEREA_EUROPE + green
        # 2) ARMENIA + green
        assert verify.query(ProductCatalog).count() == 2
    finally:
        verify.close()

    full_sql = "\n".join(fake_engine.conn.executed)
    assert "ALTER TABLE stock_items" in full_sql
    assert "CREATE INDEX ix_stock_items_product_id" in full_sql

