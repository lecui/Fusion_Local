import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).parents[1]/'server'))
PASSWORD='correct horse 123 battery'

@pytest.fixture
def api(tmp_path,monkeypatch):
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    import app
    app=importlib.reload(app)
    app.set_user('alex',PASSWORD)
    app.set_user('other',PASSWORD)
    with TestClient(app.app) as client:
        yield app,client

def login(client,name='alex',device='PC1'):
    response=client.post('/v1/login',json={'username':name,'password':PASSWORD,'device':device})
    assert response.status_code==200,response.text
    return {'Authorization':'Bearer '+response.json()['token']}

def test_single_session_and_restart_persistence(api):
    app,c=api;a=login(c);b=login(c,device='PC2')
    assert c.get('/v1/session',headers=a).status_code==401
    assert c.get('/dav/a.f3d',headers=a).status_code==401
    assert c.put('/dav/a.f3d',content=b'old PC',headers=a).status_code==401
    assert c.get('/v1/session',headers=b).json()['device']=='PC2'
    app.init()
    assert c.get('/v1/session',headers=b).status_code==200
    assert c.post('/v1/logout',headers=b).status_code==200
    assert c.get('/v1/session',headers=b).status_code==401

def test_password_reset_and_failed_login_do_not_replace_session(api):
    app,c=api;a=login(c)
    assert c.post('/v1/login',json={'username':'alex','password':'wrong'}).status_code==401
    assert c.get('/v1/session',headers=a).status_code==200
    app.set_user('alex','new password abc 123',reset=True)
    assert c.get('/v1/session',headers=a).status_code==401

def test_webdav_versions_conflicts_trash_and_isolation(api):
    app,c=api;h=login(c);other=login(c,'other')
    assert c.request('MKCOL','/dav/Models',headers=h).status_code==201
    r=c.put('/dav/Models/test.f3d',headers={**h,'If-None-Match':'*'},content=b'first');assert r.status_code==201
    etag=r.headers['etag']
    assert c.put('/dav/Models/test.f3d',headers=h,content=b'bad overwrite').status_code==428
    assert c.put('/dav/Models/test.f3d',headers={**h,'If-Match':'"stale"'},content=b'bad').status_code==412
    assert c.get('/dav/Models/test.f3d',headers=other).status_code==404
    assert c.get('/dav/Models/test.f3d',headers=h).content==b'first'
    r=c.put('/dav/Models/test.f3d',headers={**h,'If-Match':etag},content=b'second');assert r.status_code==204
    etag=r.headers['etag']
    r=c.request('PROPFIND','/dav/Models',headers={**h,'Depth':'1'})
    assert r.status_code==207 and b'test.f3d' in r.content
    r=c.request('MOVE','/dav/Models/test.f3d',headers={**h,'If-Match':etag,'Destination':'/dav/Models/renamed.f3d'})
    assert r.status_code==204
    assert c.delete('/dav/Models/renamed.f3d',headers={**h,'If-Match':etag}).status_code==204
    assert c.get('/dav/Models/renamed.f3d',headers=h).status_code==404
    assert len(c.get('/v1/trash',headers=h).json()['items'])==1
    assert c.post('/v1/restore',headers=h,json={'path':'Models/renamed.f3d','etag':etag}).status_code==200
    assert c.get('/dav/Models/renamed.f3d',headers=h).content==b'second'
    assert c.delete('/dav/Models/renamed.f3d',headers={**h,'If-Match':etag}).status_code==204
    assert c.post('/v1/purge',headers=h,json={'path':'Models/renamed.f3d','etag':etag}).status_code==409
    assert c.post('/v1/purge',headers=h,json={'path':'Models/renamed.f3d','etag':etag,'confirm':True}).status_code==200
    assert not c.get('/v1/trash',headers=h).json()['items']
    assert not list((app.DATA/'blobs').rglob('*.*'))

def test_limits_and_paths(api,monkeypatch):
    app,c=api;h=login(c)
    monkeypatch.setattr(app,'MAX_FILE',4)
    assert c.put('/dav/big.f3d',headers=h,content=b'12345').status_code==413
    assert not list((app.DATA/'incoming').iterdir())
    assert c.put('/dav/%2e%2e/escape',headers=h,content=b'1').status_code==400
    assert c.put('/dav/a%5cb',headers=h,content=b'1').status_code==400
    assert c.request('PROPFIND','/dav/',headers={**h,'Depth':'infinity'}).status_code==403
    monkeypatch.setattr(app,'QUOTA',2)
    assert c.put('/dav/a',headers=h,content=b'123').status_code==507
    assert c.get('/v1/files',headers=h).json()['items']==[]

def test_old_upload_cannot_commit_after_new_login(api):
    app,c=api;old=login(c)
    def upload_stream():
        yield b'part1'
        app.login_sync({'username':'alex','password':PASSWORD,'device':'new PC'},'testclient')
        yield b'part2'
    assert c.put('/dav/race.f3d',headers=old,content=upload_stream()).status_code==401
    with app.db() as db:
        assert db.execute('SELECT COUNT(*) FROM nodes').fetchone()[0]==0

def test_login_throttle(api):
    _,c=api
    for _ in range(12):
        assert c.post('/v1/login',json={'username':'alex','password':'bad'}).status_code==401
    assert c.post('/v1/login',json={'username':'alex','password':'bad'}).status_code==429
