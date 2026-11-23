"""Enable the Quark 1.1.20 launcher to fall back to its private log directory."""
import hashlib
import os
from pathlib import Path
import stat
import tempfile

LAUNCHER = Path('/var/apps/quark/cmd/main')
ORIGINAL = '9d642f19539338ad33050f6c53645972dbaeae96f213db6ff53858db829a5ece'
ANCHOR = b'LIFECYCLE_LOG="${LOG_DIR}/system/lifecycle.log"'
FALLBACK = b'''# The shared log tree may be inaccessible to the application's UID.
if ! ( : >> "${LOG_DIR}/system/lifecycle.log" ) 2>/dev/null; then
    LOG_DIR="${TRIM_PKGVAR:?}/logs"
    mkdir -p "${LOG_DIR}/system" "${LOG_DIR}/users" || exit 1
fi
'''


QBIT_LAUNCHER = Path('/var/apps/qBittorrent/cmd/service-setup')
QBIT_ORIGINAL = 'a50e01c18add32cbdc22a6687ec1a4a04a4d2b3af4b6ed730a18403a461ffa84'
QBIT_ANCHOR = b'LOG_FILE="${LOG_DIR}/service.log"'
QBIT_REPLACEMENT = b'''LOG_FILE="${TRIM_PKGVAR:?}/logs/service.log"
mkdir -p "${TRIM_PKGVAR}/logs" || exit 1'''


def repair(path=LAUNCHER, expected=ORIGINAL, anchor=ANCHOR, replacement=FALLBACK + ANCHOR):
    # Only replace a known root-owned launcher in root-controlled directories.
    for parent in path.parents:
        try:
            info = parent.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(info.st_mode) or info.st_uid or info.st_mode & 0o022:
            return False
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_uid or info.st_gid or info.st_mode & 0o022:
        return False
    source = path.read_bytes()
    if hashlib.sha256(source).hexdigest() != expected or source.count(anchor) != 1:
        return False
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(source.replace(anchor, replacement))
            os.fchmod(out.fileno(), stat.S_IMODE(info.st_mode))
        current = path.lstat()
        if (current.st_ino, current.st_ctime_ns) != (info.st_ino, info.st_ctime_ns):
            return False
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return True


if __name__ == '__main__':
    repair()
    repair(QBIT_LAUNCHER, QBIT_ORIGINAL, QBIT_ANCHOR, QBIT_REPLACEMENT)
