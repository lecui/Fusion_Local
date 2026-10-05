"""Export a new F3D into the local project catalog on Fusion's main thread."""
import base64, copy, os, pathlib, secrets, shutil, time, unicodedata, uuid
from native_cache import NativeCache
from cache_codec import folder_container, write_folder, u32, u64
from file_codec import FILE_FIELDS, file_container, write_file
from model_operations import folder_choices

_selection=[]
_sessions={}

def capture(app):
    """Capture browser selection before Export opens and clears it."""
    global _selection
    import adsk.fusion
    _selection=[]
    for selection in app.userInterface.activeSelections:
        entity=selection.entity
        occurrence=adsk.fusion.Occurrence.cast(entity)
        component=occurrence.component if occurrence else adsk.fusion.Component.cast(entity)
        if component:_selection.append((app.activeDocument,component))

def prepare(app,source_name):
    import adsk.fusion
    doc=app.activeDocument
    design=adsk.fusion.Design.cast(doc.products.itemByProductType('DesignProductType')) if doc else None
    if not design:raise ValueError('Откройте модель Fusion')
    matches=[component for owner,component in _selection if owner==doc and component.isValid and component.name==source_name]
    if len(matches)==1:component=matches[0]
    elif source_name in (doc.name,design.rootComponent.name):component=design.rootComponent
    else:raise ValueError('Не удалось определить экспортируемый компонент. Закройте окно, выберите компонент и повторите экспорт.')
    token=secrets.token_hex(24)
    _sessions.clear();_sessions[token]=(doc,component)
    return token

def valid_name(value):
    if not isinstance(value,str):raise ValueError('Введите имя модели')
    value=unicodedata.normalize('NFC',value.strip())
    if value.lower().endswith('.f3d'):value=value[:-4]
    if not value or len(value)>150 or value in ('.','..') or value.endswith(('.', ' ')) or any(ord(c)<32 or c in '/\\:*?"<>|' for c in value):
        raise ValueError('Недопустимое имя модели')
    return value

def register(data,folder_id,name,target,thumbnail=''):
    folders,start,end=folder_container(data);files,fs,fe=file_container(data)
    choices={x['id'] for x in folder_choices([d for d,_,_ in folders])}
    if folder_id not in choices:raise ValueError('Папка недоступна для сохранения')
    if any(d['0x48']==folder_id and d['0x88'].casefold()==name.casefold() for d,_,_ in files):
        raise ValueError('Модель с таким именем уже существует. Укажите другое имя.')
    folders=[copy.deepcopy(d) for d,_,_ in folders]
    parent=next(d for d in folders if d['key']==folder_id)
    key=pathlib.Path(target).as_posix();now=int(time.time()*1000)
    # Match a native local F3D record; never copy another model's cloud IDs.
    record={field:('' if typ=='s' else bytes(typ) if isinstance(typ,int) else 0) for field,typ in FILE_FIELDS}
    record.update(key=key,version=5)
    record.update({'0x28':key,'0x48':folder_id,'0x68':'','0x88':name,'0xc8':thumbnail,'0xe8':now,
        '0x160':4,'0x168':'application/vnd.autodesk.fusion360','0x1c8':key,
        'raw45':bytes.fromhex('010000000700000001000000000000000001000000000000000000000001000000000000000000000007000000')})
    parent['files'].append(key);parent['other'].append(key);parent['0xe8']=now
    changed=data[:start-12]+u32(1)+u64(len(folders))+b''.join(write_folder(d) for d in folders)+data[end:fs]+u32(1)+u64(len(files)+1)+b''.join(write_file(d) for d,_,_ in files)+write_file(record)+data[fe:]
    file_container(changed);folder_container(changed)
    return changed

def normalize_local_records(data,root):
    """Local F3D records have no cloud project ID; folder membership is 0x48."""
    root=pathlib.Path(root).resolve()
    rows,start,end=file_container(data);changed=[];records=[]
    for record,_,_ in rows:
        d=copy.deepcopy(record)
        path=pathlib.Path(d['key'])
        if (d['0x68'] and path.is_absolute() and path.resolve().is_relative_to(root)
                and path.is_file() and path.suffix.lower()=='.f3d'
                and d['0x1c8']==d['key'] and not d['0x1e8'] and not d['0x248']):
            d['0x68']='';changed.append(d['key'])
        records.append(d)
    if not changed:return data,[]
    result=data[:start]+u32(1)+u64(len(records))+b''.join(write_file(d) for d in records)+data[end:]
    file_container(result)
    return result,changed

