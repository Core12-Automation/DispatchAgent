"""
app/routes/env.py

Blueprint: /api/env
GET  — return masked environment variable values
POST — update environment variable values in the .env file
"""

import os

from dotenv import load_dotenv
from flask import Blueprint, jsonify, request

import config as cfg
from app.core.config_manager import get_env_path, mask_value, read_env

bp = Blueprint("env", __name__)


@bp.route("/api/env", methods=["GET"])
def get_env():
    env_vars = read_env()
    return jsonify({
        "path": str(get_env_path()),
        "vars": {
            k: {"value": mask_value(k, env_vars.get(k, "")), "set": bool(env_vars.get(k))}
            for k in cfg.ENV_KEYS
        },
    })


@bp.route("/api/env", methods=["POST"])
def post_env():
    from dotenv import set_key
    data     = request.get_json(force=True)
    env_path = str(get_env_path())
    errors   = []
    for k, v in data.get("vars", {}).items():
        if "\u2022" not in str(v):
            try:
                set_key(env_path, k, v)
            except Exception as e:
                errors.append(str(e))
    load_dotenv(env_path, override=True)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 500
    return jsonify({"ok": True})
