"""
Worker-facing routes — serves the PWA pages.
"""
from flask import Blueprint, render_template, make_response, current_app

worker_bp = Blueprint('worker', __name__)


@worker_bp.route('/')
def index():
    return render_template('worker/index.html')

@worker_bp.route("/loading-review")
def loading_review_page():
    return render_template("worker/loading_review.html")

@worker_bp.route('/manifest.json')
def manifest():
    resp = make_response(render_template('worker/manifest.json'))
    resp.headers['Content-Type'] = 'application/manifest+json'
    return resp


@worker_bp.route('/sw.js')
def service_worker():
    deploy_id = current_app.config.get('DEPLOY_ID', 'v2')
    resp = make_response(render_template('worker/sw.js', deploy_id=deploy_id))
    resp.headers['Content-Type'] = 'application/javascript'
    resp.headers['Service-Worker-Allowed'] = '/'
    # No caching on the SW file itself — browser must always fetch fresh
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return resp
