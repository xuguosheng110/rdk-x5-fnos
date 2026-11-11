"""Prepare native mount namespace parents for the selected household policy.

The vendor image can leave /vol00 at mode 0000. A writable mounted child
then remains unreachable to non-root applications. Only these three OS-side
namespace directories are prepared; mounted filesystems are not chmodded.
"""
from pathlib import Path
import os
import stat


def prepare(root: Path = Path("/")) -> None:
    root = root.resolve()
    root_device = root.stat().st_dev
    for name in ("vol00", "vol01", "vol02"):
        path = root / name
        if not path.exists() and not path.is_symlink():
            path.mkdir(mode=0o755)
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if info.st_dev != root_device or not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"unexpected mounted namespace parent: {path}")
            os.fchmod(fd, 0o755)
        finally:
            os.close(fd)


if __name__ == "__main__":
    prepare()
