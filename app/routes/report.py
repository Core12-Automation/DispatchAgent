"""
app/routes/report.py

Blueprint: /api/report/*
Manages the ticket report run, data retrieval, and PDF export.
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from app.core.state import broadcast_done, finish_run, get_tool_state, start_run
from app.services.report.builder import build_report_html, html_to_pdf
from app.services.report.service import run_ticket_report

bp = Blueprint("report", __name__)


@bp.route("/api/report/run", methods=["POST"])
def report_run():
    try:
        stop_event = start_run()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409

    get_tool_state()["report_data"] = {}
    params = request.get_json(force=True) or {}

    def _thread():
        try:
            run_ticket_report(params, stop_event)
        except Exception as e:
            from app.core.state import broadcast
            broadcast(f"\nFATAL: {e}")
        finally:
            finish_run()
            broadcast_done()

    threading.Thread(target=_thread, daemon=True).start()
    return jsonify({"ok": True})


@bp.route("/api/report/data")
def report_data():
    return jsonify(get_tool_state().get("report_data", {}))


@bp.route("/api/report/pdf")
def report_pdf():
    """Generate a PDF from the most recently run report."""
    data = get_tool_state().get("report_data", {})
    if not data or data.get("total") is None:
        return jsonify({"error": "No report data available. Run a report first."}), 400

    html_str  = build_report_html(data)
    tmp_dir   = Path(tempfile.mkdtemp())
    html_path = tmp_dir / "report.html"
    pdf_path  = tmp_dir / "report.pdf"
    html_path.write_text(html_str, encoding="utf-8")

    ok = html_to_pdf(html_path, pdf_path)
    if not ok or not pdf_path.exists():
        return jsonify({
            "error": (
                "PDF generation failed. Install Playwright "
                "(`pip install playwright && playwright install chromium`) or wkhtmltopdf."
            )
        }), 500

    return send_file(
        str(pdf_path),
        as_attachment=True,
        download_name="ticket_report.pdf",
        mimetype="application/pdf",
    )
