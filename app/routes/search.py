"""
app/routes/search.py

Blueprint: /api/search/*
Manages the deep ticket search run and result retrieval.
"""

from __future__ import annotations

import os
import threading

from flask import Blueprint, jsonify, request

from app.core.state import broadcast_done, finish_run, get_tool_state, start_run
from app.services.search import run_deep_search

bp = Blueprint("search", __name__)


@bp.route("/api/search/start", methods=["POST"])
def search_start():
    try:
        stop_event = start_run()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409

    get_tool_state()["search_results"] = []
    params = request.get_json(force=True) or {}

    def _thread():
        try:
            run_deep_search(params, stop_event)
        except Exception as e:
            from app.core.state import broadcast
            broadcast(f"\nFATAL: {e}")
        finally:
            finish_run()
            broadcast_done()

    threading.Thread(target=_thread, daemon=True).start()
    return jsonify({"ok": True})


@bp.route("/api/search/results")
def search_results():
    return jsonify(get_tool_state().get("search_results", []))


@bp.route("/api/cw-manage-url")
def cw_manage_url():
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(), override=True)
    base = (os.getenv("CWM_MANAGE_BASE") or "").rstrip("/")
    return jsonify({"base": base})
