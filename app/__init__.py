"""
app/__init__.py

Flask application factory.
"""

from flask import Flask, render_template

import config as cfg


def create_app() -> Flask:
    app = Flask(__name__, template_folder=str(cfg.TEMPLATES_DIR))

    from app.routes import register_blueprints
    register_blueprints(app)

    @app.route("/")
    def index():
        return render_template("index.html")

    return app
