"""
app/routes/mappings.py

Blueprint: /api/mappings
GET  — return the current mappings.json
POST — save new mappings.json content
"""

from flask import Blueprint, jsonify, request

from app.core.config_manager import load_config, load_mappings, save_mappings

bp = Blueprint("mappings", __name__)


@bp.route("/api/mappings", methods=["GET"])
def get_mappings():
    try:
        return jsonify(load_mappings(load_config()["mappings_path"]))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/mappings", methods=["POST"])
def post_mappings():
    try:
        save_mappings(load_config()["mappings_path"], request.get_json(force=True))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
