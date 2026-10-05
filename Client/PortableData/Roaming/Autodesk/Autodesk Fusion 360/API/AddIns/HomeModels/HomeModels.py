"""Loopback-only model bridge; all Fusion calls run on its main event thread."""
import adsk.core
import pathlib,json,threading,queue,secrets,sys
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
HERE=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent/'LocalFolders'))
import HomeModelsBridge
import CloudConnection
import CloudSync
import SyncGuard
CONFIG=json.loads((HERE/'config.json').read_text(encoding='utf-8'))
EVENT='Portable.HomeModels.Request'
pending=queue.Queue(maxsize=32)
handlers=[]
server=None
app=None
shutdown=threading.Event()
sync_lock=threading.Lock()
rerun=threading.Event()
document_handlers=[]
status_lock=threading.Lock()
connection={'ok':True,'status':'disconnected','connected':False}
exchange={'running':False,'message':'','current':0,'total':0}
catalog_revision=0

def progress(**values):
    if shutdown.is_set() or CloudConnection.PAUSED.exists():raise RuntimeError('Обмен остановлен: офлайн')
    with status_lock:
        if "message" in values:exchange.update(bytes=0,bytesTotal=0)
        exchange.update(values)

def ui_call(fn):
    task={'fn':fn,'request':{},'done':threading.Event(),'lock':threading.Lock(),'cancelled':False,'started':False}
    pending.put_nowait(task);app.fireCustomEvent(EVENT)
    while not task['done'].wait(.2):
        if shutdown.is_set():
            with task['lock']:task['cancelled']=True
            raise RuntimeError('Fusion закрывается')
    if 'exception' in task:raise task['exception']
    return task['result']

def sync_worker():
    try:
        while not shutdown.is_set():
            if shutdown.wait(1):break
            if CloudConnection.PAUSED.exists() or not connection.get('connected'):break
            rerun.clear();saved=SyncGuard.queue_snapshot()
            CloudConnection.TRANSFER.progress=progress
            result=CloudSync.execute(ui_call,progress)
            if result.get('ok'):SyncGuard.acknowledge(saved)
            progress(message=result.get('message',''),stage='Завершено' if result.get('ok') else 'Завершено с замечаниями',filename='',ok=result.get('ok',False))
            if not rerun.is_set():break
    except Exception as exc:
        with status_lock:exchange.update(message=str(exc),stage='Остановлено',ok=False)
    finally:
        SyncGuard.gate(False)
        with status_lock:exchange['running']=False
        sync_lock.release()
        if rerun.is_set() and not shutdown.is_set():enqueue_sync()

def enqueue_sync():
    if CloudConnection.PAUSED.exists():return public_status()
    if not connection.get('connected'):return public_status()
    rerun.set()
    if sync_lock.acquire(False):
        try:
            progress(running=True,message='Подключение к серверу…',stage='Подключение',filename='',current=0,total=0,ok=True)
            threading.Thread(target=sync_worker,daemon=True).start()
        except Exception:
            sync_lock.release()
            raise
    return public_status()

def public_status():
    with status_lock:
        result=dict(connection);result['sync']=dict(exchange)
    result['catalogRevision']=catalog_revision
    result['availability']=SyncGuard.snapshot()
    if result.get('connected'):
        if result['sync']['running']:result['status']='syncing'
        result['message']=result['sync']['message']
        transfer=result['sync']
        if transfer.get('running') and transfer.get('bytesTotal'):
            result['message']+=' — {}%'.format(min(100,int(100*transfer.get('bytes',0)/transfer['bytesTotal'])))
    if result['availability']['queued'] and not result['sync']['running']:
        result['sync']['stage']='В очереди на синхронизацию'
        result['message']='Ожидают синхронизации: {}. {}'.format(result['availability']['queued'],result.get('message',''))
    return result

def refresh_icon():
    def update():
        import ServerPanel,logging
        try:ServerPanel.update(app,bool(public_status().get('connected')))
        except Exception:logging.getLogger('FusionPrivateServer').exception('Cannot update clock color')
    ui_call(update)

