import errno
import os
import socket
import threading
from pathlib import Path

import paramiko
import pytest

import sync_engine as engine


class Authentication(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if (username, password) == ('tester', 'test-only-password') else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username): return 'password'

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class LocalSFTP(paramiko.SFTPServerInterface):
    def __init__(self, server, root):
        super().__init__(server)
        self.root = Path(root)

    def path(self, path):
        result = self.root / path.lstrip('/')
        if not result.resolve().is_relative_to(self.root.resolve()): raise OSError(errno.EACCES, 'outside fixture')
        return result

    def _call(self, fn):
        try: return fn()
        except OSError as exc: return paramiko.SFTPServer.convert_errno(exc.errno)

    def stat(self, path): return self._call(lambda: paramiko.SFTPAttributes.from_stat(self.path(path).stat()))
    def lstat(self, path): return self._call(lambda: paramiko.SFTPAttributes.from_stat(self.path(path).lstat()))

    def list_folder(self, path):
        def listing():
            items = []
            for p in self.path(path).iterdir():
                attr = paramiko.SFTPAttributes.from_stat(p.lstat())
                attr.filename = p.name
                items.append(attr)
            return items
        return self._call(listing)

    def mkdir(self, path, attr):
        return self._call(lambda: (self.path(path).mkdir(), paramiko.SFTP_OK)[1])

    def remove(self, path): return self._call(lambda: (self.path(path).unlink(), paramiko.SFTP_OK)[1])
    def rename(self, old, new):
        if self.path(new).exists(): return paramiko.SFTP_FAILURE
        return self._call(lambda: (os.rename(self.path(old), self.path(new)), paramiko.SFTP_OK)[1])
    def posix_rename(self, old, new): return self._call(lambda: (os.replace(self.path(old), self.path(new)), paramiko.SFTP_OK)[1])
    def chattr(self, path, attr):
        return self._call(lambda: (paramiko.SFTPServer.set_file_attr(str(self.path(path)), attr), paramiko.SFTP_OK)[1])

    def open(self, path, flags, attr):
        def opening():
            fd = os.open(self.path(path), flags, 0o600)
            mode = 'r+b' if flags & os.O_RDWR else 'wb' if flags & os.O_WRONLY else 'rb'
            stream = os.fdopen(fd, mode)
            handle = paramiko.SFTPHandle(flags)
            if flags & os.O_RDWR or not flags & os.O_WRONLY: handle.readfile = stream
            if flags & (os.O_RDWR | os.O_WRONLY): handle.writefile = stream
            return handle
        return self._call(opening)


@pytest.fixture
def ssh(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, 'STATE_DIR', tmp_path / 'state')
    root = tmp_path / 'server'
    root.mkdir()
    local = tmp_path / 'local'
    local.mkdir()
    key = paramiko.RSAKey.generate(2048)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    listener.settimeout(.2)
    stopping = threading.Event()
    transports = []
    def accept():
        while not stopping.is_set():
            try: client, _ = listener.accept()
            except socket.timeout: continue
            except OSError: break
            transport = paramiko.Transport(client)
            transports.append(transport)
            transport.add_server_key(key)
            transport.set_subsystem_handler('sftp', paramiko.SFTPServer, LocalSFTP, root=root)
            try: transport.start_server(server=Authentication())
            except (EOFError, paramiko.SSHException): transport.close()
    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    cfg = engine.config_from(dict(host='127.0.0.1', port=listener.getsockname()[1], username='tester', password='test-only-password', local=str(local), remote='/project', trusted=engine.fingerprint(key)))
    yield cfg, local, root / 'project'
    stopping.set()
    listener.close()
    for transport in transports: transport.close()
    thread.join(2)
