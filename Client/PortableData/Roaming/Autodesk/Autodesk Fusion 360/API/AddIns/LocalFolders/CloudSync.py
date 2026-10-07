"""Safe, explicit two-way synchronization for local Fusion model files."""
import base64,copy,hashlib,json,ntpath,os,pathlib,re,secrets,shutil,tempfile,time,uuid,urllib.error
import adsk.core
import CloudConnection
import SyncGuard
from native_cache import NativeCache
from cache_codec import add_folder,folder_container,write_folder,u32,u64
from file_codec import file_container,write_file
from model_operations import identity,SAMPLE_PROJECT_IDS

SYNC=CloudConnection.ROOT/'sync.bin'
UUID_RE=re.compile(r'\.([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.f3d$',re.I)

def sha(path):
    h=hashlib.sha256()
    with pathlib.Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def read_sync():
    try:return json.loads(CloudConnection.crypt(SYNC.read_bytes(),True)) if SYNC.exists() else {'models':{}}
    except Exception:return {'models':{}}

def save_sync(value):
    CloudConnection.ROOT.mkdir(parents=True,exist_ok=True)
    tmp=SYNC.with_suffix('.tmp');tmp.write_bytes(CloudConnection.crypt(json.dumps(value).encode()));os.replace(tmp,SYNC)

def model_id(path):
    match=UUID_RE.search(str(path))
    return str(uuid.UUID(match.group(1))) if match else str(uuid.uuid5(uuid.NAMESPACE_URL,identity(str(path))))

def encode_record(record):
    return {k:({'bytes':base64.b64encode(v).decode()} if isinstance(v,bytes) else v) for k,v in record.items()}

def decode_record(record):
    return {k:(base64.b64decode(v['bytes']) if isinstance(v,dict) and set(v)=={'bytes'} else v) for k,v in record.items()}

def local_models(data,root):
    folders={d['key']:d for d,_,_ in folder_container(data)[0]};result={}
    for row,_,_ in file_container(data)[0]:
        path=pathlib.Path(row['key'])
        if not ntpath.isabs(row['key']) or not path.is_file():continue
        try:path.resolve().relative_to(root.resolve())
        except ValueError:continue
        parent=folders.get(row['0x48']);names=[];seen=set();sample=False
        while parent and parent['key'] not in seen:
            seen.add(parent['key']);sample|=parent.get('0x68') in SAMPLE_PROJECT_IDS
            names.append(parent.get('0x88',''));parent=folders.get(parent.get('0x48'))
        if sample:continue
        mid=model_id(path)
        result[mid]={'path':path,'record':row,'name':row.get('0x88') or path.stem,'folder':' / '.join(reversed(names))}
    return result

def ensure_remote_dirs(state):
    for name in ('FusionModels','FusionModels/files','FusionModels/meta'):
        try:CloudConnection.dav(state,'MKCOL',name)
        except urllib.error.HTTPError as exc:
            if exc.code!=405:raise

def put_bytes(state,remote,payload,etag=None):
    fd,name=tempfile.mkstemp(prefix='fusion-sync-',suffix='.tmp');os.close(fd);path=pathlib.Path(name)
    try:
        path.write_bytes(payload)
        return put_file(state,remote,path,etag)
    finally:path.unlink(missing_ok=True)

def put_file(state,remote,path,etag=None):
    expected=sha(path);headers={'If-Match':etag} if etag else {'If-None-Match':'*'}
    for attempt in range(5):
        try:return CloudConnection.dav(state,'PUT',remote,path,headers=headers)[1].get('etag','')
        except urllib.error.HTTPError as exc:
            if exc.code!=412:raise
            _,head,_=CloudConnection.dav(state,'HEAD',remote)
            if head.get('x-content-sha256')==expected:return head.get('etag','')
            raise
        except (OSError,TimeoutError):
            if attempt==4:raise
            time.sleep(.4*(attempt+1))

def upload(state,mid,item,remote=None):
    report=getattr(CloudConnection.TRANSFER,'progress',lambda **kw:None)
    report(stage='Подготовка к отправке',filename=item['name'],message='Подготовка: '+item['name'])
    with tempfile.TemporaryDirectory(prefix='fusion-upload-') as td:
        frozen=pathlib.Path(td)/'model.f3d'
        before=item['path'].stat()
        shutil.copy2(item['path'],frozen)
        after=item['path'].stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
            raise ValueError('Модель сохраняется. Повторите синхронизацию после сохранения')
        snapshot_item=dict(item);snapshot_item['path']=frozen
        return upload_snapshot(state,mid,snapshot_item,remote)

