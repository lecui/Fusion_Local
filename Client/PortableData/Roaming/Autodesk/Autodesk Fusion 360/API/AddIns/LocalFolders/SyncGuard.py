"""Shared, thread-safe model availability and durable save queue."""
import json,os,pathlib,threading,time,secrets
import CloudConnection
LOCK=threading.RLock()
QUEUE=CloudConnection.ROOT/'pending-sync.json'
checking=False
online=False
items={}
permits=[]
try:dirty=json.loads(QUEUE.read_text(encoding='utf-8'))
except (OSError,ValueError):dirty={}
if not isinstance(dirty,dict):dirty={}

def key(path):return str(path).replace('\\','/').casefold()
def classify(local,remote,last):
    if remote is None:return 'upload'
    if local==remote:return 'same'
    if last and local==last:return 'download'
    if last and remote==last:return 'upload'
    return 'conflict'
def persist():
    QUEUE.parent.mkdir(parents=True,exist_ok=True)
    temp=QUEUE.with_suffix('.tmp');temp.write_text(json.dumps(dirty),encoding='utf-8');os.replace(temp,QUEUE)
def saved(path):
    with LOCK:dirty[key(path)]=secrets.token_hex(16);persist()
def queue_snapshot():
    with LOCK:return dict(dirty)
def acknowledge(snapshot):
    with LOCK:
        for p,generation in snapshot.items():
            if dirty.get(p)==generation:dirty.pop(p,None)
        persist()
def gate(value):
    global checking
    with LOCK:checking=bool(value)
def connected(value):
    global online
    with LOCK:online=bool(value)
def mark(path,state,name='',message=''):
    with LOCK:
        if state=='same':items.pop(key(path),None)
        else:items[key(path)]={'id':str(path),'state':state,'name':name,'message':message,'blocked':state in ('download','conflict','error','checking')}
def snapshot():
    with LOCK:return {'checking':checking,'items':[dict(v) for v in items.values()],'queued':len(dirty)}
def reason(path):
    if CloudConnection.PAUSED.exists():return ''
    with LOCK:
        if not online and not checking:return ''
        if checking:return 'Выполняется начальная проверка и синхронизация. Дождитесь её завершения.'
        item=items.get(key(path),{})
        if item.get('blocked'):return item.get('message') or 'Модель обновляется с сервера. Открытие временно недоступно.'
    return ''
def grant():
    with LOCK:permits.append(time.monotonic()+10)
def command_blocked():
    if CloudConnection.PAUSED.exists():return False
    with LOCK:
        if not online and not checking:return False
        now=time.monotonic();permits[:]=[p for p in permits if p>now]
        if permits:permits.pop(0);return False
        return checking or any(i['blocked'] for i in items.values())
