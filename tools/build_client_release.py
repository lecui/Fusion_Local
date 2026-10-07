"""Build and verify the distributable Client tree without user data."""
from pathlib import Path
import hashlib,io,json,zipfile

ROOT=Path(__file__).resolve().parents[1]
CLIENT=ROOT/'Client'
VERSION='2026.10.07-preview.1'

def digest(data):return hashlib.sha256(data).hexdigest()

def build():
 payload={}
 for path in sorted(CLIENT.rglob('*')):
  if not path.is_file() or '__pycache__' in path.parts:continue
  rel=path.relative_to(CLIENT).as_posix()
  if rel=='FULL-PATCH-MANIFEST.json':continue
  if path.suffix.lower() in ('.f3d','.f3z','.bin','.log','.crt','.key','.sqlite3','.pyc') or 'cache_local.dat' in rel.lower():
   raise ValueError('Unexpected private or generated file: '+rel)
  payload[rel]=path.read_bytes()
 add='PortableData/Roaming/Autodesk/Autodesk Fusion 360/API/AddIns/'
 home=json.loads(payload[add+'HomeModels/config.json'])
 local=json.loads(payload[add+'LocalFolders/config.json'])
 js=payload['bin/FusionCDEHostApps/fusion-home-tab/bundle.4cef078ae5b7b7298063.js']
 with zipfile.ZipFile(io.BytesIO(payload['bin/OfflineJS/OfflineJS.zip'])) as nested:
  assert nested.testzip() is None
  dashboard=nested.read('dashboard_rel.js')
 assert home['token'].encode() in js and home['token'].encode() in dashboard
 for rel,blob in payload.items():
  if rel.endswith('.py'):compile(blob,rel,'exec')
 manifest={'version':VERSION,'target':'Fusion Portable 2703.1.20 x64 300726','files':[{'path':p,'bytes':len(b),'sha256':digest(b)} for p,b in sorted(payload.items())]}
 data=json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8')
 (CLIENT/'FULL-PATCH-MANIFEST.json').write_bytes(data)
 payload['FULL-PATCH-MANIFEST.json']=data
 output=ROOT/'dist';output.mkdir(exist_ok=True)
 archive=output/f'FusionLocal-Client-{VERSION}.zip'
 with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as dest:
  for rel,blob in sorted(payload.items()):dest.writestr(rel,blob)
 with zipfile.ZipFile(archive) as check:
  assert check.testzip() is None
  assert set(check.namelist())==set(payload)
  for entry in manifest['files']:assert digest(check.read(entry['path']))==entry['sha256']
  assert all(not Path(p).is_absolute() and '..' not in Path(p).parts for p in check.namelist())
 archive.with_suffix('.zip.sha256').write_text(digest(archive.read_bytes())+'  '+archive.name+'\n',encoding='ascii')
 (output/'RELEASE_NOTES.md').write_bytes((ROOT/'RELEASE_NOTES.md').read_bytes())
 print(json.dumps({'archive':str(archive),'files':len(payload),'bytes':archive.stat().st_size,'verified':True}))

if __name__=='__main__':build()
