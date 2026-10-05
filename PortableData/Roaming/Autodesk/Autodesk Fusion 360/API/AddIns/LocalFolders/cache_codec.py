import struct, re
def u32(n): return struct.pack('<I',n)
def u64(n): return struct.pack('<Q',n)
def string(s):
    b=s.encode('utf-8') if isinstance(s,str) else s
    return u64(len(b))+b
class Reader:
    def __init__(self,data,pos=0):self.data=data;self.pos=pos
    def raw(self,n):
        if n<0 or self.pos+n>len(self.data):raise ValueError('Truncated cache')
        b=self.data[self.pos:self.pos+n];self.pos+=n;return b
    def n(self,fmt):return struct.unpack(fmt,self.raw(struct.calcsize(fmt)))[0]
    def s(self):
        n=self.n('<Q')
        if n>10000000:raise ValueError('Bad string size')
        return self.raw(n).decode('utf-8')
    def coll(self):
        v=self.n('<I');n=self.n('<Q')
        if v!=1 or n>100000:raise ValueError('Bad collection')
        return [self.s() for _ in range(n)]
FIELDS=[('version','I')]+[(hex(i),'s') for i in (0x28,0x48,0x68,0x88,0xa8,0xc8)]+[('0xe8','Q')]+[(hex(i),'s') for i in (0xf0,0x110,0x130)]+[('0x150','B'),('0x158','Q'),('0x160','I'),('0x168','s'),('0x188','s'),('0x1a8','I'),('0x1b0','s'),('0x1d0','s'),('0x1f0','Q'),('0x1f8','s'),('flags','raw3'),('children','coll'),('files','coll'),('other','coll'),('0x298','I'),('0x29c','B')]
def read_folder(r):
    start=r.pos;d={'key':r.s()}
    for name,typ in FIELDS:
        if typ=='s':d[name]=r.s()
        elif typ=='coll':d[name]=r.coll()
        elif typ=='raw3':d[name]=r.raw(3).hex()
        else:d[name]=r.n('<'+typ)
        if name=='version' and d[name]!=5:raise ValueError('Not v5')
    if d['0x29c']:
        d['0x2a0']=r.n('<I');d['0x2a8']=r.s()
    d['0x2c8']=r.s()
    return d,start,r.pos
def write_folder(d):
    b=string(d['key'])
    for name,typ in FIELDS:
        val=d[name]
        if typ=='s':b+=string(val)
        elif typ=='coll':b+=u32(1)+u64(len(val))+b''.join(string(x) for x in val)
        elif typ=='raw3':b+=bytes.fromhex(val)
        else:b+=struct.pack('<'+typ,val)
    if d['0x29c']:b+=u32(d['0x2a0'])+string(d['0x2a8'])
    return b+string(d['0x2c8'])
def find_folders(data):
    found=[]
    for m in re.finditer(rb'urn:adsk\.wipprod:fs\.folder:co\.',data):
        pos=m.start()-8
        if pos<0:continue
        try:
            r=Reader(data,pos);d,start,end=read_folder(r)
            assert write_folder(d)==data[start:end]
            found.append((d,start,end))
        except (ValueError,UnicodeDecodeError,struct.error,AssertionError):pass
    return found
def folder_container(data):
    found=find_folders(data)
    if not found:raise ValueError('Folder container not found')
    start=found[0][1];end=found[-1][2]
    if start<12 or data[start-12:start]!=u32(1)+u64(len(found)):
        raise ValueError('Unsupported folder container header')
    if any(a[2]!=b[1] for a,b in zip(found,found[1:])):
        raise ValueError('Noncontiguous folder records')
    if b''.join(write_folder(d) for d,_,_ in found)!=data[start:end]:
        raise ValueError('Cache round-trip failed')
    if len({d['key'] for d,_,_ in found})!=len(found):raise ValueError('Duplicate folder IDs')
    return found,start,end
def add_folder(data,parent_id,name):
    import copy,time,uuid,base64,unicodedata
    name=unicodedata.normalize('NFC',name.strip())
    if not name or len(name)>150 or name in ('.','..') or name[-1:] in ('.',' ') or any(ord(c)<32 or c in '/\\:*?"<>|' for c in name):
        raise ValueError('Недопустимое имя папки')
    found,start,end=folder_container(data)
    records=[copy.deepcopy(d) for d,_,_ in found]
    parent=next((d for d in records if d['key']==parent_id),None)
    if not parent:raise ValueError('Родительская папка не найдена')
    actions=parent.get('0x2a8','').split(',')
    if 'write' not in actions:raise ValueError('Нет права записи в эту папку')
    if any(d['0x48']==parent_id and d['0x88'].casefold()==name.casefold() for d in records):
        raise ValueError('Папка с таким именем уже существует')
    now=int(time.time()*1000)
    parent_path=parent['0x1f8'].rstrip('/')
    if not parent_path:
        paths={d['0x1f8'].split('/group-'+parent['0x68'])[0]+'/group-'+parent['0x68'] for d in records if '/group-'+parent['0x68'] in d['0x1f8']}
        if len(paths)!=1:raise ValueError('Не удалось определить локальный путь проекта')
        parent_path=paths.pop()
    urn='urn:adsk.wipprod:fs.folder:co.'+base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip('=')
    new=copy.deepcopy(parent)
    new.update({'key':urn,'0x28':urn,'0x48':parent_id,'0x88':name,'0xa8':'','0xc8':'','0xe8':now,'0x158':now,'0x1f0':now,'0x150':0,'0x1a8':0,'0x188':'','0x1b0':urn,'0x1d0':'','0x1f8':parent_path+'/'+name,'children':[],'files':[],'other':[],'0x2c8':''})
    new['0x168']=parent['0x168'].replace(parent_id,urn)
    parent['children'].append(urn)
    parent['0xe8']=now
    records.append(new)
    changed=data[:start-12]+u32(1)+u64(len(records))+b''.join(write_folder(d) for d in records)+data[end:]
    reread,_,_=folder_container(changed)
    assert len(reread)==len(found)+1
    return changed,new
if __name__=='__main__':
    import pathlib,json
    p=pathlib.Path(r'C:\AutodeskFusionPortable_2703.1.20_x64_300726\FusionPortable\PortableData\Local\Autodesk\Autodesk Fusion 360\LOCALWORKSPACE01\NsCloudBrowserCache_local.dat')
    data=p.read_bytes();found=find_folders(data)
    pathlib.Path('work/folders.json').write_text(json.dumps(found,ensure_ascii=False,indent=2),encoding='utf-8')
    print('folders',len(found))
    for d,start,end in found: print(start,end,d['key'],d['0x48'],d['0x68'],d['children'])
