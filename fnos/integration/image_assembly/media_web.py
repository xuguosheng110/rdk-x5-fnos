"""Install the X5 quality switch patch in the reviewed native media player."""
import hashlib
import os
from pathlib import Path
import stat
import tempfile


SHIM = Path('usr/lib/fnos-x5/quality_switch.js')
APP_TARGET = Path('var/apps/trim.media/target')
INDEX_SHA256 = '59c9346ba1a7ccbc9c843e8f0a26660e6bd719d07485bf3f2c2549f082da99ec'
PLAYER_ASSET = 'bf29a647181e25c03987ab8cc8f81a1a-DB2rIeMx.js'
PLAYER_SHA256 = '51a668435ecba3abb272a0f6344612fde18fd2c64dc74e4557e7b45abe14984f'
START = b'<script data-fnos-x5-quality-switch>\n'
END = b'\n</script>'
PATCH_START = b'/* fnos-x5-quality-switch:start */\n'
PATCH_END = b'/* fnos-x5-quality-switch:end */\n'
ANCHOR = (b'let f=Math.floor(r?.currentTime??0),p=r?.config?.mediaConfig?.duration??0,'
          b'm=Math.min(f,p),h=await ve.play.resetQuality(')


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _index(root):
    target = (root / APP_TARGET).resolve(strict=True)
    if not target.is_relative_to(root) or target.name != 'trim.media':
        raise ValueError('unexpected native media target')
    index = target / 'static/index.html'
    for path in (target, index.parent, index):
        info = path.lstat()
        if path.is_symlink() or (root == Path('/') and (info.st_uid or info.st_gid or info.st_mode & 0o002)):
            raise ValueError('untrusted native media path: ' + str(path))
    if not stat.S_ISREG(index.lstat().st_mode):
        raise ValueError('native media index is not a regular file')
    return index


def _replace(path, before, data, info):
    fd, temporary = tempfile.mkstemp(prefix='.fnos-x5-index-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            if os.geteuid() == 0:
                os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
            os.fsync(stream.fileno())
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino) or path.read_bytes() != before:
            raise RuntimeError('native media index changed during reconciliation')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _original(current, start, end):
    if current.count(start) != 1:
        return current
    position = current.index(start)
    finish = current.find(end, position + len(start))
    if finish < 0:
        return current
    return current[:position] + current[finish + len(end):]


def reconcile(root, *, enabled):
    """Reconcile the local vendor file against its original, pinned bytes."""
    root = Path(root).resolve()
    try:
        index = _index(root)
    except FileNotFoundError:
        return 'not_installed'
    except ValueError:
        return 'unknown_path'
    info = index.stat()
    current = index.read_bytes()
    original = _original(current, START, END)
    if _digest(original) != INDEX_SHA256:
        return 'unknown_index'
    if original != current:
        _replace(index, current, original, info)

    asset = index.parent / 'assets' / PLAYER_ASSET
    try:
        parent = asset.parent.lstat()
        info = asset.lstat()
    except FileNotFoundError:
        return 'unknown_player' if enabled else 'native'
    if (not stat.S_ISDIR(parent.st_mode) or not stat.S_ISREG(info.st_mode)
            or (root == Path('/') and any(
                entry.st_uid or entry.st_gid or entry.st_mode & 0o002 for entry in (parent, info)))):
        return 'unknown_player'
    current = asset.read_bytes()
    original = _original(current, PATCH_START, PATCH_END)
    if _digest(original) != PLAYER_SHA256:
        return 'unknown_player'

    desired = original
    if enabled:
        patch = (root / SHIM).read_bytes()
        if PATCH_START in patch or PATCH_END in patch or original.count(ANCHOR) != 1:
            raise ValueError('unexpected native media quality switch patch')
        desired = original.replace(ANCHOR, PATCH_START + patch + PATCH_END + ANCHOR, 1)
    if desired != current:
        _replace(asset, current, desired, info)
    return 'enabled' if enabled else 'native'
