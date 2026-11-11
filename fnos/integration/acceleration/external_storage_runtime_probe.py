import ctypes,os,pathlib,shutil,subprocess,sys,tempfile
lib=ctypes.CDLL(sys.argv[1],use_errno=True)
lib.trimacl_get_file.restype=ctypes.c_void_p
lib.trimacl_get_file.argtypes=[ctypes.c_char_p]
lib.unlinkat.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int]
free=ctypes.CDLL(None).free;free.argtypes=[ctypes.c_void_p]
os.environ['FNOS_HOME_DATA_ROOT']='auto'
source=pathlib.Path(tempfile.mkdtemp(prefix='fnos-mounted-scope-',dir='/vol1/1000'))
try:
 for ns in ('/vol00','/vol01','/vol02'):
  namespace=pathlib.Path(ns); made_namespace=not namespace.exists();namespace.mkdir(exist_ok=True)
  target=pathlib.Path(tempfile.mkdtemp(prefix='fnos-owned-scope-',dir=namespace));mounted=False
  try:
   target.chmod(0o700)
   acl=lib.trimacl_get_file(str(target).encode())
   assert not acl and target.stat().st_mode & 0o777==0o700, 'stale OS directory must not be shared'
   f=source/'probe.txt';f.write_text('controlled mounted namespace');f.chmod(0o600)
   subprocess.run(['mount','--bind',str(source),str(target)],check=True);mounted=True
   acl=lib.trimacl_get_file(str(target/'probe.txt').encode())
   assert acl and (target/'probe.txt').stat().st_mode & 0o666==0o666
   free(acl)
   assert lib.unlinkat(-7125,str(target/'probe.txt').encode(),0)==0
   assert not f.exists()
   acl=lib.trimacl_get_file(str(target).encode());assert acl;free(acl)
   env=os.environ|{'LD_PRELOAD':str(pathlib.Path(sys.argv[1]).resolve()),'FNOS_HOME_DATA_ROOT':'auto'}
   child=target/'new-from-app'
   creator='import os,pathlib,sys;assert os.environ["LD_PRELOAD"] in pathlib.Path("/proc/self/maps").read_text();os.umask(0o077);p=pathlib.Path(sys.argv[1]);p.mkdir();(p/"file.txt").write_text("mounted-shared")'
   subprocess.run(['setpriv','--reuid=1000','--regid=1000','--clear-groups','python3','-c',creator,str(child)],env=env,check=True)
   assert child.stat().st_mode & 0o777==0o777 and (child/'file.txt').stat().st_mode & 0o777==0o666
   subprocess.run(['setpriv','--reuid=65534','--regid=65534','--clear-groups','python3','-c','import pathlib,sys;p=pathlib.Path(sys.argv[1]);assert p.read_text()=="mounted-shared";p.open("a").write("-append")',str(child/'file.txt')],check=True)
   shutil.rmtree(child)
   print(ns,'PASS: mounted child sharing, cross-UID new file/directory and native permanent delete; unmounted OS directory unchanged')
  finally:
   if mounted:subprocess.run(['umount',str(target)],check=True)
   target.rmdir()
   if made_namespace:namespace.rmdir()
finally:shutil.rmtree(source)
print('Controlled bind-mount simulation only; physical USB and remote protocol acceptance not claimed')
