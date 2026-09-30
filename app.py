import argparse
import secrets
import subprocess
import sys
import threading
import webbrowser

from flask import Flask, abort, jsonify, render_template, request
from werkzeug.exceptions import HTTPException
from werkzeug.serving import make_server

from sync_engine import SyncManager, UnknownHost, config_from, connect, error_message

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024
TOKEN = secrets.token_urlsafe(32)
manager = SyncManager()


@app.before_request
def local_only():
    if request.host.split(':')[0] not in ('127.0.0.1', 'localhost'):
        abort(403)
    if request.method == 'POST':
        if not secrets.compare_digest(request.headers.get('X-Sync-Token', ''), TOKEN): abort(403)
        origin = request.headers.get('Origin')
        if origin and origin != request.host_url.rstrip('/'): abort(403)


@app.after_request
def headers(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


@app.get('/')
def home(): return render_template('index.html', token=TOKEN)


@app.get('/api/status')
def status(): return jsonify(manager.snapshot())


@app.post('/api/test')
def test_connection():
    cfg = config_from(request.get_json(), folders=False)
    client = None
    try:
        client, sftp = connect(cfg)
        return jsonify(ok=True, home=sftp.normalize('.'))
    finally:
        if client: client.close()


@app.post('/api/start')
def start():
    data = request.get_json()
    cfg = config_from(data)
    manager.start(cfg, data.get('automatic') is True)
    return jsonify(ok=True)


@app.post('/api/resolve')
def resolve():
    data = request.get_json()
    manager.resolve(data.get('path'), data.get('choice'))
    return jsonify(ok=True)


@app.post('/api/stop')
def stop():
    manager.stop()
    return jsonify(ok=True)


@app.post('/api/folder')
def folder():
    if sys.platform != 'darwin':
        raise ValueError('请直接填写本地文件夹的绝对路径。')
    result = subprocess.run(['osascript', '-e', 'POSIX path of (choose folder with prompt "选择要同步的本地文件夹")'], capture_output=True, text=True, timeout=120)
    if result.returncode: return jsonify(cancelled=True)
    return jsonify(path=result.stdout.strip())


@app.errorhandler(Exception)
def error(exc):
    if isinstance(exc, UnknownHost):
        return jsonify(error=str(exc), fingerprint=exc.fingerprint, host=exc.host), 409
    if isinstance(exc, HTTPException):
        return jsonify(error=exc.description), exc.code
    message = error_message(exc)
    data = request.get_json(silent=True)
    if isinstance(data, dict) and data.get('password'):
        message = message.replace(str(data['password']), '••••')
    return jsonify(error=message), 400


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    server = make_server('127.0.0.1', args.port, app, threaded=True)
    url = f'http://127.0.0.1:{args.port}'
    print(f'SSH 文件同步工具：{url}', flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        manager.stop()
        if manager.thread: manager.thread.join(timeout=20)
    finally:
        server.server_close()
