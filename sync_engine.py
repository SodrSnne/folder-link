"""Bidirectional SFTP synchronization with persisted hashes and explicit conflict resolution."""
import base64
import errno
import json
import tempfile
import hashlib
import os
import posixpath
import stat
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import paramiko

STATE_DIR = Path(__file__).resolve().parent / '.runtime'
HOST_KEYS_LOCK = threading.Lock()
EXCLUDES = {'.git', '.venv', 'node_modules', '__pycache__', '.DS_Store', '.runtime', '.pytest_cache'}


class UnknownHost(Exception):
    def __init__(self, host, fingerprint):
        self.host, self.fingerprint = host, fingerprint
        super().__init__('首次连接，请核对并信任服务器指纹。')


class Stopped(Exception):
    pass


def fingerprint(key):
    return 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')


class VerifyHost(paramiko.MissingHostKeyPolicy):
    def __init__(self, trusted):
        self.trusted = trusted

    def missing_host_key(self, client, hostname, key):
        actual = fingerprint(key)
        if self.trusted != actual:
            raise UnknownHost(hostname, actual)
        with HOST_KEYS_LOCK:
            path = STATE_DIR / 'known_hosts'
            keys = paramiko.HostKeys(str(path)) if path.exists() else paramiko.HostKeys()
            known = keys.lookup(hostname)
            if known and key.get_name() in known and known[key.get_name()] != key:
                raise paramiko.BadHostKeyException(hostname, key, known[key.get_name()])
            keys.add(hostname, key.get_name(), key)
            temp = path.with_suffix('.tmp')
            keys.save(str(temp))
            os.chmod(temp, 0o600)
            temp.replace(path)
            client.get_host_keys().add(hostname, key.get_name(), key)


def config_from(data, folders=True):
    if not isinstance(data, dict):
        raise ValueError('请输入连接信息。')
    cfg = {key: str(data.get(key, '')).strip() for key in ('host', 'username', 'local', 'remote', 'trusted')}
    cfg['password'] = str(data.get('password', ''))
    cfg['direction'] = str(data.get('direction', 'both'))
    if cfg['direction'] not in ('both', 'pull'): raise ValueError('无效的同步方向。')
    try:
        cfg['port'] = int(data.get('port', 22))
    except (ValueError, TypeError):
        raise ValueError('端口应为 1–65535 的整数。')
    if not cfg['host'] or any(c.isspace() for c in cfg['host']) or '/' in cfg['host']:
        raise ValueError('请输入 IP 或主机名，不要带 ssh:// 前缀。')
    if not cfg['username'] or not cfg['password']:
        raise ValueError('请填写账号和密码。')
    if not 1 <= cfg['port'] <= 65535:
        raise ValueError('端口应为 1–65535。')
    if folders:
        local = Path(cfg['local']).expanduser()
        if not cfg['local'] or not local.is_absolute() or not local.is_dir():
            raise ValueError('请选择存在的本地文件夹（绝对路径）。')
        cfg['local'] = str(local.resolve())
        if not cfg['remote'].startswith('/') or '\x00' in cfg['remote']:
            raise ValueError('远程文件夹必须使用绝对路径，例如 /home/ubuntu/project。')
        cfg['remote'] = posixpath.normpath(cfg['remote'])
        if cfg['remote'] == '/':
            raise ValueError('请选择具体的远程子文件夹，不能使用根目录。')
    return cfg


def connect(cfg):
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    hosts = STATE_DIR / 'known_hosts'
    if hosts.exists():
        client.load_host_keys(str(hosts))
    client.set_missing_host_key_policy(VerifyHost(cfg.get('trusted', '')))
    try:
        client.connect(cfg['host'], port=cfg['port'], username=cfg['username'],
                       password=cfg['password'], timeout=10, auth_timeout=10,
                       banner_timeout=10, allow_agent=False, look_for_keys=False)
        client.get_transport().set_keepalive(20)
        sftp = client.open_sftp()
        sftp.get_channel().settimeout(15)
        return client, sftp
    except Exception:
        client.close()
        raise


