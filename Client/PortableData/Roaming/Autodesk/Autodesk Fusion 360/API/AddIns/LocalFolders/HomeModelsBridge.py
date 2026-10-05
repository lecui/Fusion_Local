import base64,json,pathlib,ntpath
from native_cache import NativeCache
from cache_codec import folder_container
from file_codec import file_container
from model_operations import identity,SAMPLE_PROJECT_IDS
import ModelBridge

def catalogue(data,root):
    folders={d['key']:d for d,_,_ in folder_container(data)[0]}
    items=[];seen=set()
    for d,_,_ in file_container(data)[0]:
        key=d['key'];p=pathlib.Path(key)
        if not ntpath.isabs(key) or not ntpath.splitdrive(key)[0]:continue
        if not p.resolve().is_relative_to(root.resolve()) or not p.is_file():continue
        if identity(key) in seen:continue
        parent=folders.get(d['0x48']);parts=[];visited=set();sample=False
        while parent and parent['key'] not in visited:
            visited.add(parent['key'])
            if parent.get('0x68') in SAMPLE_PROJECT_IDS:sample=True
            parts.append(parent['0x88']);parent=folders.get(parent['0x48'])
        if sample:continue
        seen.add(identity(key))
        items.append({'id':key,'name':d['0x88'],'folder':' / '.join(reversed(parts)), 'modified':d.get('0xe8',0),'extension':p.suffix.lstrip('.'),'thumbnail':d.get('0xc8','') if d.get('0xc8','').startswith(('data:image/png;base64,','data:image/jpeg;base64,')) else ''})
    return sorted(items,key=lambda x:x['name'].casefold())

def execute(request):
    op=request.get('operation')
    if op in ('trash-list','trash-preview','restore','purge'):
        import TrashBridge
        return TrashBridge.execute(request)
    if op in ('preview','rename','delete'):return ModelBridge.execute(request)
    if op not in ('list','open'):raise ValueError('Неизвестная операция')
    native=NativeCache();native.flush();path=native.path()
    items=catalogue(path.read_bytes(),path.parent/'W.login')
    if op=='list':return {'ok':True,'items':items}
    item=next((i for i in items if identity(i['id'])==identity(request.get('id',''))),None)
    if not item:raise ValueError('Модель не найдена')
    # Use the same offline document command as Fusion's native Data panel.
    # Local file paths are not cloud DataFile IDs accepted by findFileById.
    return {'ok':True,'openArgs':{'extUrn':item['id'],'fileUri':item['id'],'loadOffline':1}}

def encoded(payload):
    try:
        if len(payload)>32768:raise ValueError('Слишком длинный запрос')
        result=execute(json.loads(base64.b64decode(payload,validate=True)))
    except Exception as exc:result={'ok':False,'error':str(exc)}
    return 'LOCAL_HOME_RESULT:'+json.dumps(result,ensure_ascii=True,separators=(',',':'))
