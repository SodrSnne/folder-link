import os
import threading
import time

import paramiko
import pytest

import sync_engine as engine


def cycle(cfg, sftp, baseline, resolutions=None):
    return engine.sync_once(cfg, sftp, threading.Event(), lambda *args: None, baseline, resolutions)


def test_bidirectional_new_changed_and_restart(ssh):
    cfg, local, remote = ssh
    (local / '子目录').mkdir()
    (local / '子目录' / 'hello.txt').write_text('local v1')
    client, sftp = engine.connect(cfg)
    baseline = {}
    try:
        assert cycle(cfg, sftp, baseline)[:3] == (1, 0, 0)
        assert (remote / '子目录' / 'hello.txt').read_text() == 'local v1'
        (remote / 'remote.txt').write_text('download')
        assert cycle(cfg, sftp, baseline)[:2] == (0, 1)
        assert (local / 'remote.txt').read_text() == 'download'
        (remote / '子目录' / 'hello.txt').write_text('remote changed')
        assert cycle(cfg, sftp, engine.load_baseline(cfg))[:2] == (0, 1)
        assert (local / '子目录' / 'hello.txt').read_text() == 'remote changed'
        (local / 'remote.txt').write_text('local changed')
        assert cycle(cfg, sftp, engine.load_baseline(cfg))[:2] == (1, 0)
        assert (remote / 'remote.txt').read_text() == 'local changed'
        assert cycle(cfg, sftp, engine.load_baseline(cfg))[:3] == (0, 0, 2)
    finally: client.close()


def test_conflicts_never_silently_overwrite_and_can_resolve(ssh):
    cfg, local, remote = ssh
    remote.mkdir()
    (local / 'a.txt').write_text('one')
    (remote / 'a.txt').write_text('two')
    client, sftp = engine.connect(cfg)
    baseline = {}
    try:
        result = cycle(cfg, sftp, baseline)
        conflict, = result[3]
        assert (local / 'a.txt').read_text() == 'one'
        assert (remote / 'a.txt').read_text() == 'two'
        assert cycle(cfg, sftp, baseline, {'a.txt': dict(conflict, choice='remote')})[:2] == (0, 1)
        assert (local / 'a.txt').read_text() == 'two'
        (local / 'a.txt').write_text('local edit')
        (remote / 'a.txt').write_text('remote edit')
        conflict, = cycle(cfg, sftp, baseline)[3]
        (remote / 'a.txt').write_text('newer remote edit')
        assert len(cycle(cfg, sftp, baseline, {'a.txt': dict(conflict, choice='local')})[3]) == 1
        conflict, = cycle(cfg, sftp, baseline)[3]
        assert cycle(cfg, sftp, baseline, {'a.txt': dict(conflict, choice='local')})[:2] == (1, 0)
        assert (remote / 'a.txt').read_text() == 'local edit'
    finally: client.close()


def test_content_hash_detects_same_size_and_timestamp(ssh):
    cfg, local, remote = ssh
    (local / 'a').write_text('abc')
    client, sftp = engine.connect(cfg)
    try:
        baseline = {}
        cycle(cfg, sftp, baseline)
        before = (remote / 'a').stat()
        (remote / 'a').write_text('xyz')
        os.utime(remote / 'a', (before.st_atime, before.st_mtime))
        assert cycle(cfg, sftp, baseline)[:2] == (0, 1)
        assert (local / 'a').read_text() == 'xyz'
    finally: client.close()


def test_no_deletion_and_symlink_protection(ssh):
    cfg, local, remote = ssh
    (local / 'a').write_text('keep')
    (local / '.git').mkdir()
    (local / '.git' / 'secret').write_text('ignore')
    client, sftp = engine.connect(cfg)
    try:
        baseline = {}
        cycle(cfg, sftp, baseline)
        (local / 'a').unlink()
        assert cycle(cfg, sftp, baseline)[:2] == (0, 1)
        assert (local / 'a').read_text() == 'keep'
        assert not (remote / '.git').exists()
        (local / 'link').symlink_to(local / 'a')
        (remote / 'link').write_text('do not overwrite')
        cycle(cfg, sftp, baseline)
        assert (remote / 'link').read_text() == 'do not overwrite'
        assert (local / 'a').read_text() == 'keep'
    finally: client.close()


def test_first_host_key_and_bad_password(ssh):
    cfg, _, _ = ssh
    with pytest.raises(engine.UnknownHost): engine.connect(dict(cfg, trusted=''))
    with pytest.raises(paramiko.AuthenticationException): engine.connect(dict(cfg, password='wrong'))
    client, _ = engine.connect(cfg)
    client.close()
    client, _ = engine.connect(dict(cfg, trusted=''))
    client.close()


