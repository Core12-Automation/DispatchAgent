"""
app/routes/run.py

Blueprint: /api/run/*
Manages the AI ticket routing run lifecycle and SSE log streaming.
"""

from __future__ import annotations

import json
import os
import queue
import threading

from flask import Blueprint, Response, jsonify, request

from app.core.config_manager import load_config
from app.core.state import (
    broadcast_done,
    finish_run,
    get_lock,
    get_state,
    start_run,
)
from app.services.router import run_routing

bp = Blueprint("run", __name__)


@bp.route("/api/run/start", methods=["POST"])
def run_start():
    try:
        stop_event = start_run()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409

    cfg = load_config()
    overrides = request.get_json(silent=True) or {}
    if "dry_run" in overrides:
        cfg["dry_run"] = bool(overrides["dry_run"])

    def _thread():
        try:
            run_routing(cfg, stop_event)
        except Exception as e:
            from app.core.state import broadcast
            broadcast(f"\nFATAL: {e}")
        finally:
            finish_run()
            broadcast_done()

    threading.Thread(target=_thread, daemon=True).start()
    return jsonify({"ok": True})


@bp.route("/api/run/stop", methods=["POST"])
def run_stop():
    state = get_state()
    with get_lock():
        if not state["running"]:
            return jsonify({"error": "No run in progress"}), 409
        state["stop_flag"].set()
    return jsonify({"ok": True})


@bp.route("/api/run/stream")
def run_stream():
    q: queue.Queue = queue.Queue(maxsize=1000)
    state = get_state()
    with get_lock():
        state["subscribers"].append(q)
        existing    = list(state["log_lines"])
        still_going = state["running"]

    def generate():
        try:
            for line in existing:
                yield f"data: {json.dumps({'line': line})}\n\n"
            if not still_going:
                yield f"data: {json.dumps({'done': True, 'summary': state.get('summary')})}\n\n"
                return
            while True:
                try:
                    msg = q.get(timeout=30)
                    if msg is None:
                        yield f"data: {json.dumps({'done': True, 'summary': state.get('summary')})}\n\n"
                        break
                    yield f"data: {json.dumps({'line': msg})}\n\n"
                except queue.Empty:
                    yield f"data: {json.dumps({'heartbeat': True})}\n\n"
        finally:
            with get_lock():
                if q in state["subscribers"]:
                    state["subscribers"].remove(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@bp.route("/api/run/status")
def run_status():
    state = get_state()
    with get_lock():
        return jsonify({
            "running":   state["running"],
            "log_count": len(state["log_lines"]),
            "summary":   state["summary"],
        })


@bp.route("/api/run/history")
def run_history():
    with get_lock():
        return jsonify(get_state()["history"])
