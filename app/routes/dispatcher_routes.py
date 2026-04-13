"""
app/routes/dispatcher_routes.py

Blueprint: /api/dispatcher/*

Provides control and monitoring endpoints for the background dispatcher service.
The dispatcher is "auto mode" — it polls ConnectWise and runs the agent loop
on a schedule.  The existing /api/run/start endpoint is "manual mode" and is
completely unaffected.

Endpoints:
    GET  /api/dispatcher/status     → {running, paused, last_run, next_run,
                                        tickets_today, uptime_secs, last_error}
    POST /api/dispatcher/toggle     → pause or resume the scheduler
    POST /api/dispatcher/run-once   → trigger one cycle immediately
    GET  /api/dispatcher/history    → recent dispatch_runs rows from DB
    GET  /api/dispatcher/decisions  → last 30 individual dispatch decisions
    GET  /api/dispatcher/metrics    → counters: today/week/month + flagged + avg time
"""

from __future__ import annotations

from flask import Blueprint, jsonify

bp = Blueprint("dispatcher_routes", __name__)


@bp.route("/api/dispatcher/status", methods=["GET"])
def dispatcher_status():
    """Return current dispatcher status."""
    from services.dispatcher import get_dispatcher
    return jsonify(get_dispatcher().get_status())


@bp.route("/api/dispatcher/toggle", methods=["POST"])
def dispatcher_toggle():
    """Pause the dispatcher if running, resume it if paused."""
    from services.dispatcher import get_dispatcher
    paused = get_dispatcher().toggle_pause()
    return jsonify({"ok": True, "paused": paused})


@bp.route("/api/dispatcher/run-once", methods=["POST"])
def dispatcher_run_once():
    """Trigger one dispatch cycle immediately (non-blocking)."""
    from services.dispatcher import get_dispatcher
    get_dispatcher().run_once()
    return jsonify({"ok": True, "message": "Dispatch cycle triggered"})


@bp.route("/api/dispatcher/history", methods=["GET"])
def dispatcher_history():
    """Return recent dispatch run records from the database."""
    try:
        from src.clients.database import SessionLocal, DispatchRun, init_db
        init_db()
        with SessionLocal() as session:
            rows = (
                session.query(DispatchRun)
                .order_by(DispatchRun.started_at.desc())
                .limit(20)
                .all()
            )
            result = []
            for r in rows:
                duration = None
                if r.started_at and r.ended_at:
                    duration = round(
                        (r.ended_at - r.started_at).total_seconds(), 1
                    )
                result.append({
                    "id":                r.id,
                    "started_at":        r.started_at.strftime("%Y-%m-%d %H:%M:%S") if r.started_at else None,
                    "ended_at":          r.ended_at.strftime("%Y-%m-%d %H:%M:%S")   if r.ended_at   else None,
                    "duration_secs":     duration,
                    "tickets_processed": r.tickets_processed,
                    "tickets_assigned":  r.tickets_assigned,
                    "tickets_flagged":   r.tickets_flagged,
                    "errors":            r.errors,
                    "trigger":           r.trigger,
                })
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/dispatcher/decisions", methods=["GET"])
def dispatcher_decisions():
    """Return last 30 individual dispatch decisions for the dashboard feed."""
    try:
        from src.clients.database import SessionLocal, DispatchDecision, init_db
        init_db()
        with SessionLocal() as session:
            rows = (
                session.query(DispatchDecision)
                .order_by(DispatchDecision.created_at.desc())
                .limit(30)
                .all()
            )
            result = []
            for r in rows:
                result.append({
                    "id":            r.id,
                    "ticket_id":     r.ticket_id,
                    "ticket_summary": (r.ticket_summary or "")[:80],
                    "assigned_to":   r.assigned_tech_identifier,
                    "reason":        r.reason,
                    "confidence":    r.confidence,
                    "was_dry_run":   r.was_dry_run,
                    "created_at":    r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else None,
                    "run_id":        r.run_id,
                })
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/dispatcher/metrics", methods=["GET"])
def dispatcher_metrics():
    """
    Return aggregate dispatch counters for the dashboard.

    Response:
        today, week, month  — decisions dispatched in those windows
        flagged_today       — sum of tickets_flagged from runs started today
        avg_dispatch_secs   — average run duration for runs completed today
        assignments_by_tech — {identifier: count} over last 7 days
    """
    try:
        from datetime import datetime, timezone, timedelta
        from src.clients.database import (
            SessionLocal, DispatchDecision, DispatchRun, init_db
        )
        import sqlalchemy as sa
        init_db()

        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start  = today_start - timedelta(days=today_start.weekday())
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        week7_start = today_start - timedelta(days=7)

        with SessionLocal() as session:
            today_count = session.query(DispatchDecision).filter(
                DispatchDecision.created_at >= today_start
            ).count()

            week_count = session.query(DispatchDecision).filter(
                DispatchDecision.created_at >= week_start
            ).count()

            month_count = session.query(DispatchDecision).filter(
                DispatchDecision.created_at >= month_start
            ).count()

            flagged_today = session.query(
                sa.func.coalesce(sa.func.sum(DispatchRun.tickets_flagged), 0)
            ).filter(
                DispatchRun.started_at >= today_start
            ).scalar() or 0

            # Average run duration for completed runs today
            runs_today = session.query(DispatchRun).filter(
                DispatchRun.started_at >= today_start,
                DispatchRun.ended_at.isnot(None),
            ).all()
            if runs_today:
                durations = [
                    (r.ended_at - r.started_at).total_seconds()
                    for r in runs_today
                    if r.ended_at and r.started_at
                ]
                avg_dispatch_secs = round(sum(durations) / len(durations), 1) if durations else None
            else:
                avg_dispatch_secs = None

            # Recent assignment counts per tech (last 7 days)
            recent = session.query(
                DispatchDecision.assigned_tech_identifier,
                sa.func.count(DispatchDecision.id),
            ).filter(
                DispatchDecision.created_at >= week7_start,
                DispatchDecision.assigned_tech_identifier.isnot(None),
            ).group_by(DispatchDecision.assigned_tech_identifier).all()

            assignments_by_tech = {ident: count for ident, count in recent}

        return jsonify({
            "today":               today_count,
            "week":                week_count,
            "month":               month_count,
            "flagged_today":       int(flagged_today),
            "avg_dispatch_secs":   avg_dispatch_secs,
            "assignments_by_tech": assignments_by_tech,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
