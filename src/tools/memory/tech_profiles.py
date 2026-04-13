"""
src/tools/memory/tech_profiles.py

Technician profile CRUD — thin wrapper around the SQLite technicians table.

Public API:
    get_tech_profile(identifier, mappings)   → dict
    update_tech_profile(identifier, updates, mappings) → dict
    get_all_tech_profiles()                  → list[dict]
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def _member_id_from_mappings(mappings: Dict, identifier: str) -> Optional[int]:
    members = {str(k).lower(): v for k, v in (mappings.get("members") or {}).items()}
    val = members.get(identifier.strip().lower())
    return int(val) if val is not None else None


def _row_to_dict(tech, identifier: str | None = None) -> Dict[str, Any]:
    """Serialise a Technician ORM row to a plain dict."""
    return {
        "id":                      tech.id,
        "technician":              identifier or tech.name,
        "cw_member_id":            tech.cw_member_id,
        "name":                    tech.name,
        "email":                   tech.email,
        "teams_user_id":           tech.teams_user_id,
        "skills":                  tech.skills,
        "specialties":             tech.specialties,
        "avg_resolution_minutes":  tech.avg_resolution_minutes,
        "total_tickets_handled":   tech.total_tickets_handled,
        "notes":                   tech.notes,
        "created_at":              tech.created_at.isoformat() if tech.created_at else None,
        "updated_at":              tech.updated_at.isoformat() if tech.updated_at else None,
    }


# ── Public functions ──────────────────────────────────────────────────────────

def get_tech_profile(
    identifier: str,
    mappings: Dict | None = None,
) -> Dict[str, Any]:
    """
    Load a technician's profile from SQLite.

    Falls back to the agent_routing section of mappings if no DB record exists.

    Args:
        identifier: CW login identifier, e.g. "jsmith"
        mappings:   Full mappings dict (for member ID and roster lookup)

    Returns:
        Dict with profile fields; found_in_db=False when falling back to roster.
    """
    mappings = mappings or {}
    member_id = _member_id_from_mappings(mappings, identifier)

    try:
        from src.clients.database import SessionLocal, Technician

        with SessionLocal() as session:
            tech: Technician | None = None

            if member_id is not None:
                tech = session.query(Technician).filter_by(cw_member_id=member_id).first()

            if tech is None:
                # Try matching by name if the identifier looks like a display name
                tech = session.query(Technician).filter(
                    Technician.name.ilike(f"%{identifier}%")
                ).first()

            if tech is None:
                # Fall back to roster
                roster = mappings.get("agent_routing") or {}
                info = roster.get(identifier) or {}
                return {
                    "technician":             identifier,
                    "found_in_db":            False,
                    "display_name":           info.get("display_name", identifier),
                    "description":            info.get("description", ""),
                    "skills":                 [],
                    "specialties":            [],
                    "avg_resolution_minutes": None,
                    "total_tickets_handled":  0,
                    "notes":                  None,
                }

            result = _row_to_dict(tech, identifier)
            result["found_in_db"] = True
            return result

    except Exception as exc:
        log.warning("DB profile lookup failed for %s: %s", identifier, exc)
        return {"technician": identifier, "found_in_db": False, "error": str(exc)}


def update_tech_profile(
    identifier: str,
    updates: Dict[str, Any],
    mappings: Dict | None = None,
) -> Dict[str, Any]:
    """
    Partial-update (or create) a technician's profile in SQLite.

    Accepted update keys:
        skills, specialties, notes, email, teams_user_id,
        avg_resolution_minutes, total_tickets_handled

    Args:
        identifier: CW login identifier
        updates:    Dict of fields to set
        mappings:   Full mappings dict

    Returns:
        {"ok": True, "technician": identifier, "updated": [...field names]}
    """
    mappings = mappings or {}
    member_id = _member_id_from_mappings(mappings, identifier)

    UPDATABLE = {
        "skills", "specialties", "notes", "email",
        "teams_user_id", "avg_resolution_minutes", "total_tickets_handled",
    }

    try:
        from src.clients.database import SessionLocal, Technician

        with SessionLocal() as session:
            tech: Technician | None = None

            if member_id is not None:
                tech = session.query(Technician).filter_by(cw_member_id=member_id).first()

            if tech is None:
                roster = mappings.get("agent_routing") or {}
                info = roster.get(identifier) or {}
                tech = Technician(
                    cw_member_id=member_id,
                    name=info.get("display_name", identifier),
                )
                session.add(tech)

            applied: List[str] = []
            for key, value in updates.items():
                if key not in UPDATABLE:
                    continue
                setattr(tech, key, value)
                applied.append(key)

            session.commit()
            return {"ok": True, "technician": identifier, "updated": applied}

    except Exception as exc:
        log.warning("DB profile update failed for %s: %s", identifier, exc)
        return {"ok": False, "technician": identifier, "error": str(exc)}


def get_all_tech_profiles() -> List[Dict[str, Any]]:
    """
    Return all technician rows from the DB, ordered by name.
    Used by the web UI /api/members endpoint.
    """
    try:
        from src.clients.database import SessionLocal, Technician

        with SessionLocal() as session:
            rows = session.query(Technician).order_by(Technician.name).all()
            return [_row_to_dict(tech) for tech in rows]

    except Exception as exc:
        log.warning("get_all_tech_profiles failed: %s", exc)
        return []
