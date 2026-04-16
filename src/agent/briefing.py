"""
src/agent/briefing.py

Builds the natural-language situation briefing injected into every dispatch
cycle's system prompt.  Gives Claude persistent awareness across cycles.

Usage:
    from src.agent.briefing import build_situation_briefing
    briefing = build_situation_briefing()
    system_prompt = BASE_PROMPT + "\\n\\n--- CURRENT SITUATION ---\\n" + briefing
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def build_situation_briefing() -> str:
    """
    Assemble a plain-text briefing from live DB state.

    Sections (skipped if empty):
      1. OPERATOR INSTRUCTIONS    — active OperatorNote rows
      2. RECENT DISPATCH HISTORY  — last 4 hours of dispatch_decisions
      3. ACTIVE INCIDENTS         — ActiveIncident rows, status in new/monitoring/assigned
      4. SUPPRESSED ALERTS        — ActiveIncident rows with status=suppressed
      5. CURRENT TECHNICIAN STATE — workload summary per tech
    """
    parts: List[str] = []

    parts.append(_build_operator_notes_section())
    parts.append(_build_recent_decisions_section())
    parts.append(_build_active_incidents_section())
    parts.append(_build_suppressed_alerts_section())
    parts.append(_build_technician_state_section())

    # Filter out empty sections
    return "\n\n".join(p for p in parts if p.strip())


# ── Section builders ──────────────────────────────────────────────────────────

def _build_operator_notes_section() -> str:
    try:
        from src.clients.database import SessionLocal, OperatorNote
        from sqlalchemy import or_

        now = datetime.now(timezone.utc)
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
                .order_by(OperatorNote.scope, OperatorNote.created_at.asc())
                .all()
            )

            if not notes:
                return "OPERATOR INSTRUCTIONS:\nNo active operator instructions."

            lines = ["OPERATOR INSTRUCTIONS:"]
            for note in notes:
                age = _relative_time(note.created_at)
                scope_label = _scope_label(note.scope, note.scope_ref)
                exp_str = (
                    f", expires {_fmt_dt(note.expires_at)}"
                    if note.expires_at else ""
                )
                lines.append(f"{scope_label} \"{note.note_text}\" (added {age}{exp_str})")

            return "\n".join(lines)

    except Exception as exc:
        log.warning("briefing: operator notes section failed: %s", exc)
        return "OPERATOR INSTRUCTIONS:\n(Unable to load — check database)"


def _build_recent_decisions_section() -> str:
    try:
        from src.clients.database import SessionLocal, DispatchDecision
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(hours=4)
        with SessionLocal() as session:
            rows = (
                session.query(DispatchDecision)
                .filter(DispatchDecision.created_at >= cutoff)
                .order_by(DispatchDecision.created_at.asc())
                .limit(30)
                .all()
            )

            if not rows:
                return ""

            lines = ["RECENT DISPATCH HISTORY (last 4 hours):"]
            for r in rows:
                ts = r.created_at.strftime("%I:%M %p") if r.created_at else "?"
                tech = r.assigned_tech_identifier or "unassigned"
                conf = f" [confidence: {r.confidence:.2f}]" if r.confidence else ""
                summary = (r.ticket_summary or "")[:60]
                lines.append(
                    f"- {ts}: Ticket #{r.ticket_id} \"{summary}\" → {tech}{conf}"
                )

            return "\n".join(lines)

    except Exception as exc:
        log.warning("briefing: recent decisions section failed: %s", exc)
        return ""


def _build_active_incidents_section() -> str:
    try:
        from src.clients.database import SessionLocal, ActiveIncident, Technician

        with SessionLocal() as session:
            rows = (
                session.query(ActiveIncident)
                .filter(ActiveIncident.status.in_(["new", "monitoring", "assigned"]))
                .order_by(ActiveIncident.last_seen.desc())
                .limit(20)
                .all()
            )

            if not rows:
                return ""

            lines = ["ACTIVE INCIDENTS:"]
            for r in rows:
                tech_name = None
                if r.assigned_tech_id:
                    tech = session.get(Technician, r.assigned_tech_id)
                    if tech:
                        tech_name = tech.name

                since = r.first_seen.strftime("%I:%M %p") if r.first_seen else "?"
                tech_str = f", assigned to {tech_name}" if tech_name else ""
                lines.append(
                    f"- INC-{r.id}: key={r.incident_key!r} — "
                    f"{r.occurrence_count} occurrence(s) since {since}"
                    f"{tech_str}, status: {r.status}"
                )

            return "\n".join(lines)

    except Exception as exc:
        log.warning("briefing: active incidents section failed: %s", exc)
        return ""


def _build_suppressed_alerts_section() -> str:
    try:
        from src.clients.database import SessionLocal, ActiveIncident

        now = datetime.now(timezone.utc)
        with SessionLocal() as session:
            rows = (
                session.query(ActiveIncident)
                .filter(
                    ActiveIncident.status == "suppressed",
                    ActiveIncident.suppressed_until > now,
                )
                .all()
            )

            if not rows:
                return ""

            lines = ["SUPPRESSED ALERTS:"]
            for r in rows:
                until_str = (
                    _fmt_dt(r.suppressed_until)
                    if r.suppressed_until else "further notice"
                )
                reason = f": {r.suppressed_reason}" if r.suppressed_reason else ""
                lines.append(
                    f"- INC-{r.id} (key={r.incident_key!r}): "
                    f"suppressed until {until_str}{reason}"
                )

            return "\n".join(lines)

    except Exception as exc:
        log.warning("briefing: suppressed alerts section failed: %s", exc)
        return ""


def _build_technician_state_section() -> str:
    try:
        from src.clients.database import SessionLocal, DispatchDecision, OperatorNote
        from sqlalchemy import or_, func

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=4)

        lines = ["CURRENT TECHNICIAN STATE:"]

        with SessionLocal() as session:
            # Count recent decisions per tech
            counts = (
                session.query(
                    DispatchDecision.assigned_tech_identifier,
                    func.count(DispatchDecision.id).label("cnt"),
                )
                .filter(DispatchDecision.created_at >= cutoff)
                .group_by(DispatchDecision.assigned_tech_identifier)
                .all()
            )

            tech_counts: Dict[str, int] = {
                row.assigned_tech_identifier: row.cnt
                for row in counts
                if row.assigned_tech_identifier
            }

            # Collect tech notes (scope=tech) to flag unavailable techs
            tech_notes_raw = (
                session.query(OperatorNote)
                .filter(
                    OperatorNote.is_active.is_(True),
                    OperatorNote.scope == "tech",
                    or_(
                        OperatorNote.expires_at.is_(None),
                        OperatorNote.expires_at > now,
                    ),
                )
                .all()
            )
            unavailable: Dict[str, str] = {}
            for n in tech_notes_raw:
                if n.scope_ref:
                    unavailable[n.scope_ref.strip().lower()] = n.note_text

        if not tech_counts and not unavailable:
            lines.append("  (No dispatch activity in the last 4 hours)")
            return "\n".join(lines)

        all_techs = set(tech_counts.keys()) | {t for t in unavailable}
        for tech in sorted(all_techs):
            cnt = tech_counts.get(tech, 0)
            ticket_str = f"{cnt} ticket(s) in last 4h" if cnt else "no recent tickets"
            note = unavailable.get(tech.lower())
            note_str = f" — ⚠ DO NOT ASSIGN: {note}" if note else ""
            lines.append(f"- {tech}: {ticket_str}{note_str}")

        return "\n".join(lines)

    except Exception as exc:
        log.warning("briefing: technician state section failed: %s", exc)
        return ""


# ── Formatting helpers ────────────────────────────────────────────────────────

def _scope_label(scope: str, scope_ref: Optional[str]) -> str:
    if scope == "global":
        return "[GLOBAL]"
    if scope == "client":
        return f"[CLIENT: {scope_ref or '?'}]"
    if scope == "tech":
        return f"[TECH: {scope_ref or '?'}]"
    if scope == "incident":
        return f"[INCIDENT: {scope_ref or '?'}]"
    return f"[{scope.upper()}]"


def _relative_time(dt: Optional[datetime]) -> str:
    if dt is None:
        return "unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 120:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _fmt_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return "N/A"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d %I:%M %p UTC")
