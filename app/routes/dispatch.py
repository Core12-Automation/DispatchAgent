"""
app/routes/dispatch.py

Blueprint: /api/dispatch/*

Provides a single-ticket test endpoint for the new agentic dispatch loop.
The existing /api/run/start endpoint (router.py) is UNCHANGED — this is an
additive route that runs the new agent on a specific ticket ID.

Endpoints:
    POST /api/dispatch/run-single   {"ticket_id": 12345, "dry_run": true}
        Fetches the ticket from ConnectWise, runs the agent loop, streams
        tool calls to the existing SSE channel (/api/run/stream), and
        returns the final result as JSON.
"""

from __future__ import annotations

import threading

from flask import Blueprint, jsonify, request

bp = Blueprint("dispatch", __name__)


@bp.route("/api/dispatch/run-single", methods=["POST"])
def dispatch_run_single():
    """
    Run the agentic dispatch loop on a single ticket.

    Body (JSON):
        ticket_id  int   required   ConnectWise ticket ID to dispatch
        dry_run    bool  optional   Override config dry_run flag

    Returns (JSON):
        {
          "status":          "ok" | "error" | "timeout" | "max_iterations",
          "ticket_id":       int,
          "summary":         str,
          "decisions_made":  [...],
          "tools_called":    [...],
          "elapsed_seconds": float,
          "iterations":      int,
          "dry_run":         bool
        }

    Errors:
        400  Missing or invalid ticket_id
        500  ConnectWise / Anthropic credential error
        504  Agent loop timed out
    """
    from app.core.config_manager import load_config, load_mappings
    from app.core.connectwise import check_credentials
    from app.core.state import broadcast
    from src.clients.connectwise import CWManageClient
    from src.agent.loop import run_dispatch

    body = request.get_json(silent=True) or {}
    ticket_id = body.get("ticket_id")
    if not ticket_id:
        return jsonify({"error": "ticket_id is required"}), 400
    try:
        ticket_id = int(ticket_id)
    except (TypeError, ValueError):
        return jsonify({"error": "ticket_id must be an integer"}), 400

    # ── Validate credentials ──────────────────────────────────────────────────
    cred_error = check_credentials()
    if cred_error:
        return jsonify({"error": cred_error}), 500

    # ── Load config + mappings ────────────────────────────────────────────────
    config = load_config()
    dry_run_override = body.get("dry_run")
    if dry_run_override is not None:
        config["dry_run"] = bool(dry_run_override)

    mappings_path: str = config.get("mappings_path", "")
    try:
        mappings = load_mappings(mappings_path)
    except Exception as exc:
        return jsonify({"error": f"Failed to load mappings: {exc}"}), 500

    # ── Fetch ticket ──────────────────────────────────────────────────────────
    try:
        cw = CWManageClient()
        ticket = cw.get_ticket(ticket_id)
    except Exception as exc:
        return jsonify({"error": f"Failed to fetch ticket #{ticket_id}: {exc}"}), 500

    # ── Run agent loop in a thread with a join timeout ────────────────────────
    # We run in a background thread so we can enforce a hard HTTP timeout,
    # but still return the result synchronously to the caller.
    result_holder: list = []
    exc_holder: list = []

    def _run():
        try:
            result_holder.append(
                run_dispatch(
                    ticket,
                    config=config,
                    mappings=mappings,
                    broadcaster=broadcast,   # streams to /api/run/stream SSE
                )
            )
        except Exception as exc:
            exc_holder.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=140)  # 140s hard HTTP timeout (loop has 120s soft limit)

    if thread.is_alive():
        return jsonify({
            "error":     "Agent loop timed out",
            "ticket_id": ticket_id,
            "status":    "timeout",
        }), 504

    if exc_holder:
        return jsonify({
            "error":     str(exc_holder[0]),
            "ticket_id": ticket_id,
            "status":    "error",
        }), 500

    result = result_holder[0] if result_holder else {"status": "error", "error": "No result"}

    status_code = 200 if result.get("status") == "ok" else 207
    return jsonify(result), status_code
