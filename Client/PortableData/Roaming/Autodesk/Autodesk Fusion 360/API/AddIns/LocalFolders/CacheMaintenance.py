"""Lossless deduplication of the two append-only collections in cache v14."""
import re
from cache_codec import Reader,u32,u64,string

def sections(data):
 r=Reader(data)
 if r.n('<I')!=14:raise ValueError('Unsupported cache version')
 for _ in range(3):r.s()
 count=r.n('<Q')
 if count>100000:raise ValueError('Invalid thumbnail count')
 for _ in range(count):
  r.s();r.s();r.raw(r.n('<Q'))
 start=r.pos
 if r.n('<B')!=1 or r.n('<I')!=1:raise ValueError('Unsupported hub collection')
 count=r.n('<Q');hub_start=r.pos
 if not 1<=count<=10000000:raise ValueError('Invalid hub count')
 # This portable profile serializes one local hub repeatedly. Locate the next
 # typed collection, then verify EVERY hub byte before removing any duplicate.
 pattern=rb'\x01\x01\x00\x00\x00.{8}\x34\x00\x00\x00\x00\x00\x00\x00urn:adsk\.wipprod:fs\.folder:co\.'
 for match in re.finditer(pattern,data[hub_start:],re.S):
  end=hub_start+match.start();size,remainder=divmod(end-hub_start,count)
  if remainder or not 100<=size<=100000:continue
  hub=data[hub_start:hub_start+size]
  if not hub.startswith(u32(20)):continue
  if data[hub_start:end]!=hub*count:continue
  q=Reader(data,end+5);n=q.n('<Q')
  if not 1<=n<=10000000:continue
  values=[q.s() for _ in range(n)]
  if any(not v.startswith('urn:adsk.wipprod:fs.folder:co.') for v in values):continue
  return start,q.pos,hub,count,values
 raise ValueError('Unrecognized or nonidentical hub records; cache left unchanged')

def compact(data):
 start,end,hub,count,values=sections(data)
 unique=list(dict.fromkeys(values))
 if count==1 and len(unique)==len(values):return data
 return data[:start]+b'\x01'+u32(1)+u64(1)+hub+b'\x01'+u32(1)+u64(len(unique))+b''.join(string(v) for v in unique)+data[end:]

def reload_view(data):
 start,end,_,_,_=sections(data)
 # Native v14 readers append these collections instead of replacing them.
 # A false presence flag skips loading and preserves the current native list.
 return data[:start]+b'\x00\x00'+data[end:]
