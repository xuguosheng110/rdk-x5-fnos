"""Real-board creation/umask and cross-user access regression; owned fixtures only."""
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile


def main():
    root = Path(tempfile.mkdtemp(prefix="fnos-create-validation-", dir="/vol1/1000"))
    outside = Path(tempfile.mkdtemp(prefix="fnos-create-outside-"))
    private = Path(tempfile.mkdtemp(prefix="fnos-create-private-", dir="/vol1/@appmeta"))
    try:
        os.chown(root, 1000, 1001)
        old_umask = os.umask(0o077)
        os.setegid(1001)
        os.seteuid(1000)
        folder = root / "nested"
        folder.mkdir(mode=0o700)
        p = folder / "new.bin"
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, b"cross-app household data")
        os.close(fd)
        fd_dir = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        fd = os.open("relative.bin", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=fd_dir)
        os.write(fd, b"openat path"); os.close(fd); os.close(fd_dir)
        os.seteuid(0)
        os.setegid(0)
        q = outside / "keep-private.bin"
        fd = os.open(q, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        r = private / "keep-private.bin"
        fd = os.open(r, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        os.umask(old_umask)
        assert stat.S_IMODE(folder.stat().st_mode) == 0o777, "new user folder is not shared"
        for f in (p, folder / "relative.bin"):
            assert stat.S_IMODE(f.stat().st_mode) == 0o666, "new user file is not shared"
        assert stat.S_IMODE(q.stat().st_mode) == 0o600, "OS file permissions changed"
        assert stat.S_IMODE(r.stat().st_mode) == 0o600, "app-private metadata permissions changed"
        child = os.fork()
        if not child:
            try:
                os.setgroups([]); os.setgid(65534); os.setuid(65534)
                assert p.read_bytes() == b"cross-app household data"
                with p.open("ab") as f: f.write(b" + second application")
                os._exit(0)
            except BaseException:
                os._exit(1)
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0, "different uid could not read and append"
        print({"result": "PASS", "create_umask": "0077", "file_mode": "0666", "directory_mode": "0777",
               "cross_user": "uid 1000 creates; uid 65534 reads and appends", "os_private_modes": "0600 preserved",
               "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
    finally:
        os.seteuid(0); os.setegid(0)
        shutil.rmtree(root); shutil.rmtree(outside); shutil.rmtree(private)


if __name__ == "__main__":
    main()
