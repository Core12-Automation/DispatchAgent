"""
tests/test_memory_tools.py

Tests for src/tools/memory/decision_log.py using an in-memory SQLite database.

Verifies:
  - log_dispatch_decision writes a row and returns ok=True
  - All fields are stored and retrievable
  - alternatives_considered is serialised/deserialised as JSON
  - Missing mappings are handled gracefully
  - get_decision_history returns rows within the time window
  - get_decision_history filters by tech_identifier
  - Empty history returns []
  - Technician ORM model: skills/specialties round-trip through JSON properties
"""

from __future__ import annotations

import pytest


# ── log_dispatch_decision ─────────────────────────────────────────────────────

class TestLogDispatchDecision:
    def test_creates_row_returns_ok(self, in_memory_db, mock_mappings):
        from src.tools.memory.decision_log import log_dispatch_decision

        result = log_dispatch_decision(
            ticket_id=12345,
            tech_identifier="akloss",
            reason="Best VPN skills on the team",
            confidence=0.9,
            alternatives_considered=[{"identifier": "jsmith", "reason": "Less relevant"}],
            ticket_summary="VPN not working after Windows update",
            was_dry_run=True,
            mappings=mock_mappings,
        )

        assert result["ok"] is True
        assert result["ticket_id"] == 12345
        assert "decision_id" in result
        assert isinstance(result["decision_id"], int)

    def test_all_fields_stored_correctly(self, in_memory_db, mock_mappings):
        from src.tools.memory.decision_log import log_dispatch_decision
        from src.clients.database import DispatchDecision

        log_dispatch_decision(
            ticket_id=99,
            tech_identifier="jsmith",
            reason="Server expertise",
            confidence=0.75,
            alternatives_considered=[],
            ticket_summary="Server crashed",
            was_dry_run=False,
            mappings=mock_mappings,
        )

        with in_memory_db() as session:
            row = session.query(DispatchDecision).filter_by(ticket_id=99).first()

        assert row is not None
        assert row.assigned_tech_identifier == "jsmith"
        assert row.reason == "Server expertise"
        assert abs(row.confidence - 0.75) < 0.001
        assert row.was_dry_run is False
        assert row.ticket_summary == "Server crashed"

    def test_dry_run_stored_as_true(self, in_memory_db, mock_mappings):
        from src.tools.memory.decision_log import log_dispatch_decision
        from src.clients.database import DispatchDecision

        log_dispatch_decision(
            5001, "akloss", "reason", 0.8, [],
            was_dry_run=True, mappings=mock_mappings
        )

        with in_memory_db() as session:
            row = session.query(DispatchDecision).filter_by(ticket_id=5001).first()

        assert row.was_dry_run is True

    def test_alternatives_serialised_as_json(self, in_memory_db, mock_mappings):
        from src.tools.memory.decision_log import log_dispatch_decision
        from src.clients.database import DispatchDecision

        alts = [
            {"identifier": "jsmith", "reason": "Less available"},
            {"identifier": "mwilson", "reason": "Wrong skill set"},
        ]

        log_dispatch_decision(
            500, "akloss", "primary", 0.85, alts,
            was_dry_run=True, mappings=mock_mappings
        )

        with in_memory_db() as session:
            row = session.query(DispatchDecision).filter_by(ticket_id=500).first()

        stored = row.alternatives_considered
        assert len(stored) == 2
        assert stored[0]["identifier"] == "jsmith"
        assert stored[1]["identifier"] == "mwilson"

    def test_ticket_summary_truncated_at_500(self, in_memory_db, mock_mappings):
        from src.tools.memory.decision_log import log_dispatch_decision
        from src.clients.database import DispatchDecision

        long_summary = "A" * 600

        log_dispatch_decision(
            600, "akloss", "reason", 0.9, [],
            ticket_summary=long_summary, was_dry_run=True, mappings=mock_mappings
        )

        with in_memory_db() as session:
            row = session.query(DispatchDecision).filter_by(ticket_id=600).first()

        assert len(row.ticket_summary) <= 500

    def test_works_without_mappings(self, in_memory_db):
        """log_dispatch_decision is callable even with empty mappings."""
        from src.tools.memory.decision_log import log_dispatch_decision

        result = log_dispatch_decision(
            ticket_id=1,
            tech_identifier="unknown_tech",
            reason="reason",
            confidence=0.5,
            alternatives_considered=[],
            was_dry_run=True,
            mappings={},
        )

        assert result["ok"] is True

    def test_multiple_decisions_accumulate(self, in_memory_db, mock_mappings):
        from src.tools.memory.decision_log import log_dispatch_decision
        from src.clients.database import DispatchDecision

        for i in range(5):
            log_dispatch_decision(
                ticket_id=i,
                tech_identifier="akloss",
                reason=f"reason {i}",
                confidence=0.8,
                alternatives_considered=[],
                was_dry_run=True,
                mappings=mock_mappings,
            )

        with in_memory_db() as session:
            count = session.query(DispatchDecision).count()

        assert count == 5


# ── get_decision_history ──────────────────────────────────────────────────────