def watch_session():
    global connection
    first=True
    notified=False
    was_connected=False
    while not shutdown.is_set():
        try:
            result=CloudConnection.handle({'operation':'cloud-status'})
            with status_lock:connection=result
            SyncGuard.connected(result.get('connected',False))
            refresh_icon()
            if result.get('status')=='revoked' and not notified:
                notified=True
                ui_call(lambda:app.userInterface.messageBox('Включён офлайн-режим.\n\nСессия завершена: выполнен вход на другом устройстве, истёк срок входа или доступ отозван.\nЛокальные модели доступны. Для возобновления обмена откройте панель сервера кнопкой с часами.','Включён офлайн-режим'))
            if result.get('connected'):notified=False
            if result.get('connected') and (first or not was_connected):
                SyncGuard.gate(True);enqueue_sync();first=False
            elif not result.get('connected'):SyncGuard.gate(False)
            was_connected=bool(result.get('connected'))
        except Exception as exc:
            CloudConnection.PAUSED.touch()
            with status_lock:connection={'ok':True,'status':'offline','connected':False,'message':str(exc)}
            SyncGuard.connected(False);SyncGuard.gate(False)
            was_connected=False
            try:refresh_icon()
            except Exception:pass
        if shutdown.wait(10):break

def check_open(path,permit=True):
    if CloudConnection.PAUSED.exists():return {'ok':True}
    if not public_status().get('connected') and not SyncGuard.snapshot()['checking']:return {'ok':True}
    state=CloudConnection.read_state()
    if not state:return {'ok':True}
    info=ui_call(lambda:HomeModelsBridge.execute({'operation':'open','id':path}))
    message=SyncGuard.reason(path)
    if message:raise ValueError(message)
    remote=CloudSync.remote_models(state);mid=CloudSync.model_id(path)
    known=CloudSync.read_sync()
    if known.get('account')!=state['address']+'|'+state['username']:known={}
    last=known.get('models',{}).get(mid,{}).get('hash')
    action=SyncGuard.classify(CloudSync.sha(path),remote.get(mid,{}).get('hash'),last)
    if action in ('download','conflict'):
        message='На сервере есть изменённая версия. Необходимо обновить файлы.' if action=='download' else 'Серверная и локальная версии различаются. Требуется разрешить конфликт перед открытием.'
        SyncGuard.mark(path,action,pathlib.Path(path).stem,message)
        if action=='download':enqueue_sync()
        raise ValueError(message)
    if action=='upload':SyncGuard.saved(path);enqueue_sync()
    if SyncGuard.reason(path):raise ValueError(SyncGuard.reason(path))
    if permit:SyncGuard.grant()
    return info

class Saved(adsk.core.DocumentEventHandler):
    def notify(self,args):
        path=args.fullPath
        if not path and args.document and args.document.dataFile:path=args.document.dataFile.id
        SyncGuard.saved(path or '*')
        enqueue_sync()

class Opened(adsk.core.DocumentEventHandler):
    def notify(self,args):
        path=args.fullPath
        try:
            import DocumentNames
            DocumentNames.restore(args.document,path)
        except Exception:
            import logging
            logging.getLogger("FusionPrivateServer").exception("Cannot restore model display name")
        if not path and args.document and args.document.dataFile:path=args.document.dataFile.id
        if not path or not pathlib.Path(path).is_file():return
        def verify():
            try:check_open(path,False)
            except Exception as exc:
                text=str(exc)
                if text=='Модель не найдена':return
                try:ui_call(lambda:app.userInterface.messageBox(text,'Проверка серверной версии'))
                except Exception:pass
        threading.Thread(target=verify,daemon=True).start()

class OpenCommand(adsk.core.ApplicationCommandEventHandler):
    def notify(self,args):
        if 'export' in args.commandId.lower():
            import ProjectExport
            ProjectExport.capture(app)
        if args.commandId in ('PLM360OpenAttachmentCommand','OpenNonFusionCADFileCommand','OpenAddInSupportedFileCommand') and SyncGuard.command_blocked():
            args.isCanceled=True
            app.userInterface.messageBox('Обновление моделей ещё не завершено. Дождитесь синхронизации или включите офлайн-режим.','Синхронизация моделей')

class Handler(adsk.core.CustomEventHandler):
    def notify(self,args):
        while True:
            try:task=pending.get_nowait()
            except queue.Empty:return
            with task['lock']:
                if task['cancelled']:continue
                task['started']=True
            try:
                task['result']=task['fn']() if 'fn' in task else HomeModelsBridge.execute(task['request'])
            except Exception as exc:
                task['exception']=exc;task['result']={'ok':False,'error':str(exc)}
            finally:task['done'].set()

