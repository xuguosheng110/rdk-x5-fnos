#!/usr/bin/env python3
"""Join verified release parts; never opens or writes any physical device."""
from pathlib import Path
import hashlib,json,os
base=Path(__file__).resolve().parent
meta=json.loads((base/'parts.json').read_text())
target=base/meta['file']
if target.exists():
 h=hashlib.sha256()
 with target.open('rb') as f:
  for data in iter(lambda:f.read(8*1024*1024),b''):h.update(data)
 actual=h.hexdigest()
 if actual==meta['sha256']:print('Already verified:',target);raise SystemExit(0)
 raise SystemExit('Output exists with a different hash. Move it aside before retrying.')
created=False
tmp=target.with_suffix(target.suffix+'.assembling')
total=hashlib.sha256()
try:
 with tmp.open('xb') as out:
  created=True
  for part in meta['parts']:
   p=base/part['name'];h=hashlib.sha256();size=0
   with p.open('rb') as source:
    while True:
     data=source.read(8*1024*1024)
     if not data:break
     out.write(data);h.update(data);total.update(data);size+=len(data)
   if size!=part['size'] or h.hexdigest()!=part['sha256']:raise ValueError('Bad or incomplete part: '+p.name)
  if total.hexdigest()!=meta['sha256']:raise ValueError('Joined image hash mismatch')
  out.flush();os.fsync(out.fileno())
 os.replace(tmp,target)
except BaseException:
 if created:tmp.unlink(missing_ok=True)
 raise
print('SHA256 verified:',target)
print('Decompress XZ before flashing. This script does not flash any disk.')
