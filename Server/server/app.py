"""Single-instance account server and versioned, session-authenticated WebDAV subset."""
import asyncio
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree as ET

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

DATA = Path(os.environ.get('DATA_DIR', '/data'))
MAX_FILE = int(os.environ.get('MAX_FILE_BYTES', str(2 * 1024**3)))
QUOTA = int(os.environ.get('ACCOUNT_QUOTA_BYTES', str(20 * 1024**3)))
SESSION_SECONDS = int(os.environ.get('SESSION_SECONDS', str(30 * 86400)))
PH = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
DUMMY = PH.hash(secrets.token_urlsafe(24))
app = FastAPI(title='Fusion Private Cloud', version='1.0.0', docs_url=None, redoc_url=None, openapi_url=None)


@contextmanager
def db(write=False):
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATA / 'accounts.sqlite3', timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    if write:
        conn.execute('BEGIN IMMEDIATE')
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init():
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
          CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
            token TEXT, expires REAL, device TEXT, disabled INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS nodes (
            user TEXT NOT NULL REFERENCES users(id), path TEXT NOT NULL, kind TEXT NOT NULL,
            blob TEXT, size INTEGER NOT NULL DEFAULT 0, etag TEXT NOT NULL, modified REAL NOT NULL,
            deleted REAL, PRIMARY KEY(user,path));
          CREATE TABLE IF NOT EXISTS versions (
            user TEXT NOT NULL, path TEXT NOT NULL, blob TEXT NOT NULL, size INTEGER NOT NULL,
            etag TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(user,path,etag));
          CREATE TABLE IF NOT EXISTS attempts (key TEXT PRIMARY KEY, started REAL, count INTEGER);
        ''')
    (DATA / 'blobs').mkdir(exist_ok=True)
    (DATA / 'incoming').mkdir(exist_ok=True)


init()


def username(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{3,64}', value):
        raise ValueError('Login must be 3-64 characters: letters, numbers, dot, underscore, dash')
    return value.lower()


def set_user(name, password, reset=False):
    name = username(name)
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError('Password must have 12-256 characters')
    hashed = PH.hash(password)
    with db(True) as c:
        if reset:
            if not c.execute('UPDATE users SET password=?,token=NULL,expires=NULL WHERE name=?', (hashed,name)).rowcount:
                raise ValueError('Account not found')
            uid=c.execute('SELECT id FROM users WHERE name=?',(name,)).fetchone()[0]
        else:
            uid=uuid.uuid4().hex;c.execute('INSERT INTO users(id,name,password) VALUES(?,?,?)', (uid,name,hashed))
    ensure_user_directory(uid,name)


def ensure_user_directory(uid,name):
    folder=DATA/'users'/name;folder.mkdir(parents=True,exist_ok=True)
    info={'id':uid,'username':name,'storage':'Binary model data is indexed in accounts.sqlite3 and stored under /data/blobs/'+uid}
    temp=folder/'account.json.tmp';temp.write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temp,folder/'account.json')
    visible=folder/'models'
    if not visible.exists() and not visible.is_symlink():
        try:visible.symlink_to(Path('../../blobs')/uid,target_is_directory=True)
        except OSError:pass


def token_hash(request):
    value = request.headers.get('authorization', '')
    if not value.startswith('Bearer ') or not 30 <= len(value) <= 200:
        raise HTTPException(401, 'SESSION_REVOKED')
    return hashlib.sha256(value[7:].encode()).hexdigest()


def authenticated(c, token):
    row = c.execute('SELECT * FROM users WHERE token=? AND expires>? AND disabled=0', (token,time.time())).fetchone()
    if row is None:
        raise HTTPException(401, 'SESSION_REVOKED')
    return row


async def small_json(request):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 8192:
            raise HTTPException(413, 'Request too large')
    try:
        obj = json.loads(data)
        if not isinstance(obj,dict):
            raise ValueError()
        return obj
    except (ValueError,UnicodeError):
        raise HTTPException(400, 'Invalid JSON')


def check_rate(ip, name):
    now=time.time()
    with db(True) as c:
        c.execute('DELETE FROM attempts WHERE started<?', (now-900,))
        for key,limit in [('ip:'+ip,30),('name:'+name,12)]:
            row=c.execute('SELECT * FROM attempts WHERE key=?',(key,)).fetchone()
            if row and row['count'] >= limit:
                raise HTTPException(429,'Too many login attempts; wait 15 minutes')
            c.execute('INSERT INTO attempts VALUES(?,?,1) ON CONFLICT(key) DO UPDATE SET count=count+1',(key,now))


def login_sync(body,ip):
    try:
        name=username(body.get('username'))
    except ValueError:
        raise HTTPException(400,'Invalid login')
    password=body.get('password','')
    if not isinstance(password,str) or len(password)>256:
        raise HTTPException(400,'Invalid password')
    check_rate(ip,name)
    with db() as c:
        user=c.execute('SELECT * FROM users WHERE name=?',(name,)).fetchone()
    try:
        PH.verify(user['password'] if user else DUMMY,password)
    except VerificationError:
        raise HTTPException(401,'Invalid login or password')
    if user is None or user['disabled']:
        raise HTTPException(401,'Invalid login or password')
    ensure_user_directory(user['id'],user['name'])
    token=secrets.token_urlsafe(48);expires=time.time()+SESSION_SECONDS
    device=str(body.get('device','Fusion'))[:100]
    with db(True) as c:
        # A password reset or disable during verification must not resurrect a session.
        count=c.execute('UPDATE users SET token=?,expires=?,device=? WHERE id=? AND password=? AND disabled=0',
            (hashlib.sha256(token.encode()).hexdigest(),expires,device,user['id'],user['password'])).rowcount
        if count!=1:
            raise HTTPException(401,'Account changed; sign in again')
    return {'token':token,'username':name,'expires':expires,'device':device,'server_version':'1.0.0'}


@app.get('/healthz')
def health():
    return {'ok':True,'service':'fusion-private-cloud','version':'1.0.0'}


@app.post('/v1/login')
async def login(request:Request):
    body=await asyncio.wait_for(small_json(request),15)
    return await run_in_threadpool(login_sync,body,request.client.host if request.client else 'unknown')


@app.get('/v1/session')
def session(request:Request):
    with db() as c:
        user=authenticated(c,token_hash(request))
        return {'username':user['name'],'device':user['device'],'expires':user['expires']}


@app.post('/v1/logout')
def logout(request:Request):
    with db(True) as c:
        user=authenticated(c,token_hash(request))
        c.execute('UPDATE users SET token=NULL,expires=NULL WHERE id=?',(user['id'],))
    return {'ok':True}


def clean_path(path):
    if not isinstance(path,str):raise HTTPException(400,'Invalid path')
    path=unicodedata.normalize('NFC',path.rstrip('/'))
    if not path:
        return ''
    parts=path.split('/')
    if len(path)>1024 or len(parts)>32 or any(not p or p in ('.','..') or len(p)>200 or p.endswith((' ','.')) or any(ord(x)<32 or x in '\\:*?"<>|' for x in p) for p in parts):
        raise HTTPException(400,'Invalid path')
    return path


def node(c,user,path,deleted=False):
    row=c.execute('SELECT * FROM nodes WHERE user=? AND path=?',(user,path)).fetchone()
    if row is None or (row['deleted'] is not None and not deleted):
        raise HTTPException(404,'Not found')
    return row


def ensure_parent(c,user,path):
    parent=path.rpartition('/')[0]
    if parent and node(c,user,parent)['kind']!='folder':
        raise HTTPException(409,'Parent is not a folder')


def precondition(request,row):
    if row:
        if request.headers.get('if-none-match')=='*':
            raise HTTPException(412,'Already exists')
        if request.headers.get('if-match')!=row['etag']:
            raise HTTPException(412 if request.headers.get('if-match') else 428,'Version changed; refresh before writing')
    elif request.headers.get('if-match'):
        raise HTTPException(412,'File no longer exists')


def blob_path(user,blob):
    return DATA/'blobs'/user/blob


def serialized(row):
    return {k:row[k] for k in ['path','kind','size','etag','modified','deleted']}


@app.get('/v1/files')
def files(request:Request):
    with db() as c:
        user=authenticated(c,token_hash(request))
        rows=c.execute('SELECT * FROM nodes WHERE user=? AND deleted IS NULL ORDER BY path',(user['id'],))
        return {'items':[serialized(r) for r in rows]}


@app.get('/v1/sync-index')
def sync_index(request:Request):
    """Return all small Fusion metadata records in one authenticated response."""
    with db() as c:
        user=authenticated(c,token_hash(request));uid=user['id']
        rows=list(c.execute('SELECT * FROM nodes WHERE user=? AND deleted IS NULL',(uid,)))
        bypath={row['path']:row for row in rows};models=[]
        for row in rows:
            match=re.fullmatch(r'FusionModels/meta/([0-9a-f-]{36})\.json',row['path'],re.I)
            if not match or row['size']>512*1024:continue
            try:meta=json.loads(blob_path(uid,row['blob']).read_bytes())
            except (OSError,ValueError,UnicodeError):continue
            file=bypath.get('FusionModels/files/'+match.group(1).lower()+'.f3d')
            if file:
                meta['meta_etag']=row['etag'];meta['file_etag']=file['etag'];models.append(meta)
        return {'models':models}


@app.get('/v1/trash')
def trash(request:Request):
    with db() as c:
        user=authenticated(c,token_hash(request))
        return {'items':[serialized(r) for r in c.execute('SELECT * FROM nodes WHERE user=? AND deleted IS NOT NULL ORDER BY deleted DESC',(user['id'],))]}


@app.post('/v1/restore')
async def restore(request:Request):
    body=await small_json(request);path=clean_path(body.get('path',''))
    with db(True) as c:
        user=authenticated(c,token_hash(request));row=node(c,user['id'],path,True)
        if row['deleted'] is None:
            raise HTTPException(409,'Already restored')
        if body.get('etag')!=row['etag']:
            raise HTTPException(412,'Version changed')
        ensure_parent(c,user['id'],path)
        c.execute('UPDATE nodes SET deleted=NULL,modified=? WHERE user=? AND path=?',(time.time(),user['id'],path))
    return {'ok':True}


@app.post('/v1/purge')
async def purge(request:Request):
    body=await small_json(request);path=clean_path(body.get('path',''))
    garbage=[]
    with db(True) as c:
        user=authenticated(c,token_hash(request));row=node(c,user['id'],path,True)
        if row['deleted'] is None or body.get('confirm') is not True or body.get('etag')!=row['etag']:
            raise HTTPException(409,'Confirm deletion of the current trash entry')
        candidates=[r[0] for r in c.execute('SELECT DISTINCT blob FROM versions WHERE user=? AND path=?',(user['id'],path))]
        c.execute('DELETE FROM versions WHERE user=? AND path=?',(user['id'],path))
        c.execute('DELETE FROM nodes WHERE user=? AND path=?',(user['id'],path))
        for blob in candidates:
            if not c.execute('SELECT 1 FROM versions WHERE user=? AND blob=?',(user['id'],blob)).fetchone():
                garbage.append(blob_path(user['id'],blob))
    # Metadata commits first: a failed unlink can leave an orphan, never a broken live reference.
    # Recheck references under a write transaction so a simultaneous upload cannot reuse the blob.
    with db(True) as c:
        for candidate in garbage:
            if not c.execute('SELECT 1 FROM versions WHERE user=? AND blob=?',(candidate.parent.name,candidate.name)).fetchone():
                candidate.unlink(missing_ok=True)
    return {'ok':True}


async def upload(request,path,token):
    if not path:
        raise HTTPException(405,'Cannot replace root')
    with db() as c:
        authenticated(c,token)
    temp=DATA/'incoming'/uuid.uuid4().hex
    size=0;sha=hashlib.sha256()
    try:
        with temp.open('xb') as out:
            async for chunk in request.stream():
                size+=len(chunk)
                if size>MAX_FILE:
                    raise HTTPException(413,'File exceeds server size limit')
                sha.update(chunk);out.write(chunk)
            out.flush();os.fsync(out.fileno())
        blob=sha.hexdigest();etag='"'+uuid.uuid4().hex+'"'
        with db(True) as c:
            user=authenticated(c,token) # Recheck AFTER upload, serialized against new login.
            ensure_parent(c,user['id'],path)
            old=c.execute('SELECT * FROM nodes WHERE user=? AND path=?',(user['id'],path)).fetchone()
            if old and (old['deleted'] is not None or old['kind']!='file'):
                raise HTTPException(409,'Path belongs to a folder or trash entry')
            precondition(request,old)
            total=c.execute('SELECT COALESCE(SUM(size),0) FROM (SELECT blob,MAX(size) size FROM versions WHERE user=? GROUP BY blob)',(user['id'],)).fetchone()[0]
            already=c.execute('SELECT 1 FROM versions WHERE user=? AND blob=?',(user['id'],blob)).fetchone()
            if total+(0 if already else size)>QUOTA:
                raise HTTPException(507,'Account storage quota reached')
            dest=blob_path(user['id'],blob);dest.parent.mkdir(parents=True,exist_ok=True)
            if not dest.exists():
                os.replace(temp,dest)
            now=time.time()
            c.execute('INSERT INTO versions VALUES(?,?,?,?,?,?)',(user['id'],path,blob,size,etag,now))
            c.execute('INSERT INTO nodes VALUES(?,?,?,?,?,?,?,NULL) ON CONFLICT(user,path) DO UPDATE SET blob=excluded.blob,size=excluded.size,etag=excluded.etag,modified=excluded.modified',
                (user['id'],path,'file',blob,size,etag,now))
        return Response(status_code=204 if old else 201,headers={'ETag':etag,'X-Content-SHA256':blob})
    finally:
        temp.unlink(missing_ok=True)


def dav_xml(rows):
    ET.register_namespace('D','DAV:')
    root=ET.Element('{DAV:}multistatus')
    for row in rows:
        response=ET.SubElement(root,'{DAV:}response')
        ET.SubElement(response,'{DAV:}href').text='/dav/'+quote(row['path'],safe='/')+('/' if row['kind']=='folder' and row['path'] else '')
        ps=ET.SubElement(response,'{DAV:}propstat');prop=ET.SubElement(ps,'{DAV:}prop')
        ET.SubElement(prop,'{DAV:}displayname').text=row['path'].rpartition('/')[2] or 'Files'
        typ=ET.SubElement(prop,'{DAV:}resourcetype')
        if row['kind']=='folder':ET.SubElement(typ,'{DAV:}collection')
        ET.SubElement(prop,'{DAV:}getcontentlength').text=str(row['size'])
        ET.SubElement(prop,'{DAV:}getetag').text=row['etag']
        ET.SubElement(ps,'{DAV:}status').text='HTTP/1.1 200 OK'
    return ET.tostring(root,encoding='utf-8',xml_declaration=True)


@app.api_route('/dav/{raw:path}',methods=['OPTIONS','PROPFIND','GET','HEAD','PUT','MKCOL','DELETE','MOVE'])
async def dav(request:Request,raw:str):
    path=clean_path(raw);token=token_hash(request)
    if request.method=='PUT':
        return await upload(request,path,token)
    writing=request.method in ('MKCOL','DELETE','MOVE')
    with db(writing) as c:
        user=authenticated(c,token);uid=user['id']
        if request.method=='OPTIONS':
            return Response(headers={'Allow':'OPTIONS, PROPFIND, GET, HEAD, PUT, MKCOL, DELETE, MOVE'})
        if request.method=='MKCOL':
            if not path:raise HTTPException(405,'Root exists')
            ensure_parent(c,uid,path)
            if c.execute('SELECT 1 FROM nodes WHERE user=? AND path=?',(uid,path)).fetchone():raise HTTPException(405,'Already exists')
            if request.headers.get('content-length','0')!='0' or request.headers.get('transfer-encoding'):
                raise HTTPException(415,'Collection body unsupported')
            c.execute('INSERT INTO nodes VALUES(?,?,?,NULL,0,?,?,NULL)',(uid,path,'folder','"'+uuid.uuid4().hex+'"',time.time()))
            return Response(status_code=201)
        row=node(c,uid,path) if path else {'path':'','kind':'folder','size':0,'etag':'"root"'}
        if request.method=='PROPFIND':
            depth=request.headers.get('depth','1')
            if depth not in ('0','1'):raise HTTPException(403,'Only Depth 0 or 1 supported')
            rows=[row]
            if depth=='1' and row['kind']=='folder':
                rows.extend(r for r in c.execute('SELECT * FROM nodes WHERE user=? AND deleted IS NULL ORDER BY path',(uid,)) if r['path'].rpartition('/')[0]==path)
            return Response(dav_xml(rows),207,media_type='application/xml')
        if request.method in ('GET','HEAD'):
            if row['kind']!='file':raise HTTPException(405,'Use PROPFIND for folders')
            file=blob_path(uid,row['blob']);size=row['size'];etag=row['etag'];blob=row['blob']
        elif request.method in ('DELETE','MOVE'):
            if not path:raise HTTPException(403,'Cannot modify root')
            precondition(request,row)
            descendants=[r for r in c.execute('SELECT * FROM nodes WHERE user=?',(uid,)) if r['path'].startswith(path+'/')]
            if row['kind']=='folder' and descendants:raise HTTPException(409,'Folder is not empty; manage files individually')
            if request.method=='DELETE':
                c.execute('UPDATE nodes SET deleted=? WHERE user=? AND path=?',(time.time(),uid,path))
            else:
                dest=urlsplit(request.headers.get('destination',''))
                if dest.netloc and dest.netloc!=request.headers.get('host'):raise HTTPException(502,'Cross-server move unsupported')
                if not dest.path.startswith('/dav/') or dest.query or dest.fragment:raise HTTPException(400,'Invalid destination')
                target=clean_path(unquote(dest.path[5:]))
                if not target:raise HTTPException(403,'Cannot overwrite root')
                ensure_parent(c,uid,target)
                if c.execute('SELECT 1 FROM nodes WHERE user=? AND path=?',(uid,target)).fetchone():raise HTTPException(412,'Destination already exists')
                c.execute('UPDATE nodes SET path=?,modified=? WHERE user=? AND path=?',(target,time.time(),uid,path))
                c.execute('UPDATE versions SET path=? WHERE user=? AND path=?',(target,uid,path))
            return Response(status_code=204)
        else:
            raise HTTPException(405,'Unsupported method')
    headers={'Content-Length':str(size),'ETag':etag,'X-Content-SHA256':blob,'Cache-Control':'no-store'}
    if request.method=='HEAD':return Response(headers=headers)
    def chunks():
        with file.open('rb') as f:
            while True:
                with db() as c:authenticated(c,token)
                chunk=f.read(1024*1024)
                if not chunk:break
                yield chunk
    return StreamingResponse(chunks(),media_type='application/octet-stream',headers=headers)


@app.middleware('http')
async def response_headers(request,call_next):
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Cache-Control']='no-store'
    return response
