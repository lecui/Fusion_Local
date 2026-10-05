from cache_codec import *
FILE_FIELDS=FIELDS[:FIELDS.index(('0x168','s'))]+[(hex(i),'s') for i in (0x168,0x188,0x1a8,0x1c8,0x1e8,0x208)]+[('raw45',45),('0x248','s'),('0x268','s'),('raw17',17),('0x2a8','s')]
def read_file(r):
    start=r.pos; d={'key':r.s()}
    for key,typ in FILE_FIELDS:
        if typ=='s':d[key]=r.s()
        elif isinstance(typ,int):d[key]=r.raw(typ)
        else:d[key]=r.n('<'+typ)
        if key=='version' and d[key]!=5:raise ValueError('Not file v5')
    if d['raw17'][0]:
        d['0x2c8']=r.n('<I');d['0x2d0']=r.s()
    return d,start,r.pos

def write_file(d):
    b=string(d['key'])
    for key,typ in FILE_FIELDS:
        if typ=='s':b+=string(d[key])
        elif isinstance(typ,int):b+=d[key]
        else:b+=struct.pack('<'+typ,d[key])
    if '0x2c8' in d:b+=u32(d['0x2c8'])+string(d['0x2d0'])
    return b

def file_container(data):
    _,_,end=folder_container(data)
    r=Reader(data,end)
    version=r.n('<I');n=r.n('<Q')
    if version!=1 or n>100000:raise ValueError('Unsupported file container')
    rows=[read_file(r) for _ in range(n)]
    assert data[end:r.pos]==u32(1)+u64(n)+b''.join(write_file(d) for d,_,_ in rows)
    return rows,end,r.pos
if __name__=='__main__':
    import pathlib,json
    p=pathlib.Path(r'C:\AutodeskFusionPortable_2703.1.20_x64_300726\FusionPortable\PortableData\Local\Autodesk\Autodesk Fusion 360\LOCALWORKSPACE01\NsCloudBrowserCache_local.dat')
    data=p.read_bytes();rows,start,end=file_container(data)
    print(len(rows),start,end,len(data))
    pathlib.Path('work/files_parsed.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2,default=lambda x:x.hex()),encoding='utf-8')
    for d,_,_ in rows[-3:]:print({k:v for k,v in d.items() if isinstance(v,str)})
