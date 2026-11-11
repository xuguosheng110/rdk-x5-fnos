"""Exercise household mount compatibility using disposable loop files only.

Run as root with LD_PRELOAD in a private mount namespace. No existing block
device is formatted: mkfs receives newly created regular files, before losetup.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

assert os.geteuid() == 0
assert os.readlink('/proc/self/ns/mnt') != os.readlink('/proc/1/ns/mnt')
assert os.environ['LD_PRELOAD'] in Path('/proc/self/maps').read_text()
subprocess.run(['mount', '--make-rprivate', '/'], check=True)
work = Path(tempfile.mkdtemp(prefix='fnos-mount-options-'))
target = Path('/vol' + str(910000000 + os.getpid()))
assert not os.path.lexists(target)
target.mkdir(mode=0o700)
outside = work / 'outside'; outside.mkdir()
loops = []
checks = []


def run(*args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, **kwargs)


def fixture(fs):
    path = work / (fs + '.img')
    with path.open('xb') as f:
        f.truncate(128 * 1024 * 1024)
    assert path.is_file() and not path.is_symlink()
    args = ['mkfs.btrfs', '-q', '-f', '-m', 'single', '-d', 'single'] if fs == 'btrfs' else ['mkfs.ext4', '-q', '-F', '-O', 'quota,project']
    subprocess.run([*args, str(path)], check=True, capture_output=True)
    loop = run('losetup', '--find', '--show', str(path), check=True).stdout.strip()
    loops.append(loop)
    info = json.loads(run('losetup', '--json', '--list', '--output', 'NAME,BACK-FILE', loop, check=True).stdout)['loopdevices']
    assert len(info) == 1 and info[0]['name'] == loop
    assert Path(info[0]['back-file']).resolve() == path.resolve()
    return loop


def rejected(name, source, destination, fs, options, policy='auto'):
    env = dict(os.environ, FNOS_HOME_DATA_ROOT=policy)
    result = run('mount', '-t', fs, '-o', options, source, str(destination), env=env)
    assert result.returncode != 0 and not os.path.ismount(destination), (name, result.stderr)
    checks.append(name)


try:
    btrfs = fixture('btrfs')
    rejected('no_policy_keeps_trimacl_rejection', btrfs, target, 'btrfs', 'trimacl', '')
    rejected('outside_volume_keeps_trimacl_rejection', btrfs, outside, 'btrfs', 'trimacl')
    rejected('unknown_option_not_removed', btrfs, target, 'btrfs', 'trimacl_bad')
    rejected('option_value_not_removed', btrfs, target, 'btrfs', 'trimacl=1')
    env = dict(os.environ, FNOS_HOME_DATA_ROOT='auto')
    run('mount', '-t', 'btrfs', '-o', 'ro,nosuid,nodev,trimacl,compress=zstd:1', btrfs, str(target), env=env, check=True)
    info = json.loads(run('findmnt', '--json', '--target', str(target), '-o', 'SOURCE,TARGET,FSTYPE,OPTIONS', check=True).stdout)['filesystems'][0]
    assert info['target'] == str(target) and info['fstype'] == 'btrfs'
    assert Path(info['source']).resolve() == Path(btrfs).resolve()
    options = info['options'].split(',')
    assert all(x in options for x in ('ro', 'nosuid', 'nodev', 'compress=zstd:1'))
    assert 'trimacl' not in options
    checks.append('btrfs_preserves_readonly_security_and_compression_options')
    run('umount', str(target), check=True)
    ext4 = fixture('ext4')
    run('mount', '-t', 'ext4', '-o', 'trimacl,prjquota,nosuid,nodev', ext4, str(target), env=env, check=True)
    info = json.loads(run('findmnt', '--json', '--target', str(target), '-o', 'SOURCE,TARGET,FSTYPE,OPTIONS', check=True).stdout)['filesystems'][0]
    assert info['target'] == str(target) and info['fstype'] == 'ext4'
    assert Path(info['source']).resolve() == Path(ext4).resolve()
    assert all(x in info['options'].split(',') for x in ('prjquota', 'nosuid', 'nodev'))
    checks.append('ext4_preserves_project_quota_and_security_options')
    run('umount', str(target), check=True)
    print(json.dumps({'status': 'PASS', 'cases': checks, 'count': len(checks),
                      'scope': 'disposable regular-file loop fixtures; existing disks untouched'}))
finally:
    for path in (target, outside):
        if os.path.ismount(path):
            run('umount', str(path), check=True)
    for loop in reversed(loops):
        run('losetup', '--detach', loop, check=True)
    target.rmdir()
    shutil.rmtree(work)
