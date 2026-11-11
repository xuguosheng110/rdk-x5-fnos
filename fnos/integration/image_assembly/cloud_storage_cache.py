"""Start native remote storage only with a persistent, identity-bound cache.

The first native default data volume is pinned: a later default-volume change
must not silently abandon pending uploads. An absent/replaced volume prevents
daemon startup; systemd retries. Never fall back to the operating-system disk.
"""
import json
import errno
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

STATE = 'var/lib/fnos-x5/webdav-cache/state.json'


def checked_path(root, relative):
    path = root / relative
    if path.resolve() != path:
        raise ValueError('symlink in persistent cache path')
    return path


def mounted_volume(mountinfo, volume):
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) > 6 and fields[4] == volume and 'rw' in fields[5].split(','):
            return True
    return False


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('cache metadata directory is not private')


def prepare(root=Path('/'), run=subprocess.run):
    root = Path(root).resolve(strict=True)
    def command(args):
        result = run(args, capture_output=True, text=True, timeout=30,
                     stdin=subprocess.DEVNULL, check=True)
        return result.stdout.strip()
    state_file = checked_path(root, STATE)
    previous = json.loads(state_file.read_text()) if state_file.exists() else None
    if previous:
        volume = previous['volume']
    else:
        selected = command([str(root / 'usr/local/bin/appcenter-cli'), 'default-volume'])
        if not re.fullmatch(r'[1-9][0-9]*', selected):
            raise RuntimeError('waiting for native default data volume')
        volume = '/vol' + selected
    if not re.fullmatch(r'/vol[1-9][0-9]*', volume):
        raise ValueError('invalid pinned data volume')
    mounts = (root / 'proc/self/mountinfo').read_text()
    if not mounted_volume(mounts, volume):
        raise RuntimeError('waiting for pinned writable data volume')
    volume_path = checked_path(root, volume.lstrip('/'))
    if not volume_path.is_dir():
        raise RuntimeError('data volume unavailable')
    uuid = command(['findmnt', '-n', '-o', 'UUID', '--mountpoint', str(volume_path)])
    if not re.fullmatch(r'[A-Za-z0-9-]+', uuid):
        raise ValueError('data volume lacks a stable UUID')
    if previous and previous['uuid'] != uuid:
        raise ValueError('pinned data volume identity changed')
    cache = checked_path(root, volume.lstrip('/') + '/@sysappmeta/fnos-remote-write-cache')
    private_directory(cache.parent)
    private_directory(cache)
    private_directory(state_file.parent)
    if not previous:
        fd, temp = tempfile.mkstemp(prefix='.webdav-cache-', dir=state_file.parent)
        try:
            with os.fdopen(fd, 'w') as out:
                json.dump({'volume': volume, 'uuid': uuid}, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, state_file)
            directory = os.open(state_file.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    return {'RCLONE_CACHE_DIR': str(cache), 'RCLONE_VFS_CACHE_MODE': 'writes',
            'RCLONE_VFS_CACHE_MAX_SIZE': '2G', 'RCLONE_VFS_CACHE_MIN_FREE_SPACE': '1G',
            'RCLONE_VFS_WRITE_BACK': '1s'}


def recover_disconnected_mounts(mountinfo, listdir=os.listdir, run=subprocess.run):
    """Remove only native rclone mounts whose userspace endpoint is gone.

    No lazy/forced unmount, no live mounts, and no traversal of other filesystems.
    Persistent upload cache is left intact for rclone to recover.
    """
    recovered = []
    for line in mountinfo.splitlines():
        fields = line.split()
        if '-' not in fields or len(fields) < 7:
            continue
        separator = fields.index('-')
        if separator + 1 >= len(fields) or fields[separator+1] != 'fuse.rclone':
            continue
        target = fields[4]
        if not re.fullmatch(r'/vol02/[0-9]+-[0-9]+-[a-f0-9]+', target):
            continue
        try:
            listdir(target)
        except OSError as error:
            if error.errno != errno.ENOTCONN:
                raise
            run(['umount', target], check=True, timeout=15,
                stdin=subprocess.DEVNULL, capture_output=True)
            recovered.append(target)
    return recovered


if __name__ == '__main__':
    environment = dict(os.environ, **prepare())
    recover_disconnected_mounts(Path('/proc/self/mountinfo').read_text())
    os.execve('/usr/trim/bin/cloud_storage_dav', ['cloud_storage_dav'], environment)
