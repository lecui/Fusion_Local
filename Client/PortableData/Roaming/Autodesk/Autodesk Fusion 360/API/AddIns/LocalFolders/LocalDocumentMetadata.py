"""Native local document metadata used for editable cached Fusion documents."""
import os,pathlib,uuid,xml.etree.ElementTree as ET
from cache_codec import folder_container
from file_codec import file_container

def metadata(record,folders):
 byid={d['key']:d for d in folders};parent=byid.get(record['0x48']);project=parent;seen=set()
 while project and project.get('0x48'):
  if project['key'] in seen:raise ValueError('Cyclic project tree')
  seen.add(project['key']);project=byid.get(project['0x48'])
 if not parent or not project:raise ValueError('Local document project is missing')
 root=ET.Element('MetaData',Version='1.6')
 meta=ET.SubElement(root,'WIPFileMetaData',AssetType='FusionAssetType',DocType='FusionDocType',FileVersion='0',LocalVersion='1',QontextPermaLinkId='',RequeueCount='0')
 identity=str(uuid.uuid5(uuid.NAMESPACE_URL,record['key']))
 values={'Description':'','DisplayName':'','Name':record['0x88'],'FileName':record['0x88']+'.f3d','DesignFilePath':record['key'],
 'hubUrl':'https://local.autodesk360.com','hubId':'local','isBusinessHub':'1','ForImport':'0','FreezeVersion':'0','OfflineNew':'0',
 'ProjectName':project['0x88'],'CreationState':'0','LineageUri':'','VersionUri':'','isSaveAsLatest':'0','LinksLoaded':'1','LinkedFilesDownloaded':'1',
 'parentFolderUrn':parent['key'],'operationType':'fileCreateDocument','ProjectId':project['0x68'],'ProjectDmId':'a360prod:controlled:12'+project['0x68'],
 'ProjectFolderUrn':project['key'],'oxygenId':'','UserDisplayName':'','documentUniqueIdAcrossVersions':identity,'documentIdentifier':identity,
 'ECM':'0','newDesignFromLocalWithCloud':'0','NonFusionCADFile':'0','FileMimeType':'','DerivedF3d':'','copiedDesignPendingUpload':'false',
 'checkConflictForConfiguration':'true','lineageId':identity,'RegionId':'us'}
 for key,value in values.items():ET.SubElement(meta,key).text=value
 for key in ('lineageName','lineageCreatedTime','lineageLastModifiedTime','lineageCreatedUserId','lineageLastModifiedUserId','versionCreatedTime','Notes','QontextData','uploadRetry','storageUri','sourceLineageUrn','sourceTimestamp','newRootPartNumber','SimulationResultFiles','contextFlag','anycadKey','moreOptions','relatedFileVersionUrns'):
  ET.SubElement(meta,key)
 props=ET.SubElement(meta,'Properties');ET.SubElement(props,'DesignModelRevisionID').text=str(uuid.uuid4());ET.SubElement(props,'EIPContext').text='{}'
 custom=ET.SubElement(meta,'CustomProperties');ET.SubElement(custom,'DesignModelRevisionID').text=str(uuid.uuid4());ET.SubElement(custom,'EIPContext').text='{}'
 ET.SubElement(meta,'CustomLineageProperties');ET.SubElement(meta,'FileSize',Bytes='0')
 ET.indent(root)
 return ET.tostring(root,encoding='utf-16',xml_declaration=True)

def ensure(data,root):
 root=pathlib.Path(root).resolve();folders=[d for d,_,_ in folder_container(data)[0]];created=[]
 for record,_,_ in file_container(data)[0]:
  path=pathlib.Path(record['key'])
  if not path.is_absolute() or not path.resolve().is_relative_to(root) or not path.is_file() or path.suffix.lower()!='.f3d':continue
  sidecar=pathlib.Path(str(path)+'._xx')
  if sidecar.exists():continue
  payload=metadata(record,folders)
  # Never replace metadata written by Fusion itself.
  temporary=sidecar.with_name(sidecar.name+'.'+uuid.uuid4().hex+'.tmp')
  try:
   with temporary.open('xb') as stream:stream.write(payload)
   # On Windows rename refuses to replace an existing destination.
   try:temporary.rename(sidecar)
   except FileExistsError:continue
  finally:temporary.unlink(missing_ok=True)
  created.append(path.as_posix())
 return created