def repair_local_records():
    native=NativeCache();native.flush();cache=native.path();before=cache.read_bytes()
    changed,ids=normalize_local_records(before,cache.parent/'W.login'/'F')
    import LocalDocumentMetadata
    if not ids:
        return LocalDocumentMetadata.ensure(before,cache.parent/'W.login'/'F')
    backup=cache.parent/'LocalFoldersBackups'/('local-export-availability-'+time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(4))
    backup.mkdir(parents=True);shutil.copy2(cache,backup/cache.name)
    temp=cache.with_suffix('.export-repair.tmp');temp.write_bytes(changed)
    try:
        os.replace(temp,cache);native.load(cache)
    except Exception:
        temp.write_bytes(before);os.replace(temp,cache);native.load(cache);raise
    return ids+LocalDocumentMetadata.ensure(changed,cache.parent/'W.login'/'F')

def execute(app,payload):
    native=NativeCache();native.flush();cache=native.path()
    if cache.name!='NsCloudBrowserCache_local.dat':raise ValueError('Не найден локальный каталог Fusion')
    data=cache.read_bytes()
    if payload['operation']=='project-export-folders':
        return {'ok':True,'scope':prepare(app,payload.get('sourceName','')),'items':folder_choices([d for d,_,_ in folder_container(data)[0]])}
    import adsk.fusion
    doc=app.activeDocument
    design=adsk.fusion.Design.cast(doc.products.itemByProductType('DesignProductType')) if doc else None
    if not design:raise ValueError('Откройте модель Fusion для экспорта')
    scope=_sessions.get(payload.get('scope'))
    if not scope or scope[0]!=doc or not scope[1].isValid:raise ValueError('Объект экспорта изменился. Откройте окно экспорта заново.')
    component=scope[1]
    name=valid_name(payload.get('name'));folder=payload.get('folder')
    if not folder:raise ValueError('Не удалось получить папку из штатного обработчика этой сборки Fusion.')
    root=cache.parent/'W.login'/'F';root.mkdir(parents=True,exist_ok=True)
    target=root/('_'+name+'.'+str(uuid.uuid4())+'.f3d')
    register(data,folder,name,target) # Validate destination before exporting.
    staging=root/('.export-'+secrets.token_hex(12));staging.mkdir()
    exported=staging/'model.f3d';preview=staging/'preview.png'
    backup=cache.parent/'LocalFoldersBackups'/('project-export-'+time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(4))
    try:
        manager=design.exportManager
        options=manager.createFusionArchiveExportOptions(str(exported),component)
        if not manager.execute(options) or not exported.is_file() or not exported.stat().st_size:
            raise ValueError('Fusion не удалось экспортировать модель в F3D')
        thumbnail=''
        try:
            if app.activeViewport.saveAsImageFile(str(preview),256,256):thumbnail='data:image/png;base64,'+base64.b64encode(preview.read_bytes()).decode()
        except Exception:pass
        native.flush();before=cache.read_bytes()
        changed=register(before,folder,name,target,thumbnail)
        backup.mkdir(parents=True);shutil.copy2(cache,backup/cache.name)
        temp=staging/'catalog.dat';temp.write_bytes(changed)
        os.replace(exported,target)
        try:
            os.replace(temp,cache)
            import LocalDocumentMetadata
            LocalDocumentMetadata.ensure(changed,root)
            native.load(cache)
        except Exception:
            temp.write_bytes(before);os.replace(temp,cache);native.load(cache)
            # Keep the completed export recoverable if catalog registration fails.
            os.replace(target,backup/target.name)
            sidecar=pathlib.Path(str(target)+'._xx')
            if sidecar.exists():os.replace(sidecar,backup/sidecar.name)
            raise
        return {'ok':True,'id':target.as_posix(),'name':name}
    finally:
        # Delete only known temporary files created in this operation.
        for item in (exported,preview,staging/'catalog.dat'):item.unlink(missing_ok=True)
        try:staging.rmdir()
        except OSError:pass
