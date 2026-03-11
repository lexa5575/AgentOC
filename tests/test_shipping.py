"""Tests for db.shipping module — parse, select_package, address CRUD, job queue."""

import pytest

from db.shipping import (
    claim_next_shipping_job,
    complete_shipping_job,
    create_shipping_job,
    fail_shipping_job,
    get_order_shipping_address,
    parse_city_state_zip,
    save_order_shipping_address,
    select_package,
)


# ── parse_city_state_zip ──────────────────────────────────────────────

class TestParseCityStateZip:
    def test_comma_state_code_zip(self):
        result = parse_city_state_zip("Chicago, IL 60601")
        assert result == ("Chicago", "IL", "60601")

    def test_comma_full_state_zip(self):
        result = parse_city_state_zip("Springfield, Illinois 62701")
        assert result == ("Springfield", "IL", "62701")

    def test_comma_full_state_zip_texas(self):
        result = parse_city_state_zip("El Paso, Texas 79912")
        assert result == ("El Paso", "TX", "79912")

    def test_no_comma_state_code_zip(self):
        result = parse_city_state_zip("San Fernando CA 91340")
        assert result == ("San Fernando", "CA", "91340")

    def test_garbage_returns_none(self):
        assert parse_city_state_zip("garbage") is None

    def test_empty_returns_none(self):
        assert parse_city_state_zip("") is None
        assert parse_city_state_zip("   ") is None

    def test_zip_plus_four(self):
        result = parse_city_state_zip("Freedom, PA 15042-1960")
        assert result == ("Freedom", "PA", "15042-1960")

    def test_roseville(self):
        result = parse_city_state_zip("Roseville, CA 95747")
        assert result == ("Roseville", "CA", "95747")


# ── select_package ────────────────────────────────────────────────────

class TestSelectPackage:
    def test_single_device_only(self):
        items = [{"quantity": 1, "product_type": "device"}]
        assert select_package(items, "LA_MAKS", "CA") == "Z device"

    def test_device_plus_sticks_is_envelope(self):
        items = [
            {"quantity": 1, "product_type": "device"},
            {"quantity": 2, "product_type": "stick"},
        ]
        # total=3, different state → ENVELOPE
        assert select_package(items, "LA_MAKS", "NY") == "ENVELOPE"

    def test_same_state_3_is_shipping_bag(self):
        items = [{"quantity": 3, "product_type": "stick"}]
        assert select_package(items, "LA_MAKS", "CA") == "SHIPPING BAG"

    def test_different_state_3_is_envelope(self):
        items = [{"quantity": 3, "product_type": "stick"}]
        assert select_package(items, "LA_MAKS", "NY") == "ENVELOPE"

    def test_same_state_4_is_shipping_bag(self):
        items = [{"quantity": 4, "product_type": "stick"}]
        assert select_package(items, "CHICAGO_MAX", "IL") == "SHIPPING BAG"

    def test_different_state_4_is_shipping_bag(self):
        items = [{"quantity": 4, "product_type": "stick"}]
        assert select_package(items, "LA_MAKS", "NY") == "SHIPPING BAG"

    def test_over_12_returns_none(self):
        items = [{"quantity": 13, "product_type": "stick"}]
        assert select_package(items, "LA_MAKS", "CA") is None

    def test_single_stick_first_class(self):
        items = [{"quantity": 1, "product_type": "stick"}]
        assert select_package(items, "LA_MAKS", "CA") == "FIRST CLASS"

    def test_5_sticks_gbox(self):
        items = [{"quantity": 5, "product_type": "stick"}]
        assert select_package(items, "LA_MAKS", "CA") == "G Box 4-6"

    def test_7_sticks_medium_box(self):
        items = [{"quantity": 7, "product_type": "stick"}]
        assert select_package(items, "LA_MAKS", "CA") == "MEDIUM BOX"

    def test_12_sticks_medium_box(self):
        items = [{"quantity": 12, "product_type": "stick"}]
        assert select_package(items, "LA_MAKS", "CA") == "MEDIUM BOX"

    def test_device_with_sticks_not_z_device(self):
        """Device + sticks → NOT Z device (Z device = 1 device + 0 sticks only)."""
        items = [
            {"quantity": 1, "product_type": "device"},
            {"quantity": 1, "product_type": "stick"},
        ]
        # total=2, same state → SHIPPING BAG
        assert select_package(items, "LA_MAKS", "CA") == "SHIPPING BAG"


# ── Address snapshot CRUD (DB tests) ─────────────────────────────────