def upload_snapshot(state,mid,item,remote=None):
    report=getattr(CloudConnection.TRANSFER,'progress',lambda **kw:None)
    file_remote='FusionModels/files/'+mid+'.f3d';meta_remote='FusionModels/meta/'+mid+'.json'
    file_etag=(remote or {}).get('file_etag')
    report(stage='Отправка файла',filename=item['name'],message='Отправка: '+item['name'])
    new_file_etag=put_file(state,file_remote,item['path'],file_etag)
    payload={'version':1,'id':mid,'name':item['name'],'folder':item['folder'],'hash':sha(item['path']),'file_etag':new_file_etag,'modified':int(item['record'].get('0xe8',0)),'record':encode_record(item['record'])}
    report(stage='Сохранение структуры и свойств на сервере',message='Сохранение данных: '+item['name'])
    put_bytes(state,meta_remote,json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode(),(remote or {}).get('meta_etag'))
    return payload

def remote_models(state):
    rows=CloudConnection.request(state,'/v1/sync-index').get('models',[])
    return {str(meta['id']).lower():meta for meta in rows if isinstance(meta,dict) and isinstance(meta.get('id'),str)}

def open_model(path):
    for doc in adsk.core.Application.get().documents:
        if doc.dataFile and identity(doc.dataFile.id)==identity(str(path)):return True
    return False

def safe_name(value):
    value=re.sub(r'[\\/:*?"<>|\x00-\x1f]','_',value).strip(' .')[:120]
    return value or 'Model'

def incoming_folder(data,meta=None):
    meta=meta or {}
    parts=meta.get('folder','Default Project').split(' / ')
    rows,start,end=folder_container(data);folders=[copy.deepcopy(d) for d,_,_ in rows]
    roots=[d for d in folders if not d.get('0x48') and d.get('0x68') not in SAMPLE_PROJECT_IDS and 'write' in d.get('0x2a8','').split(',')]
    root=next((d for d in roots if d.get('0x88')==parts[0]),None)
    if root is None:raise ValueError('Создайте локальный проект «'+parts[0]+'» и повторите синхронизацию')
    parent=root
    for name in parts[1:]:
        if not name or safe_name(name)!=name:raise ValueError('Недопустимое имя папки на сервере')
        child=next((d for d in folders if d['0x48']==parent['key'] and d['0x88']==name),None)
        if child is None:
            child=copy.deepcopy(parent)
            urn='urn:adsk.wipprod:fs.folder:co.'+base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip('=')
            child.update(key=urn,children=[],files=[],other=[])
            child.update({'0x28':urn,'0x48':parent['key'],'0x88':name,'0x1b0':urn,'0x150':0,'0x1a8':0,'0xa8':'','0xc8':'','0x188':'','0x1d0':'','0x2c8':''})
            child['0x168']=parent['0x168'].replace(parent['key'],urn)
            child['0x1f8']=parent['0x1f8'].rstrip('/')+'/'+name if parent['0x1f8'] else ''
            parent['children'].append(urn);folders.append(child)
        parent=child
    changed=data[:start-12]+u32(1)+u64(len(folders))+b''.join(write_folder(d) for d in folders)+data[end:]
    folder_container(changed)
    return changed,parent

