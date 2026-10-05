"""Deleted model catalogue and surgical restore from local deletion backups."""
import copy,hashlib,json,os,pathlib,shutil,time,secrets
from native_cache import NativeCache
from cache_codec import folder_container,write_folder,u32,u64
from file_codec import file_container,write_file
from model_operations import identity,is_model_destination

def digest(data):return hashlib.sha256(data).hexdigest()

def entries(path,data):
    root=(path.parent/'W.login').resolve();backups=(path.parent/'LocalFoldersBackups').resolve()
    active={identity(d['key']) for d,_,_ in file_container(data)[0]}
    found=[]
    for manifest in sorted(backups.glob('*/operation.json'),reverse=True):
        try:
            op=json.loads(manifest.read_text(encoding='utf-8'))
            if op.get('operation') not in ('delete','model-delete'):continue
            cache=manifest.parent/'cache.dat'
            old=cache.read_bytes();records={identity(d['key']):d for d,_,_ in file_container(old)[0]}
            folders={d['key']:d for d,_,_ in folder_container(old)[0]}
            for source,saved in op.get('files',[]):
                source=pathlib.Path(source).resolve();saved=pathlib.Path(saved).resolve()
                if not source.is_relative_to(root) or not saved.is_relative_to((manifest.parent/'files').resolve()):continue
                if not saved.is_relative_to(backups) or not saved.is_file():continue
                model=records.get(identity(str(source)))
                if not model or identity(model['key']) in active or source.exists():continue
                parts=[];parent=folders.get(model['0x48']);seen=set()
                while parent and parent['key'] not in seen:
                    seen.add(parent['key']);parts.append(parent['0x88']);parent=folders.get(parent['0x48'])
                token=digest((manifest.parent.name+'\0'+identity(model['key'])).encode())
                thumb=model.get('0xc8','')
                found.append({'id':token,'name':model['0x88'],'folder':' / '.join(reversed(parts)),
                    'thumbnail':thumb if thumb.startswith(('data:image/png;base64,','data:image/jpeg;base64,')) else '',
                    'extension':source.suffix.lstrip('.'),'deleted':manifest.parent.name[:15],
                    '_source':source,'_saved':saved,'_old':old,'_model':model})
        except (OSError,ValueError,KeyError,TypeError):continue
    return found

def revision(data,item):return digest(data+item['id'].encode()+item['_saved'].read_bytes())

def public(item,data):
    return {**{k:v for k,v in item.items() if not k.startswith('_')},'revision':revision(data,item)}

def restore_cache(data,old,model):
    fr,start,end=folder_container(data);rr,fs,fe=file_container(data)
    folders=[copy.deepcopy(d) for d,_,_ in fr];files=[copy.deepcopy(d) for d,_,_ in rr]
    byid={d['key']:d for d in folders};oldfolders={d['key']:d for d,_,_ in folder_container(old)[0]}
    model=copy.deepcopy(model)
    if any(identity(d['key'])==identity(model['key']) for d in files):raise ValueError('Модель уже существует')
    missing=[];parent=model['0x48'];seen=set()
    while parent not in byid:
        if parent in seen or parent not in oldfolders:raise ValueError('Не найдена исходная папка')
        seen.add(parent);folder=oldfolders[parent]
        if not folder['0x48']:raise ValueError('Исходный проект отсутствует. Восстановление невозможно')
        missing.append(folder);parent=folder['0x48']
    if not is_model_destination(byid[parent],byid):raise ValueError('Исходная папка недоступна для записи')
    for saved in reversed(missing):
        if any(d['0x48']==saved['0x48'] and d['0x88'].casefold()==saved['0x88'].casefold() for d in folders):
            raise ValueError('Уже существует другая папка с исходным именем')
        folder=copy.deepcopy(saved)
        folder['children']=[];folder['files']=[];folder['other']=[]
        parent=byid[folder['0x48']]
        folder['0x1f8']=parent['0x1f8'].rstrip('/')+'/'+folder['0x88']
        folder['0x68']=parent['0x68']
        parent['children'].append(folder['key']);folders.append(folder);byid[folder['key']]=folder
    dest=byid[model['0x48']]
    if any(d['0x48']==dest['key'] and d['0x88'].casefold()==model['0x88'].casefold() for d in files):
        raise ValueError('В исходной папке уже есть модель с таким именем')
    if model['0x68']:model['0x68']=dest['0x68']
    for folder in folders:
        for field in ('files','other'):folder[field]=[x for x in folder[field] if identity(x)!=identity(model['key'])]
    dest['files'].append(model['key'])
    oldparent=oldfolders.get(model['0x48'],{})
    if any(identity(x)==identity(model['key']) for x in oldparent.get('other',[])):dest['other'].append(model['key'])
    dest['0xe8']=int(time.time()*1000);files.append(model)
    result=data[:start-12]+u32(1)+u64(len(folders))+b''.join(write_folder(d) for d in folders)+data[end:fs]+u32(1)+u64(len(files))+b''.join(write_file(d) for d in files)+data[fe:]
    folder_container(result);file_container(result)
    return result

def execute(request):
    op=request.get('operation')
    if op not in ('trash-list','trash-preview','restore','purge'):raise ValueError('Неизвестная операция')
    native=NativeCache();native.flush();path=native.path();before=path.read_bytes();allitems=entries(path,before)
    if op=='trash-list':
        seen=set();items=[]
        for item in allitems:
            key=identity(item['_model']['key'])
            if key not in seen:seen.add(key);items.append(public(item,before))
        return {'ok':True,'items':items}
    item=next((i for i in allitems if i['id']==request.get('id')),None)
    if not item:raise ValueError('Модель уже восстановлена или удалена окончательно. Обновите список')
    if op=='trash-preview':return {'ok':True,**public(item,before)}
    if request.get('revision')!=revision(before,item):raise ValueError('Данные изменились. Обновите список и повторите операцию')
    source=item['_source'];saved=item['_saved']
    if op=='purge':
        if request.get('confirmed') is not True:raise ValueError('Требуется подтверждение окончательного удаления')
        copies={i['_saved'] for i in allitems if identity(i['_model']['key'])==identity(item['_model']['key'])}
        if path.read_bytes()!=before or source.exists():raise ValueError('Данные изменились. Повторите операцию')
        # Only validated payload files are removed, never whole backup directories.
        for candidate in copies:candidate.unlink()
        return {'ok':True,'name':item['name']}
    changed=restore_cache(before,item['_old'],item['_model'])
    undo=path.parent/'LocalFoldersBackups'/('restore-'+time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3))
    undo.mkdir();(undo/'cache.dat').write_bytes(before)
    source.parent.mkdir(parents=True,exist_ok=True);created=False;applied=False
    temp=path.with_suffix('.restore.tmp')
    try:
        with source.open('xb') as output:
            created=True
            with saved.open('rb') as inp:shutil.copyfileobj(inp,output)
        if digest(source.read_bytes())!=digest(saved.read_bytes()):raise ValueError('Не удалось проверить восстановленный файл')
        temp.write_bytes(changed)
        if path.read_bytes()!=before:raise ValueError('Хранилище изменилось. Повторите восстановление')
        os.replace(temp,path);applied=True;native.load(path)
        import CloudDeletion
        CloudDeletion.record(str(source),'restore')
    except Exception:
        if applied:
            temp.write_bytes(before);os.replace(temp,path);native.load(path)
        if created:source.unlink()
        raise
    finally:
        if temp.exists():temp.unlink()
    return {'ok':True,'name':item['name']}
