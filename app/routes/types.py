"""
app/routes/types.py

CRUD endpoints for ConnectWise support types stored in the database.

GET    /api/types          — list all types (sorted by name)
POST   /api/types          — add or update a type  {name, cw_id}
DELETE /api/types/<cw_id>  — remove a type by its CW integer ID
POST   /api/types/sync     — re-seed the DB from SUPPORT_TYPES constant (idempotent)
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from src.clients.database import (
    SessionLocal,
    SupportType,
    get_all_support_types,
    seed_support_types,
)

bp = Blueprint("types", __name__, url_prefix="/api/types")


@bp.get("")
def list_types():
    """Return all support types sorted by name."""
    return jsonify(get_all_support_types()), 200


@bp.post("")
def upsert_type():
    """
    Add a new type or update an existing one.
    Body: {"name": "Quickbooks", "cw_id": 1217}
    """
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    cw_id = body.get("cw_id")

    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        cw_id = int(cw_id)
    except (TypeError, ValueError):
        return jsonify({"error": "cw_id must be an integer"}), 400

    with SessionLocal() as session:
        row = session.query(SupportType).filter_by(name=name).first()
        if row is None:
            row = SupportType(name=name, cw_id=cw_id)
            session.add(row)
            action = "created"
        else:
            row.cw_id = cw_id
            action = "updated"
        session.commit()
        return jsonify({"action": action, "name": row.name, "cw_id": row.cw_id}), 200


@bp.delete("/<int:cw_id>")
def delete_type(cw_id: int):
    """Remove a support type by its CW integer ID."""
    with SessionLocal() as session:
        row = session.query(SupportType).filter_by(cw_id=cw_id).first()
        if row is None:
            return jsonify({"error": f"No type with cw_id={cw_id}"}), 404
        name = row.name
        session.delete(row)
        session.commit()
        return jsonify({"deleted": name, "cw_id": cw_id}), 200


@bp.post("/sync")
def sync_types():
    """Re-seed the support_types table from the built-in SUPPORT_TYPES catalogue."""
    count = seed_support_types()
    return jsonify({"synced": count, "message": f"{count} types inserted or updated"}), 200
