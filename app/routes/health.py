"""
Health check and keep-alive ping for Heroku / monitoring.

BUG FIX: replaced graph_client.test_connection() (dormant, requires Graph
credentials) with db_client.ping() so /health reflects actual system health.
"""
from flask import Blueprint, jsonify, current_app
from app.services.db_client import ping as db_ping

health_bp = Blueprint('health', __name__)


@health_bp.route('/health')
def healthcheck():
    db_ok = db_ping()
    return jsonify({
        "status": "healthy" if db_ok else "degraded",
        "app": current_app.config.get('APP_NAME'),
        "version": current_app.config.get('APP_VERSION'),
        "db": "ok" if db_ok else "unreachable",
    }), 200 if db_ok else 503


@health_bp.route('/ping')
def ping():
    """Lightweight keep-alive endpoint. No DB or Graph calls."""
    return jsonify({"ok": True}), 200
