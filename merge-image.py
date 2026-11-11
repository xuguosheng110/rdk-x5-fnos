#!/usr/bin/env python3
"""Join verified release parts; never opens or writes any physical device."""
from pathlib import Path
import hashlib,json,os
base=Path(__file__).resolve().parent
meta={'file': 'rdk-x5-fnos-v0.1.0.img.xz', 'sha256': 'd70d3ee64033102b3dbba3630bcd68983dc78303542a2c35faab2a85d56557ac', 'parts': [{'name': 'rdk-x5-fnos-v0.1.0.img.xz.part01', 'size': 134217728, 'sha256': 'dfd47be060b86ad18f46db7ac4e79623b2c694c48ca94cb8bba829bb9c8d6af7'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part02', 'size': 134217728, 'sha256': 'bbbd8e59f3221828a08f2fdcd8e707c5d35bcfda7890dc4d2f40e39c5fccf53b'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part03', 'size': 134217728, 'sha256': '8d8c6cad07fbedfea222afcf312ca4ec28d2c015e3737c72761658ffe7b29f86'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part04', 'size': 134217728, 'sha256': 'b0f6c4c8f2c851b06ea22bb6d1ca17b2cc6b681a1585053cfa6d4a717003c650'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part05', 'size': 134217728, 'sha256': 'f2b2e01f55b643efc05ca1c9ba2f761b6031e44a32bfebdf2bfcbb107e42a904'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part06', 'size': 134217728, 'sha256': '4774a9d9b7e317925b79a782d60ec5db42209a00cbeb054da4f03717945c2062'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part07', 'size': 134217728, 'sha256': 'd806a5c56a18ab7075de82c5c002017adbf8adaefa12137d3999364aee0f5f7a'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part08', 'size': 134217728, 'sha256': 'ec90d4de1b7f6a3a04786731aa95dc8a2d423ac78ff1695c87a37d8a8739df05'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part09', 'size': 134217728, 'sha256': 'b51fee5f386a70cf9614a722eaf8bcf2a2f9c2ff3426a4eede58b2f232a21248'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part10', 'size': 134217728, 'sha256': 'f7e75deb9d5c6f4b4481521156721e33a6c409c14bd47201fc417394eb7ebba2'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part11', 'size': 134217728, 'sha256': '025c120dacc549ebdf60ec72943873a7c2bd4bfff08b6206734f8bb87353676f'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part12', 'size': 134217728, 'sha256': '5a82ffe85781dc53774ae0657baf67b5da6670eae6f43e18fdaede449a41e2fe'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part13', 'size': 134217728, 'sha256': '9a5075c3a0694f0cadd534765d247e3e19c55c3165ca1f9f818889d52cf22b9c'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part14', 'size': 134217728, 'sha256': '2dff59fec4f9a7549844a7ec02c4923a8f27bc966c6347f04a6713a4a9dc64b4'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part15', 'size': 134217728, 'sha256': 'fbd928ca36338196de793935433653a159ea8e80b151c6916a8bc28077906525'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part16', 'size': 134217728, 'sha256': 'e0d84cebf0c56a10d78913da687eca81ad957701e68b3268931e9d92617d83a6'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part17', 'size': 134217728, 'sha256': '36f0f6db1005f8568602cbe75145e649d8b6792c8969bc2c745dd7a88f82ac37'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part18', 'size': 134217728, 'sha256': '414426db65dd74fc07c02fcd6f32dc201686df7e9f58f096ff37ff62d257ab6c'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part19', 'size': 134217728, 'sha256': 'b27787a3a6e1abdb7cb229f9c1e4e265991c112896869f1495e6b182faed4624'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part20', 'size': 134217728, 'sha256': '37bed674a935b71baa07ae44e798fb40e47b0e9524ce4530c7684250f315ddb7'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part21', 'size': 134217728, 'sha256': '160c495e0051a517a95a11cba5f43a785a45ea608db4ca2f1bd988ab50781383'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part22', 'size': 134217728, 'sha256': '5fee78a94e6321f58cfeea8c7357e282dc9af333a22e8489cb040510d4741788'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part23', 'size': 134217728, 'sha256': '4f294548a546e90f806a172e446082e5860ca88d8d47c897a9dd7c2724e5e5ce'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part24', 'size': 134217728, 'sha256': 'febdaff8a8d7c29260e9eaea45886608c3cad166cf713a7c27505b9de11b8f6c'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part25', 'size': 134217728, 'sha256': '6691ac568880df26220f5a51d9b7cb59add6596c91e43a16d1ce9f3d68bd04ef'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part26', 'size': 134217728, 'sha256': '6fa0cc2b1cb1e4b983953cd8c70660e1b0f9cbf56426079cfe050bb0879eacd8'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part27', 'size': 134217728, 'sha256': '279308f1588af16bfbca9fa4f8735753df9066120af944efb209f3bf8470c1a7'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part28', 'size': 134217728, 'sha256': 'a9a489f51617695d57870dfe94ed1ae1dd16587a24f2c68ba2de65791254096f'}, {'name': 'rdk-x5-fnos-v0.1.0.img.xz.part29', 'size': 39796996, 'sha256': 'a11895f70c5e02f18bc2f249c38ec43730e729aae2f8e8a9733f9cc26e49f68f'}]}
if '--download' in __import__('sys').argv:
 import urllib.request, concurrent.futures
 url='https://github.com/xuguosheng110/rdk-x5-fnos/releases/download/v0.1.0/'
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
