import base64,json,pathlib,hashlib,time,secrets,os,shutil,traceback
from native_cache import NativeCache
from model_operations import inspect_model,transform_model,folder_choices,identity
HERE=pathlib.Path(__file__).resolve().parent

def digest(data):return hashlib.sha256(data).hexdigest()
def execute(request):
    import adsk.core
    operation=request.get('operation');model_id=request.get('id')
    if operation not in ('preview','rename','move','delete') or not isinstance(model_id,str):raise ValueError('Некорректный запрос')
    native=NativeCache();native.flush();path=native.path()
    if path.name!='NsCloudBrowserCache_local.dat':raise ValueError('Только локальное хранилище')
    before=path.read_bytes();folders,files,model,parent,_=inspect_model(before,model_id)
    root=(path.parent/'W.login').resolve();source=pathlib.Path(model['key']).resolve()
    if not root.is_relative_to(path.parent.resolve()) or not source.is_relative_to(root):raise ValueError('Модель вне локального хранилища')
    if not source.is_file():raise ValueError('Файл модели отсутствует')
    if operation=='preview':return {'ok':True,'name':model['0x88'],'parent':parent['key'],'folders':folder_choices(folders),'revision':digest(before)}
    if request.get('revision')!=digest(before):raise ValueError('Данные изменились. Откройте меню модели повторно.')
    for doc in adsk.core.Application.get().documents:
        if doc.dataFile and identity(doc.dataFile.id)==identity(model['key']):raise ValueError('Сначала закройте эту модель, затем повторите операцию')
    changed,updated=transform_model(before,model_id,operation,request.get('value'))
    backup=path.parent/'LocalFoldersBackups'/(time.strftime('%Y%m%d-%H%M%S')+'-model-'+operation+'-'+secrets.token_hex(3))
    backup.mkdir(parents=True);(backup/'cache.dat').write_bytes(before)
    saved=backup/'files'/source.relative_to(root)
    if operation=='delete':
        for d in files:
            if d['key']==model['key']:continue
            if any(identity(d.get(k,''))==identity(str(source)) for k in ('key','0x28','0x1c8')):raise ValueError('Файл используется другой записью модели')
        saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,saved)
        if digest(source.read_bytes())!=digest(saved.read_bytes()):raise ValueError('Резервная копия не прошла проверку')
    (backup/'operation.json').write_text(json.dumps({'operation':'model-'+operation,'id':model['key'],'name':model['0x88'],'value':request.get('value'),'files':[[str(source),str(saved)]] if operation=='delete' else []},ensure_ascii=False),encoding='utf-8')
    temp=path.with_suffix('.model.tmp');temp.write_bytes(changed)
    if path.read_bytes()!=before:temp.unlink();raise ValueError('Хранилище изменилось. Повторите операцию.')
    removed=False
    try:
        os.replace(temp,path);native.load(path)
        if operation=='delete':
            if digest(source.read_bytes())!=digest(saved.read_bytes()):raise ValueError('Файл изменился во время удаления')
            source.unlink();removed=True
            import CloudDeletion
            CloudDeletion.record(model['key'],expected=digest(saved.read_bytes()))
        else:
            import CloudDeletion
            CloudDeletion.record(model['key'],'metadata')
    except Exception:
        if removed:shutil.copy2(saved,source)
        temp.write_bytes(before);os.replace(temp,path);native.load(path);raise
    return {'ok':True,'name':updated['0x88'],'id':updated['key'],'operation':operation}

def encoded(payload):
    try:
        if len(payload)>32768:raise ValueError('Слишком длинный запрос')
        result=execute(json.loads(base64.b64decode(payload,validate=True)))
    except Exception as exc:
        result={'ok':False,'error':str(exc)}
        with (HERE/'errors.log').open('a',encoding='utf-8') as log:log.write(traceback.format_exc()+'\n')
    return 'LOCAL_MODEL_RESULT:'+json.dumps(result,ensure_ascii=True,separators=(',',':'))