def error_message(exc):
    if isinstance(exc, paramiko.AuthenticationException):
        return '登录失败：请检查账号、密码和服务器是否允许密码登录。'
    if isinstance(exc, paramiko.BadHostKeyException):
        return '服务器指纹已变化，连接已停止。请核实服务器身份后更新 known_hosts。'
    if isinstance(exc, (TimeoutError,)): return '连接或传输超时，请检查网络和 SSH 端口。'
    if isinstance(exc, UnknownHost): return str(exc)
    return str(exc) or type(exc).__name__


def remote_stat(sftp, path):
    try:
        return sftp.lstat(path)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None
        raise


def ensure_directory(sftp, path):
    """Inspect every ancestor to prevent traversal through remote symlinks."""
    current = '/'
    for part in path.strip('/').split('/'):
        if not part: continue
        current = posixpath.join(current, part)
        attr = remote_stat(sftp, current)
        if attr is None:
            sftp.mkdir(current)
        elif not stat.S_ISDIR(attr.st_mode):
            raise ValueError(f'远程路径不是普通文件夹（可能是符号链接）：{current}')


def digest(stream, stop):
    result = hashlib.sha256()
    while True:
        if stop.is_set(): raise Stopped()
        chunk = stream.read(1024 * 1024)
        if not chunk: return result.hexdigest()
        result.update(chunk)


def local_digest(path, stop):
    if path.is_symlink(): raise ValueError(f'不能覆盖本地符号链接：{path}')
    if not path.exists(): return None
    with path.open('rb') as stream: return digest(stream, stop)


def remote_digest(sftp, path, stop):
    attr = remote_stat(sftp, path)
    if attr is None: return None
    if not stat.S_ISREG(attr.st_mode): raise ValueError(f'远程目标不是普通文件：{path}')
    with sftp.open(path, 'rb') as stream: return digest(stream, stop)


def mapping_key(cfg):
    identity = [cfg[k] for k in ('host', 'port', 'username', 'local', 'remote')]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def load_baseline(cfg):
    path = STATE_DIR / (mapping_key(cfg) + '.json')
    if not path.exists(): return {}
    return json.loads(path.read_text())


def save_baseline(cfg, baseline):
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    path = STATE_DIR / (mapping_key(cfg) + '.json')
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(baseline, ensure_ascii=False))
    os.chmod(temp, 0o600)
    temp.replace(path)


def scan_local(root):
    files, dirs, blocked = set(), set(), set()
    def fail(exc): raise exc
    for folder, children, names in os.walk(root, followlinks=False, onerror=fail):
        for name in children[:]:
            path = Path(folder, name)
            if name in EXCLUDES or name.startswith('.ssh-sync-') or path.is_symlink():
                blocked.add(path.relative_to(root).as_posix())
                children.remove(name)
            else: dirs.add(path.relative_to(root).as_posix())
        for name in names:
            path = Path(folder, name)
            rel = path.relative_to(root).as_posix()
            if name in EXCLUDES or name.startswith('.ssh-sync-') or path.is_symlink() or not path.is_file(): blocked.add(rel)
            else: files.add(rel)
    return files, dirs, blocked


def scan_remote(sftp, root, stop):
    files, dirs, blocked = set(), set(), set()
    pending = ['']
    while pending:
        if stop.is_set(): raise Stopped()
        rel = pending.pop()
        for attr in sftp.listdir_attr(posixpath.join(root, rel)):
            name = attr.filename
            if name in ('.', '..') or '/' in name or '\\' in name:
                raise ValueError('服务器返回了无效文件名。')
            path = posixpath.join(rel, name)
            if name in EXCLUDES or name.startswith('.ssh-sync-'): blocked.add(path)
            elif stat.S_ISDIR(attr.st_mode):
                dirs.add(path)
                pending.append(path)
            elif stat.S_ISREG(attr.st_mode): files.add(path)
            else: blocked.add(path)
    return files, dirs, blocked


def blocked_path(path, blocked):
    return any(path == item or path.startswith(item + '/') for item in blocked)


def safe_local_parent(root, rel):
    current = root
    for part in Path(rel).parts[:-1]:
        current = current / part
        if current.is_symlink(): raise ValueError(f'不能写入本地符号链接：{current}')
        current.mkdir(exist_ok=True)


