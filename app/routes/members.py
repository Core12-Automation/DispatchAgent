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

import time
import threading
from flask import Blueprint, jsonify, request

from src.tools.memory.tech_profiles import (
    get_all_tech_profiles,
    get_tech_profile,
    update_tech_profile,
)

bp = Blueprint("members", __name__)

# ── In-memory workload cache ──────────────────────────────────────────────────
_workload_cache: dict = {"data": None, "fetched_at": 0.0}
_workload_lock  = threading.Lock()
_WORKLOAD_TTL   = 300  # seconds (5 minutes)


def _load_mappings_safe() -> dict:
    """Load mappings.json without raising if the file is absent."""
    try:
        from app.core.config_manager import load_config, load_mappings
        config = load_config()
        return load_mappings(config["mappings_path"])
    except Exception:
        return {}


def _resolve_cw_member_id(ident: str) -> int | None:
    """Return the CW member ID for a technician identifier (name or DB id)."""
    try:
        from src.clients.database import SessionLocal, Technician
        with SessionLocal() as session:
            if ident.isdigit():
                tech = session.query(Technician).filter_by(id=int(ident)).first()
            else:
                tech = (
                    session.query(Technician)
                    .filter(Technician.name == ident)
                    .first()
                )
        if tech and tech.cw_member_id:
            return int(tech.cw_member_id)
    except Exception:
        pass
    return None


# ── POST /api/members/sync ────────────────────────────────────────────────────

@bp.route("/api/members/sync", methods=["POST"])
def sync_members_from_mappings():
    """
    Idempotent migration: ensure every member in mappings.json has a DB record
    with their full name populated from agent_routing.display_name.

    Creates missing Technician rows and updates name where the stored name still
    equals the bare identifier (i.e. was never set to a real display name).
    """
    try:
        from src.clients.database import SessionLocal, Technician
        from datetime import datetime, timezone

        mappings  = _load_mappings_safe()
        members   = mappings.get("members") or {}
        routing   = mappings.get("agent_routing") or {}

        created = 0
        updated = 0

        with SessionLocal() as session:
            for ident, cw_member_id_raw in members.items():
                try:
                    cw_member_id = int(cw_member_id_raw)
                except (TypeError, ValueError):
                    continue

                info         = routing.get(ident) or {}
                display_name = info.get("display_name", "").strip()
                full_name    = display_name or ident

                tech = session.query(Technician).filter_by(cw_member_id=cw_member_id).first()
                if tech is None:
                    tech = Technician(
                        cw_member_id=cw_member_id,
                        cw_identifier=ident,
                        name=full_name,
                        routable=bool(info.get("routable", True)),
                        description=info.get("description", "") or "",
                        is_active=True,
                        created_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc),
                    )
                    session.add(tech)
                    created += 1
                else:
                    changed = False
                    if not tech.cw_identifier:
                        tech.cw_identifier = ident
                        changed = True
                    if display_name and tech.name == ident:
                        tech.name = display_name
                        changed = True
                    if tech.routable is None:
                        tech.routable = bool(info.get("routable", True))
                        changed = True
                    if not tech.description and info.get("description"):
                        tech.description = info["description"]
                        changed = True
                    if changed:
                        tech.updated_at = datetime.now(timezone.utc)
                        updated += 1

            session.commit()

        return jsonify({"ok": True, "created": created, "updated": updated})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


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
        body = request.get_json(force=True) or {}
        # Pull cw_member_id out of the body (not a DB column to SET, just for lookup)
        cw_member_id_raw = body.pop("cw_member_id", None)
        try:
            cw_member_id = int(cw_member_id_raw) if cw_member_id_raw is not None else None
        except (TypeError, ValueError):
            cw_member_id = None
        mappings = _load_mappings_safe()
        result = update_tech_profile(ident, body, mappings, cw_member_id=cw_member_id)
        if not result.get("ok"):
            return jsonify(result), 500
        # Return the persisted row so the caller can confirm what was saved
        from src.clients.database import SessionLocal, Technician
        with SessionLocal() as session:
            tech = None
            if cw_member_id is not None:
                tech = session.query(Technician).filter_by(cw_member_id=cw_member_id).first()
            if tech is None:
                tech = session.query(Technician).filter_by(cw_identifier=ident).first()
            if tech:
                result["saved"] = {
                    "routable":    bool(tech.routable),
                    "description": tech.description or "",
                    "name":        tech.name,
                }
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── DELETE /api/members/<ident> ───────────────────────────────────────────────

