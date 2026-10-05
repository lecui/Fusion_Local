"""Durable, account-scoped explicit delete/restore intents for cloud sync."""
import json,os,pathlib,threading,urllib.error
import CloudConnection
LOCK=threading.RLock()
PATH=CloudConnection.ROOT/'model-operations.json'
def read():
 return json.loads(PATH.read_text(encoding='utf-8')) if PATH.exists() else {}
def save(data):
 tmp=PATH.with_suffix('.tmp');tmp.write_text(json.dumps(data),encoding='utf-8');os.replace(tmp,PATH)
def account(state):return state['address']+'|'+state['username']
def record(path,action='delete',expected=None):
 return record_many([(path,action,expected)])

def record_many(operations):
 operations=list(operations)
 import CloudSync,SyncBridge
 state=CloudConnection.read_state()
 if not state:
  profile=CloudConnection.handle({'operation':'cloud-profile'})
  if not profile.get('address') or not profile.get('username'):return
  state=profile
 scope=account(state)
 with LOCK:
  data=read();items=data.setdefault(scope,{})
  for path,action,expected in operations:
   mid=CloudSync.model_id(path)
   if action=='metadata' and items.get(mid,{}).get('action')=='restore':continue
   items[mid]={'action':action,'path':str(path),'expected':expected}
  save(data)
 import SyncGuard
 try:
  for path,action,expected in operations:SyncGuard.saved(path)
 except OSError:pass # The account-scoped operation queue is already durable.
 # A scheduling failure must not roll back an already persisted local operation.
 if SyncBridge.runtime:
  try:SyncBridge.runtime.enqueue_sync()
  except Exception:pass
def pending(state):
 with LOCK:return dict(read().get(account(state),{}))
def acknowledge(state,mid,entry):
 with LOCK:
  data=read();items=data.get(account(state),{})
  if items.get(mid)==entry:items.pop(mid,None);save(data)
def process(state,remote,progress):
 errors=[];blocked=set()
 for mid,entry in pending(state).items():
  if entry['action']!='delete':continue
  blocked.add(mid)
  try:
   other=remote.get(mid)
   if other and entry.get('expected') and other.get('hash')!=entry['expected']:
    raise ValueError('серверная модель изменена после локальной копии; удаление остановлено')
   progress(stage='Удаление на сервере',filename=pathlib.Path(entry['path']).name,message='Удаление модели на сервере')
   # Delete content first. Repeating after a timeout is harmless (404 is accepted).
   for kind,ext in [('files','.f3d'),('meta','.json')]:
    resource='FusionModels/'+kind+'/'+mid+ext
    try:
     _,head,_=CloudConnection.dav(state,'HEAD',resource)
     if kind=='files' and entry.get('expected') and head.get('x-content-sha256')!=entry['expected']:
      raise ValueError('серверный файл изменился; удаление остановлено')
     if kind=='meta' and other and other.get('meta_etag')!=head.get('etag'):
      raise ValueError('серверные свойства изменились; удаление остановлено')
     CloudConnection.dav(state,'DELETE',resource,headers={'If-Match':head['etag']})
    except urllib.error.HTTPError as exc:
     if exc.code!=404:raise
   acknowledge(state,mid,entry)
  except Exception as exc:errors.append(pathlib.Path(entry['path']).name+': '+str(exc))
 return blocked,errors

def remote_deleted(state):
 import re
 items=CloudConnection.request(state,'/v1/trash').get('items',[]);result=set()
 for item in items:
  match=re.fullmatch(r'FusionModels/files/([0-9a-f-]{36})\.f3d',str(item.get('path','')),re.I)
  if match:result.add(match.group(1).lower())
 return result

def restore_remote(state,mid):
 """Restore explicitly requested trash entries before the ordinary sync comparison."""
 entries=CloudConnection.request(state,'/v1/trash').get('items',[])
 paths={'FusionModels/files/'+mid+'.f3d','FusionModels/meta/'+mid+'.json'}
 for item in entries:
  if item.get('path') in paths:
   CloudConnection.request(state,'/v1/restore',{'path':item['path'],'etag':item['etag']})
