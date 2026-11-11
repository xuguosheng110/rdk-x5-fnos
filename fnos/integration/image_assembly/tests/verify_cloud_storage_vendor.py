import hashlib, importlib.util, json, os, pathlib, tempfile
import sys
base=pathlib.Path(sys.argv[1])
spec=importlib.util.spec_from_file_location('compat',base/'cloud_storage_compat.py'); m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
original=(base/'cloud-storage-dav.original').read_bytes()
assert hashlib.sha256(original).hexdigest()==m.ORIGINAL_SHA256
checks={}
payload=m.patched(original)
assert hashlib.sha256(payload).hexdigest()=='36ddd755393f1e5e9d7cfd057e058e18e468d53b67efd28a0176e332253b54f5'
checks['exact_reviewed_output']=True
assert m.patched(payload)==payload
checks['patch_idempotent']=True
with tempfile.TemporaryDirectory(dir=base) as tmp:
 root=pathlib.Path(tmp); target=root/'usr/trim/bin/cloud_storage_dav';target.parent.mkdir(parents=True);target.write_bytes(original);target.chmod(0o755)
 result=m.install(root);assert target.read_bytes()==payload and target.stat().st_mode&0o777==0o755
 backup=root/'usr/lib/fnos-x5/vendor-original'/('cloud_storage_dav-'+m.ORIGINAL_SHA256)
 assert backup.read_bytes()==original
 checks['atomic_install_and_original_backup']=True
 inode=target.stat().st_ino;m.install(root);assert target.stat().st_ino==inode
 checks['repeat_install_no_replace']=True
 backup.write_bytes(b'corrupt')
 try:m.install(root)
 except ValueError:pass
 else:raise AssertionError('corrupt backup accepted')
 assert target.read_bytes()==payload
 checks['bad_backup_rejected_without_target_change']=True
with tempfile.TemporaryDirectory(dir=base) as tmp:
 root=pathlib.Path(tmp);target=root/'usr/trim/bin/cloud_storage_dav';target.parent.mkdir(parents=True)
 bad=bytearray(original);bad[-1]^=1;target.write_bytes(bad)
 try:m.install(root)
 except ValueError:pass
 else:raise AssertionError('unknown version accepted')
 assert target.read_bytes()==bad and not (root/'usr/lib').exists()
 checks['unknown_version_no_mutation']=True
 target.unlink();external=root/'external';external.write_bytes(original);target.symlink_to(external)
 try:m.install(root)
 except ValueError:pass
 else:raise AssertionError('symlink accepted')
 assert external.read_bytes()==original
 checks['symlink_target_rejected']=True
print(json.dumps({'result':'PASS','checks':checks,'receipt':result},indent=2))
