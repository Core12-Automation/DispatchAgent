"""
app/routes/health.py

Blueprint: GET /health

Returns HTTP 200 when every subsystem is healthy, 503 when degraded.
If the dispatcher thread has silently died, this endpoint will attempt to
restart it and report the outcome.

Response schema
───────────────
{
    "flask":                  "ok",
    "dispatcher":             "running" | "paused" | "restarted" | "dead" | "error",
    "db":                     "ok" | "error",
    "cw_api":                 "ok" | "unconfigured" | "error",
    "last_dispatch":          "<YYYY-MM-DD HH:MM:SS UTC>" | null,
    "claude_calls_this_hour": <int>,
    "cw_calls_this_hour":     <int>
}

Optionally: "dispatcher_error", "db_error", "cw_api_error" strings on failure.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify

bp = Blueprint("health", __name__)
log = logging.getLogger(__name__)


@bp.route("/health", methods=["GET"])
def health():
    result: dict = {"flask": "ok"}
    degraded = False

    # ── Dispatcher ────────────────────────────────────────────────────────────
    try:
        from services.dispatcher import get_dispatcher
        dispatcher = get_dispatcher()
        status = dispatcher.get_status()

        if not status.get("running"):
            log.critical(
                "Health check: dispatcher scheduler is not running — attempting restart"
            )
            try:
                dispatcher.start()
                result["dispatcher"] = "restarted"
                log.info("Health check: dispatcher successfully restarted")
            except Exception as restart_exc:
                result["dispatcher"] = "dead"
                result["dispatcher_error"] = str(restart_exc)
                degraded = True
                log.error("Health check: dispatcher restart failed: %s", restart_exc)
        elif status.get("paused"):
            result["dispatcher"] = "paused"
        else:
            result["dispatcher"] = "running"

        result["last_dispatch"] = status.get("last_run")

    except Exception as exc:
        log.error("Health check: dispatcher probe raised: %s", exc)
        result["dispatcher"] = "error"
        result["dispatcher_error"] = str(exc)
        degraded = True

    # ── Database ──────────────────────────────────────────────────────────────
    try:
        from src.clients.database import SessionLocal, DispatchDecision
        with SessionLocal() as session:
            session.query(DispatchDecision).limit(1).all()
        result["db"] = "ok"
    except Exception as exc:
        log.critical("Health check: database unreachable: %s", exc)
        result["db"] = "error"
        result["db_error"] = str(exc)
        degraded = True

    # ── ConnectWise API ───────────────────────────────────────────────────────
    # We only validate credentials are present — we do NOT make a live CW call
    # here because health checks must be fast and non-destructive.
    try:
        from src.clients.connectwise import CWConfig
        cfg = CWConfig.from_env()
        err = cfg.missing_credentials_error()
        if err:
            result["cw_api"] = "unconfigured"
            result["cw_api_error"] = err
        else:
            result["cw_api"] = "ok"
    except Exception as exc:
        log.error("Health check: CW config probe raised: %s", exc)
        result["cw_api"] = "error"
        result["cw_api_error"] = str(exc)

    # ── Rate limit counters ───────────────────────────────────────────────────
    try:
        from app.core.rate_limiter import get_claude_limiter, get_cw_limiter
        result["claude_calls_this_hour"] = get_claude_limiter().calls_this_hour()
        result["cw_calls_this_hour"]     = get_cw_limiter().calls_this_hour()
    except Exception:
        pass

    http_status = 503 if degraded else 200
    return jsonify(result), http_status
