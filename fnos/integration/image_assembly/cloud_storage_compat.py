"""Pinned native WebDAV mount request correction for fnOS ARM 1.2.0701.

The daemon explicitly sends CacheMode=0, overriding rclone's VFS defaults.
Point that one map value at its existing read-only integer 2 (writes).
Cache placement/lifecycle must be configured separately before deployment.
"""
import hashlib
import os
from pathlib import Path
import stat
import struct
import tempfile

ORIGINAL_SHA256 = "45cc84d576618803f50ee9efc3228526d63f6ce322171a754627c7fb1c2618a1"
ADDRESS = 0x89F044
BEFORE = 0x91164084  # add x4,x4,#0x590 -> int(0)
AFTER = 0x91166084   # add x4,x4,#0x598 -> int(2)


def offset(data, address, size, executable=False):
    if data[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", data, 18)[0] != 183:
        raise ValueError("expected reviewed AArch64 ELF")
    table = struct.unpack_from("<Q", data, 32)[0]
    width, count = struct.unpack_from("<HH", data, 54)
    for i in range(count):
        kind, flags, off, va, _, length, _, _ = struct.unpack_from("<IIQQQQQQ", data, table+i*width)
        if kind == 1 and (not executable or flags & 1) and va <= address and address+size <= va+length:
            return off+address-va
    raise ValueError("reviewed address outside expected ELF segment")


def patched(data):
    off = offset(data, ADDRESS, 4, executable=True)
    value = struct.unpack_from("<I", data, off)[0]
    original = bytearray(data)
    if value == AFTER:
        struct.pack_into("<I", original, off, BEFORE)
    elif value != BEFORE:
        raise ValueError("unreviewed native mount instruction")
    if hashlib.sha256(original).hexdigest() != ORIGINAL_SHA256:
        raise ValueError("unreviewed cloud-storage executable")
    for address, expected in [(0x13A7590, 0), (0x13A7598, 2)]:
        if struct.unpack_from("<Q", original, offset(original, address, 8))[0] != expected:
            raise ValueError("reviewed integer constant mismatch")
    struct.pack_into("<I", original, off, AFTER)
    return bytes(original)


def install(root):
    """Apply to an offline image, retaining the hash-verified vendor original.

    Caller must separately stage persistent cache placement before enabling
    the daemon. Unknown vendor builds are rejected before any mutation.
    """
    root = Path(root).resolve(strict=True)
    target = root / "usr/trim/bin/cloud_storage_dav"
    if target.resolve(strict=True) != target:
        raise ValueError("symlink in cloud-storage executable path")
    info = target.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("cloud-storage executable is not a regular file")
    current = target.read_bytes()
    payload = patched(current)
    original = bytearray(payload)
    struct.pack_into("<I", original, offset(original, ADDRESS, 4, True), BEFORE)
    parent = root / "usr/lib/fnos-x5/vendor-original"
    if parent.resolve() != parent:
        raise ValueError("symlink in vendor backup path")
    # This is the public vendor executable, not credentials. Keep it readable
    # for offline image verification while forbidding unprivileged writes.
    parent.mkdir(parents=True, mode=0o755, exist_ok=True)
    if parent.stat().st_mode & 0o022:
        raise ValueError("writable vendor backup directory")
    backup = parent / ("cloud_storage_dav-" + ORIGINAL_SHA256)
    try:
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        if backup.is_symlink() or hashlib.sha256(backup.read_bytes()).hexdigest() != ORIGINAL_SHA256:
            raise ValueError("vendor backup mismatch")
    else:
        with os.fdopen(fd, "wb") as out:
            out.write(original)
            out.flush()
            os.fsync(out.fileno())
    if current != payload:
        fd, tmp = tempfile.mkstemp(prefix=".cloud-storage-compat-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(payload)
                os.fchown(out.fileno(), info.st_uid, info.st_gid)
                os.fchmod(out.fileno(), stat.S_IMODE(info.st_mode))
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, target)
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return {"original_sha256": ORIGINAL_SHA256,
            "patched_sha256": hashlib.sha256(payload).hexdigest(),
            "cache_mode": "writes", "cache_placement_required": True}