class TestAddressSnapshot:
    def test_save_and_get(self, db_session):
        save_order_shipping_address(
            "Test@Example.com", "ORD-1", "Test User",
            "123 Main St", "Chicago, IL 60601",
        )
        addr = get_order_shipping_address("test@example.com", "ORD-1")
        assert addr is not None
        assert addr["client_name"] == "Test User"
        assert addr["street"] == "123 Main St"
        assert addr["city_state_zip"] == "Chicago, IL 60601"

    def test_email_normalization(self, db_session):
        save_order_shipping_address(
            "  TEST@Example.COM  ", "ORD-2", "Name",
            "456 Oak Ave", "Miami, FL 33101",
        )
        # Lookup with different casing
        addr = get_order_shipping_address("test@example.com", "ORD-2")
        assert addr is not None
        assert addr["street"] == "456 Oak Ave"

    def test_upsert_updates_existing(self, db_session):
        save_order_shipping_address(
            "a@b.com", "ORD-3", "Old Name", "Old St", "Old, CA 90001",
        )
        save_order_shipping_address(
            "a@b.com", "ORD-3", "New Name", "New St", "New, CA 90002",
        )
        addr = get_order_shipping_address("a@b.com", "ORD-3")
        assert addr["client_name"] == "New Name"
        assert addr["street"] == "New St"

    def test_get_none_order_id(self, db_session):
        assert get_order_shipping_address("a@b.com", None) is None

    def test_get_missing(self, db_session):
        assert get_order_shipping_address("nobody@x.com", "ORD-99") is None


# ── Shipping job creation + queue (DB tests) ──────────────────────────

def _make_fulfillment_event(session_factory, email="test@example.com", order_id="ORD-1"):
    """Helper: insert a FulfillmentEvent and return its id."""
    from db.models import FulfillmentEvent
    session = session_factory()
    ev = FulfillmentEvent(
        client_email=email,
        order_id=order_id,
        trigger_type="new_order_postpay",
        status="updated",
        warehouse="LA_MAKS",
    )
    session.add(ev)
    session.commit()
    eid = ev.id
    session.close()
    return eid


class TestCreateShippingJob:
    def test_creates_pending_job(self, db_session, monkeypatch):
        monkeypatch.setattr("db.shipping.send_telegram", lambda *a, **kw: None)
        eid = _make_fulfillment_event(db_session)

        job_id = create_shipping_job(
            fulfillment_event_id=eid,
            client_email="test@example.com",
            order_id="ORD-1",
            client_name="Test User",
            street="123 Main St",
            city_state_zip="Roseville, CA 95747",
            address_source="order_snapshot",
            warehouse="LA_MAKS",
            items=[{"base_flavor": "Green", "quantity": 2, "product_type": "stick"}],
        )
        assert job_id is not None

        # Verify in DB
        from db.models import ShippingJob
        session = db_session()
        job = session.query(ShippingJob).filter_by(id=job_id).first()
        assert job.status == "pending"
        assert job.city == "Roseville"
        assert job.state == "CA"
        assert job.zipcode == "95747"
        assert job.package_type == "SHIPPING BAG"  # same state, 2 items
        assert job.address_source == "order_snapshot"
        session.close()

    def test_unparseable_address_returns_none(self, db_session, monkeypatch):
        sent = []
        monkeypatch.setattr("db.shipping.send_telegram", lambda msg, **kw: sent.append(msg))
        eid = _make_fulfillment_event(db_session)

        result = create_shipping_job(
            fulfillment_event_id=eid,
            client_email="t@x.com",
            order_id="ORD-1",
            client_name="X",
            street="123 St",
            city_state_zip="garbage address",
            address_source="order_snapshot",
            warehouse="LA_MAKS",
            items=[{"base_flavor": "Green", "quantity": 1, "product_type": "stick"}],
        )
        assert result is None
        assert len(sent) == 1  # Telegram alert sent

    def test_over_12_items_returns_none(self, db_session, monkeypatch):
        sent = []
        monkeypatch.setattr("db.shipping.send_telegram", lambda msg, **kw: sent.append(msg))
        eid = _make_fulfillment_event(db_session)

        result = create_shipping_job(
            fulfillment_event_id=eid,
            client_email="t@x.com",
            order_id="ORD-1",
            client_name="X",
            street="123 St",
            city_state_zip="Chicago, IL 60601",
            address_source="order_snapshot",
            warehouse="LA_MAKS",
            items=[{"base_flavor": "Green", "quantity": 13, "product_type": "stick"}],
        )
        assert result is None
        assert len(sent) == 1

    def test_duplicate_fulfillment_event_id(self, db_session, monkeypatch):
        monkeypatch.setattr("db.shipping.send_telegram", lambda *a, **kw: None)
        eid = _make_fulfillment_event(db_session)

        items = [{"base_flavor": "Green", "quantity": 1, "product_type": "stick"}]
        job1 = create_shipping_job(
            fulfillment_event_id=eid, client_email="t@x.com", order_id="ORD-1",
            client_name="X", street="1 St", city_state_zip="Miami, FL 33101",
            address_source="order_snapshot", warehouse="MIAMI_MAKS", items=items,
        )
        job2 = create_shipping_job(
            fulfillment_event_id=eid, client_email="t@x.com", order_id="ORD-1",
            client_name="X", street="1 St", city_state_zip="Miami, FL 33101",
            address_source="order_snapshot", warehouse="MIAMI_MAKS", items=items,
        )
        assert job1 is not None
        assert job2 is None  # duplicate blocked

    def test_client_record_source_sends_telegram(self, db_session, monkeypatch):
        sent = []
        monkeypatch.setattr("db.shipping.send_telegram", lambda msg, **kw: sent.append(msg))
        eid = _make_fulfillment_event(db_session)

        create_shipping_job(
            fulfillment_event_id=eid, client_email="t@x.com", order_id="ORD-1",
            client_name="X", street="1 St", city_state_zip="Miami, FL 33101",
            address_source="client_record", warehouse="MIAMI_MAKS",
            items=[{"base_flavor": "Green", "quantity": 1, "product_type": "stick"}],
        )
        assert any("client record" in s for s in sent)


