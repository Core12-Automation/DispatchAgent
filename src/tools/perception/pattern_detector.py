"""
src/tools/perception/pattern_detector.py

Pre-processes tickets BEFORE Claude sees them.  Detects repeats, storms, and
known patterns so the agent does not treat every ticket as brand new.

Usage:
    detector = PatternDetector()
    enriched = detector.analyze_ticket(ticket)   # adds "_context" key
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


class PatternDetector:
    """
    Enriches incoming tickets with incident context and operator note matches.

    Every ticket that passes through gets a ``_context`` dict injected with:
        is_repeat, incident_id, occurrence_count, first_seen,
        already_assigned_to, is_storm, is_suppressed, suppressed_until,
        matching_operator_notes
    """

    # ── Public API ────────────────────────────────────────────────────────────

    def analyze_ticket(self, ticket: Dict[str, Any]) -> Dict[str, Any]:
        """Return the ticket with ``_context`` enrichment added."""
        fingerprint = self.generate_fingerprint(ticket)
        incident = self.get_or_create_incident(fingerprint, ticket)

        now = datetime.now(timezone.utc)
        suppressed = bool(
            incident.suppressed_until and incident.suppressed_until > now
        )

        context: Dict[str, Any] = {
            "is_repeat":             incident.occurrence_count > 1,
            "incident_id":           incident.id,
            "incident_key":          incident.incident_key,
            "occurrence_count":      incident.occurrence_count,
            "first_seen":            incident.first_seen.isoformat(),
            "already_assigned_to":   self._get_tech_name(incident.assigned_tech_id),
            "is_storm":              (
                incident.occurrence_count >= 3
                and self._within_hours(incident.first_seen, 1)
            ),
            "is_suppressed":         suppressed,
            "suppressed_until":      (
                incident.suppressed_until.isoformat()
                if incident.suppressed_until else None
            ),
            "suppressed_reason":     incident.suppressed_reason,
            "matching_operator_notes": self._find_matching_notes(ticket, incident.incident_key),
        }

        return {**ticket, "_context": context}

    def generate_fingerprint(self, ticket: Dict[str, Any]) -> str:
        """
        Normalise a ticket into a short key that groups "same alert" tickets.

        Steps:
          1. Lower-case the summary
          2. Strip ticket numbers, IPs, timestamps, UUIDs, standalone numbers
          3. Combine with company name (same alert from different clients →
             separate incidents)
          4. SHA-256, truncate to 16 hex chars
        """
        summary = (ticket.get("summary") or "").lower()
        company_obj = ticket.get("company")
        if isinstance(company_obj, dict):
            company = company_obj.get("name", "unknown").lower()
        else:
            company = str(company_obj or "unknown").lower()

        # Strip ticket numbers (#1234)
        summary = re.sub(r"#\d+", "", summary)
        # Strip IP addresses
        summary = re.sub(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", "IP", summary)
        # Strip dates
        summary = re.sub(r"\d{4}-\d{2}-\d{2}", "", summary)
        # Strip times
        summary = re.sub(r"\d{2}:\d{2}(:\d{2})?", "", summary)
        # Strip UUIDs (partial)
        summary = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}", "", summary)
        # Replace remaining standalone numbers with N
        summary = re.sub(r"\b\d+\b", "N", summary)
        # Collapse whitespace
        summary = re.sub(r"\s+", " ", summary).strip()

        raw = f"{company}|{summary}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def get_or_create_incident(
        self, fingerprint: str, ticket: Dict[str, Any]
    ):
        """
        Find the open incident matching *fingerprint*, or create a new one.

        Returns an ``ActiveIncident`` ORM object (already committed).
        """
        from src.clients.database import SessionLocal, ActiveIncident

        ticket_id = ticket.get("id")
        now = datetime.now(timezone.utc)

        try:
            with SessionLocal() as session:
                incident = (
                    session.query(ActiveIncident)
                    .filter(
                        ActiveIncident.incident_key == fingerprint,
                        ActiveIncident.status.notin_(["resolved"]),
                    )
                    .first()
                )

                if incident:
                    # Existing — update counts and ticket list
                    incident.occurrence_count += 1
                    incident.last_seen = now
                    existing_ids = incident.ticket_ids
                    if ticket_id and ticket_id not in existing_ids:
                        incident.ticket_ids = existing_ids + [ticket_id]
                    session.commit()
                    session.refresh(incident)
                else:
                    # New incident
                    incident = ActiveIncident(
                        incident_key=fingerprint,
                        first_seen=now,
                        last_seen=now,
                        occurrence_count=1,
                        status="new",
                    )
                    incident.ticket_ids = [ticket_id] if ticket_id else []
                    session.add(incident)
                    session.commit()
                    session.refresh(incident)

                # Detach a plain copy so it survives session close
                return _IncidentSnapshot(incident)

        except Exception as exc:
            log.warning("PatternDetector.get_or_create_incident failed: %s", exc)
            # Return a minimal placeholder so the caller never crashes
            return _IncidentSnapshot.empty(fingerprint)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _get_tech_name(self, assigned_tech_id: Optional[int]) -> Optional[str]:
        """Resolve a technicians.id → display name."""
        if assigned_tech_id is None:
            return None
        try:
            from src.clients.database import SessionLocal, Technician
            with SessionLocal() as session:
                tech = session.get(Technician, assigned_tech_id)
                return tech.name if tech else None
        except Exception:
            return None

    def _find_matching_notes(
        self, ticket: Dict[str, Any], incident_key: str
    ) -> List[str]:
        """
        Return active operator note texts that apply to this ticket.

        Matches:
          - scope='global'  → always included
          - scope='client'  → scope_ref matches company name (case-insensitive)
                              or any alias from config.COMPANY_ALIASES
          - scope='incident' → scope_ref == incident_key
          - scope='tech'    → always included (so Claude knows who is unavailable)
        """
        from src.clients.database import SessionLocal, OperatorNote
        from sqlalchemy import or_

        company_obj = ticket.get("company")
        if isinstance(company_obj, dict):
            company_name = (company_obj.get("name") or "").strip().lower()
        else:
            company_name = str(company_obj or "").strip().lower()

        # Gather all known aliases for this company from config
        company_aliases: List[str] = [company_name]
        try:
            from config import COMPANY_ALIASES
            for canonical, aliases in COMPANY_ALIASES.items():
                if canonical.strip().lower() == company_name:
                    company_aliases += [a.strip().lower() for a in aliases]
                    break
        except Exception:
            pass

        now = datetime.now(timezone.utc)
        matching: List[str] = []

        try:
            with SessionLocal() as session:
                notes = (
                    session.query(OperatorNote)
                    .filter(
                        OperatorNote.is_active.is_(True),
                        or_(
                            OperatorNote.expires_at.is_(None),
                            OperatorNote.expires_at > now,
                        ),
                    )
                    .order_by(OperatorNote.scope, OperatorNote.created_at.desc())
                    .all()
                )

                for note in notes:
                    scope = note.scope
                    ref = (note.scope_ref or "").strip().lower()

                    if scope == "global":
                        matching.append(note.note_text)
                    elif scope == "tech":
                        # Include all tech notes so Claude knows who's unavailable
                        matching.append(f"[TECH: {note.scope_ref}] {note.note_text}")
                    elif scope == "client":
                        if ref in company_aliases:
                            matching.append(note.note_text)
                    elif scope == "incident":
                        if ref == incident_key:
                            matching.append(note.note_text)

        except Exception as exc:
            log.warning("PatternDetector._find_matching_notes failed: %s", exc)

        return matching

    def _within_hours(self, dt: datetime, hours: float) -> bool:
        """Return True if *dt* is within the last *hours* hours."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() < hours * 3600