def transfer(cfg, sftp, rel, direction, expected_local, expected_remote, stop):
    root = Path(cfg['local'])
    local, remote = root / rel, posixpath.join(cfg['remote'], rel)
    if direction == 'local':
        ensure_directory(sftp, posixpath.dirname(remote))
        temp = posixpath.join(posixpath.dirname(remote), '.ssh-sync-' + uuid.uuid4().hex)
        try:
            before = local.stat()
            def progress(done, total):
                if stop.is_set(): raise Stopped()
            sftp.put(str(local), temp, callback=progress)
            if remote_digest(sftp, temp, stop) != expected_local or local_digest(local, stop) != expected_local:
                raise ValueError(f'文件在传输中发生修改，请重新同步：{rel}')
            if remote_digest(sftp, remote, stop) != expected_remote:
                raise ValueError(f'远程文件在传输中发生修改，未覆盖：{rel}')
            sftp.chmod(temp, stat.S_IMODE(before.st_mode) & 0o777)
            sftp.utime(temp, (int(before.st_atime), int(before.st_mtime)))
            if stop.is_set(): raise Stopped()
            if expected_remote is None: sftp.rename(temp, remote)
            else:
                try: sftp.posix_rename(temp, remote)
                except OSError as exc:
                    raise OSError(f'无法原子覆盖 {rel}，原文件已保留。服务器需支持 posix-rename。') from exc
        finally:
            try: sftp.remove(temp)
            except OSError: pass
    else:
        safe_local_parent(root, rel)
        fd, temp = tempfile.mkstemp(prefix='.ssh-sync-', dir=local.parent)
        os.close(fd)
        try:
            attr = sftp.stat(remote)
            def progress(done, total):
                if stop.is_set(): raise Stopped()
            sftp.get(remote, temp, callback=progress)
            if local_digest(Path(temp), stop) != expected_remote or remote_digest(sftp, remote, stop) != expected_remote:
                raise ValueError(f'远程文件在传输中发生修改，请重新同步：{rel}')
            if local_digest(local, stop) != expected_local:
                raise ValueError(f'本地文件在传输中发生修改，未覆盖：{rel}')
            os.chmod(temp, stat.S_IMODE(attr.st_mode) & 0o777)
            os.utime(temp, (attr.st_atime or attr.st_mtime, attr.st_mtime))
            if stop.is_set(): raise Stopped()
            os.replace(temp, local)
        finally:
            if os.path.exists(temp): os.unlink(temp)


def sync_once(cfg, sftp, stop, log, baseline, resolutions=None):
    root = Path(cfg['local'])
    if root.is_symlink() or not root.is_dir(): raise ValueError('本地文件夹已不存在或变成符号链接，同步已停止。')
    pulling = cfg.get('direction') == 'pull'
    if pulling:
        attr = remote_stat(sftp, cfg['remote'])
        if attr is None or not stat.S_ISDIR(attr.st_mode): raise ValueError('远程文件夹不存在或不是普通目录。')
    else: ensure_directory(sftp, cfg['remote'])
    lf, ld, lb = scan_local(root)
    rf, rd, rb = scan_remote(sftp, cfg['remote'], stop)
    blocked = lb | rb
    for rel in (lf & rd) | (rf & ld):
        raise ValueError(f'两端文件与文件夹类型冲突，请手动处理：{rel}')
    for rel in sorted(rd if pulling else ld | rd):
        if blocked_path(rel, blocked): continue
        safe_local_parent(root, rel + '/placeholder')
        if not pulling: ensure_directory(sftp, posixpath.join(cfg['remote'], rel))
    uploaded = downloaded = skipped = 0
    conflicts = []
    resolutions = resolutions or {}
    for rel in sorted(rf if pulling else lf | rf):
        if stop.is_set(): raise Stopped()
        if blocked_path(rel, blocked): continue
        local = local_digest(root / rel, stop)
        remote = remote_digest(sftp, posixpath.join(cfg['remote'], rel), stop)
        if local == remote:
            if local is not None: baseline[rel] = local
            skipped += 1
            continue
        previous = baseline.get(rel)
        choice = resolutions.get(rel)
        direction = None
        if choice and choice['local'] == local and choice['remote'] == remote:
            direction = choice['choice']
        elif local is None: direction = 'remote'
        elif remote is None and not pulling: direction = 'local'
        elif previous is not None and remote == previous and not pulling: direction = 'local'
        elif previous is not None and local == previous: direction = 'remote'
        if direction is None:
            conflicts.append(dict(path=rel, local=local, remote=remote))
            continue
        if pulling and direction == 'local':
            skipped += 1
            continue  # Explicit keep-local during a pull never uploads.
        transfer(cfg, sftp, rel, direction, local, remote, stop)
        baseline[rel] = local if direction == 'local' else remote
        if direction == 'local': uploaded += 1
        else: downloaded += 1
        log('success', ('↑ 已上传 · ' if direction == 'local' else '↓ 已下载 · ') + rel)
    for rel in set(baseline) - (lf | rf): baseline.pop(rel, None)
    save_baseline(cfg, baseline)
    return uploaded, downloaded, skipped, conflicts


class SyncManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.logs = deque(maxlen=200)
        self.resolutions = {}
        self.mapping = None
        self.state = dict(running=False, mode='idle', uploaded=0, downloaded=0, skipped=0, conflicts=[], last_sync=None, error=None, direction='both')

    def log(self, level, message):
        with self.lock:
            self.logs.append(dict(time=time.strftime('%H:%M:%S'), level=level, message=message))

    def snapshot(self):
        with self.lock:
            return dict(self.state, logs=list(self.logs))

    def start(self, cfg, automatic):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError('同步正在运行，请先停止当前任务。')
            self.stop_event.clear()
            key = mapping_key(cfg)
            if self.mapping != key: self.resolutions.clear()
            self.mapping = key
            self.state.update(running=True, mode='auto' if automatic else 'once', uploaded=0, downloaded=0,
                              skipped=0, conflicts=[], last_sync=None, error=None, direction=cfg.get('direction', 'both'))
            self.logs.clear()
            self.thread = threading.Thread(target=self._run, args=(cfg, automatic), daemon=True)
            self.thread.start()

    def _run(self, cfg, automatic):
        client = None
        last_conflicts = []
        try:
            baseline = load_baseline(cfg)
            self.log('info', '正在连接 SSH…')
            client, sftp = connect(cfg)
            self.log('success', '连接成功，开始同步')
            while not self.stop_event.is_set():
                with self.lock:
                    resolutions, self.resolutions = self.resolutions, {}
                uploaded, downloaded, skipped, conflicts = sync_once(cfg, sftp, self.stop_event, self.log, baseline, resolutions)
                if conflicts != last_conflicts:
                    if conflicts: self.log('error', f'{len(conflicts)} 个文件存在两端修改冲突，请在下方选择保留版本。')
                    last_conflicts = conflicts
                with self.lock:
                    self.state['uploaded'] += uploaded
                    self.state['downloaded'] += downloaded
                    self.state['conflicts'] = conflicts
                    self.state.update(skipped=skipped, last_sync=time.strftime('%H:%M:%S'))
                if uploaded or downloaded or not automatic:
                    self.log('success', f'检查完成 · 上传 {uploaded} 个，下载 {downloaded} 个，冲突 {len(conflicts)} 个')
                if not automatic or self.stop_event.wait(3): break
        except Stopped:
            self.log('info', '已停止同步')
        except Exception as exc:
            message = error_message(exc).replace(cfg['password'], '••••')
            with self.lock: self.state['error'] = message
            self.log('error', message)
        finally:
            if client: client.close()
            cfg['password'] = ''
            with self.lock:
                self.state.update(running=False, mode='idle')

    def resolve(self, path, choice):
        if choice not in ('local', 'remote'): raise ValueError('请选择保留本地或远程版本。')
        with self.lock:
            conflict = next((c for c in self.state['conflicts'] if c['path'] == path), None)
            if conflict is None: raise ValueError('冲突状态已更新，请刷新后重试。')
            self.resolutions[path] = dict(conflict, choice=choice)

    def stop(self):
        self.stop_event.set()
        self.log('info', '正在停止，等待当前网络操作结束…')
