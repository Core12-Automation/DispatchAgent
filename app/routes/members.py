"""
app/routes/members.py

Blueprint: /api/members

Member management backed by SQLite (technicians table), not portal_config.json.

Endpoints:
    GET  /api/members          — list all technicians
    GET  /api/members/<ident>  — single profile (by CW login identifier or DB id)
    PUT  /api/members/<ident>  — partial update (skills, specialties, notes, …)

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
