"""Pinned fnOS ARM 1.2.0701 installer compatibility for a standard Linux kernel.

The original installer chmods directories to 0644 before running install_init
as the package UID. Preserve directory modes while retaining chown, regular
file 0644, symlink handling and error propagation. Its shared download parent
also needs search permission for that UID. No application is promoted to root.

Addresses are virtual addresses in the exact vendor ELF below. Unknown builds
fail closed; the immutable original is retained for rollback.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import stat
import struct
import tempfile

ORIGINAL_SHA256 = "9794a320644a47e68f6811a7be0bb8075084ede2c073d1cbb5439935dc87b2f3"

# Preserve IsDir in the unused non-pointer slot at SP+104 of the existing
# 112-byte frame. After successful chown, directories return without chmod.
# Regular files continue to the original chmod(0644). All error branches and
# the separate no-follow symlink branch remain unchanged. No new calls/frame.
PATCHES = {
    0xD395B4: (0x36000060, 0xB9006BE0),  # str w0,[sp,#104]
    0xD395B8: (0xB24003E1, 0x36000040),  # tbz w0,#0,0xd395c0
    0xD395BC: (0x14000009, 0x14000016),  # b 0xd39614
    0xD39674: (0xF94043E1, 0xB9406BE2),  # ldr w2,[sp,#104]
    0xD39678: (0xF9403FE3, 0x35FFFC42),  # cbnz w2,0xd39600
    0xD3967C: (0xAA1F03E2, 0xF94043E1),  # ldr x1,[sp,#128]
    0xD39680: (0xAA1F03E0, 0xF9403FE3),  # ldr x3,[sp,#120]
    0xD39684: (0x14000019, 0x1400001A),  # b 0xd396ec (chmod path)
    0xD2E738: (0xD2803D02, 0xD2803DA2),  # download parent mkdir: 0750 -> 0755
}


def offsets(data: bytes) -> dict[int, int]:
    if data[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", data, 18)[0] != 183:
        raise ValueError("expected reviewed AArch64 ELF")
    phoff = struct.unpack_from("<Q", data, 32)[0]
    size, count = struct.unpack_from("<HH", data, 54)
    result = {}
    for i in range(count):
        typ, flags, off, va, _, length, _, _ = struct.unpack_from("<IIQQQQQQ", data, phoff+i*size)
        if typ == 1 and flags & 1:
            for address in PATCHES:
                if va <= address and address + 4 <= va + length:
                    result[address] = off + address - va
    if len(result) != len(PATCHES):
        raise ValueError("installer patch addresses not in executable segments")
    return result


def reviewed_original(data: bytes) -> bytes:
    if hashlib.sha256(data).hexdigest() == ORIGINAL_SHA256:
        return data
    normalized = bytearray(data)
    for address, off in offsets(data).items():
        before, after = PATCHES[address]
        if struct.unpack_from("<I", data, off)[0] != after:
            raise ValueError("unreviewed fnOS application-center binary")
        struct.pack_into("<I", normalized, off, before)
    if hashlib.sha256(normalized).hexdigest() != ORIGINAL_SHA256:
        raise ValueError("unreviewed fnOS application-center binary")
    return bytes(normalized)


def patched(data: bytes) -> bytes:
    original = reviewed_original(data)
    result = bytearray(original)
    for address, off in offsets(original).items():
        before, after = PATCHES[address]
        if struct.unpack_from("<I", original, off)[0] != before:
            raise ValueError("installer instruction mismatch")
        struct.pack_into("<I", result, off, after)
    return bytes(result)


def prepare_existing_download_parents() -> None:
    # New volumes use the patched native mkdir. Existing cache parents need
    # a one-time migration. Only real native data-volume mounts are selected.
    mounts = Path("/proc/self/mountinfo").read_text().splitlines()
    for line in mounts:
        mount = line.split()[4]
        if not re.fullmatch(r"/vol[1-9][0-9]*", mount):
            continue
        path = Path(mount) / "appcenter-downloads"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            continue
        try:
            info = os.fstat(fd)
            if info.st_uid != 0 or not stat.S_ISDIR(info.st_mode):
                raise ValueError("unexpected app download parent ownership")
            os.fchmod(fd, 0o755)
        finally:
            os.close(fd)


def apply() -> None:
    target = Path("/usr/trim/bin/trim_app_center")
    info = target.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
        raise ValueError("unexpected application-center executable")
    current = target.read_bytes()
    original = reviewed_original(current)
    payload = patched(current)
    backup_dir = Path("/usr/lib/fnos-x5/vendor-original")
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    backup_info = backup_dir.lstat()
    if not stat.S_ISDIR(backup_info.st_mode) or backup_info.st_uid != 0 or backup_info.st_mode & 0o022:
        raise ValueError("unsafe vendor backup directory")
    backup = backup_dir / ("trim_app_center-" + ORIGINAL_SHA256)
    if not backup.exists():
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(original)
            out.flush()
            os.fsync(out.fileno())
    if backup.is_symlink() or hashlib.sha256(backup.read_bytes()).hexdigest() != ORIGINAL_SHA256:
        raise ValueError("vendor backup mismatch")
    if payload != current:
        fd, tmp = tempfile.mkstemp(prefix=".fnos-install-compat-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(payload)
                os.fchown(out.fileno(), info.st_uid, info.st_gid)
                os.fchmod(out.fileno(), stat.S_IMODE(info.st_mode))
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    prepare_existing_download_parents()
    print("Native installer directory compatibility active: " + hashlib.sha256(payload).hexdigest())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", required=True)
    parser.parse_args()
    apply()
