"""
Indus FG Warehouse Tracker — Flask Application Factory
"""
import os
import sys
import time
import logging
import threading
from flask import Flask


def create_app():
    app = Flask(
        __name__,
        static_folder=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static'
        ),
        template_folder=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'templates'
        ),
    )

    app.config.from_object('app.config.Config')

    # Unique ID per deploy: version + startup epoch.
    # Changes every redeploy → service worker installs a fresh cache.
    version = app.config.get('APP_VERSION', '2')
    deploy_id = f"{version}-{int(time.time())}"
    app.config['DEPLOY_ID'] = deploy_id

    _setup_logging(app)
    _validate_environment(app)

    from app.routes.worker import worker_bp
    from app.routes.api    import api_bp
    from app.routes.admin  import admin_bp
    from app.routes.health import health_bp
    from app.routes.master import master_bp

    app.register_blueprint(worker_bp)
    app.register_blueprint(api_bp,    url_prefix='/api')
    app.register_blueprint(admin_bp,  url_prefix='/admin')
    app.register_blueprint(health_bp)
    app.register_blueprint(master_bp, url_prefix='/master')

    @app.errorhandler(404)
    def not_found(e):
        return {"error": "Not found"}, 404

    @app.errorhandler(500)
    def internal_error(e):
        app.logger.error(f"Internal server error: {e}")
        return {"error": "Something went wrong. Please try again."}, 500

    # Keep eco dyno awake: self-ping every 25 minutes.
    # Only starts once (guarded by env flag so multiple gunicorn workers
    # don't each spawn their own thread).
    if not os.environ.get('_KEEPALIVE_STARTED'):
        os.environ['_KEEPALIVE_STARTED'] = '1'
        _start_keepalive(app)

    app.logger.info(f"Indus Warehouse Tracker started. deploy_id={deploy_id}")
    return app


def _start_keepalive(app):
    """Ping /ping every 25 min to prevent Heroku eco dyno from sleeping."""
    import requests as _requests

    def _ping_loop():
        time.sleep(120)  # Wait 2 min before first ping so the dyno is fully up
        while True:
            try:
                port = os.environ.get('PORT', '5000')
                _requests.get(f'http://localhost:{port}/ping', timeout=10)
            except Exception:
                pass  # Silently ignore — dyno may still be booting
            time.sleep(25 * 60)

    t = threading.Thread(target=_ping_loop, daemon=True)
    t.start()


def _setup_logging(app):
    level = logging.DEBUG if app.config.get('DEBUG') else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s')
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('msal').setLevel(logging.WARNING)


def _validate_environment(app):
    """
    Check that the critical runtime variables are present.

    DATABASE_URL is mandatory — the app cannot function without Postgres.
    Missing Graph vars are no longer fatal (graph_client is dormant);
    a debug-level note is emitted so logs stay clean on Heroku.
    """
    db_url = app.config.get('DATABASE_URL', '')
    if not db_url or db_url == 'postgresql://localhost/indus_warehouse_tracker':
        app.logger.warning(
            'DATABASE_URL is not set or is still the localhost default. '
            'Set the Heroku Postgres add-on or DATABASE_URL config var.'
        )
    else:
        app.logger.info('DATABASE_URL configured (\u2713)')

    # Graph vars are kept for the transition window only.
    # Emit a single debug line so they\'re visible in verbose logs
    # but don\'t pollute normal INFO output.
    graph_vars = [
        'GRAPH_TENANT_ID', 'GRAPH_CLIENT_ID',
        'GRAPH_CLIENT_SECRET', 'SHAREPOINT_SITE_ID',
    ]
    missing_graph = [v for v in graph_vars if not app.config.get(v)]
    if missing_graph:
        app.logger.debug(
            'Graph config vars not set (expected \u2014 graph_client is dormant): %s',
            ', '.join(missing_graph),
        )
