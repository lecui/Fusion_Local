"""Per-Windows-user cloud login; credentials and session protected by DPAPI."""
import base64,ctypes,hashlib,http.client,json,os,pathlib,socket,ssl,threading,time,urllib.error,urllib.parse,urllib.request
from ctypes import wintypes

LEGACY_ROOT=pathlib.Path(os.environ.get('LOCALAPPDATA',str(pathlib.Path.home())))/'FusionPortableCloud'
ROOT=pathlib.Path.home()/'AppData'/'Local'/'FusionPortableCloud'
PROFILE=ROOT/'profile.bin'
PAUSED=ROOT/'offline.flag'
ROOT.mkdir(parents=True,exist_ok=True)
if LEGACY_ROOT!=ROOT and not (ROOT/'session.bin').exists() and (LEGACY_ROOT/'session.bin').exists():
    import shutil
    shutil.copy2(LEGACY_ROOT/'session.bin',ROOT/'session.bin')
STATE=ROOT/'session.bin'
LOCK=threading.RLock()
_message=''
TRANSFER=threading.local()

class Blob(ctypes.Structure):
    _fields_=[('cbData',wintypes.DWORD),('pbData',ctypes.POINTER(ctypes.c_ubyte))]

def crypt(data,decrypt=False):
    buffer=ctypes.create_string_buffer(data)
    source=Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)));dest=Blob()
    dll=ctypes.WinDLL('crypt32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree.restype=ctypes.c_void_p
    if decrypt:
        fn=dll.CryptUnprotectData
        fn.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        args=(ctypes.byref(source),None,None,None,None,1,ctypes.byref(dest))
    else:
        fn=dll.CryptProtectData
        fn.argtypes=[ctypes.POINTER(Blob),wintypes.LPCWSTR,ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        args=(ctypes.byref(source),'Fusion cloud session',None,None,None,1,ctypes.byref(dest))
    fn.restype=wintypes.BOOL
    if not fn(*args):raise RuntimeError('Windows не удалось защитить данные подключения')
    try:return ctypes.string_at(dest.pbData,dest.cbData)
    finally:kernel.LocalFree(ctypes.cast(dest.pbData,ctypes.c_void_p))

def read_state():
    if not STATE.exists():return None
    try:return json.loads(crypt(STATE.read_bytes(),True))
    except Exception:raise RuntimeError('Не удалось прочитать сохранённую сессию Windows. Подключитесь заново')

def save_state(state):
    ROOT.mkdir(parents=True,exist_ok=True)
    tmp=STATE.with_suffix('.tmp');tmp.write_bytes(crypt(json.dumps(state).encode()));os.replace(tmp,STATE)

def revoke(state):
    global _message
    with LOCK:
        current=read_state()
        if not state.get('token') or not current or current.get('token')!=state['token']:return
        # Invalidate only the session, never the encrypted saved login profile.
        STATE.unlink(missing_ok=True);PAUSED.touch()
        _message='Включён офлайн-режим: выполнен вход на другом устройстве, истёк срок сессии или доступ отозван. Для повторного входа нажмите «Подключиться».'

def normalize_address(value):
    if not isinstance(value,str):raise ValueError('Введите адрес сервера')
    value=value.strip()
    if '://' not in value:value='https://'+value
    url=urllib.parse.urlsplit(value)
    if url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('','/'):
        raise ValueError('Укажите HTTPS-адрес или IP сервера, без пути и пароля')
    if url.port is not None and not 1<=url.port<=65535:raise ValueError('Некорректный порт')
    return urllib.parse.urlunsplit(('https',url.netloc,'','',''))

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def require_online():
    if PAUSED.exists():raise RuntimeError('Офлайн: изменения сохранены в очереди до подключения')

def request(state,path,body=None):
    if path!='/v1/login':require_online()
    ca=state.get('ca','').strip()
    if ca:
        cert=pathlib.Path(ca).expanduser().resolve()
        if not cert.is_file() or cert.stat().st_size>1024*1024:raise ValueError('Не найден файл сертификата сервера')
        context=ssl.create_default_context(cafile=str(cert))
    else:context=ssl.create_default_context()
    # Do not send account credentials to a redirect destination or a configured HTTP proxy.
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context),NoRedirect())
    headers={'Accept':'application/json'}
    if state.get('token'):headers['Authorization']='Bearer '+state['token']
    data=None if body is None else json.dumps(body).encode('utf-8')
    if data is not None:headers['Content-Type']='application/json'
    # Session probes must fail fast: startup and the watchdog never retry them.
    probe=path=='/v1/session'
    attempts=1 if probe else (5 if body is None else 1)
    timeout=3 if probe else 12
    for attempt in range(attempts):
        if path!='/v1/login':require_online()
        try:
            req=urllib.request.Request(state['address']+path,data=data,headers=headers)
            with opener.open(req,timeout=timeout) as response:
                payload=response.read(2*1024*1024+1)
                if len(payload)>2*1024*1024:raise ValueError('Слишком большой ответ сервера')
                return json.loads(payload)
        except urllib.error.HTTPError as exc:
            if exc.code==401:revoke(state)
            raise
        except (urllib.error.URLError,TimeoutError,OSError):
            if attempt+1>=attempts:raise
            time.sleep(.35*(attempt+1))

