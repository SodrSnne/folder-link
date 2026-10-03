"""Private JSON-lines bridge: independent SSH sessions over parent/child pipes."""
import json
import posixpath
import stat
import sys
import threading
import time
from pathlib import Path

import sync_engine as engine

# The default manager keeps compatibility with the original single-session bridge.
manager = engine.SyncManager()
managers = {}
active_configs = {}
closed_sessions = set()
sessions_lock = threading.RLock()
output_lock = threading.Lock()
shutting_down = False


def session_id(data):
    value = data.get('_session', 'default')
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError('无效的 SSH 标签页。')
    return value


def get_manager(key):
    with sessions_lock:
        if shutting_down or key in closed_sessions:
            raise ValueError('标签页已关闭。')
        if key == 'default': return manager
        if key not in managers: managers[key] = engine.SyncManager()
        return managers[key]


def overlaps(left, right):
    return left == right or left.startswith(right.rstrip('/') + '/') or right.startswith(left.rstrip('/') + '/')


def browse(data):
    cfg = engine.config_from(data, folders=False)
    client = None
    try:
        client, sftp = engine.connect(cfg)
        requested = str(data.get('path', '')).strip()
        if requested and not requested.startswith('/'):
            raise ValueError('请输入远程绝对路径，或点击主目录。')
        path = sftp.normalize(requested or '.')
        if not stat.S_ISDIR(sftp.stat(path).st_mode):
            raise ValueError('请选择远程文件夹。')
        entries = []
        for item in sftp.listdir_attr(path):
            name = item.filename
            if name in ('.', '..') or '/' in name or '\\' in name or '\x00' in name:
                continue
            kind = 'directory' if stat.S_ISDIR(item.st_mode) else 'file' if stat.S_ISREG(item.st_mode) else 'link'
            entries.append({'name': name, 'path': posixpath.join(path, name), 'kind': kind, 'size': item.st_size or 0})
        entries.sort(key=lambda item: (item['kind'] != 'directory', item['name'].casefold()))
        return {'path': path, 'parent': posixpath.dirname(path) or '/', 'entries': entries}
    finally:
        cfg['password'] = ''
        if client: client.close()


def handle(command):
    kind = command.get('method')
    data = command.get('data', {})
    if not isinstance(data, dict): raise ValueError('无效的请求。')
    key = session_id(data)
    current = get_manager(key)
    if kind == 'status': return current.snapshot()
    if kind == 'browse': return browse(data)
    if kind == 'test':
        cfg = engine.config_from(data, folders=False)
        client = None
        try:
            client, sftp = engine.connect(cfg)
            return {'home': sftp.normalize('.')}
        finally:
            cfg['password'] = ''
            if client: client.close()
    if kind == 'start':
        cfg = engine.config_from(data)
        with sessions_lock:
            if shutting_down or key in closed_sessions: raise ValueError('标签页已关闭。')
            for other_key, other_cfg in active_configs.items():
                if other_key == key: continue
                other = manager if other_key == 'default' else managers.get(other_key)
                if other is None or not other.snapshot()['running']: continue
                same_server = all(cfg[k] == other_cfg[k] for k in ('host', 'port', 'username'))
                if overlaps(cfg['local'], other_cfg['local']) or (same_server and overlaps(cfg['remote'], other_cfg['remote'])):
                    raise ValueError('该文件夹与另一个运行中的标签页重叠，请先停止对应任务。')
            current.start(cfg, data.get('automatic') is True and cfg['direction'] != 'pull')
            active_configs[key] = {k: cfg[k] for k in ('host', 'port', 'username', 'local', 'remote')}
    elif kind == 'stop': current.stop()
    elif kind == 'resolve': current.resolve(data.get('path'), data.get('choice'))
    elif kind == 'close':
        with sessions_lock: closed_sessions.add(key)
        current.stop()
        if current.thread: current.thread.join(timeout=20)
        if current.thread and current.thread.is_alive():
            with sessions_lock: closed_sessions.discard(key)
            raise ValueError('正在结束网络操作，请稍后再次关闭标签页。')
        with sessions_lock:
            managers.pop(key, None)
            active_configs.pop(key, None)
        return {'ok': True, 'status': current.snapshot()}
    else: raise ValueError('未知操作。')
    return {'ok': True}


def respond(command):
    response = {'id': command.get('id')}
    try: response['result'] = handle(command)
    except engine.UnknownHost as exc:
        response['error'] = {'message': str(exc), 'host': exc.host, 'fingerprint': exc.fingerprint}
    except Exception as exc:
        message = engine.error_message(exc)
        data = command.get('data', {})
        password = data.get('password', '') if isinstance(data, dict) else ''
        if password: message = message.replace(password, '••••')
        response['error'] = {'message': message}
    finally:
        if isinstance(command.get('data'), dict): command['data'].pop('password', None)
    with output_lock:
        print(json.dumps(response, ensure_ascii=False), flush=True)


def main():
    global shutting_down
    engine.STATE_DIR = Path.home() / 'Library' / 'Application Support' / 'Folder Link'
    for line in sys.stdin:
        try:
            if len(line) > 65536: continue
            command = json.loads(line)
            if not isinstance(command, dict): continue
            if command.get('method') == 'shutdown': break
            threading.Thread(target=respond, args=(command,), daemon=True).start()
        except (ValueError, TypeError): continue
    with sessions_lock:
        shutting_down = True
        active = [manager, *managers.values()]
        for item in active: item.stop()
    deadline = time.monotonic() + 20
    for item in active:
        if item.thread: item.thread.join(timeout=max(0, deadline - time.monotonic()))


if __name__ == '__main__': main()
