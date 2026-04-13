"""
src/tools/memory/decision_log.py

Dispatch decision persistence and query layer.

Public API:
    log_dispatch_decision(ticket_id, tech_identifier, reason, confidence,
                          alternatives_considered, *, ticket_summary,
                          was_dry_run, mappings)   → dict

    get_decision_history(days=7, tech_identifier=None) → list[dict]

    get_dispatch_run_history(limit=20)               → list[dict]
        Replaces the in-memory history kept in app/core/state.py.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def _member_id_from_mappings(mappings: Dict, identifier: str) -> Optional[int]:
    members = {str(k).lower(): v for k, v in (mappings.get("members") or {}).items()}
    val = members.get(identifier.strip().lower())
    return int(val) if val is not None else None


# ── Public functions ──────────────────────────────────────────────────────────

def log_dispatch_decision(
    ticket_id: int,
    tech_identifier: str,
    reason: str,
    confidence: float,
    alternatives_considered: List[Dict[str, Any]],
    *,
    ticket_summary: str = "",
    was_dry_run: bool = True,
    mappings: Dict | None = None,
    run_id: int | None = None,
) -> Dict[str, Any]:
    """
    Insert a dispatch decision into the dispatch_decisions table.

    Args:
        ticket_id:               CW ticket ID
        tech_identifier:         Login identifier of assigned tech, e.g. "jsmith"
        reason:                  Human-readable rationale from the agent
        confidence:              0.0–1.0 confidence score
        alternatives_considered: List of dicts, e.g. [{"identifier": "akloss", "reason": "…"}]
        ticket_summary:          Short ticket description for FTS
        was_dry_run:             True when running in dry-run mode
        mappings:                Full mappings dict (for member ID resolution)
        run_id:                  FK to dispatch_runs.id if inside a batch run

    Returns:
        {"ok": True, "decision_id": int, "ticket_id": int}  on success
        {"ok": False, "error": str, "ticket_id": int}        on failure
    """
    mappings = mappings or {}
    member_id = _member_id_from_mappings(mappings, tech_identifier)

    try:
        from src.clients.database import SessionLocal, DispatchDecision, Technician

        with SessionLocal() as session:
            tech_db_id: Optional[int] = None
            if member_id is not None:
                tech = session.query(Technician).filter_by(cw_member_id=member_id).first()
                if tech:
                    tech_db_id = tech.id

            decision = DispatchDecision(
                ticket_id=ticket_id,
                ticket_summary=(ticket_summary or "")[:500],
                assigned_tech_id=tech_db_id,
                assigned_tech_identifier=tech_identifier,
                reason=reason,
                confidence=float(confidence),
                was_dry_run=was_dry_run,
                run_id=run_id,
            )
            decision.alternatives_considered = alternatives_considered or []
            session.add(decision)
            session.commit()

            return {"ok": True, "decision_id": decision.id, "ticket_id": ticket_id}

    except Exception as exc:
        log.warning("Failed to log dispatch decision for ticket %s: %s", ticket_id, exc)
        return {"ok": False, "error": str(exc), "ticket_id": ticket_id}


def get_decision_history(
    days: int = 7,
    tech_identifier: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch recent dispatch decisions, optionally filtered by technician.

    Args:
        days:             How many days back to look (default 7)
        tech_identifier:  If given, only return decisions for this tech

    Returns:
        List of decision dicts, newest first.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    try:
        from src.clients.database import SessionLocal, DispatchDecision

        with SessionLocal() as session:
            q = (
                session.query(DispatchDecision)
                .filter(DispatchDecision.created_at >= cutoff)
            )
            if tech_identifier:
                q = q.filter(
                    DispatchDecision.assigned_tech_identifier == tech_identifier
                )
            rows = q.order_by(DispatchDecision.created_at.desc()).all()

            return [
                {
                    "id":                    r.id,
                    "ticket_id":             r.ticket_id,
                    "ticket_summary":        r.ticket_summary,
                    "assigned_to":           r.assigned_tech_identifier,
                    "reason":                r.reason,
                    "confidence":            r.confidence,
                    "alternatives":          r.alternatives_considered,
                    "was_dry_run":           r.was_dry_run,
                    "created_at":            r.created_at.isoformat() if r.created_at else None,
                    "run_id":                r.run_id,
                }
                for r in rows
            ]

    except Exception as exc:
        log.warning("get_decision_history failed: %s", exc)
        return []


def get_dispatch_run_history(limit: int = 20) -> List[Dict[str, Any]]:
    """
    Return the most recent dispatch runs from SQLite.

    Replaces the in-memory history list in app/core/state.py._state["history"].
    The web UI's /api/run/history endpoint should read from here instead.

    Args:
        limit: Maximum number of runs to return (default 20)

    Returns:
        List of run summary dicts, newest first.
    """
    try:
        from src.clients.database import SessionLocal, DispatchRun

        with SessionLocal() as session:
            rows = (
                session.query(DispatchRun)
                .order_by(DispatchRun.started_at.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "id":                r.id,
                    "started_at":        r.started_at.isoformat() if r.started_at else None,
                    "ended_at":          r.ended_at.isoformat() if r.ended_at else None,
                    "tickets_processed": r.tickets_processed,
                    "tickets_assigned":  r.tickets_assigned,
                    "tickets_flagged":   r.tickets_flagged,
                    "errors":            r.errors,
                    "trigger":           r.trigger,
                    "duration_seconds":  (
                        (r.ended_at - r.started_at).total_seconds()
                        if r.ended_at and r.started_at else None
                    ),
                }
                for r in rows
            ]

    except Exception as exc:
        log.warning("get_dispatch_run_history failed: %s", exc)
        return []