def ssl_context(state):
    ca=state.get('ca','').strip()
    if ca:
        cert=pathlib.Path(ca).expanduser().resolve()
        if not cert.is_file() or cert.stat().st_size>1024*1024:raise ValueError('Не найден файл сертификата сервера')
        return ssl.create_default_context(cafile=str(cert))
    return ssl.create_default_context()

def dav(state,method,remote,source=None,headers=None,max_bytes=8*1024*1024):
    """Small, redirect-free WebDAV client. File uploads/downloads are streamed."""
    url=urllib.parse.urlsplit(state['address'])
    path='/dav/'+urllib.parse.quote(remote.strip('/'),safe='/')
    h={'Authorization':'Bearer '+state['token'],'Accept':'application/octet-stream'}
    h.update(headers or {})
    attempts=5 if method in ('GET','HEAD','MKCOL') else 1
    for attempt in range(attempts):
        require_online()
        conn=http.client.HTTPSConnection(url.hostname,url.port or 443,context=ssl_context(state),timeout=8)
        try:
            conn.connect();conn.sock.settimeout(120 if source is not None else 12)
            if source is not None:
                source=pathlib.Path(source);h['Content-Length']=str(source.stat().st_size)
                conn.putrequest(method,path)
                for key,value in h.items():conn.putheader(key,str(value))
                conn.endheaders()
                transferred=0
                with source.open('rb') as stream:
                    while True:
                        chunk=stream.read(1024*1024)
                        if not chunk:break
                        conn.send(chunk);transferred+=len(chunk)
                        callback=getattr(TRANSFER,"progress",None)
                        if callback:callback(bytes=transferred,bytesTotal=source.stat().st_size)
            else:conn.request(method,path,headers=h)
            response=conn.getresponse();body=response.read(max_bytes+1)
            if len(body)>max_bytes:raise ValueError('Слишком большой ответ сервера')
            allowed={'GET':(200,),'HEAD':(200,),'PUT':(201,204),'MKCOL':(201,405),'DELETE':(204,),'MOVE':(204,)}.get(method,(200,201,204))
            if response.status not in allowed:
                exc=urllib.error.HTTPError(state['address']+path,response.status,response.reason,response.headers,None);exc.cloud_body=body;raise exc
            return response.status,{k.lower():v for k,v in response.getheaders()},body
        except urllib.error.HTTPError as exc:
            if exc.code==401:revoke(state)
            raise
        except (TimeoutError,OSError,http.client.HTTPException):
            if attempt+1>=attempts:raise
            time.sleep(.35*(attempt+1))
        finally:conn.close()

def dav_download(state,remote,target):
    url=urllib.parse.urlsplit(state['address']);path='/dav/'+urllib.parse.quote(remote.strip('/'),safe='/')
    target=pathlib.Path(target);tmp=target.with_suffix(target.suffix+'.part')
    for attempt in range(5):
        require_online()
        conn=http.client.HTTPSConnection(url.hostname,url.port or 443,context=ssl_context(state),timeout=8)
        try:
            conn.connect();conn.sock.settimeout(120)
            conn.request('GET',path,headers={'Authorization':'Bearer '+state['token'],'Accept':'application/octet-stream'})
            response=conn.getresponse()
            if response.status!=200:
                response.read(65537);raise urllib.error.HTTPError(state['address']+path,response.status,response.reason,response.headers,None)
            transferred=0;total=int(response.getheader('Content-Length','0'))
            with tmp.open('wb') as out:
                while True:
                    chunk=response.read(1024*1024)
                    if not chunk:break
                    out.write(chunk);transferred+=len(chunk)
                    callback=getattr(TRANSFER,"progress",None)
                    if callback:callback(bytes=transferred,bytesTotal=total)
            os.replace(tmp,target);return {k.lower():v for k,v in response.getheaders()}
        except urllib.error.HTTPError as exc:
            tmp.unlink(missing_ok=True)
            if exc.code==401:revoke(state)
            raise
        except (TimeoutError,OSError,http.client.HTTPException):
            tmp.unlink(missing_ok=True)
            if attempt==4:raise
            time.sleep(.35*(attempt+1))
        finally:conn.close()

