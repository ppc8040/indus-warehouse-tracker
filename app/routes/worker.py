"""
Worker-facing routes — serves the PWA pages.
"""
from flask import Blueprint, render_template, make_response

worker_bp = Blueprint('worker', __name__)


@worker_bp.route('/')
def index():
    return render_template('worker/index.html')


@worker_bp.route('/manifest.json')
def manifest():
    resp = make_response(render_template('worker/manifest.json'))
    resp.headers['Content-Type'] = 'application/manifest+json'
    return resp


@worker_bp.route('/sw.js')
def service_worker():
    resp = make_response(render_template('worker/sw.js'))
    resp.headers['Content-Type'] = 'application/javascript'
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp
