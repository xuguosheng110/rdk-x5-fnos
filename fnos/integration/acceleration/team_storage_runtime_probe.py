"""Real Linux cross-UID creation check for a unique owned team directory."""
import os,pathlib,shutil,subprocess,sys,tempfile,json
library=sys.argv[1]
base=pathlib.Path('/vol1/@team');created=not base.exists();base.mkdir(exist_ok=True)
fixture=pathlib.Path(tempfile.mkdtemp(prefix='fnos-owned-team-',dir=base));fixture.chmod(0o777)
env=os.environ|{'LD_PRELOAD':library,'FNOS_HOME_DATA_ROOT':'auto','FNOS_HOME_TEST_LIBRARY':str(pathlib.Path(library).resolve())}
creator='import os,pathlib,sys;assert os.environ["FNOS_HOME_TEST_LIBRARY"] in pathlib.Path("/proc/self/maps").read_text(), "test library not loaded";os.umask(0o077);p=pathlib.Path(sys.argv[1]);p.mkdir();(p/"file.txt").write_text("team-shared");print(oct(p.stat().st_mode & 0o777),oct((p/"file.txt").stat().st_mode & 0o777))'
try:
 child=fixture/'child'
 r=subprocess.run(['setpriv','--reuid=1000','--regid=1000','--clear-groups','python3','-c',creator,str(child)],env=env,capture_output=True,text=True,check=True)
 print(r.stdout.strip())
 r=subprocess.run(['setpriv','--reuid=65534','--regid=65534','--clear-groups','python3','-c','import pathlib,sys;p=pathlib.Path(sys.argv[1]);assert p.read_text()=="team-shared";p.open("a").write("-append")',str(child/'file.txt')],capture_output=True,text=True)
 print(json.dumps({'cross_uid_pass':r.returncode==0,'file_mode':oct((child/'file.txt').stat().st_mode & 0o777),'directory_mode':oct(child.stat().st_mode & 0o777)}));assert r.returncode==0,r.stderr[-300:]
finally:
 shutil.rmtree(fixture)
 if created:base.rmdir()