class TestGetDecisionHistory:
    def test_returns_recent_decisions(self, in_memory_db, mock_mappings):
        from src.tools.memory.decision_log import log_dispatch_decision, get_decision_history

        log_dispatch_decision(101, "akloss", "A", 0.9, [], was_dry_run=True, mappings=mock_mappings)
        log_dispatch_decision(102, "jsmith", "B", 0.7, [], was_dry_run=True, mappings=mock_mappings)

        history = get_decision_history(days=7)

        assert len(history) == 2
        ticket_ids = {h["ticket_id"] for h in history}
        assert 101 in ticket_ids
        assert 102 in ticket_ids

    def test_filters_by_tech_identifier(self, in_memory_db, mock_mappings):
        from src.tools.memory.decision_log import log_dispatch_decision, get_decision_history

        log_dispatch_decision(201, "akloss", "r", 0.9, [], was_dry_run=True, mappings=mock_mappings)
        log_dispatch_decision(202, "jsmith", "r", 0.8, [], was_dry_run=True, mappings=mock_mappings)
        log_dispatch_decision(203, "akloss", "r", 0.7, [], was_dry_run=True, mappings=mock_mappings)

        akloss_history = get_decision_history(days=7, tech_identifier="akloss")

        assert len(akloss_history) == 2
        assert all(h["assigned_to"] == "akloss" for h in akloss_history)

    def test_empty_db_returns_empty_list(self, in_memory_db):
        from src.tools.memory.decision_log import get_decision_history

        history = get_decision_history(days=7)
        assert history == []

    def test_returned_entries_have_required_keys(self, in_memory_db, mock_mappings):
        from src.tools.memory.decision_log import log_dispatch_decision, get_decision_history

        log_dispatch_decision(
            5000, "akloss", "test reason", 0.85, [],
            ticket_summary="Test ticket",
            was_dry_run=True,
            mappings=mock_mappings,
        )

        history = get_decision_history(days=1)
        assert len(history) == 1

        entry = history[0]
        required_keys = {
            "id", "ticket_id", "ticket_summary", "assigned_to",
            "reason", "confidence", "alternatives", "was_dry_run", "created_at",
        }
        assert required_keys.issubset(entry.keys())
        assert entry["ticket_id"] == 5000
        assert entry["assigned_to"] == "akloss"
        assert abs(entry["confidence"] - 0.85) < 0.001
        assert entry["was_dry_run"] is True

    def test_returned_newest_first(self, in_memory_db, mock_mappings):
        """get_decision_history returns rows ordered newest → oldest."""
        from src.tools.memory.decision_log import log_dispatch_decision, get_decision_history

        log_dispatch_decision(301, "akloss", "r", 0.9, [], was_dry_run=True, mappings=mock_mappings)
        log_dispatch_decision(302, "jsmith", "r", 0.8, [], was_dry_run=True, mappings=mock_mappings)

        history = get_decision_history(days=7)

        # Newest inserted should appear first (SQLite ROWID order matches insert order here)
        assert history[0]["ticket_id"] == 302

    def test_filter_for_nonexistent_tech_returns_empty(self, in_memory_db, mock_mappings):
        from src.tools.memory.decision_log import log_dispatch_decision, get_decision_history

        log_dispatch_decision(400, "akloss", "r", 0.9, [], was_dry_run=True, mappings=mock_mappings)

        result = get_decision_history(days=7, tech_identifier="nobody_here")
        assert result == []


# ── Technician ORM model ──────────────────────────────────────────────────────

class TestTechnicianModel:
    def test_skills_json_roundtrip(self, in_memory_db):
        from src.clients.database import Technician

        skills = ["networking", "vpn", "firewall"]

        with in_memory_db() as session:
            tech = Technician(cw_member_id=100, name="Alex Kloss")
            tech.skills = skills
            session.add(tech)
            session.commit()
            tech_id = tech.id

        with in_memory_db() as session:
            retrieved = session.query(Technician).filter_by(id=tech_id).first()

        assert retrieved.skills == skills

    def test_specialties_json_roundtrip(self, in_memory_db):
        from src.clients.database import Technician

        specialties = ["SonicWall", "Cisco ASA"]

        with in_memory_db() as session:
            tech = Technician(cw_member_id=101, name="Jane Smith")
            tech.specialties = specialties
            session.add(tech)
            session.commit()
            tech_id = tech.id

        with in_memory_db() as session:
            retrieved = session.query(Technician).filter_by(id=tech_id).first()

        assert retrieved.specialties == specialties

    def test_empty_skills_returns_empty_list(self, in_memory_db):
        from src.clients.database import Technician

        with in_memory_db() as session:
            tech = Technician(cw_member_id=102, name="Bob Jones")
            session.add(tech)
            session.commit()
            tech_id = tech.id

        with in_memory_db() as session:
            retrieved = session.query(Technician).filter_by(id=tech_id).first()

        assert retrieved.skills == []
        assert retrieved.specialties == []

    def test_technician_uniqueness_by_cw_member_id(self, in_memory_db):
        """Two Technician rows cannot share the same cw_member_id (UNIQUE constraint)."""
        from sqlalchemy.exc import IntegrityError
        from src.clients.database import Technician

        with in_memory_db() as session:
            t1 = Technician(cw_member_id=999, name="Tech A")
            session.add(t1)
            session.commit()

        with pytest.raises(Exception):  # IntegrityError or similar
            with in_memory_db() as session:
                t2 = Technician(cw_member_id=999, name="Tech B")
                session.add(t2)
                session.commit()
