#!/usr/bin/env python3
"""Join verified release parts; never opens or writes any physical device."""
from pathlib import Path
import hashlib,json,os
base=Path(__file__).resolve().parent
meta={'file': 'rdk-x5-fnos-v0.3.1.img.xz', 'sha256': '59e6a7f8af809d2d8a2e692b384335843aa5227e0925b3ef3857adb853c15791', 'parts': [{'name': 'rdk-x5-fnos-v0.3.1.img.xz.part01', 'size': 1073741824, 'sha256': '084b79d724cb9c3628d071b5c985fdd36f3922d6600393388f7868c8c46877c7'}, {'name': 'rdk-x5-fnos-v0.3.1.img.xz.part02', 'size': 1073741824, 'sha256': 'd785872b776b6305f5452988cf8cf56a5838ae69f6a31d69caa1c823bdbb0a53'}, {'name': 'rdk-x5-fnos-v0.3.1.img.xz.part03', 'size': 465366380, 'sha256': '5859ff2e1fa8310511b3ce022e4ada6e92efc937c0d8d72d1b09c275d03d6984'}]}
if '--download' in __import__('sys').argv:
 import urllib.request, concurrent.futures
 url='https://github.com/xuguosheng110/rdk-x5-fnos/releases/download/v0.3.1/'
 def fetch(part):
  target=base/part['name']
  if target.exists():
   with target.open('rb') as f:
    h=hashlib.sha256()
    for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
   if target.stat().st_size==part['size'] and h.hexdigest()==part['sha256']:return
   raise ValueError('Existing part has wrong hash: '+target.name)
  tmp=target.with_suffix(target.suffix+'.download');created=False
  try:
   with tmp.open('xb') as out:
    created=True;h=hashlib.sha256();size=0
    with urllib.request.urlopen(url+part['name'],timeout=120) as response:
     for data in iter(lambda:response.read(1024*1024),b''):out.write(data);h.update(data);size+=len(data)
   if size!=part['size'] or h.hexdigest()!=part['sha256']:raise ValueError('Download hash mismatch: '+part['name'])
   os.replace(tmp,target);print('Downloaded and verified:',target.name,flush=True)
  except BaseException:
   if created:tmp.unlink(missing_ok=True)
   raise
 with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:list(pool.map(fetch,meta['parts']))

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