def install_new(native,cache_path,data,root,mid,meta,download):
    original=cache_path.read_bytes()
    data,folder=incoming_folder(data,meta);files,fs,fe=file_container(data);folders,start,end=folder_container(data)
    name=safe_name(meta.get('name','Model'));target=root/'F'/('_'+name+'.'+mid+'.f3d')
    if target.exists():target=root/'F'/('_'+name+'-'+secrets.token_hex(2)+'.'+mid+'.f3d')
    record=decode_record(meta['record']);old=record.get('key','')
    record['key']=target.as_posix();record['0x28']=target.as_posix();record['0x1c8']=target.as_posix();record['0x48']=folder['key'];record['0x88']=name;record['0xe8']=int(time.time()*1000)
    record['0x68']='' # A local model's project is determined by its parent folder.
    folder_records=[copy.deepcopy(d) for d,_,_ in folders];dest=next(d for d in folder_records if d['key']==folder['key'])
    # A missing physical file can still have catalog records. Replace those
    # records instead of appending another reference when downloading again.
    replaced={identity(d['key']) for d,_,_ in files if ntpath.isabs(d['key']) and model_id(d['key'])==mid}
    for row in folder_records:
        for field in ('files','other'):row[field]=[key for key in row[field] if identity(key) not in replaced]
    dest['files'].append(target.as_posix());dest['other'].append(target.as_posix());dest['0xe8']=record['0xe8']
    file_records=[copy.deepcopy(d) for d,_,_ in files if identity(d['key']) not in replaced]+[record]
    changed=data[:start-12]+u32(1)+u64(len(folder_records))+b''.join(write_folder(d) for d in folder_records)+data[end:fs]+u32(1)+u64(len(file_records))+b''.join(write_file(d) for d in file_records)+data[fe:]
    folder_container(changed);file_container(changed)
    temp_cache=cache_path.with_suffix('.sync.tmp');temp_cache.write_bytes(changed)
    try:
        target.parent.mkdir(parents=True,exist_ok=True);os.replace(download,target);os.replace(temp_cache,cache_path)
        import LocalDocumentMetadata
        LocalDocumentMetadata.ensure(changed,root/'F')
        native.load(cache_path)
    except Exception:
        temp_cache.write_bytes(original);os.replace(temp_cache,cache_path);native.load(cache_path)
        target.unlink(missing_ok=True);pathlib.Path(str(target)+'._xx').unlink(missing_ok=True);raise
    return changed,target

def repair_import(cache,root,mid,meta):
    native=NativeCache();native.flush();before=cache.read_bytes()
    items=local_models(before,root);item=items.get(mid)
    if not item:return
    # Only repair records created by the old importer directly under W.login.
    if item['path'].parent.resolve()!=root.resolve():return
    if open_model(item['path']):raise ValueError('Закройте модель перед исправлением её записи')
    data,dest=incoming_folder(before,meta)
    folders,start,end=folder_container(data);files,fs,fe=file_container(data)
    target=root/'F'/item['path'].name;target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists() and sha(target)!=sha(item['path']):raise ValueError('Файл назначения уже существует и отличается')
    old=item['record']['key'];key=target.as_posix()
    folder_rows=[copy.deepcopy(d) for d,_,_ in folders]
    file_rows=[copy.deepcopy(d) for d,_,_ in files]
    for f in folder_rows:
        for field in ('files','other'):f[field]=[v for v in f[field] if identity(v)!=identity(old)]
        if f['key']==dest['key']:f['files'].append(key);f['other'].append(key)
    for record in file_rows:
        if identity(record['key'])==identity(old):
            for field in ('key','0x28','0x1c8'):record[field]=key
            record['0x48']=dest['key']
            if record.get('0x68'):record['0x68']=dest['0x68']
    changed=data[:start-12]+u32(1)+u64(len(folder_rows))+b''.join(write_folder(d) for d in folder_rows)+data[end:fs]+u32(1)+u64(len(file_rows))+b''.join(write_file(d) for d in file_rows)+data[fe:]
    folder_container(changed);file_container(changed)
    backup=cache.parent/'LocalFoldersBackups'/('sync-repair-'+time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3))
    backup.mkdir(parents=True);(backup/'cache.dat').write_bytes(before)
    shutil.copy2(item['path'],target)
    temp=cache.with_suffix('.sync.tmp');temp.write_bytes(changed)
    try:os.replace(temp,cache);native.load(cache)
    except Exception:
        temp.write_bytes(before);os.replace(temp,cache);native.load(cache);raise
    # Preserve the old physical copy as a recovery source.

def overwrite_existing(native,cache_path,item,download):
    if open_model(item['path']):raise ValueError('открыта в Fusion')
    backup=cache_path.parent/'LocalFoldersBackups'/(time.strftime('%Y%m%d-%H%M%S')+'-cloud-'+secrets.token_hex(3))
    backup.mkdir(parents=True);shutil.copy2(item['path'],backup/item['path'].name)
    os.replace(download,item['path'])

