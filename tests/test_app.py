from app import app, TOKEN


def test_http_access_controls():
    client = app.test_client()
    assert client.get('/').status_code == 200
    assert client.get('/', headers={'Host': 'evil.example'}).status_code == 403
    assert client.post('/api/stop', json={}).status_code == 403
    assert client.post('/api/stop', json={}, headers={'X-Sync-Token': TOKEN, 'Origin': 'https://evil.example'}).status_code == 403
    assert client.post('/api/stop', json={}, headers={'X-Sync-Token': TOKEN}).status_code == 200
    assert client.post('/api/start', json={}, headers={'X-Sync-Token': TOKEN}).status_code == 400
    assert client.get('/').headers['Content-Security-Policy'].startswith("default-src 'self'")


def test_status_never_includes_credentials():
    response = app.test_client().get('/api/status')
    assert response.status_code == 200
    assert 'password' not in response.get_json()


def test_api_connection_trust_start_and_conflict_resolution(ssh, monkeypatch):
    import time
    import app as web
    from sync_engine import SyncManager
    cfg, local, remote = ssh
    monkeypatch.setattr(web, 'manager', SyncManager())
    client = app.test_client()
    headers = {'X-Sync-Token': TOKEN}
    response = client.post('/api/test', json=dict(cfg, trusted=''), headers=headers)
    assert response.status_code == 409
    assert response.json['fingerprint'] == cfg['trusted']
    assert client.post('/api/test', json=cfg, headers=headers).status_code == 200
    remote.mkdir()
    (local / 'a').write_text('local')
    (remote / 'a').write_text('remote')
    assert client.post('/api/start', json=cfg, headers=headers).status_code == 200
    web.manager.thread.join(10)
    assert len(client.get('/api/status').json['conflicts']) == 1
    assert client.post('/api/resolve', json={'path': 'a', 'choice': 'remote'}, headers=headers).status_code == 200
    assert client.post('/api/start', json=cfg, headers=headers).status_code == 200
    web.manager.thread.join(10)
    state = client.get('/api/status').json
    assert state['conflicts'] == []
    assert state['downloaded'] == 1
    assert (local / 'a').read_text() == 'remote'
