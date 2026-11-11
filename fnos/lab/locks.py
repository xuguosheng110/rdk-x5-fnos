"""One board, one cooperative file lock.

``board_id`` -> stable lock file is the only lock protocol used by the lab
executor, the CP5 USB entry and the legacy flash adapter.  The protocol is
deliberately tiny so it can be implemented by tools that do not import this
package::

    <root>/locks/board-<first 32 hex of sha256(board_id)>.lock

``root`` is ``$FNOS_BOARD_LOCK_ROOT``, then ``$FNOS_LAB_STATE_DIR``, then
``~/.local/state/fnos-lab``.

Safety properties implemented here:

* the lock directory and file are opened relative to an ``O_DIRECTORY`` fd,
  with ``O_NOFOLLOW``; a symlink, directory or FIFO at the lock path is refused;
* after ``flock`` the path is compared with the opened inode so a rename/
  replacement cannot silently move other processes to another lock;
* a child process can adopt an inherited lock fd, but only after the fd is
  proven to reference the same regular inode as the canonical path *and* an
  independent open cannot acquire the lock.  A plain boolean is never accepted;
* an inherited fd is never unlocked on exit (that would release the parent's
  lock); only the owning process unlocks.

The lock is cooperative.  A phone app, a person unplugging the cable or an
unmodified script cannot be stopped by a file lock.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from dataclasses import dataclass
from typing import Iterator

from .errors import Refused
from .util import secure_dir, utc_now

BOARD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
LOCK_ROOT = "locks"
LOCK_PROTOCOL = "fnos-board-lock/v1"
LOCK_ENV_FD = "FNOS_BOARD_LOCK_FD"
LOCK_ENV_PATH = "FNOS_BOARD_LOCK_PATH"
LOCK_ENV_BOARD = "FNOS_BOARD_ID"
LOCK_ENV_ID = "FNOS_BOARD_LOCK_ID"


def _validate_board_id(board_id: str) -> str:
    if not isinstance(board_id, str) or not BOARD_ID.fullmatch(board_id):
        raise Refused(
            "board_id must be 1..64 characters of letters, digits, dot, dash or underscore"
        )
    return board_id


def lock_root(override: Path | str | None = None) -> Path:
    """Return the shared lock root.

    ``FNOS_BOARD_LOCK_ROOT`` is the cross-tool override; the older
    ``FNOS_LAB_STATE_DIR`` remains supported so existing lab state directories
    keep working.
    """
    if override is not None:
        return Path(override).expanduser()
    for name in ("FNOS_BOARD_LOCK_ROOT", "FNOS_LAB_STATE_DIR"):
        value = os.environ.get(name)
        if value:
            return Path(value).expanduser()
    return Path.home() / ".local" / "state" / "fnos-lab"


def board_lock_name(board_id: str) -> str:
    board_id = _validate_board_id(board_id)
    digest = hashlib.sha256(board_id.encode("utf-8")).hexdigest()[:32]
    return f"board-{digest}.lock"


def board_lock_path(state_root: Path | str | None, board_id: str) -> Path:
    """Return the stable lock path for a board under ``state_root``."""
    board_id = _validate_board_id(board_id)
    return lock_root(state_root) / LOCK_ROOT / board_lock_name(board_id)


def _open_lock_dir(root: Path) -> tuple[int, Path]:
    root = Path(root).expanduser()
    if root.is_symlink():
        raise Refused("private lock root must not be a symlink")
    secure_dir(root)
    lock_dir = root / LOCK_ROOT
    if lock_dir.is_symlink():
        raise Refused("lock directory must not be a symlink")
    secure_dir(lock_dir)
    try:
        fd = os.open(lock_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise Refused("lock directory is a symlink; refusing to use it") from None
        raise Refused(f"cannot open lock directory: {exc.strerror}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise Refused("lock directory is not a directory")
        os.fchmod(fd, 0o700)
    except Refused:
        os.close(fd)
        raise
    except OSError:
        os.close(fd)
        raise Refused("cannot secure the lock directory") from None
    return fd, lock_dir


def _open_lock_file(dir_fd: int, name: str) -> int:
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise Refused(
                "lock file is a symlink; refusing to follow it (possible lock bypass)"
            ) from None
        if exc.errno == errno.EISDIR:
            raise Refused("lock path is a directory, not a lock file") from None
        raise Refused(f"cannot open board lock: {exc.strerror}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refused("lock path is not a regular file; refusing to use it")
        if info.st_nlink != 1:
            raise Refused("lock file has extra hard links; refusing a shared lock inode")
        os.fchmod(fd, 0o600)
    except Refused:
        os.close(fd)
        raise
    except OSError:
        os.close(fd)
        raise Refused("cannot set private permissions on the board lock") from None
    return fd


def _same_inode(fd: int, dir_fd: int, name: str) -> bool:
    opened = os.fstat(fd)
    try:
        entry = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return (opened.st_dev, opened.st_ino) == (entry.st_dev, entry.st_ino)


def _write_metadata(fd: int, board_id: str, acquired_at: str, lock_id: str) -> None:
    metadata = json.dumps(
        {
            "schema": LOCK_PROTOCOL,
            "board_id": board_id,
            "pid": os.getpid(),
            "lock_id": lock_id,
            "acquired_at": acquired_at,
            "purpose": "cooperative board operation lock (power/serial/flash/boot/verify)",
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    view = memoryview(metadata + b"\n")
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("short lock metadata write")
        view = view[count:]
    os.fsync(fd)


def _read_metadata(fd: int) -> dict:
    try:
        raw = os.pread(fd, 8192, 0)
    except OSError:
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class HeldLock:
    board_id: str
    path: Path
    fd: int
    acquired_at: str
    lock_id: str
    inherited: bool = False

    def revalidate(self) -> None:
        """Raise if the canonical path no longer names the locked inode."""
        try:
            entry = os.stat(self.path, follow_symlinks=False)
        except OSError:
            raise Refused(
                "board lock path disappeared or was replaced; refusing to continue"
            ) from None
        opened = os.fstat(self.fd)
        if not stat.S_ISREG(entry.st_mode):
            raise Refused("board lock path is no longer a regular file")
        if (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino):
            raise Refused(
                "board lock path was replaced while held; refusing to continue"
            )
        if entry.st_nlink != 1:
            raise Refused("board lock path gained extra hard links while held")

    def metadata(self) -> dict:
        return _read_metadata(self.fd)

    def child_env(self) -> dict[str, str]:
        """Environment + ``pass_fds`` payload for a reviewed child backend."""
        return {
            LOCK_ENV_FD: str(self.fd),
            LOCK_ENV_PATH: str(self.path),
            LOCK_ENV_BOARD: self.board_id,
            LOCK_ENV_ID: self.lock_id,
        }

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return (self.fd,)


@contextlib.contextmanager
def board_lock(
    state_root: Path | str | None,
    board_id: str,
    *,
    blocking: bool = False,
) -> Iterator[HeldLock]:
    """Acquire the single board lock.

    ``blocking=False`` refuses immediately; queueing is intentionally not the
    default.  The lock file is never unlinked: removing it would create the
    classic two-inode race.
    """
    board_id = _validate_board_id(board_id)
    root = lock_root(state_root)
    dir_fd, lock_dir = _open_lock_dir(root)
    name = board_lock_name(board_id)
    path = lock_dir / name
    fd = -1
    try:
        fd = _open_lock_file(dir_fd, name)
        if not _same_inode(fd, dir_fd, name):
            raise Refused("board lock path changed while opening it")
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError:
            raise Refused(
                f"board {board_id} is busy; another laboratory operation holds the lock"
            ) from None
        except OSError as exc:
            raise Refused(f"cannot lock board {board_id}: {exc.strerror}") from None
        if not _same_inode(fd, dir_fd, name):
            raise Refused("board lock path was replaced during acquisition")
        acquired_at = utc_now()
        lock_id = secrets.token_hex(16)
        try:
            _write_metadata(fd, board_id, acquired_at, lock_id)
        except OSError as exc:
            raise Refused(f"cannot write board lock metadata: {exc.strerror}") from None
        if not _same_inode(fd, dir_fd, name):
            raise Refused("board lock path was replaced after acquisition")
        held = HeldLock(
            board_id=board_id,
            path=path,
            fd=fd,
            acquired_at=acquired_at,
            lock_id=lock_id,
        )
        yield held
    finally:
        if fd >= 0:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.close(dir_fd)
        except OSError:
            pass


def adopt_inherited_lock(
    fd: int,
    path: str | Path,
    board_id: str,
    *,
    expected_lock_id: str | None = None,
) -> HeldLock:
    """Adopt an fd passed by a reviewed parent.

    This is a capability, not a boolean: the fd must be an open regular file
    for the canonical lock path, carry matching board metadata, and an
    independent open of that path must fail to acquire the lock.  The returned
    handle never calls ``LOCK_UN``; it only closes the child's fd.
    """
    board_id = _validate_board_id(board_id)
    if not isinstance(fd, int) or isinstance(fd, bool) or fd < 3:
        raise Refused("inherited lock fd is invalid")
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise Refused("inherited lock path must be absolute")
    try:
        opened = os.fstat(fd)
        entry = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise Refused(
            f"inherited lock fd/path is not usable: {exc.strerror}"
        ) from None
    if not stat.S_ISREG(opened.st_mode):
        raise Refused("inherited lock fd is not a regular file")
    if opened.st_nlink != 1:
        raise Refused("inherited lock fd has extra hard links")
    if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
        raise Refused(
            "inherited lock fd does not match the canonical board lock path"
        )
    if path.name != board_lock_name(board_id):
        raise Refused("inherited lock path is not the canonical path for this board")
    # The owning process wrote metadata through this exact open file
    # description, so the shared offset is at EOF.  A freshly opened fd to the
    # same inode starts at offset 0; rejecting that makes a copied fd number
    # insufficient.  (The fd number alone is never accepted.)
    try:
        offset = os.lseek(fd, 0, os.SEEK_CUR)
    except OSError:
        raise Refused("inherited lock fd has no readable offset") from None
    if opened.st_size <= 0 or offset != opened.st_size:
        raise Refused(
            "inherited lock fd is not the parent's capability "
            "(offset does not match the lock metadata)"
        )
    metadata = _read_metadata(fd)
    if metadata.get("schema") != LOCK_PROTOCOL or metadata.get("board_id") != board_id:
        raise Refused("inherited lock metadata does not match the board")
    if expected_lock_id is not None and metadata.get("lock_id") != expected_lock_id:
        raise Refused("inherited lock id does not match the parent capability")

    # Prove the passed fd itself is the holder's open file description, not a
    # fresh open that merely copied metadata/offset.  Order matters:
    #   1. an independent probe must FAIL to acquire the lock (a holder exists);
    #   2. flock(fd) must SUCCEED (the fd shares the holder's OFD, not a new one);
    #   3. a second independent probe must still fail (we did not create a new
    #      lock and no one released it in between).
    # A plain os.open + lseek(SEEK_END) + copied lock_id cannot pass step 2.
    probes: list[int] = []
    try:
        for _ in range(2):
            probe = os.open(
                path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            )
            probes.append(probe)
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise Refused(
                    "no live holder for the inherited lock fd; refusing a forged capability"
                )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused(
                "inherited fd does not share the held lock "
                "(a fresh open cannot be used as a capability)"
            ) from None
        except OSError as exc:
            raise Refused(
                f"cannot verify inherited fd ownership: {exc.strerror}"
            ) from None
        # The second probe already ran before flock(fd); run one after to prove
        # the accepted fd did not merely acquire a brand-new lock.
        after = os.open(
            path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        )
        probes.append(after)
        try:
            fcntl.flock(after, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise Refused("lock disappeared after adopting inherited fd")
    except Refused:
        raise
    except OSError as exc:
        raise Refused(
            f"cannot verify inherited lock holder: {exc.strerror}"
        ) from None
    finally:
        for probe in probes:
            try:
                os.close(probe)
            except OSError:
                pass

    return HeldLock(
        board_id=board_id,
        path=path,
        fd=fd,
        acquired_at=str(metadata.get("acquired_at") or ""),
        lock_id=str(metadata.get("lock_id") or ""),
        inherited=True,
    )


def adopt_from_environment() -> HeldLock:
    """Adopt the fd/path/board triple placed in the environment by a parent."""
    try:
        fd = int(os.environ[LOCK_ENV_FD])
        path = os.environ[LOCK_ENV_PATH]
        board_id = os.environ[LOCK_ENV_BOARD]
    except (KeyError, ValueError):
        raise Refused("no inherited board lock capability in the environment") from None
    return adopt_inherited_lock(
        fd,
        path,
        board_id,
        expected_lock_id=os.environ.get(LOCK_ENV_ID),
    )


def close_inherited(held: HeldLock) -> None:
    """Close a child's copy of an inherited fd without releasing the lock."""
    try:
        os.close(held.fd)
    except OSError:
        pass
