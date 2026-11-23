#!/usr/bin/env python3
"""Join verified release parts (system image; download the FPK separately); never opens or writes any physical device.
Usage: python3 merge-image.py [--download]"""
from pathlib import Path
import hashlib,os,sys,urllib.request,concurrent.futures
base=Path(__file__).resolve().parent
URL='https://github.com/xuguosheng110/rdk-x5-fnos/releases/download/v0.3.3/'
FILES=__import__('json').loads('[{"file": "fnos-x5-v0.3.3-pr4-rc1.img.xz", "size": 2478653552, "sha256": "ea02e5b8b158aaf1fdee4ec22bfcee5d9bb6412ecd68199d8b2bf9ce874a76d0", "parts": [{"name": "fnos-x5-v0.3.3-pr4-rc1.img.xz.part01", "size": 536870912, "sha256": "23510f22828fb309a040cedba13b31dc64b33d2953b9b41a86d759007945beea"}, {"name": "fnos-x5-v0.3.3-pr4-rc1.img.xz.part02", "size": 536870912, "sha256": "fa8447c340f4788a52d4eca42c44109c620fb2f772640433ee16b0447df362dc"}, {"name": "fnos-x5-v0.3.3-pr4-rc1.img.xz.part03", "size": 536870912, "sha256": "66b389f81cc57c03fd0306dfe8579e02ecc770114d5c89e9b0fdc3adfa08ce0d"}, {"name": "fnos-x5-v0.3.3-pr4-rc1.img.xz.part04", "size": 536870912, "sha256": "40adc2e2b6a382ce13018a97fb46fa7b8b7540684c7d6c1da12743c98205aa81"}, {"name": "fnos-x5-v0.3.3-pr4-rc1.img.xz.part05", "size": 331169904, "sha256": "b95f136bf3be7ad6e5348508ffbc4ad0fede4585300156f999326a72cb02f716"}]}]')
def sha(path):
 h=hashlib.sha256()
 with open(path,'rb') as f:
  for b in iter(lambda:f.read(8*1024*1024),b''):h.update(b)
 return h.hexdigest()
def fetch(part):
 target=base/part['name']
 if target.exists():
  if target.stat().st_size==part['size'] and sha(target)==part['sha256']:return
  raise ValueError('Existing part has wrong hash: '+target.name)
 tmp=target.with_suffix(target.suffix+'.download');created=False
 try:
  with tmp.open('xb') as out:
   created=True;h=hashlib.sha256();size=0
   with urllib.request.urlopen(URL+part['name'],timeout=120) as r:
    for d in iter(lambda:r.read(1024*1024),b''):out.write(d);h.update(d);size+=len(d)
  if size!=part['size'] or h.hexdigest()!=part['sha256']:raise ValueError('Download hash mismatch: '+part['name'])
  os.replace(tmp,target);print('Downloaded and verified:',target.name,flush=True)
 except BaseException:
  if created:tmp.unlink(missing_ok=True)
  raise
def join(meta):
 target=base/meta['file']
 if target.exists():
  if sha(target)==meta['sha256']:print('Already verified:',target);return
  raise SystemExit('Output exists with a different hash. Move it aside before retrying: '+str(target))
 missing=[p['name'] for p in meta['parts'] if not (base/p['name']).exists()]
 if missing:print('Skipping',meta['file'],'- missing parts:',', '.join(missing));return
 tmp=target.with_suffix(target.suffix+'.assembling');created=False;total=hashlib.sha256()
 try:
  with tmp.open('xb') as out:
   created=True
   for part in meta['parts']:
    h=hashlib.sha256();size=0
    with (base/part['name']).open('rb') as src:
     for d in iter(lambda:src.read(8*1024*1024),b''):out.write(d);h.update(d);total.update(d);size+=len(d)
    if size!=part['size'] or h.hexdigest()!=part['sha256']:raise ValueError('Bad or incomplete part: '+part['name'])
   if total.hexdigest()!=meta['sha256']:raise ValueError('Joined hash mismatch: '+meta['file'])
   out.flush();os.fsync(out.fileno())
  os.replace(tmp,target)
 except BaseException:
  if created:tmp.unlink(missing_ok=True)
  raise
 print('SHA256 verified:',target)
if '--download' in sys.argv:
 with concurrent.futures.ThreadPoolExecutor(4) as pool:list(pool.map(fetch,[p for f in FILES for p in f['parts']]))
for meta in FILES:join(meta)
print('Decompress the .img.xz before flashing. This script does not flash any disk.')