class HTTP(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,status,data):
        body=json.dumps(data,ensure_ascii=True).encode()
        self.send_response(status)
        self.send_header('Access-Control-Allow-Origin','*')
        self.send_header('Access-Control-Allow-Headers','Content-Type, X-Local-Token')
        self.send_header('Access-Control-Allow-Methods','POST, OPTIONS')
        self.send_header('Access-Control-Allow-Private-Network','true')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)));self.end_headers()
        try:self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError):pass
    def do_OPTIONS(self):self.reply(200,{})
    def do_POST(self):
        global connection,catalog_revision
        if self.path!='/models' or not secrets.compare_digest(self.headers.get('X-Local-Token',''),CONFIG['token']):
            self.reply(403,{'ok':False,'error':'Access denied'});return
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=32768:raise ValueError('Invalid request size')
            request=json.loads(self.rfile.read(size))
            if not isinstance(request,dict):raise ValueError('Invalid request')
            op=request.get('operation')
            if op=='cloud-status':self.reply(200,public_status());return
            if op=='cloud-sync':self.reply(200,enqueue_sync());return
            if op=='cloud-check-open':self.reply(200,check_open(request.get('id','')));return
            if op in ('project-export-folders','project-export-save'):
                import ProjectExport
                result=ui_call(lambda:ProjectExport.execute(app,request))
                if op=='project-export-save':
                    SyncGuard.saved(result['id'])
                    with status_lock:catalog_revision+=1
                    enqueue_sync()
                self.reply(200,result);return
            if str(op).startswith('cloud-'):
                result=CloudConnection.handle(request)
                if op in ('cloud-login','cloud-logout','cloud-online','cloud-offline'):
                    with status_lock:connection=result
                    SyncGuard.connected(result.get('connected',False))
                    refresh_icon()
                if op in ('cloud-login','cloud-online') and result.get('connected'):
                    SyncGuard.gate(True);enqueue_sync()
                self.reply(200,result);return
            task={'request':request,'done':threading.Event(),'lock':threading.Lock(),'cancelled':False,'started':False}
            pending.put_nowait(task);app.fireCustomEvent(EVENT)
            if not task['done'].wait(900 if request.get('operation')=='cloud-sync' else 60):
                with task['lock']:
                    if not task['started']:task['cancelled']=True
                self.reply(503,{'ok':False,'error':'Fusion занят. Обновите список перед повторением операции.'});return
            self.reply(200,task['result'])
        except Exception as exc:self.reply(400,{'ok':False,'error':str(exc)})

def run(context):
    global server,app
    app=adsk.core.Application.get()
    import SyncBridge
    SyncBridge.runtime=sys.modules[__name__]
    shutdown.clear()
    try:SyncGuard.gate(bool(CloudConnection.read_state()) and not CloudConnection.PAUSED.exists())
    except Exception:SyncGuard.gate(False)
    import ProjectExport,logging
    try:ProjectExport.repair_local_records()
    except Exception:logging.getLogger('FusionPrivateServer').exception('Cannot repair local model availability')
    import ServerPanel
    ServerPanel.start(app)
    import DocumentNames
    for document in app.documents:
        try:DocumentNames.restore(document)
        except Exception:logging.getLogger("FusionPrivateServer").exception("Cannot restore open model name")
    event=app.registerCustomEvent(EVENT);handler=Handler();event.add(handler);handlers.append(handler)
    saved=Saved();app.documentSaved.add(saved);document_handlers.append((app.documentSaved,saved))
    opened=Opened();app.documentOpened.add(opened);document_handlers.append((app.documentOpened,opened))
    opening=OpenCommand();app.userInterface.commandStarting.add(opening);document_handlers.append((app.userInterface.commandStarting,opening))
    try:
        server=ThreadingHTTPServer(('127.0.0.1',CONFIG['port']),HTTP)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        threading.Thread(target=watch_session,daemon=True).start()
    except Exception:
        app.unregisterCustomEvent(EVENT);handlers.clear();raise

def stop(context):
    global server
    shutdown.set()
    for event,handler in document_handlers:event.remove(handler)
    document_handlers.clear()
    import ServerPanel
    ServerPanel.stop(app)
    if server:server.shutdown();server.server_close();server=None
    if app:app.unregisterCustomEvent(EVENT)
    handlers.clear()
