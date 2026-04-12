"""
Health check and keep-alive ping for Heroku / monitoring.
"""
from flask import Blueprint, jsonify, current_app
from app.services.graph_client import test_connection

health_bp = Blueprint('health', __name__)


@health_bp.route('/health')
def healthcheck():
    graph = "not_checked"
    try:
        graph = test_connection().get('status', 'unknown')
    except Exception:
        graph = "error"
    return jsonify({
        "status": "healthy",
        "app": current_app.config.get('APP_NAME'),
        "version": current_app.config.get('APP_VERSION'),
        "graph_api": graph,
    }), 200


@health_bp.route('/ping')
def ping():
    """Lightweight keep-alive endpoint. No DB or Graph calls."""
    return jsonify({"ok": True}), 200
