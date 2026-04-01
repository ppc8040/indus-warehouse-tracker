"""
Indus FG Warehouse Tracker — Flask Application Factory
"""
import os
import sys
import logging
from flask import Flask


def create_app():
    app = Flask(
        __name__,
        static_folder=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static'),
        template_folder=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'templates'),
    )

    app.config.from_object('app.config.Config')
    _setup_logging(app)
    _validate_environment(app)

    from app.routes.worker import worker_bp
    from app.routes.api import api_bp
    from app.routes.admin import admin_bp
    from app.routes.health import health_bp

    app.register_blueprint(worker_bp)
    app.register_blueprint(api_bp, url_prefix='/api')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(health_bp)

    @app.errorhandler(404)
    def not_found(e):
        return {"error": "Not found"}, 404

    @app.errorhandler(500)
    def internal_error(e):
        app.logger.error(f"Internal server error: {e}")
        return {"error": "Something went wrong. Please try again."}, 500

    app.logger.info("Indus Warehouse Tracker v2 started.")
    return app


def _setup_logging(app):
    level = logging.DEBUG if app.config.get('DEBUG') else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('msal').setLevel(logging.WARNING)


def _validate_environment(app):
    required = ['GRAPH_TENANT_ID', 'GRAPH_CLIENT_ID', 'GRAPH_CLIENT_SECRET',
                 'SHAREPOINT_SITE_ID', 'WORKBOOK_FILENAME']
    missing = [v for v in required if not app.config.get(v)]
    if missing:
        app.logger.warning(f"Missing config vars: {', '.join(missing)}. Graph API will not work.")
