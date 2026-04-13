"""
app/routes/config.py

Blueprint: /api/config
GET  — return the current portal configuration
POST — save a new portal configuration
"""

from flask import Blueprint, jsonify, request

from app.core.config_manager import load_config, save_config

bp = Blueprint("config", __name__)


@bp.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(load_config())


@bp.route("/api/config", methods=["POST"])
def post_config():
    try:
        save_config(request.get_json(force=True))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
