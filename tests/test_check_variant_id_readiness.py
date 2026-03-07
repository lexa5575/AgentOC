"""Tests for Phase 7: variant_id readiness check script.

Uses SQLite in-memory DB via the shared db_session fixture.
"""

import json
from unittest.mock import patch

import pytest
from sqlalchemy import text

from db.models import ClientOrderItem, FulfillmentEvent
from scripts.check_variant_id_readiness import run_readiness_check


def _create_index(session):
    """Create the uq_client_order_variant partial unique index."""
    conn = session.get_bind().connect()
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_client_order_variant "
        "ON client_order_items (client_email, order_id, variant_id) "
        "WHERE variant_id IS NOT NULL AND order_id IS NOT NULL"
    ))
    conn.commit()
    conn.close()


def _add_item(session, email, order_id, base_flavor, variant_id=None):
    """Insert a ClientOrderItem (minimal fields)."""
    item = ClientOrderItem(
        client_email=email.lower().strip(),
        order_id=order_id,
        product_name=base_flavor,
        base_flavor=base_flavor,
        product_type="stick",
        quantity=1,
        variant_id=variant_id,
    )
    session.add(item)
    session.flush()
    return item


class TestCheckVariantIdReadiness:

    def test_go_true_high_coverage(self, db_session):
        """[P7-T1] go=true when coverage >= threshold and no blocking issues."""
        session = db_session()
        # 19 with variant_id, 1 without → 95% coverage
        for i in range(19):
            _add_item(session, f"u{i}@example.com", f"#{i}", "Silver", variant_id=10)
        _add_item(session, "null@example.com", "#99", "Bronze", variant_id=None)
        session.commit()
        _create_index(session)

        engine = session.get_bind()
        report = run_readiness_check(
            min_coverage=95.0,
            max_ambiguous_pct=5.1,
            bind=engine,
        )

        assert report["go_no_go"] is True
        assert report["reasons"] == []
        assert report["total_rows"] == 20
        assert report["with_variant_rows"] == 19
        assert report["null_variant_rows"] == 1
        assert report["pct_with_variant"] == 95.0
        assert report["partial_unique_index_exists"] is True

    def test_go_false_low_coverage(self, db_session):
        """[P7-T2] go=false when coverage below threshold."""
        session = db_session()
        # 1 with, 9 without → 10% coverage
        _add_item(session, "a@example.com", "#1", "Silver", variant_id=10)
        for i in range(9):
            _add_item(session, f"n{i}@example.com", f"#N{i}", "Bronze", variant_id=None)
        session.commit()

        engine = session.get_bind()
        report = run_readiness_check(min_coverage=95.0, bind=engine)

        assert report["go_no_go"] is False
        assert any("coverage" in r for r in report["reasons"])
        assert report["pct_with_variant"] == 10.0

    def test_go_false_duplicate_groups(self, db_session):
        """[P7-T3] go=false when duplicate groups exist."""
        session = db_session()
        # Two rows with same (email, order_id, variant_id)
        # Use different base_flavor to bypass old unique constraint
        _add_item(session, "a@example.com", "#1", "Silver", variant_id=10)
        _add_item(session, "a@example.com", "#1", "Silver EU", variant_id=10)
        session.commit()

        engine = session.get_bind()
        report = run_readiness_check(
            min_coverage=0.0,
            max_ambiguous_pct=100.0,
            bind=engine,
        )

        assert report["go_no_go"] is False
        assert report["duplicate_groups_for_uq_client_order_variant"] == 1
        assert any("duplicate" in r for r in report["reasons"])

    def test_report_has_required_keys(self, db_session):
        """[P7-T4] Report JSON contains all required keys."""
        session = db_session()
        _add_item(session, "a@example.com", "#1", "Silver", variant_id=10)
        session.commit()

        engine = session.get_bind()
        report = run_readiness_check(bind=engine)

        required_keys = {
            "total_rows",
            "with_variant_rows",
            "null_variant_rows",
            "pct_with_variant",
            "duplicate_groups_for_uq_client_order_variant",
            "partial_unique_index_exists",
            "blocked_ambiguous_last_24h",
            "unresolved_strict_last_24h",
            "go_no_go",
            "reasons",
        }
        assert required_keys.issubset(set(report.keys()))

    def test_empty_table_go_false(self, db_session):
        """[P7-T5] Empty table → coverage 0% → go=false."""
        session = db_session()
        session.commit()

        engine = session.get_bind()
        report = run_readiness_check(bind=engine)

        assert report["go_no_go"] is False
        assert report["total_rows"] == 0
        assert report["pct_with_variant"] == 0.0

    def test_stuck_processing_blocks_go(self, db_session):
        """[P7-T6] Stuck processing events → go=false."""
        session = db_session()
        # Good coverage
        for i in range(20):
            _add_item(session, f"u{i}@example.com", f"#{i}", "Silver", variant_id=10)
        # Stuck fulfillment event
        event = FulfillmentEvent(
            client_email="stuck@example.com",
            order_id="#STUCK",
            trigger_type="new_order_postpay",
            status="processing",
        )
        session.add(event)
        session.commit()

        engine = session.get_bind()
        report = run_readiness_check(
            min_coverage=0.0,
            max_ambiguous_pct=100.0,
            bind=engine,
        )

        assert report["go_no_go"] is False
        assert report["stuck_processing_events"] == 1
        assert any("stuck" in r for r in report["reasons"])

    def test_custom_thresholds(self, db_session):
        """Custom thresholds override defaults."""
        session = db_session()
        # 8 with, 2 without → 80% coverage
        for i in range(8):
            _add_item(session, f"u{i}@example.com", f"#{i}", "Silver", variant_id=10)
        _add_item(session, "n1@example.com", "#N1", "Bronze")
        _add_item(session, "n2@example.com", "#N2", "Gold")
        session.commit()
        _create_index(session)

        engine = session.get_bind()

        # Default 95% → fail
        r1 = run_readiness_check(bind=engine)
        assert r1["go_no_go"] is False

        # Custom 80% coverage, 25% ambiguous → pass
        r2 = run_readiness_check(
            min_coverage=80.0,
            max_ambiguous_pct=25.0,
            bind=engine,
        )
        assert r2["go_no_go"] is True

    def test_unresolved_strict_count_uses_blocked_reason(self, db_session):
        """[P7.1-T1] unresolved_strict counts blocked events with reason=unresolved_variant_strict."""
        session = db_session()
        _add_item(session, "a@example.com", "#1", "Silver", variant_id=10)
        session.commit()

        # blocked event with reason=unresolved_variant_strict in details_json
        event = FulfillmentEvent(
            client_email="blocked@example.com",
            order_id="#B1",
            trigger_type="payment_received_prepay",
            status="blocked_ambiguous_variant",
            details_json=json.dumps({
                "v": 2,
                "reason": "unresolved_variant_strict",
                "skipped_items": [{"base_flavor": "Silver"}],
            }),
        )
        session.add(event)
        # Another blocked event with reason=ambiguous_variant (should NOT count)
        event2 = FulfillmentEvent(
            client_email="ambig@example.com",
            order_id="#B2",
            trigger_type="payment_received_prepay",
            status="blocked_ambiguous_variant",
            details_json=json.dumps({
                "v": 2,
                "reason": "ambiguous_variant",
                "skipped_items": [{"base_flavor": "Bronze", "product_ids_count": 3}],
            }),
        )
        session.add(event2)
        session.commit()

        engine = session.get_bind()
        report = run_readiness_check(
            min_coverage=0.0,
            max_ambiguous_pct=100.0,
            bind=engine,
        )

        assert report["unresolved_strict_last_24h"] == 1
        assert report["blocked_ambiguous_last_24h"] == 2

    def test_go_false_when_index_missing(self, db_session):
        """[P7.1-T3] Missing uq_client_order_variant → go=false."""
        session = db_session()
        # Perfect coverage, no blockers — but no index
        _add_item(session, "a@example.com", "#1", "Silver", variant_id=10)
        session.commit()

        engine = session.get_bind()
        report = run_readiness_check(
            min_coverage=0.0,
            max_ambiguous_pct=100.0,
            bind=engine,
        )

        assert report["go_no_go"] is False
        assert report["partial_unique_index_exists"] is False
        assert any("uq_client_order_variant" in r for r in report["reasons"])

    def test_go_true_when_index_present(self, db_session):
        """[P7.1-T4] Index present + good coverage → index blocker removed."""
        session = db_session()
        for i in range(20):
            _add_item(session, f"u{i}@example.com", f"#{i}", "Silver", variant_id=10)
        session.commit()
        _create_index(session)

        engine = session.get_bind()
        report = run_readiness_check(
            min_coverage=0.0,
            max_ambiguous_pct=100.0,
            bind=engine,
        )

        assert report["partial_unique_index_exists"] is True
        # No index-related reason
        assert not any("uq_client_order_variant" in r for r in report["reasons"])

    def test_fail_closed_on_fulfillment_events_error(self, db_session):
        """[P8.1-T1] Exception in _check_fulfillment_events → go_no_go=False."""
        session = db_session()
        # Perfect coverage, index present — would be go=True normally
        for i in range(20):
            _add_item(session, f"u{i}@example.com", f"#{i}", "Silver", variant_id=10)
        # Insert a blocked event so the details_json parsing path runs
        event = FulfillmentEvent(
            client_email="err@example.com",
            order_id="#ERR",
            trigger_type="new_order_postpay",
            status="blocked_ambiguous_variant",
            details_json="INVALID_JSON_BOOM",
        )
        session.add(event)
        session.commit()
        _create_index(session)

        engine = session.get_bind()

        # Patch parse_details_json at its source module — the local import
        # inside _check_fulfillment_events picks it up from db.fulfillment.
        with patch(
            "db.fulfillment.parse_details_json",
            side_effect=RuntimeError("simulated parse crash"),
        ):
            report = run_readiness_check(
                min_coverage=0.0,
                max_ambiguous_pct=100.0,
                bind=engine,
            )

        assert report["go_no_go"] is False
        assert any("fulfillment_events check failed" in r for r in report["reasons"])