@bp.route("/api/members/<ident>", methods=["DELETE"])
def delete_member(ident: str):
    """
    Permanently delete a technician from the DB.

    <ident> can be the CW login identifier or a numeric DB id.
    Also removes the member from mappings.json (members + agent_routing).
    """
    try:
        from src.clients.database import SessionLocal, Technician
        from app.core.config_manager import load_config, load_mappings, save_mappings

        with SessionLocal() as session:
            if ident.isdigit():
                tech = session.query(Technician).filter_by(id=int(ident)).first()
            else:
                tech = (
                    session.query(Technician)
                    .filter(
                        (Technician.cw_identifier == ident) |
                        (Technician.name == ident)
                    )
                    .first()
                )
            if tech is None:
                return jsonify({"ok": True, "deleted": False, "reason": "not found in DB"})

            session.delete(tech)
            session.commit()

        # Also remove from mappings.json so the agent roster stays clean
        try:
            config   = load_config()
            mappings = load_mappings(config["mappings_path"])
            mappings.get("members", {}).pop(ident, None)
            mappings.get("agent_routing", {}).pop(ident, None)
            save_mappings(config["mappings_path"], mappings)
        except Exception:
            pass  # mappings update is best-effort

        return jsonify({"ok": True, "deleted": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── GET /api/members/<ident>/schedule ─────────────────────────────────────────

@bp.route("/api/members/<ident>/schedule", methods=["GET"])
def get_member_schedule(ident: str):
    """
    Fetch a technician's upcoming ConnectWise schedule entries.

    Query params:
        days_ahead  int  (default 3, max 14)

    Returns the schedule dict from get_technician_schedule, or an error.
    """
    try:
        days_ahead = min(14, max(1, int(request.args.get("days_ahead", 3))))

        cw_member_id = _resolve_cw_member_id(ident)
        if cw_member_id is None:
            return jsonify({"error": f"Technician '{ident}' not found or has no CW member ID"}), 404

        from src.clients.connectwise import CWManageClient
        from src.tools.perception.technicians import get_technician_schedule
        cw = CWManageClient()
        result = get_technician_schedule(cw, cw_member_id, days_ahead=days_ahead)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── GET /api/members/workload ─────────────────────────────────────────────────

@bp.route("/api/members/workload", methods=["GET"])
def get_members_workload():
    """
    Return current open ConnectWise ticket counts per technician.

    Results are cached for 5 minutes so the page never blocks on a CW API call.
    Pass ?refresh=1 to force an immediate cache bust.

    Response:
        {
          "counts":     {"<tech_name>": <open_ticket_count>, ...},
          "total_open": <int>,
          "cached":     <bool>,
          "fetched_at": <unix_timestamp>
        }
    """
    force_refresh = request.args.get("refresh") == "1"

    with _workload_lock:
        age = time.time() - _workload_cache["fetched_at"]
        if not force_refresh and _workload_cache["data"] is not None and age < _WORKLOAD_TTL:
            return jsonify({**_workload_cache["data"], "cached": True})

    # Fetch fresh data outside the lock so concurrent requests don't pile up
    try:
        from src.clients.connectwise import CWManageClient
        from src.clients.database import SessionLocal, Technician

        cw = CWManageClient()

        # Fetch all open tickets in one call
        tickets = cw.fetch_all_tickets(
            conditions="closedFlag = false",
            page_size=250,
        ) or []

        # Build cw_member_id → name map from DB
        with SessionLocal() as session:
            db_techs = {
                t.cw_member_id: t.name
                for t in session.query(Technician).all()
                if t.cw_member_id is not None
            }

        # Count open tickets per technician by CW member ID
        counts_by_id: dict[int, int] = {}
        for ticket in tickets:
            owner = ticket.get("owner") or {}
            oid = owner.get("id")
            if oid is not None:
                try:
                    oid = int(oid)
                    counts_by_id[oid] = counts_by_id.get(oid, 0) + 1
                except (TypeError, ValueError):
                    pass

        # Map to tech names (use DB name as key, matching what the dashboard uses)
        counts: dict[str, int] = {}
        for cw_id, count in counts_by_id.items():
            name = db_techs.get(cw_id)
            if name:
                counts[name] = count

        result = {
            "counts":     counts,
            "total_open": len(tickets),
            "cached":     False,
            "fetched_at": time.time(),
        }

        with _workload_lock:
            _workload_cache["data"]       = {k: v for k, v in result.items() if k != "cached"}
            _workload_cache["fetched_at"] = result["fetched_at"]

        return jsonify(result)

    except Exception as exc:
        # Return stale cache on error rather than failing the page
        with _workload_lock:
            if _workload_cache["data"] is not None:
                return jsonify({**_workload_cache["data"], "cached": True, "stale": True})
        return jsonify({"error": str(exc), "counts": {}, "total_open": 0}), 500
