"""Main-thread bridge from Fusion's dashboard to its local cache."""
import json, pathlib, time, os, secrets, traceback, base64
from native_cache import NativeCache
from cache_codec import add_folder, folder_container
HERE=pathlib.Path(__file__).resolve().parent

def create(name,parent_id,project_id):
    native=NativeCache()
    native.flush()
    path=native.path()
    if path.name!='NsCloudBrowserCache_local.dat':raise ValueError('Поддерживается только локальное хранилище этой сборки')
    before=path.read_bytes()
    records,_,_=folder_container(before)
    parent=next((d for d,_,_ in records if d['key']==parent_id),None)
    if parent is None:raise ValueError('Родительская папка не найдена')
    if project_id != parent['0x68']:
        try:project=base64.b64decode(project_id[2:]+'===').decode()
        except Exception:raise ValueError('Некорректный проект: '+project_id)
        if not project.endswith('#'+parent['0x68']):raise ValueError('Папка принадлежит другому проекту')
    changed,folder=add_folder(before,parent_id,name)
    backup=path.parent/'LocalFoldersBackups'
    backup.mkdir(exist_ok=True)
    (backup/(time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3)+'.dat')).write_bytes(before)
    temp=path.with_suffix('.localfolders.tmp')
    temp.write_bytes(changed)
    if path.read_bytes()!=before:
        temp.unlink();raise RuntimeError('Данные изменились во время операции. Повторите создание папки.')
    os.replace(temp,path)
    try:native.load(path)
    except Exception:
        temp.write_bytes(before);os.replace(temp,path);native.load(path);raise
    return {'id':folder['key'],'name':folder['0x88'],'isFolder':True,'parentFolderId':parent_id,'projectId':project_id,'lastModified':folder['0xe8'],'mimeType':'folder','path':folder['0x1f8']}

def create_encoded(encoded):
    try:
        if len(encoded)>8192:raise ValueError('Слишком длинный запрос')
        request=json.loads(base64.b64decode(encoded,validate=True))
        if not all(isinstance(request.get(k),str) for k in ('name','parentFolderID','projectID')):raise ValueError('Некорректный запрос')
        result={'ok':True,'folder':create(request['name'],request['parentFolderID'],request['projectID'])}
    except Exception as exc:
        result={'ok':False,'error':str(exc)}
        with (HERE/'errors.log').open('a',encoding='utf-8') as f:f.write(traceback.format_exc()+'\n')
    return 'LOCAL_FOLDER_RESULT:'+json.dumps(result,ensure_ascii=True,separators=(',',':'))

def manage_encoded(encoded):
    """Run on Fusion's UI thread; obtain native dialog confirmation before mutation."""
    import adsk.core,hashlib,shutil
    from folder_operations import inspect,transform
    try:
        request=json.loads(base64.b64decode(encoded,validate=True))
        folder_id=request['id'];operation=request['operation']
        if operation not in ('rename','delete') or not isinstance(folder_id,str):raise ValueError('Некорректная операция')
        native=NativeCache();native.flush();path=native.path()
        if path.name!='NsCloudBrowserCache_local.dat':raise ValueError('Только локальное хранилище')
        before=path.read_bytes()
        folders,files,target,ids,selected,_=inspect(before,folder_id)
        app=adsk.core.Application.get();ui=app.userInterface
        for doc in app.documents:
            if doc.dataFile and doc.dataFile.parentFolder.id in ids:
                raise ValueError('Сначала закройте документы из этой папки и её вложенных папок')
        name=None
        if operation=='rename':
            name,cancelled=ui.inputBox('Новое имя папки:', 'Переименовать папку',target['0x88'])
            if cancelled:return 'LOCAL_FOLDER_RESULT:'+json.dumps({'ok':True,'cancelled':True})
        else:
            message='Удалить папку «'+target['0x88']+'» со всем содержимым?\n\nВложенных папок: '+str(len(ids)-1)+'\nМоделей: '+str(len(selected))+'\n\nПеред удалением будет сохранена резервная копия.'
            choice=ui.messageBox(message,'Удалить папку',adsk.core.MessageBoxButtonTypes.YesNoButtonType,adsk.core.MessageBoxIconTypes.WarningIconType)
            if choice!=adsk.core.DialogResults.DialogYes:return 'LOCAL_FOLDER_RESULT:'+json.dumps({'ok':True,'cancelled':True})
        native.flush()
        if path.read_bytes()!=before:raise ValueError('Содержимое изменилось. Повторите операцию.')
        changed,report=transform(before,folder_id,operation,name)
        backup=path.parent/'LocalFoldersBackups'/(time.strftime('%Y%m%d-%H%M%S')+'-'+operation+'-'+secrets.token_hex(3))
        backup.mkdir(parents=True)
        (backup/'cache.dat').write_bytes(before)
        moves=[]
        if operation=='delete':
            root=(path.parent/'W.login').resolve()
            if not root.is_relative_to(path.parent.resolve()):raise ValueError('Локальное хранилище перенаправлено за пределы профиля')
            other={str(pathlib.Path(d[k]).resolve()).casefold() for d in files if d['0x48'] not in ids for k in ('key','0x28','0x1c8') if len(d.get(k,''))>3 and d[k][1:3] in (':/',':\\')}
            candidates=set()
            for d in selected:
                for key in ('key','0x28','0x1c8'):
                    value=d.get(key,'')
                    if len(value)>3 and value[1:3] in (':/',':\\'):
                        candidate=pathlib.Path(value)
                        resolved=candidate.resolve()
                        if str(resolved).casefold() in other:continue
                        if not resolved.is_relative_to(root) or candidate.is_symlink():raise ValueError('Файл находится вне локального хранилища')
                        if resolved.is_file():candidates.add(resolved)
            for source in sorted(candidates):
                dest=backup/'files'/source.relative_to(root)
                dest.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(source,dest)
                if hashlib.sha256(source.read_bytes()).digest()!=hashlib.sha256(dest.read_bytes()).digest():raise ValueError('Ошибка проверки резервной копии')
                moves.append((source,dest))
        (backup/'operation.json').write_text(json.dumps({'operation':operation,'id':folder_id,'name':report['name'],'files':[(str(a),str(b)) for a,b in moves]},ensure_ascii=False),encoding='utf-8')
        temp=path.with_suffix('.localfolders.tmp');temp.write_bytes(changed)
        if path.read_bytes()!=before:raise ValueError('Хранилище изменилось во время операции')
        removed=[]
        try:
            os.replace(temp,path);native.load(path)
            for source,dest in moves:
                if hashlib.sha256(source.read_bytes()).digest()!=hashlib.sha256(dest.read_bytes()).digest():raise ValueError('Файл изменился во время удаления')
                source.unlink();removed.append((source,dest))
            if operation=='delete':
                import CloudDeletion
                CloudDeletion.record_many([(str(source),'delete',hashlib.sha256(dest.read_bytes()).hexdigest()) for source,dest in moves])
            elif operation=='rename':
                import CloudDeletion
                CloudDeletion.record_many([(d['key'],'metadata',None) for d in selected])
        except Exception:
            for source,dest in removed:shutil.copy2(dest,source)
            temp.write_bytes(before);os.replace(temp,path);native.load(path)
            raise
        result={'ok':True,'operation':operation,'id':folder_id,'name':report['name']}
    except Exception as exc:
        result={'ok':False,'error':str(exc)}
        with (HERE/'errors.log').open('a',encoding='utf-8') as f:f.write(traceback.format_exc()+'\n')
    return 'LOCAL_FOLDER_RESULT:'+json.dumps(result,ensure_ascii=True,separators=(',',':'))
