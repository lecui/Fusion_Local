"""Local model operations. Rename/move preserve physical paths and model IDs."""
import copy,ntpath,time,unicodedata
from cache_codec import folder_container,write_folder,u32,u64
from file_codec import file_container,write_file

def identity(value):return ntpath.normcase(ntpath.normpath(value))
def inspect_model(data,model_id):
    fr,start,end=folder_container(data);rr,fs,fe=file_container(data)
    folders=[copy.deepcopy(d) for d,_,_ in fr];files=[copy.deepcopy(d) for d,_,_ in rr]
    matches=[d for d in files if identity(d['key'])==identity(model_id)]
    if len(matches)!=1:raise ValueError('Модель не найдена или её запись неоднозначна')
    model=matches[0]
    if not ntpath.isabs(model['key']) or not ntpath.splitdrive(model['key'])[0]:raise ValueError('Операция поддерживается для локальных моделей')
    parent=next((d for d in folders if d['key']==model['0x48']),None)
    if parent is None or 'write' not in parent.get('0x2a8','').split(','):raise ValueError('Нет права изменения модели в этой папке')
    if model.get('0x2d0') and 'write' not in model['0x2d0'].split(','):raise ValueError('Модель доступна только для чтения')
    return folders,files,model,parent,(start,end,fs,fe)

# Built-in sample catalog in this portable installation; shared by all sample roots.
SAMPLE_PROJECT_IDS = frozenset({'20240913804164576'})

def is_model_destination(folder, byid):
    seen=set()
    while folder:
        if folder['key'] in seen:return False
        seen.add(folder['key'])
        if folder.get('0x68') in SAMPLE_PROJECT_IDS or folder.get('0x88','').startswith('KEY$sam$'):return False
        if 'write' not in folder.get('0x2a8','').split(','):return False
        parent=folder.get('0x48','')
        if not parent:return True
        folder=byid.get(parent)
    return False

def folder_choices(folders):
    byid={d['key']:d for d in folders}
    def label(d):
        parts=[];seen=set()
        while d:
            if d['key'] in seen:raise ValueError('Цикл в структуре папок')
            seen.add(d['key']);parts.append(d['0x88']);d=byid.get(d['0x48'])
        return ' / '.join(reversed(parts))
    return sorted([{'id':d['key'],'label':label(d)} for d in folders if is_model_destination(d,byid)],key=lambda d:d['label'].casefold())

def transform_model(data,model_id,operation,value=None):
    folders,files,model,parent,(start,end,fs,fe)=inspect_model(data,model_id)
    key=model['key'];oldparent=parent['key'];now=int(time.time()*1000)
    if operation=='rename':
        if not isinstance(value,str):raise ValueError('Не задано имя')
        name=unicodedata.normalize('NFC',value.strip())
        if not name or len(name)>150 or name in ('.','..') or name.endswith('.') or any(ord(c)<32 or c in '/\\:*?"<>|' for c in name):raise ValueError('Недопустимое имя модели')
        dest=parent
    elif operation=='move':
        dest=next((d for d in folders if d['key']==value),None)
        if dest is None or not is_model_destination(dest,{d['key']:d for d in folders}):raise ValueError('Папка назначения недоступна для записи')
        name=model['0x88']
    elif operation=='delete':dest=None;name=model['0x88']
    else:raise ValueError('Неизвестная операция')
    if dest and any(d['key']!=key and d['0x48']==dest['key'] and d['0x88'].casefold()==name.casefold() for d in files):raise ValueError('Модель с таким именем уже есть в выбранной папке')
    if operation=='rename':model['0x88']=name;model['0xe8']=now
    else:
        membership={field:any(identity(x)==identity(key) for x in parent[field]) for field in ('files','other')}
        for folder in folders:
            for field in ('files','other'):folder[field]=[x for x in folder[field] if identity(x)!=identity(key)]
        if operation=='delete':files=[d for d in files if d['key']!=key]
        else:
            model['0x48']=dest['key'];model['0xe8']=now
            if model['0x68']:model['0x68']=dest['0x68']
            dest['files'].append(key)
            if membership['other']:dest['other'].append(key)
            dest['0xe8']=now
    parent['0xe8']=now
    changed=data[:start-12]+u32(1)+u64(len(folders))+b''.join(write_folder(d) for d in folders)+data[end:fs]+u32(1)+u64(len(files))+b''.join(write_file(d) for d in files)+data[fe:]
    folder_container(changed);file_container(changed)
    return changed,model
