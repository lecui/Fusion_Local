"""Pure cache transformations; no filesystem changes."""
import copy,time,unicodedata
from cache_codec import folder_container,write_folder,u32,u64
from file_codec import file_container,write_file

def inspect(data,folder_id):
    rows,start,end=folder_container(data); folders=[copy.deepcopy(d) for d,_,_ in rows]
    byid={d['key']:d for d in folders}
    target=byid.get(folder_id)
    if not target or target['0x48'] not in byid:raise ValueError('Корневую папку проекта изменять нельзя')
    if 'write' not in target.get('0x2a8','').split(','):raise ValueError('Нет права изменения папки')
    descendants={folder_id}
    while True:
        expanded=descendants|{d['key'] for d in folders if d['0x48'] in descendants}
        if expanded==descendants:break
        descendants=expanded
    if target['0x48'] in descendants:raise ValueError('Обнаружен цикл в структуре папок')
    files,fs,fe=file_container(data)
    files=[copy.deepcopy(d) for d,_,_ in files]
    selected=[d for d in files if d['0x48'] in descendants]
    return folders,files,target,descendants,selected,(start,end,fs,fe)

def transform(data,folder_id,operation,name=None):
    folders,files,target,ids,selected,(start,end,fs,fe)=inspect(data,folder_id)
    now=int(time.time()*1000)
    if operation=='rename':
        name=unicodedata.normalize('NFC',name.strip())
        if not name or len(name)>150 or name in ('.','..') or name.endswith('.') or any(ord(c)<32 or c in '/\\:*?"<>|' for c in name):raise ValueError('Недопустимое имя папки')
        if any(d['key']!=folder_id and d['0x48']==target['0x48'] and d['0x88'].casefold()==name.casefold() for d in folders):raise ValueError('Папка с таким именем уже существует')
        oldpath=target['0x1f8'].rstrip('/')
        if not oldpath:raise ValueError('Не определён путь папки')
        newpath=oldpath.rsplit('/',1)[0]+'/'+name
        for d in folders:
            if d['key'] in ids:
                path=d['0x1f8']
                if path==oldpath or path.startswith(oldpath+'/'):d['0x1f8']=newpath+path[len(oldpath):]
                d['0xe8']=now
        target['0x88']=name
    elif operation=='delete':
        removed={d['key'] for d in selected}
        folders=[d for d in folders if d['key'] not in ids]
        files=[d for d in files if d['key'] not in removed]
        for d in folders:
            for field in ('children','files','other'):
                d[field]=[x for x in d[field] if x not in ids and x not in removed]
            if d['key']==target['0x48']:d['0xe8']=now
    else:raise ValueError('Неизвестная операция')
    changed=data[:start-12]+u32(1)+u64(len(folders))+b''.join(write_folder(d) for d in folders)+data[end:fs]+u32(1)+u64(len(files))+b''.join(write_file(d) for d in files)+data[fe:]
    folder_container(changed);file_container(changed)
    return changed,{'name':target['0x88'],'folders':len(ids)-1,'files':len(selected),'ids':sorted(ids),'removedFiles':selected if operation=='delete' else []}
