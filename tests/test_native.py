import importlib

import pytest

import sync_engine


def test_native_bridge_bidirectional_and_conflicts(ssh, monkeypatch):
    cfg, local, remote = ssh
    state_dir = sync_engine.STATE_DIR
    from macos import backend
    monkeypatch.setattr(sync_engine, 'STATE_DIR', state_dir)
    monkeypatch.setattr(backend, 'manager', sync_engine.SyncManager())
    assert 'home' in backend.handle({'method': 'test', 'data': cfg})
    (local / 'hello').write_text('local version')
    backend.handle({'method': 'start', 'data': dict(cfg)})
    backend.manager.thread.join(10)
    assert backend.handle({'method': 'status'})['uploaded'] == 1
    assert (remote / 'hello').read_text() == 'local version'
    (remote / 'hello').write_text('remote version')
    backend.handle({'method': 'start', 'data': dict(cfg)})
    backend.manager.thread.join(10)
    assert (local / 'hello').read_text() == 'remote version'
    (local / 'hello').write_text('both edited local')
    (remote / 'hello').write_text('both edited remote')
    backend.handle({'method': 'start', 'data': dict(cfg)})
    backend.manager.thread.join(10)
    assert len(backend.handle({'method': 'status'})['conflicts']) == 1
    backend.handle({'method': 'resolve', 'data': {'path': 'hello', 'choice': 'local'}})
    backend.handle({'method': 'start', 'data': dict(cfg)})
    backend.manager.thread.join(10)
    assert (remote / 'hello').read_text() == 'both edited local'
    assert backend.handle({'method': 'status'})['conflicts'] == []


def test_native_bridge_password_redaction(monkeypatch, capsys):
    from macos import backend
    def fail(command): raise ValueError('server said hidden-password')
    monkeypatch.setattr(backend, 'handle', fail)
    command = {'id': '1', 'method': 'test', 'data': {'password': 'hidden-password'}}
    backend.respond(command)
    assert 'hidden-password' not in capsys.readouterr().out
    assert 'password' not in command['data']


@pytest.fixture
def sessions(monkeypatch):
    from macos import backend
    monkeypatch.setattr(backend, 'managers', {})
    monkeypatch.setattr(backend, 'active_configs', {})
    monkeypatch.setattr(backend, 'closed_sessions', set())
    monkeypatch.setattr(backend, 'shutting_down', False)
    yield backend
    for current in backend.managers.values(): current.stop()
    for current in backend.managers.values():
        if current.thread: current.thread.join(20)


def test_browse_remote_nested_hidden_and_invalid_path(ssh, sessions):
    cfg, _, remote = ssh
    remote.mkdir()
    (remote / '子文件夹').mkdir()
    (remote / '.hidden').mkdir()
    (remote / 'readme.txt').write_text('hello')
    (remote / 'alias').symlink_to(remote / '子文件夹')
    result = sessions.handle({'method': 'browse', 'data': dict(cfg, path='/project')})
    assert result['path'] == '/project'
    assert result['parent'] == '/'
    assert {item['name'] for item in result['entries']} == {'子文件夹', '.hidden', 'readme.txt', 'alias'}
    assert [item['kind'] for item in result['entries']][:2] == ['directory', 'directory']
    assert next(item for item in result['entries'] if item['name'] == 'alias')['kind'] == 'link'
    result = sessions.handle({'method': 'browse', 'data': dict(cfg, path='/project/子文件夹')})
    assert result['parent'] == '/project'
    assert result['entries'] == []
    with pytest.raises(ValueError): sessions.handle({'method': 'browse', 'data': dict(cfg, path='relative')})
    with pytest.raises(OSError): sessions.handle({'method': 'browse', 'data': dict(cfg, path='/missing')})
    assert sessions.handle({'method': 'browse', 'data': dict(cfg, path='')})['path'] == '/'


def test_multiple_tabs_parallel_stop_and_overlap(ssh, sessions):
    import time
    cfg, local, remote = ssh
    local2 = local.parent / 'second-local'
    local2.mkdir()
    (local / 'one').write_text('first')
    (local2 / 'two').write_text('second')
    a = dict(cfg, _session='A', automatic=True)
    b = dict(cfg, _session='B', automatic=True, local=str(local2), remote='/second-remote')
    sessions.handle({'method': 'start', 'data': a})
    sessions.handle({'method': 'start', 'data': b})
    def wait_for(predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate(): return
            time.sleep(.05)
        pytest.fail('Timed out waiting for independent sessions')
    wait_for(lambda: sessions.handle({'method': 'status', 'data': {'_session': 'A'}})['uploaded'] == 1)
    wait_for(lambda: sessions.handle({'method': 'status', 'data': {'_session': 'B'}})['uploaded'] == 1)
    assert (remote / 'one').read_text() == 'first'
    assert (remote.parent / 'second-remote' / 'two').read_text() == 'second'
    assert not (remote / 'two').exists()
    with pytest.raises(ValueError, match='重叠'):
        sessions.handle({'method': 'start', 'data': dict(cfg, _session='overlap', remote='/unrelated')})
    sessions.handle({'method': 'stop', 'data': {'_session': 'A'}})
    sessions.managers['A'].thread.join(10)
    assert not sessions.handle({'method': 'status', 'data': {'_session': 'A'}})['running']
    assert sessions.handle({'method': 'status', 'data': {'_session': 'B'}})['running']
    (remote.parent / 'second-remote' / 'two').write_text('downloaded on B')
    wait_for(lambda: (local2 / 'two').read_text() == 'downloaded on B')
    sessions.handle({'method': 'close', 'data': {'_session': 'A'}})
    assert 'A' not in sessions.managers
    assert sessions.handle({'method': 'status', 'data': {'_session': 'B'}})['running']
    with pytest.raises(ValueError, match='已关闭'):
        sessions.handle({'method': 'start', 'data': a})
