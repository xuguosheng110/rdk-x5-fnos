"""Native mount discovery regression, run as root in a private mount namespace.

Uses only its own temporary /volN names; never opens a block device. The
preloaded bridge is exercised through the real fnOS libnfile ABI.
"""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import tempfile

assert os.geteuid() == 0
assert os.readlink('/proc/self/ns/mnt') != os.readlink('/proc/1/ns/mnt')
assert os.environ['LD_PRELOAD'] in Path('/proc/self/maps').read_text()
subprocess.run(['mount', '--make-rprivate', '/'], check=True)
native = ctypes.CDLL('/lib/aarch64-linux-gnu/libnfile.so', use_errno=True)
native.is_mountpoint.argtypes = [ctypes.c_char_p, ctypes.c_int]
native.is_mountpoint.restype = ctypes.c_int
created = []
mounted = []
checks = []


def query(path, flags=0x100):
    return native.is_mountpoint(os.fsencode(path), flags)


def expect(name, path, want, flags=0x100):
    got = query(path, flags)
    assert got == want, (name, got, want)
    checks.append(name)


# Claim an empty directory exclusively before assigning any derived name.
anchor = Path(tempfile.mkdtemp(prefix='fnos-firstmount-'))
root = Path('/vol' + str(900000000 + os.getpid()))
link = Path(str(root) + '1')
file = Path(str(root) + '2')
assert all(not os.path.lexists(p) for p in (root, link, file))
try:
    os.environ['FNOS_HOME_DATA_ROOT'] = 'auto'
    expect('missing_volume_nofollow', root, 0)
    expect('missing_volume_follow', root, 0, 0)
    expect('missing_descendant_still_protected', root / 'missing', 1)
    expect('invalid_flags_protected', root, 1, 0x40000000)
    os.environ.pop('FNOS_HOME_DATA_ROOT')
    expect('policy_unset_preserves_native', root, 1)
    os.environ['FNOS_HOME_DATA_ROOT'] = '/vol*'
    expect('wildcard_policy_missing_volume', root, 0)
    root.mkdir(); created.append(root)
    expect('existing_unmounted_directory', root, 0)
    link.symlink_to(root); created.append(link)
    expect('root_symlink_nofollow_protected', link, 1)
    expect('root_symlink_follow_protected', link, 1, 0)
    file.touch(exist_ok=False); created.append(file)
    expect('root_file_protected', file, 1)
    subprocess.run(['mount', '--bind', str(anchor), str(root)], check=True)
    mounted.append(root)
    expect('same_filesystem_bind_root_protected', root, 1)
    subprocess.run(['umount', str(root)], check=True); mounted.remove(root)
    expect('unmounted_directory_reusable', root, 0)
    subprocess.run(['mount', '-t', 'tmpfs', '-o', 'size=1m', 'tmpfs', str(root)], check=True)
    mounted.append(root)
    expect('mounted_volume_protected', root, 1)
    child = root / 'child'; child.mkdir()
    expect('mounted_data_child_not_mountpoint', child, 0)
    expect('mounted_data_missing_still_protected', root / 'missing', 1)
    print(json.dumps({'status': 'PASS', 'cases': checks, 'count': len(checks),
                      'scope': 'native ABI in private mount namespace; no data disk touched'}))
finally:
    for path in reversed(mounted):
        subprocess.run(['umount', str(path)], check=True)
    for path in reversed(created):
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            path.rmdir()
    anchor.rmdir()
