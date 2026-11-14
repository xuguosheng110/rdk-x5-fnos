#!/usr/bin/env python3
"""Join verified release parts (system image and acceleration FPK); never opens or writes any physical device.
Usage: python3 merge-image.py [--download]"""
from pathlib import Path
import hashlib,os,sys,urllib.request,concurrent.futures
base=Path(__file__).resolve().parent
URL='https://github.com/xuguosheng110/rdk-x5-fnos/releases/download/v0.3.1/'
FILES=json_files=__import__('json').loads('[{"file": "rdk-x5-fnos-v0.3.1.img.xz", "size": 2612850028, "sha256": "59e6a7f8af809d2d8a2e692b384335843aa5227e0925b3ef3857adb853c15791", "parts": [{"name": "rdk-x5-fnos-v0.3.1.img.xz.part01", "size": 536870912, "sha256": "e6c42673e28c719bb63570819443a45f1061535485f19cc51a2b6dacebc8e713"}, {"name": "rdk-x5-fnos-v0.3.1.img.xz.part02", "size": 536870912, "sha256": "3cf0068361a7848b040a7dffbad73e5d4f490241ccf83506693c12d8521d1f98"}, {"name": "rdk-x5-fnos-v0.3.1.img.xz.part03", "size": 536870912, "sha256": "452e5d3f91b2e297bc28dc0910bf38d8c6fd62e85e091142847ab4f48bba1228"}, {"name": "rdk-x5-fnos-v0.3.1.img.xz.part04", "size": 536870912, "sha256": "7e31b5e2da7576571a656222c1f22998fe32a8ab101296ea74648aed2e7df64c"}, {"name": "rdk-x5-fnos-v0.3.1.img.xz.part05", "size": 465366380, "sha256": "5859ff2e1fa8310511b3ce022e4ada6e92efc937c0d8d72d1b09c275d03d6984"}]}, {"file": "fnos-x5-accel-0.3.5-runtime.fpk", "size": 1301594067, "sha256": "da8fc1a064fcdea991d976fca4693bd48badd62ba9f6215845b1ab5a4b3bb2b3", "parts": [{"name": "fnos-x5-accel-0.3.5-runtime.fpk.part01", "size": 536870912, "sha256": "2f06e218564fe18be83a5faecd4dea61465017ce2a8a6653b5ef28683197e342"}, {"name": "fnos-x5-accel-0.3.5-runtime.fpk.part02", "size": 536870912, "sha256": "a934f9f13c1a2ec193537164ea6c0692cfc2336d6dc64a530ec2ae2cc9eeb520"}, {"name": "fnos-x5-accel-0.3.5-runtime.fpk.part03", "size": 227852243, "sha256": "7f8e640ce28c06855bd30f8feb98fe884a4dac964e1aa124c5b2897b89581b08"}]}]')
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