def snapshot():
    native=NativeCache();native.flush();cache=native.path()
    return cache,cache.read_bytes()

def commit_download(state,cache,root,mid,meta,temp,expected=None):
    if CloudConnection.PAUSED.exists():raise ValueError('Обмен остановлен: офлайн')
    current=CloudConnection.read_state()
    if not current or current.get('token')!=state.get('token'):raise ValueError('Сессия изменилась')
    native=NativeCache();native.flush();data=cache.read_bytes()
    models=local_models(data,root);item=models.get(mid)
    if item:
        if expected is None or sha(item['path'])!=expected:raise ValueError('Локальная модель изменилась; повторите синхронизацию')
        overwrite_existing(native,cache,item,temp)
    else:
        if expected is not None:raise ValueError('Модель перемещена или удалена во время обмена')
        install_new(native,cache,data,root,mid,meta,temp)

def execute(ui_call=lambda fn:fn(), progress=lambda **kw:None):
    CloudConnection.require_online()
    progress(stage='Подключение',filename='',message='Проверка подключения к серверу')
    state=CloudConnection.read_state()
    if not state:raise ValueError('Сначала подключитесь к серверу')
    try:CloudConnection.request(state,'/v1/session');ensure_remote_dirs(state)
    except urllib.error.HTTPError as exc:
        if exc.code==401:raise ValueError('Сессия завершена. Подключитесь к серверу заново')
        raise ValueError('Сервер отклонил синхронизацию: HTTP '+str(exc.code))
    except OSError:raise ValueError('Сервер недоступен. Проверьте адрес, HTTPS и доступ к серверу')
    cache,data=ui_call(snapshot);root=cache.parent/'W.login'
    root.mkdir(parents=True,exist_ok=True)
    progress(stage='Получение списка моделей',message='Получение списка моделей с сервера')
    try:remote=remote_models(state)
    except urllib.error.HTTPError as exc:raise ValueError('Сервер отклонил получение списка: HTTP '+str(exc.code))
    except (OSError,TimeoutError):raise ValueError('Сервер перестал отвечать во время синхронизации')
    import CloudDeletion
    deleted_ids,deletion_errors=CloudDeletion.process(state,remote,progress)
    remote={mid:meta for mid,meta in remote.items() if mid not in deleted_ids}
    server_deleted=CloudDeletion.remote_deleted(state)
    intents=CloudDeletion.pending(state)
    restore_errors=[]
    for mid,entry in intents.items():
        if entry['action']!='restore':continue
        try:CloudDeletion.restore_remote(state,mid)
        except Exception as exc:
            deleted_ids.add(mid);restore_errors.append(pathlib.Path(entry['path']).name+': '+str(exc))
    if any(entry['action']=='restore' for entry in intents.values()):
        remote={mid:meta for mid,meta in remote_models(state).items() if mid not in deleted_ids}
    for mid,meta in remote.items():
        progress(stage='Проверка структуры папок',filename=meta.get('name',mid),message='Проверка структуры: '+meta.get('name',mid))
        ui_call(lambda mid=mid,meta=meta:repair_import(cache,root,mid,meta))
    cache,data=ui_call(snapshot)
    local=local_models(data,root);sync=read_sync()
    account=state['address']+'|'+state['username']
    if sync.get('account')!=account:sync={'account':account,'models':{}}
    known=sync.setdefault('models',{})
    # Publish the complete plan before replacing any model, so every view can lock it.
    hashes={mid:sha(item['path']) for mid,item in local.items()}
    for mid,item in local.items():
        action=SyncGuard.classify(hashes[mid],remote.get(mid,{}).get('hash'),known.get(mid,{}).get('hash'))
        SyncGuard.mark(item['path'],action,item['name'],'Локальная и серверная версии различаются. Требуется разрешить конфликт.' if action=='conflict' else '')
    SyncGuard.gate(False)
    uploaded=downloaded=unchanged=0;conflicts=[];errors=list(deletion_errors)+restore_errors;completed=set()
    total=len(set(local)|set(remote));done=0
    for mid,item in list(local.items()):
        done+=1;progress(message='Проверка: '+item['name'],stage='Сравнение версий',filename=item['name'],current=done,total=total)
        try:
            local_hash=hashes[mid];other=remote.get(mid);last=known.get(mid,{}).get('hash')
            if mid in deleted_ids:continue
            if other is None and mid in server_deleted and intents.get(mid,{}).get('action')!='restore':
                raise ValueError('модель удалена на сервере; повторная отправка отменена. Для возвращения используйте восстановление из корзины')
            if other is None:
                progress(message='Отправка: '+item['name']);meta=upload(state,mid,item);known[mid]={'hash':meta['hash']};uploaded+=1;SyncGuard.mark(item['path'],'same');completed.add(mid);continue
            remote_hash=other.get('hash','')
            if intents.get(mid,{}).get('action')=='metadata':
                progress(stage='Обновление имени и папки',filename=item['name'],message='Сохранение свойств: '+item['name'])
                updated=dict(other);updated.update(name=item['name'],folder=item['folder'])
                updated.pop('meta_etag',None)
                other['meta_etag']=put_bytes(state,'FusionModels/meta/'+mid+'.json',json.dumps(updated,ensure_ascii=False).encode('utf-8'),other.get('meta_etag'))
                other.update(name=item['name'],folder=item['folder'])
                CloudDeletion.acknowledge(state,mid,intents[mid])
            if local_hash==remote_hash:known[mid]={'hash':local_hash};unchanged+=1;SyncGuard.mark(item['path'],'same');completed.add(mid);continue
            if last and local_hash==last:
                progress(message='Получение: '+item['name'],stage='Загрузка файла')
                temp=root/('.cloud-'+mid+'.part');CloudConnection.dav_download(state,'FusionModels/files/'+mid+'.f3d',temp)
                progress(stage='Проверка целостности',message='Проверка файла: '+item['name'])
                if sha(temp)!=remote_hash:raise ValueError('контрольная сумма сервера не совпала')
                progress(stage='Сохранение в локальный проект',message='Сохранение: '+item['name'])
                ui_call(lambda:commit_download(state,cache,root,mid,other,temp,local_hash));known[mid]={'hash':remote_hash};downloaded+=1;SyncGuard.mark(item['path'],'same')
            elif last and remote_hash==last:
                progress(message='Отправка: '+item['name']);meta=upload(state,mid,item,other);known[mid]={'hash':meta['hash']};uploaded+=1;SyncGuard.mark(item['path'],'same');completed.add(mid)
            else:conflicts.append(item['name'])
        except Exception as exc:
            errors.append(item['name']+': '+str(exc))
            if SyncGuard.reason(item['path']):SyncGuard.mark(item['path'],'error',item['name'],'Обновление не завершено: '+str(exc))
    for mid,other in remote.items():
        if mid in local:continue
        done+=1;progress(message='Получение: '+other.get('name',mid),stage='Загрузка файла',filename=other.get('name',mid),current=done,total=total)
        temp=root/('.cloud-'+mid+'.part')
        try:
            CloudConnection.dav_download(state,'FusionModels/files/'+mid+'.f3d',temp)
            progress(stage='Проверка целостности',message='Проверка файла: '+other.get('name',mid))
            if sha(temp)!=other.get('hash'):raise ValueError('контрольная сумма сервера не совпала')
            progress(stage='Сохранение в локальный проект',message='Сохранение: '+other.get('name',mid))
            ui_call(lambda:commit_download(state,cache,root,mid,other,temp));known[mid]={'hash':other['hash']};downloaded+=1
        except Exception as exc:temp.unlink(missing_ok=True);errors.append(other.get('name',mid)+': '+str(exc))
    for mid,entry in intents.items():
        if entry['action'] in ('restore','metadata') and mid in completed:
            CloudDeletion.acknowledge(state,mid,entry)
    save_sync(sync)
    ok=not conflicts and not errors
    message='Синхронизация завершена: отправлено {}, получено {}, без изменений {}'.format(uploaded,downloaded,unchanged)
    if conflicts:message+='; конфликты: '+', '.join(conflicts)
    if errors:message+='; ошибки: '+'; '.join(errors[:3])
    return {'ok':ok,'status':'connected','connected':True,'message':message,'uploaded':uploaded,'downloaded':downloaded,'unchanged':unchanged,'conflicts':conflicts,'errors':errors}
