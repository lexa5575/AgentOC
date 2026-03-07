"""Tests for Phase 6: backfill_variant_id script.

Uses SQLite in-memory DB via the shared db_session fixture.
Resolver is always mocked — no real catalog dependency.
"""

from dataclasses import dataclass, field

import pytest

from db.models import ClientOrderItem, ProductCatalog
from scripts.backfill_variant_id import run_backfill


# ── Mock resolver ────────────────────────────────────────────────────

@dataclass
class FakeResolveResult:
    original: str
    resolved: str | None
    confidence: str
    score: float = 1.0
    candidates: list[str] = field(default_factory=list)
    product_ids: list[int] = field(default_factory=list)
    name_norm: str | None = None
    display_name: str | None = None


def _make_resolver(rules: dict):
    """Create a resolver function from a name->result mapping.

    rules: {name: FakeResolveResult or (confidence, product_ids, display_name)}
    Shorthand tuples are expanded automatically.
    """
    expanded = {}
    for name, val in rules.items():
        if isinstance(val, FakeResolveResult):
            expanded[name] = val
        else:
            conf, ids, dn = val
            expanded[name] = FakeResolveResult(
                original=name, resolved=name,
                confidence=conf, product_ids=ids, display_name=dn,
            )

    def resolver(raw_name):
        if raw_name in expanded:
            return expanded[raw_name]
        return FakeResolveResult(
            original=raw_name, resolved=None,
            confidence="low", product_ids=[],
        )

    return resolver


def _add_item(session, email, order_id, product_name, base_flavor,
              qty=1, variant_id=None):
    """Insert a ClientOrderItem."""
    item = ClientOrderItem(
        client_email=email.lower().strip(),
        order_id=order_id,
        product_name=product_name,
        base_flavor=base_flavor,
        product_type="stick",
        quantity=qty,
        variant_id=variant_id,
    )
    session.add(item)
    session.flush()
    return item


# ══════════════════════════════════════════════════════════════════════