class TestJobQueue:
    def _create_job(self, db_session, monkeypatch, email="t@x.com", order_id="ORD-1"):
        monkeypatch.setattr("db.shipping.send_telegram", lambda *a, **kw: None)
        eid = _make_fulfillment_event(db_session, email=email, order_id=order_id)
        return create_shipping_job(
            fulfillment_event_id=eid, client_email=email, order_id=order_id,
            client_name="Test", street="1 St", city_state_zip="Chicago, IL 60601",
            address_source="order_snapshot", warehouse="CHICAGO_MAX",
            items=[{"base_flavor": "Green", "quantity": 1, "product_type": "stick"}],
        )

    def test_claim_returns_job(self, db_session, monkeypatch):
        self._create_job(db_session, monkeypatch)
        job = claim_next_shipping_job()
        assert job is not None
        assert job["status"] == "claimed"
        assert job["claim_token"] is not None
        assert job["retry_count"] == 1

    def test_claim_empty_queue(self, db_session):
        assert claim_next_shipping_job() is None

    def test_complete_with_correct_token(self, db_session, monkeypatch):
        self._create_job(db_session, monkeypatch)
        job = claim_next_shipping_job()
        ok = complete_shipping_job(job["id"], job["claim_token"])
        assert ok is True

        # Verify status in DB
        from db.models import ShippingJob
        session = db_session()
        j = session.query(ShippingJob).filter_by(id=job["id"]).first()
        assert j.status == "filled"
        assert j.filled_at is not None
        session.close()

    def test_complete_with_wrong_token(self, db_session, monkeypatch):
        self._create_job(db_session, monkeypatch)
        job = claim_next_shipping_job()
        ok = complete_shipping_job(job["id"], "wrong-token")
        assert ok is False

    def test_transient_fail_requeues(self, db_session, monkeypatch):
        self._create_job(db_session, monkeypatch)
        job = claim_next_shipping_job()
        ok = fail_shipping_job(job["id"], job["claim_token"], "transient error")
        assert ok is True

        from db.models import ShippingJob
        session = db_session()
        j = session.query(ShippingJob).filter_by(id=job["id"]).first()
        assert j.status == "pending"
        assert j.claim_token is None
        assert j.claimed_until is None
        session.close()

    def test_permanent_fail(self, db_session, monkeypatch):
        self._create_job(db_session, monkeypatch)
        job = claim_next_shipping_job()
        ok = fail_shipping_job(job["id"], job["claim_token"], "fatal", permanent=True)
        assert ok is True

        from db.models import ShippingJob
        session = db_session()
        j = session.query(ShippingJob).filter_by(id=job["id"]).first()
        assert j.status == "failed"
        session.close()

    def test_reset_retry_decrements(self, db_session, monkeypatch):
        self._create_job(db_session, monkeypatch)
        job = claim_next_shipping_job()
        assert job["retry_count"] == 1
        fail_shipping_job(job["id"], job["claim_token"], "auth", reset_retry=True)

        from db.models import ShippingJob
        session = db_session()
        j = session.query(ShippingJob).filter_by(id=job["id"]).first()
        assert j.retry_count == 0
        session.close()

    def test_exhausted_job_marked_failed(self, db_session, monkeypatch):
        self._create_job(db_session, monkeypatch)

        # Claim and requeue 3 times
        for _ in range(3):
            job = claim_next_shipping_job()
            if job:
                fail_shipping_job(job["id"], job["claim_token"], "err")

        # Next claim triggers cleanup
        result = claim_next_shipping_job()
        assert result is None

        from db.models import ShippingJob
        session = db_session()
        j = session.query(ShippingJob).first()
        assert j.status == "failed"
        assert j.error_message == "max retries exceeded"
        session.close()

    def test_claimed_job_not_double_claimed(self, db_session, monkeypatch):
        self._create_job(db_session, monkeypatch)
        job1 = claim_next_shipping_job()
        job2 = claim_next_shipping_job()
        assert job1 is not None
        assert job2 is None  # already claimed, not expired
