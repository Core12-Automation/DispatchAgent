"""
app/__init__.py

Flask application factory.
"""

import logging
import os

from flask import Flask, render_template

import config as cfg

log = logging.getLogger(__name__)


def create_app() -> Flask:
    # Logging must be configured before Flask creates its own handlers so that
    # app.logger propagates to our root handlers.
    from app.core.logging_config import configure_logging
    configure_logging()

    app = Flask(__name__, template_folder=str(cfg.TEMPLATES_DIR))

    from app.routes import register_blueprints
    register_blueprints(app)

    @app.route("/")
    def index():
        return render_template("index.html")

    # ── Initialise DB and start the background dispatcher ─────────────────────
    # Guard against double-start: when debug=True the Werkzeug reloader spawns a
    # monitor (parent) and a worker (child, WERKZEUG_RUN_MAIN=true).  We only
    # want the dispatcher running in the worker — or once if debug=False.
    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        try:
            from src.clients.database import init_db, migrate_db
            init_db()
            migrate_db()
            log.info("Database initialised successfully")
        except Exception as exc:
            log.critical("DB init failed: %s", exc)

        try:
            from services.dispatcher import get_dispatcher
            get_dispatcher().start()
            log.info("Background dispatcher started")
        except Exception as exc:
            log.critical("Dispatcher start failed: %s", exc)

    return app
