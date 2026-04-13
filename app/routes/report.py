"""
app/routes/report.py

Blueprint: /api/report/*
Runs the ticket report and serves the generated PDF.
"""

from __future__ import annotations

import threading
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from app.core.state import broadcast, broadcast_done, finish_run, get_tool_state, start_run
from app.services.report.service import run_ticket_report

bp = Blueprint("report", __name__)


@bp.route("/api/report/run", methods=["POST"])
def report_run():
    try:
        stop_event = start_run()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409

    get_tool_state()["report_pdf_path"] = None
    params = request.get_json(force=True) or {}

    def _thread():
        try:
            run_ticket_report(params, stop_event)
        except Exception as e:
            broadcast(f"\nFATAL: {e}")
        finally:
            finish_run()
            broadcast_done()

    threading.Thread(target=_thread, daemon=True).start()
    return jsonify({"ok": True})


@bp.route("/api/report/pdf")
def report_pdf():
    """Download the most recently generated report PDF."""
    pdf_path = get_tool_state().get("report_pdf_path")
    if not pdf_path or not Path(pdf_path).exists():
        return jsonify({"error": "No report available. Run a report first."}), 400

    return send_file(
        pdf_path,
        as_attachment=True,
        download_name="ticket_report.pdf",
        mimetype="application/pdf",
    )
