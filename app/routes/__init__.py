"""
app/routes/__init__.py

Registers all Flask blueprints with the application.
"""

from flask import Flask


def register_blueprints(app: Flask) -> None:
    from app.routes.config    import bp as config_bp
    from app.routes.env       import bp as env_bp
    from app.routes.mappings  import bp as mappings_bp
    from app.routes.members   import bp as members_bp
    from app.routes.run       import bp as run_bp
    from app.routes.bulk_edit import bp as bulk_edit_bp
    from app.routes.search    import bp as search_bp
    from app.routes.report    import bp as report_bp
    from app.routes.dispatch  import bp as dispatch_bp

    for bp in (config_bp, env_bp, mappings_bp, members_bp, run_bp, bulk_edit_bp, search_bp, report_bp, dispatch_bp):
        app.register_blueprint(bp)