def test_auto_sync_and_stop(ssh):
    cfg, local, remote = ssh
    manager = engine.SyncManager()
    (local / 'a').write_text('start')
    manager.start(dict(cfg), True)
    def wait_for(predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate(): return
            time.sleep(.05)
        pytest.fail(str(manager.snapshot()))
    try:
        wait_for(lambda: (remote / 'a').exists())
        (remote / 'a').write_text('from remote')
        wait_for(lambda: (local / 'a').read_text() == 'from remote')
    finally:
        manager.stop()
        manager.thread.join(20)
    assert not manager.snapshot()['running']
    assert manager.snapshot()['error'] is None


def test_cancel_preserves_existing_file_and_cleans_temp(ssh, monkeypatch):
    cfg, local, remote = ssh
    (local / 'a').write_text('original')
    client, sftp = engine.connect(cfg)
    baseline = {}
    try:
        cycle(cfg, sftp, baseline)
        (local / 'a').write_text('modified')
        def interrupted(*args, **kwargs): raise engine.Stopped()
        monkeypatch.setattr(sftp, 'put', interrupted)
        with pytest.raises(engine.Stopped): cycle(cfg, sftp, baseline)
        assert (remote / 'a').read_text() == 'original'
        assert not list(remote.glob('.ssh-sync-*'))
    finally: client.close()


def test_unsupported_atomic_replace_keeps_original(ssh, monkeypatch):
    cfg, local, remote = ssh
    (local / 'a').write_text('original')
    client, sftp = engine.connect(cfg)
    baseline = {}
    try:
        cycle(cfg, sftp, baseline)
        (local / 'a').write_text('changed')
        def unsupported(*args): raise OSError('unsupported')
        monkeypatch.setattr(sftp, 'posix_rename', unsupported)
        with pytest.raises(OSError, match='原文件已保留'): cycle(cfg, sftp, baseline)
        assert (remote / 'a').read_text() == 'original'
        assert not list(remote.glob('.ssh-sync-*'))
    finally: client.close()


def test_download_interruption_preserves_original(ssh, monkeypatch):
    cfg, local, remote = ssh
    (local / 'a').write_text('original')
    client, sftp = engine.connect(cfg)
    baseline = {}
    try:
        cycle(cfg, sftp, baseline)
        (remote / 'a').write_text('changed')
        def interrupted(source, target, **kwargs):
            from pathlib import Path
            Path(target).write_text('partial')
            raise engine.Stopped()
        monkeypatch.setattr(sftp, 'get', interrupted)
        with pytest.raises(engine.Stopped): cycle(cfg, sftp, baseline)
        assert (local / 'a').read_text() == 'original'
        assert not list(local.glob('.ssh-sync-*'))
    finally: client.close()


def test_pull_never_uploads_and_preserves_conflicting_local_files(ssh):
    cfg, local, remote = ssh
    cfg['direction'] = 'pull'
    remote.mkdir()
    (remote / 'download').write_text('from server')
    (remote / 'empty').mkdir()
    (local / 'local-only').write_text('never upload')
    (local / 'local-folder').mkdir()
    (local / 'conflict').write_text('my edits')
    (remote / 'conflict').write_text('remote edits')
    client, sftp = engine.connect(cfg)
    baseline = {}
    try:
        result = cycle(cfg, sftp, baseline)
        assert result[:2] == (0, 1)
        assert (local / 'download').read_text() == 'from server'
        assert (local / 'empty').is_dir()
        assert not (remote / 'local-only').exists()
        assert not (remote / 'local-folder').exists()
        assert (local / 'conflict').read_text() == 'my edits'
        conflict, = result[3]
        assert cycle(cfg, sftp, baseline, {'conflict': dict(conflict, choice='local')})[3] == []
        assert (remote / 'conflict').read_text() == 'remote edits'
        assert (local / 'conflict').read_text() == 'my edits'
        conflict, = cycle(cfg, sftp, baseline)[3]
        assert cycle(cfg, sftp, baseline, {'conflict': dict(conflict, choice='remote')})[:2] == (0, 1)
        assert (local / 'conflict').read_text() == 'remote edits'
        (remote / 'download').write_text('new remote version')
        assert cycle(cfg, sftp, baseline)[:2] == (0, 1)
        assert (local / 'download').read_text() == 'new remote version'
    finally: client.close()


def test_pull_does_not_create_missing_remote_directory(ssh):
    cfg, local, remote = ssh
    cfg['direction'] = 'pull'
    client, sftp = engine.connect(cfg)
    try:
        with pytest.raises(ValueError, match='不存在'): cycle(cfg, sftp, {})
        assert not remote.exists()
    finally: client.close()