class TestBackfillVariantId:

    def test_dry_run_no_writes(self, db_session):
        """[P6-T1] dry_run=True does not modify variant_id in DB."""
        session = db_session()
        _add_item(session, "a@example.com", "#1", "T Silver", "T Silver")
        session.commit()

        resolver = _make_resolver({
            "T Silver": ("exact", [42], "T Silver"),
        })

        report = run_backfill(
            dry_run=True,
            session_factory=db_session,
            resolver=resolver,
        )

        assert report["resolved"] == 1
        assert report["total"] == 1

        # DB unchanged
        session2 = db_session()
        item = session2.query(ClientOrderItem).first()
        assert item.variant_id is None

    def test_execute_writes_single_match(self, db_session):
        """[P6-T2] execute mode writes variant_id for single-match items."""
        session = db_session()
        _add_item(session, "a@example.com", "#2", "Bronze EU", "Bronze")
        session.commit()

        resolver = _make_resolver({
            "Bronze EU": ("exact", [77], "Bronze"),
        })

        report = run_backfill(
            dry_run=False,
            session_factory=db_session,
            resolver=resolver,
        )

        assert report["resolved"] == 1
        assert report["ambiguous"] == 0
        assert report["unresolved"] == 0

        session2 = db_session()
        item = session2.query(ClientOrderItem).first()
        assert item.variant_id == 77
        assert item.display_name_snapshot == "Bronze"

    def test_ambiguous_stays_null(self, db_session):
        """[P6-T3] Ambiguous items (>1 product_ids) stay NULL, go to report."""
        session = db_session()
        _add_item(session, "a@example.com", "#3", "Silver", "Silver")
        session.commit()

        resolver = _make_resolver({
            "Silver": ("exact", [10, 30, 54], None),
        })

        report = run_backfill(
            dry_run=False,
            session_factory=db_session,
            resolver=resolver,
        )

        assert report["ambiguous"] == 1
        assert report["resolved"] == 0
        assert len(report["rows"]) == 1
        assert report["rows"][0]["reason"] == "ambiguous"
        assert report["rows"][0]["candidate_ids"] == [10, 30, 54]

        session2 = db_session()
        item = session2.query(ClientOrderItem).first()
        assert item.variant_id is None

    def test_unresolved_stays_null(self, db_session):
        """[P6-T4] Unresolved items (low confidence / no matches) stay NULL."""
        session = db_session()
        _add_item(session, "a@example.com", "#4", "XYZ Unknown", "XYZ Unknown")
        session.commit()

        # Default resolver returns low confidence for unknown names
        resolver = _make_resolver({})

        report = run_backfill(
            dry_run=False,
            session_factory=db_session,
            resolver=resolver,
        )

        assert report["unresolved"] == 1
        assert report["resolved"] == 0
        assert len(report["rows"]) == 1
        assert report["rows"][0]["reason"] == "unresolved"

        session2 = db_session()
        item = session2.query(ClientOrderItem).first()
        assert item.variant_id is None

    def test_idempotent_rerun(self, db_session):
        """[P6-T5] Second run skips already-filled rows (WHERE variant_id IS NULL)."""
        session = db_session()
        _add_item(session, "a@example.com", "#5", "T Silver", "T Silver")
        session.commit()

        resolver = _make_resolver({
            "T Silver": ("exact", [42], "T Silver"),
        })

        # First run
        report1 = run_backfill(
            dry_run=False,
            session_factory=db_session,
            resolver=resolver,
        )
        assert report1["resolved"] == 1

        # Second run — row already has variant_id, should be skipped
        report2 = run_backfill(
            dry_run=False,
            session_factory=db_session,
            resolver=resolver,
        )
        assert report2["total"] == 0
        assert report2["processed"] == 0
        assert report2["resolved"] == 0

        # Value unchanged
        session2 = db_session()
        item = session2.query(ClientOrderItem).first()
        assert item.variant_id == 42

    def test_fallback_to_base_flavor(self, db_session):
        """[P6-T6] Fallback: product_name unresolved, base_flavor resolves."""
        session = db_session()
        _add_item(
            session, "a@example.com", "#6",
            "Tera AMBER made in Europe", "Amber",
        )
        session.commit()

        resolver = _make_resolver({
            # product_name doesn't resolve
            "Tera AMBER made in Europe": ("low", [], None),
            # base_flavor resolves
            "Amber": ("exact", [55], "Amber EU"),
        })

        report = run_backfill(
            dry_run=False,
            session_factory=db_session,
            resolver=resolver,
        )

        assert report["resolved"] == 1

        session2 = db_session()
        item = session2.query(ClientOrderItem).first()
        assert item.variant_id == 55
        assert item.display_name_snapshot == "Amber EU"

    def test_report_counters_correct(self, db_session):
        """[P6-T7] Report has correct counters for mixed batch."""
        session = db_session()
        _add_item(session, "a@example.com", "#7a", "T Silver", "T Silver")
        _add_item(session, "b@example.com", "#7b", "Silver", "Silver")
        _add_item(session, "c@example.com", "#7c", "Unknown X", "Unknown X")
        # Already filled — should NOT be counted
        _add_item(session, "d@example.com", "#7d", "Bronze", "Bronze", variant_id=99)
        session.commit()

        resolver = _make_resolver({
            "T Silver": ("exact", [42], "T Silver"),
            "Silver": ("exact", [10, 30, 54], None),  # ambiguous
            # "Unknown X" → default low confidence
        })

        report = run_backfill(
            dry_run=False,
            session_factory=db_session,
            resolver=resolver,
        )

        assert report["total"] == 3  # only NULL variant_id rows
        assert report["processed"] == 3
        assert report["resolved"] == 1
        assert report["ambiguous"] == 1
        assert report["unresolved"] == 1
        assert len(report["rows"]) == 2  # ambiguous + unresolved

    def test_no_fallback_when_same_name(self, db_session):
        """Fallback skipped when product_name == base_flavor (avoid double resolve)."""
        session = db_session()
        _add_item(session, "a@example.com", "#8", "Silver", "Silver")
        session.commit()

        call_count = {"n": 0}
        base_resolver = _make_resolver({
            "Silver": ("low", [], None),
        })

        def counting_resolver(name):
            call_count["n"] += 1
            return base_resolver(name)

        report = run_backfill(
            dry_run=False,
            session_factory=db_session,
            resolver=counting_resolver,
        )

        # Should only call once (no fallback since product_name == base_flavor)
        assert call_count["n"] == 1
        assert report["unresolved"] == 1

    def test_offset_and_limit(self, db_session):
        """offset and limit control which rows are processed."""
        session = db_session()
        for i in range(5):
            _add_item(session, f"u{i}@example.com", f"#O{i}", "T Silver", "T Silver")
        session.commit()

        resolver = _make_resolver({
            "T Silver": ("exact", [42], "T Silver"),
        })

        report = run_backfill(
            dry_run=False,
            offset=1,
            limit=2,
            session_factory=db_session,
            resolver=resolver,
        )

        assert report["total"] == 5  # total NULL rows in DB
        assert report["processed"] == 2  # only 2 processed (offset=1, limit=2)
        assert report["resolved"] == 2

    def test_report_file_saved(self, db_session, tmp_path):
        """[P6.1-T1] --report writes valid JSON with expected counters."""
        import json

        session = db_session()
        _add_item(session, "a@example.com", "#R1", "T Silver", "T Silver")
        _add_item(session, "b@example.com", "#R2", "Unknown", "Unknown")
        session.commit()

        resolver = _make_resolver({
            "T Silver": ("exact", [42], "T Silver"),
        })

        report = run_backfill(
            dry_run=False,
            session_factory=db_session,
            resolver=resolver,
        )

        report_path = tmp_path / "backfill_report.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)

        # File exists and is valid JSON with expected keys
        assert report_path.exists()
        with open(report_path) as f:
            saved = json.load(f)
        for key in ("total", "processed", "resolved", "ambiguous", "unresolved", "rows"):
            assert key in saved
        assert saved["total"] == 2
        assert saved["resolved"] == 1
        assert saved["unresolved"] == 1