def public(state,status='connected',message=''):
    if not state and PROFILE.exists():
        try:
            profile=json.loads(crypt(PROFILE.read_bytes(),True))
            state={key:profile.get(key,'') for key in ('address','username','ca')}
        except Exception:
            state=None
    return {'ok':True,'status':status,'connected':status=='connected','address':state.get('address','') if state else '',
        'username':state.get('username','') if state else '', 'ca':state.get('ca','') if state else '', 'message':message}

def handle(payload):
    global _message
    if payload.get('operation')=='cloud-profile':
        profile=json.loads(crypt(PROFILE.read_bytes(),True)) if PROFILE.exists() else {}
        return dict(profile,ok=True)
    # A user pause must not wait for the watchdog's network timeout.
    if payload.get('operation')=='cloud-offline':
        PAUSED.touch()
        return public(read_state(),'offline','Обмен отключён пользователем')
    with LOCK:
        op=payload.get('operation')
        if op=='cloud-profile':
            profile=json.loads(crypt(PROFILE.read_bytes(),True)) if PROFILE.exists() else {}
            return dict(profile,ok=True)
        if op=='cloud-online':
            PAUSED.unlink(missing_ok=True)
            state=read_state()
            if state:
                result=handle({'operation':'cloud-status'})
                if result.get('connected'):return result
                if result.get('status')=='offline':return result
            profile=handle({'operation':'cloud-profile'})
            if not profile.get('password'):raise ValueError('Сначала укажите данные подключения')
            return handle(dict(profile,operation='cloud-login'))
        if op=='cloud-login':
            state={'address':normalize_address(payload.get('address','')),'ca':str(payload.get('ca','')).strip()}
            name=payload.get('username','').strip();password=payload.get('password','')
            if not name or not isinstance(password,str) or not password:raise ValueError('Введите логин и пароль')
            try:result=request(state,'/v1/login',{'username':name,'password':password,'device':socket.gethostname()})
            except urllib.error.HTTPError as exc:
                if exc.code==401:raise ValueError('Неверный логин или пароль')
                if exc.code==429:raise ValueError('Слишком много попыток входа. Подождите 15 минут')
                raise ValueError('Сервер отклонил вход: HTTP '+str(exc.code))
            if not isinstance(result,dict) or not isinstance(result.get('token'),str) or not isinstance(result.get('username'),str):
                raise ValueError('Ответ не соответствует серверу Fusion Cloud')
            state.update(token=result['token'],username=result['username'],expires=result['expires'])
            try:save_state(state)
            except Exception:
                try:request(state,'/v1/logout',{})
                finally:raise
            profile=dict(address=state['address'],username=name,password=password,ca=state['ca'])
            temp=PROFILE.with_suffix('.tmp');temp.write_bytes(crypt(json.dumps(profile).encode()));os.replace(temp,PROFILE)
            PAUSED.unlink(missing_ok=True)
            _message='';return public(state)
        if op=='cloud-logout':
            state=read_state()
            try:
                if state and not PAUSED.exists():request(state,'/v1/logout',{})
            except (OSError,ValueError):pass
            STATE.unlink(missing_ok=True);PAUSED.touch();_message='';return public(None,'disconnected')
        if PAUSED.exists():return public(read_state(),'revoked' if _message else 'offline',_message or 'Обмен отключён пользователем')
        if op not in ('cloud-status','cloud-files'):raise ValueError('Неизвестная команда подключения')
        state=read_state()
        if not state:return public(None,'revoked' if _message else 'disconnected',_message)
        try:
            response=request(state,'/v1/session' if op=='cloud-status' else '/v1/files')
            if PAUSED.exists():return public(state,'revoked' if _message else 'offline',_message or 'Обмен отключён пользователем')
            result=public(state)
            if op=='cloud-files':result['items']=response['items']
            return result
        except urllib.error.HTTPError as exc:
            if exc.code==401:
                revoke(state)
                return public(state,'revoked',_message)
            raise ValueError('Ошибка сервера: HTTP '+str(exc.code))
        except (OSError,ValueError):
            PAUSED.touch()
            return public(state,'offline','Сервер недоступен. Изменения остаются в очереди. Для возобновления нажмите «Подключиться».')
