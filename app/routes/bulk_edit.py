"""
app/routes/bulk_edit.py

Blueprint: /api/bulk-edit/*
Manages the bulk ticket editing run lifecycle.
Shares the global run state with other run-type operations.
"""

from __future__ import annotations

import threading

from flask import Blueprint, jsonify, request

from app.core.state import broadcast_done, finish_run, start_run
from app.services.bulk_editor import run_bulk_editor

bp = Blueprint("bulk_edit", __name__)


@bp.route("/api/bulk-edit/start", methods=["POST"])
def bulk_edit_start():
    try:
        stop_event = start_run()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409

    params = request.get_json(force=True) or {}

    def _thread():
        try:
            run_bulk_editor(params, stop_event)
        except Exception as e:
            from app.core.state import broadcast
            broadcast(f"\nFATAL: {e}")
        finally:
            finish_run()
            broadcast_done()

    threading.Thread(target=_thread, daemon=True).start()
    return jsonify({"ok": True})