# ── Lightweight snapshot so session can be closed ────────────────────────────

class _IncidentSnapshot:
    """Plain-object copy of an ActiveIncident for use after session close."""

    __slots__ = (
        "id", "incident_key", "occurrence_count", "first_seen", "last_seen",
        "ticket_ids", "status", "assigned_tech_id",
        "suppressed_until", "suppressed_reason", "notes",
    )

    def __init__(self, orm_obj) -> None:
        self.id = orm_obj.id
        self.incident_key = orm_obj.incident_key
        self.occurrence_count = orm_obj.occurrence_count
        self.first_seen = orm_obj.first_seen
        self.last_seen = orm_obj.last_seen
        self.ticket_ids = list(orm_obj.ticket_ids)
        self.status = orm_obj.status
        self.assigned_tech_id = orm_obj.assigned_tech_id
        self.suppressed_until = orm_obj.suppressed_until
        self.suppressed_reason = orm_obj.suppressed_reason
        self.notes = orm_obj.notes

    @classmethod
    def empty(cls, fingerprint: str) -> "_IncidentSnapshot":
        """Return a zero-state snapshot for error recovery."""
        obj = object.__new__(cls)
        obj.id = None
        obj.incident_key = fingerprint
        obj.occurrence_count = 1
        obj.first_seen = datetime.now(timezone.utc)
        obj.last_seen = datetime.now(timezone.utc)
        obj.ticket_ids = []
        obj.status = "new"
        obj.assigned_tech_id = None
        obj.suppressed_until = None
        obj.suppressed_reason = None
        obj.notes = None
        return obj
