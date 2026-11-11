"""Real libnfile ABI regression; requires root and the selected LD_PRELOAD."""
import ctypes, json, os, pathlib, shutil, subprocess, tempfile

assert os.environ['LD_PRELOAD'] in pathlib.Path('/proc/self/maps').read_text()
os.environ['FNOS_HOME_DATA_ROOT'] = 'auto'
native = ctypes.CDLL('/usr/trim/lib/libnfile.so', use_errno=True)
native.is_mountpoint.argtypes = [ctypes.c_char_p, ctypes.c_int]
native.is_mountpoint.restype = ctypes.c_int
def mountpoint(path, flags=0x100):
    return native.is_mountpoint(os.fsencode(path), flags)

root = pathlib.Path(tempfile.mkdtemp(prefix='fnos-mountpoint-test-', dir='/vol1/1000'))
mounted = False
try:
    plain = root/'plain'; plain.mkdir()
    os.chdir('/')
    assert mountpoint(str(plain).lstrip('/')) == 0, 'native relative directory reported as a mountpoint'
    assert mountpoint(plain) == 0, 'ordinary directory reported as a mountpoint'
    assert mountpoint('/vol1') == 1, 'real data volume must stay protected'
    target = root/'bound'; target.mkdir()
    subprocess.run(['mount', '--bind', str(plain), str(target)], check=True); mounted = True
    assert plain.stat().st_dev == target.stat().st_dev
    assert mountpoint(target) == 1, 'same-filesystem bind mount must stay protected'
    child = target/'child'; child.mkdir()
    assert mountpoint(child) == 0
    link = root/'link'; link.symlink_to(target)
    assert mountpoint(link) == 0 and mountpoint(link, 0) == 1
    assert mountpoint(root/'missing') == 1, 'native ABI treats errors as protected'
    print(json.dumps({'ordinary_directory':False, 'volume_mount':True, 'same_device_bind_mount':True,
                      'mounted_child':False, 'symlink_nofollow':False, 'symlink_follow':True,
                      'missing_protected':True, 'result':'PASS'}))
finally:
    if mounted: subprocess.run(['umount', str(root/'bound')], check=True)
    shutil.rmtree(root)
