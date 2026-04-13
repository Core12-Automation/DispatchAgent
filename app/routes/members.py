"""
app/routes/members.py

Blueprint: /api/members

Member management backed by SQLite (technicians table), not portal_config.json.

Endpoints:
    GET  /api/members           — list all technicians
    GET  /api/members/presence  — batch Teams presence for all techs with teams_user_id
    GET  /api/members/<ident>   — single profile (by CW login identifier or DB id)
    PUT  /api/members/<ident>   — partial update (skills, specialties, notes, …)

The mappings board/status editor (/api/mappings) is unchanged; only member
(technician) profile data is served from the DB.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from src.tools.memory.tech_profiles import (
    get_all_tech_profiles,
    get_tech_profile,
    update_tech_profile,
)

bp = Blueprint("members", __name__)


def _load_mappings_safe() -> dict:
    """Load mappings.json without raising if the file is absent."""
    try:
        from app.core.config_manager import load_config, load_mappings
        config = load_config()
        return load_mappings(config["mappings_path"])
    except Exception:
        return {}


# ── GET /api/members ──────────────────────────────────────────────────────────

@bp.route("/api/members", methods=["GET"])
def list_members():
    """Return all technician profiles from SQLite, ordered by name."""
    try:
        profiles = get_all_tech_profiles()
        return jsonify(profiles)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── GET /api/members/presence ─────────────────────────────────────────────────

@bp.route("/api/members/presence", methods=["GET"])
def members_presence():
    """
    Batch-fetch Teams presence for all technicians that have a teams_user_id set.

    Returns a list of:
        {teams_user_id, name, availability, activity}

    availability is one of: Available, Busy, Away, BeRightBack,
                            DoNotDisturb, Offline, PresenceUnknown
    Returns [] if Teams credentials are not configured.
    """
    try:
        from src.clients.database import SessionLocal, Technician
        with SessionLocal() as session:
            techs = (
                session.query(Technician)
                .filter(Technician.teams_user_id.isnot(None))
                .all()
            )
            user_ids  = [t.teams_user_id for t in techs if t.teams_user_id]
            tech_map  = {t.teams_user_id: t.name for t in techs if t.teams_user_id}

        if not user_ids:
            return jsonify([])

        from src.clients.teams import TeamsClient
        client = TeamsClient()
        presence_list = client.get_users_presence(user_ids)

        result = [
            {
                "teams_user_id": p.get("id"),
                "name":          tech_map.get(p.get("id"), p.get("id", "")),
                "availability":  p.get("availability", "PresenceUnknown"),
                "activity":      p.get("activity", "Unknown"),
            }
            for p in presence_list
        ]
        return jsonify(result)
    except Exception as exc:
        # Missing credentials or Graph API error — return empty list gracefully
        return jsonify([])


# ── GET /api/members/<ident> ──────────────────────────────────────────────────

@bp.route("/api/members/<ident>", methods=["GET"])
def get_member(ident: str):
    """
    Return a single technician profile.

    <ident> can be:
      - A CW login identifier, e.g.  "jsmith"
      - A numeric DB id,            e.g.  "3"
    """
    try:
        mappings = _load_mappings_safe()

        # Numeric lookup by DB id
        if ident.isdigit():
            from src.clients.database import SessionLocal, Technician
            with SessionLocal() as session:
                tech = session.query(Technician).filter_by(id=int(ident)).first()
            if tech is None:
                return jsonify({"error": f"Technician id={ident} not found"}), 404
            # Build identifier from members mapping (reverse lookup)
            members = mappings.get("members") or {}
            rev = {str(v): k for k, v in members.items()}
            identifier = rev.get(str(tech.cw_member_id), tech.name)
            profile = get_tech_profile(identifier, mappings)
        else:
            profile = get_tech_profile(ident, mappings)

        if not profile.get("found_in_db") and profile.get("error"):
            return jsonify({"error": profile["error"]}), 500

        return jsonify(profile)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── PUT /api/members/<ident> ──────────────────────────────────────────────────

@bp.route("/api/members/<ident>", methods=["PUT"])
def update_member(ident: str):
    """
    Partial-update a technician's profile.

    Body (JSON) — all fields optional:
        {
          "skills":                 ["networking", "azure_ad"],
          "specialties":            ["Tier-2", "server-admin"],
          "notes":                  "Prefers morning tickets.",
          "email":                  "jsmith@example.com",
          "teams_user_id":          "guid",
          "avg_resolution_minutes": 45,
          "total_tickets_handled":  120
        }

    Returns:
        {"ok": true, "technician": "<ident>", "updated": ["skills", ...]}
    """
    try:
        updates = request.get_json(force=True) or {}
        mappings = _load_mappings_safe()
        result = update_tech_profile(ident, updates, mappings)
        if not result.get("ok"):
            return jsonify(result), 500
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
