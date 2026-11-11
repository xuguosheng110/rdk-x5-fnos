"""Small standard-library helpers shared by the lab commands."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .errors import Refused

CHUNK = 1024 * 1024


def utc_now() -> str:
    """Return a compact, timezone-explicit timestamp."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json_bytes(value: Any) -> bytes:
    """Deterministic JSON used for integrity signatures, never for logs."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> tuple[str, int]:
    """Hash a regular file and return ``(hexdigest, bytes_read)``."""
    digest = hashlib.sha256()
    total = 0
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def secure_dir(path: Path) -> Path:
    """Create a private directory, refusing a symlinked directory path."""
    path = Path(path).expanduser()
    if path.is_symlink():
        raise Refused(f"private directory must not be a symlink: {path}")
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise Refused(f"cannot create private directory {path}: {exc.strerror}") from None
    if not path.is_dir():
        raise Refused(f"private path is not a directory: {path}")
    try:
        path.chmod(0o700)
    except OSError:
        # Best effort; the caller still gets a clear error if writing fails.
        pass
    return path


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    mode: int = 0o600,
    overwrite: bool = True,
) -> None:
    """Write ``data`` atomically in the target directory.

    A symlink at the destination is refused rather than replaced.  With
    ``overwrite=False`` an existing path is never replaced: the temporary file
    is hard-linked into place, so a concurrent creator can win only once.
    """
    path = Path(path)
    if path.is_symlink():
        raise Refused(f"refusing to write through a symlink: {path}")
    parent = path.parent
    if not parent.is_dir():
        raise Refused(f"destination directory does not exist: {parent}")
    if not overwrite and path.exists():
        raise Refused(f"refusing to overwrite an existing path: {path}")

    fd, temp_name = tempfile.mkstemp(prefix=".fnos-lab-", dir=str(parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temp_name, path)
        else:
            try:
                os.link(temp_name, path)
            except FileExistsError:
                raise Refused(f"refusing to overwrite an existing path: {path}") from None
            except OSError as exc:
                raise Refused(
                    f"cannot create {path} without overwriting: {exc.strerror}"
                ) from None
        _fsync_dir(parent)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def atomic_write_json(
    path: Path,
    value: Any,
    *,
    mode: int = 0o600,
    overwrite: bool = True,
) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    atomic_write_bytes(
        Path(path),
        (payload + "\n").encode("utf-8"),
        mode=mode,
        overwrite=overwrite,
    )


def read_json(path: Path, *, max_bytes: int = 4 * 1024 * 1024) -> Any:
    """Read a JSON file without following a symlink."""
    path = Path(path)
    if path.is_symlink():
        raise Refused(f"refusing to read a symlinked JSON file: {path}")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise Refused(f"cannot open JSON file {path}: {exc.strerror}") from None
    try:
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(max_bytes + 1)
    except OSError as exc:
        raise Refused(f"cannot read JSON file {path}: {exc.strerror}") from None
    if len(raw) > max_bytes:
        raise Refused(f"JSON file is unexpectedly large: {path}")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refused(f"invalid JSON in {path}: {type(exc).__name__}") from None


def regular_path(path: Path, *, label: str) -> Path:
    """Resolve an absolute regular file, rejecting symlinks and directories."""
    path = Path(path)
    if not path.is_absolute():
        raise Refused(f"{label} must be an absolute path: {path}")
    if path.is_symlink():
        raise Refused(f"{label} must not be a symlink: {path}")
    if not path.exists():
        raise Refused(f"{label} does not exist: {path}")
    if not path.is_file():
        raise Refused(f"{label} is not a regular file: {path}")
    return path


def image_fingerprint(path: Path) -> dict[str, Any]:
    """Hash an image once and prove its identity did not change during the read."""
    path = regular_path(path, label="image")
    before = path.stat()
    digest, size = sha256_file(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise Refused(f"image changed while it was being hashed: {path}")
    if size != after.st_size:
        raise Refused(f"image size read {size} does not match stat {after.st_size}: {path}")
    return {
        "path": str(path),
        "sha256": digest,
        "size_bytes": size,
        "mtime_ns": after.st_mtime_ns,
    }


def pid_alive(pid: int) -> bool:
    """Return whether ``pid`` currently exists (no permission side effects)."""
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
