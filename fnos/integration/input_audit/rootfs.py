"""Read-only filesystem views.

Backends never mount an image.  ext4 is read through ``debugfs``; directory
fixtures are read through an in-process chroot-like resolver; squashfs uses
``unsquashfs -cat``.  Anything unsupported is reported as a gap.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Iterator

from .image import extract_range

MAX_PATH = 1024
ALLOWED_TOOLS = {"debugfs", "dumpe2fs", "unsquashfs", "btrfs", "zstd"}
FORBIDDEN_TOOLS = {"mount", "umount", "losetup", "sudo", "systemctl", "service", "sh", "bash", "chroot"}


class RootFSError(RuntimeError):
    pass


class PathRefused(ValueError):
    pass


class TooLarge(RootFSError):
    pass


class ToolMissing(RootFSError):
    pass


def validate_image_path(path: str) -> str:
    if not isinstance(path, str):
        raise PathRefused("path must be a string")
    if not path.startswith("/"):
        raise PathRefused("path must be absolute inside the image")
    if len(path) > MAX_PATH:
        raise PathRefused("path too long")
    if any(c in path for c in "\x00\n\r\"\\"):
        raise PathRefused("control/quote characters are not accepted")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise PathRefused("parent traversal is not accepted")
    return "/" + "/".join(parts)


class ToolRunner:
    """Small allow-listed subprocess wrapper with an audit record."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(self, argv: list[str], timeout: int = 180, binary: bool = False):
        tool = Path(argv[0]).name
        if tool in FORBIDDEN_TOOLS:
            raise RootFSError(f"forbidden host tool: {tool}")
        if tool not in ALLOWED_TOOLS:
            raise RootFSError(f"host tool not allow-listed: {tool}")
        self.commands.append(list(argv))
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise ToolMissing(f"{tool} not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise RootFSError(f"{tool} timed out after {timeout}s") from exc
        if binary:
            return proc.returncode, proc.stdout, proc.stderr
        return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


@dataclass
class Entry:
    name: str
    path: str
    mode: int
    size: int
    is_dir: bool
    is_symlink: bool


class RootFS:
    fstype: str
    backend: str

    def exists(self, path: str) -> bool:
        raise NotImplementedError

    def is_dir(self, path: str) -> bool:
        raise NotImplementedError

    def readlink(self, path: str) -> str | None:
        raise NotImplementedError

    def read_bytes(self, path: str, max_bytes: int = 8 * 1024 * 1024) -> bytes | None:
        raise NotImplementedError

    def listdir(self, path: str) -> list[Entry]:
        raise NotImplementedError

    def stat(self, path: str) -> Entry | None:
        raise NotImplementedError

    def walk(self, path: str, max_depth: int = 3, max_entries: int = 5000) -> Iterator[Entry]:
        base = validate_image_path(path)
        queue = [(base, 0)]
        seen = 0
        while queue:
            current, depth = queue.pop(0)
            try:
                entries = self.listdir(current)
            except RootFSError:
                continue
            for entry in sorted(entries, key=lambda e: e.name):
                seen += 1
                if seen > max_entries:
                    return
                yield entry
                if entry.is_dir and depth + 1 < max_depth:
                    queue.append((entry.path, depth + 1))

    def read_text(self, path: str, max_bytes: int = 1024 * 1024) -> str | None:
        if any(c in path for c in "\x00\n\r"):
            raise PathRefused("control characters in path")
        data = self.read_bytes(path, max_bytes=max_bytes)
        if data is None:
            return None
        if b"\x00" in data[:4096]:
            return None
        return data.decode("utf-8", "replace")


LS_RE = re.compile(r"^/(\d+)/([0-7]+)/(\d+)/(\d+)/(.*)/(\d+)/$")


class Ext4Backend(RootFS):
    """ext4 view via ``debugfs`` on a private partition copy."""

    backend = "debugfs"

    def __init__(
        self,
        image_path: Path,
        offset: int,
        size: int,
        work_dir: Path,
        runner: ToolRunner,
        partition_index: int,
        fstype: str = "ext4",
    ) -> None:
        self.source_image = image_path
        self.offset = offset
        self.size = size
        self.work_dir = work_dir
        self.runner = runner
        self.partition_index = partition_index
        self.fstype = fstype
        self.extracted: dict | None = None
        self._ls_cache: dict[str, list[Entry]] = {}
        self._stat_cache: dict[str, Entry | None] = {}

    def _image(self) -> Path:
        if self.extracted is None:
            dest = self.work_dir / f"rootfs-p{self.partition_index}.ext4"
            if dest.exists() and dest.stat().st_size == self.size:
                import hashlib

                h = hashlib.sha256()
                with open(dest, "rb") as f:
                    for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
                        h.update(block)
                self.extracted = {"path": str(dest), "size": dest.stat().st_size, "sha256": h.hexdigest(), "reused": True}
            else:
                self.extracted = extract_range(self.source_image, self.offset, self.size, dest)
                self.extracted["reused"] = False
        return Path(self.extracted["path"])

    def _cmd(self, command: str) -> tuple[int, str, str]:
        return self.runner.run(["debugfs", "-R", command, str(self._image())])

    def exists(self, path: str) -> bool:
        return self.stat(path) is not None

    def is_dir(self, path: str) -> bool:
        entry = self.stat(path)
        return bool(entry and entry.is_dir)

    def stat(self, path: str) -> Entry | None:
        path = validate_image_path(path)
        if path in self._stat_cache:
            return self._stat_cache[path]
        rc, out, err = self._cmd(f'stat "{path}"')
        if rc != 0 or "Inode:" not in out:
            self._stat_cache[path] = None
            return None
        type_match = re.search(r"Type:\s+(\S+)", out)
        kind = type_match.group(1) if type_match else ""
        size_match = re.search(r"Size:\s+(\d+)", out)
        size = int(size_match.group(1)) if size_match else 0
        mode_match = re.search(r"Mode:\s+([0-7]+)", out)
        mode = int(mode_match.group(1), 8) if mode_match else 0
        is_dir = kind == "directory"
        is_symlink = kind == "symlink"
        entry = Entry(os.path.basename(path) or "/", path, mode, size, is_dir, is_symlink)
        self._stat_cache[path] = entry
        return entry

    def readlink(self, path: str) -> str | None:
        path = validate_image_path(path)
        entry = self.stat(path)
        if not entry or not entry.is_symlink:
            return None
        rc, out, err = self._cmd(f'stat "{path}"')
        m = re.search(r'(?:Fast link dest|Symlink dest):\s+"([^"]*)"', out)
        if m:
            return m.group(1)
        # Long symlink: debugfs prints the target with "cat" in some versions.
        rc, out, err = self._cmd(f'cat "{path}"')
        if rc == 0 and out:
            target = out.splitlines()[0].strip()
            if target:
                return target
        return None

    def read_bytes(self, path: str, max_bytes: int = 8 * 1024 * 1024) -> bytes | None:
        path = validate_image_path(path)
        entry = self.stat(path)
        if entry is None or entry.is_dir:
            return None
        if entry.size > max_bytes:
            raise TooLarge(f"{path} is {entry.size} bytes (limit {max_bytes})")
        rc, out, err = self.runner.run(["debugfs", "-R", f'cat "{path}"', str(self._image())], binary=True)
        if rc != 0 or b"File not found" in err:
            return None
        return out

    def listdir(self, path: str) -> list[Entry]:
        path = validate_image_path(path)
        if path in self._ls_cache:
            return self._ls_cache[path]
        rc, out, err = self._cmd(f'ls -p "{path}"')
        if rc != 0 or "File not found" in err:
            return []
        entries: list[Entry] = []
        for line in out.splitlines():
            m = LS_RE.match(line.strip())
            if not m:
                continue
            _inode, mode_s, _uid, _gid, name, size_s = m.groups()
            if name in (".", ".."):
                continue
            mode = int(mode_s, 8)
            child = path.rstrip("/") + "/" + name
            entries.append(
                Entry(
                    name=name,
                    path=child,
                    mode=mode,
                    size=int(size_s),
                    is_dir=(mode & 0o170000) == 0o040000,
                    is_symlink=(mode & 0o170000) == 0o120000,
                )
            )
        self._ls_cache[path] = entries
        return entries


class DirectoryBackend(RootFS):
    """Chroot-like view of a host directory, for fixtures and tests."""

    backend = "directory"

    def __init__(self, root: Path, fstype: str = "fixture") -> None:
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise RootFSError(f"not a directory: {root}")
        self.fstype = fstype

    def _resolve(self, path: str, follow_last: bool = True) -> Path:
        path = validate_image_path(path)
        pending = [p for p in path.split("/") if p and p != "."]
        resolved: list[str] = []
        hops = 0
        while pending:
            part = pending.pop(0)
            if part == "..":
                if resolved:
                    resolved.pop()
                continue
            candidate = self.root.joinpath(*resolved, part)
            is_last = not pending
            if candidate.is_symlink() and (follow_last or not is_last):
                hops += 1
                if hops > 40:
                    raise PathRefused("symlink loop")
                target = os.readlink(candidate)
                if target.startswith("/"):
                    resolved = []
                pending = [p for p in target.split("/") if p and p != "."] + pending
                continue
            resolved.append(part)
        host = self.root.joinpath(*resolved)
        try:
            host.relative_to(self.root)
        except ValueError as exc:
            raise PathRefused("path escapes fixture root") from exc
        return host

    def _lstat(self, path: str) -> Path:
        return self._resolve(path, follow_last=False)

    def stat(self, path: str) -> Entry | None:
        try:
            host = self._lstat(path)
            st = host.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return None
        mode = st.st_mode
        return Entry(
            name=os.path.basename(path) or "/",
            path=validate_image_path(path),
            mode=mode,
            size=st.st_size,
            is_dir=(mode & 0o170000) == 0o040000,
            is_symlink=(mode & 0o170000) == 0o120000,
        )

    def exists(self, path: str) -> bool:
        return self.stat(path) is not None

    def is_dir(self, path: str) -> bool:
        entry = self.stat(path)
        return bool(entry and entry.is_dir)

    def readlink(self, path: str) -> str | None:
        try:
            host = self._lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return None
        return os.readlink(host) if os.path.islink(host) else None

    def read_bytes(self, path: str, max_bytes: int = 8 * 1024 * 1024) -> bytes | None:
        try:
            host = self._resolve(path, follow_last=True)
            if not host.is_file():
                return None
            if host.stat().st_size > max_bytes:
                raise TooLarge(f"{path} is {host.stat().st_size} bytes (limit {max_bytes})")
            return host.read_bytes()
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            return None

    def listdir(self, path: str) -> list[Entry]:
        try:
            host = self._resolve(path, follow_last=True)
        except (FileNotFoundError, NotADirectoryError):
            return []
        if not host.is_dir():
            return []
        entries: list[Entry] = []
        for name in os.listdir(host):
            child = path.rstrip("/") + "/" + name
            try:
                entry = self.stat(child)
            except PathRefused:
                # A filename with a newline/quote/backslash cannot be passed
                # safely through any backend; skip it instead of aborting.
                continue
            if entry is not None:
                entries.append(entry)
        return entries


class BtrfsBackend(RootFS):
    """btrfs view via ``btrfs restore`` into a private directory (no mount)."""

    backend = "btrfs-restore"

    def __init__(
        self,
        image_path: Path,
        offset: int,
        size: int,
        work_dir: Path,
        runner: ToolRunner,
        partition_index: int,
    ) -> None:
        self.source_image = image_path
        self.offset = offset
        self.size = size
        self.work_dir = work_dir
        self.runner = runner
        self.partition_index = partition_index
        self.fstype = "btrfs"
        self.restore_info: dict = {}
        tool = shutil.which("btrfs")
        if tool is None:
            raise ToolMissing("btrfs not in PATH; provide a private btrfs-progs binary (no sudo install)")
        self.tool = tool
        self._view: DirectoryBackend | None = None

    def _ensure(self) -> DirectoryBackend:
        if self._view is not None:
            return self._view
        part = self.work_dir / f"rootfs-p{self.partition_index}.btrfs"
        if not part.exists() or part.stat().st_size != self.size:
            self.restore_info["partition"] = extract_range(self.source_image, self.offset, self.size, part)
        else:
            self.restore_info["partition"] = {"path": str(part), "size": part.stat().st_size, "reused": True}
        dest = self.work_dir / f"btrfs-root-p{self.partition_index}"
        marker = dest / ".fnos-audit-restore-ok"
        if not marker.exists():
            work_root = self.work_dir.resolve()
            dest_resolved = dest.resolve() if dest.exists() else dest
            try:
                dest_resolved.relative_to(work_root)
            except ValueError as exc:
                raise RootFSError("refusing to clean a btrfs restore path outside the work dir") from exc
            if dest.exists():
                shutil.rmtree(dest)
            dest.mkdir(parents=True)
            rc, out, err = self.runner.run(
                [self.tool, "restore", "-i", "-S", str(part), str(dest)],
                timeout=900,
            )
            self.restore_info.update(
                {
                    "dest": str(dest),
                    "rc": rc,
                    "stderr_tail": "\n".join(err.splitlines()[-20:]),
                    "partial_possible": bool(err.strip()),
                }
            )
            if not (dest / "etc").exists() and not (dest / "usr").exists():
                raise RootFSError(f"btrfs restore produced no root tree: {err[-500:]}")
            marker.write_text("ok\n", encoding="utf-8")
        else:
            self.restore_info.update({"dest": str(dest), "rc": 0, "reused": True})
        self._view = DirectoryBackend(dest, fstype="btrfs")
        return self._view

    def exists(self, path: str) -> bool:
        return self._ensure().exists(path)

    def is_dir(self, path: str) -> bool:
        return self._ensure().is_dir(path)

    def readlink(self, path: str) -> str | None:
        return self._ensure().readlink(path)

    def read_bytes(self, path: str, max_bytes: int = 8 * 1024 * 1024) -> bytes | None:
        return self._ensure().read_bytes(path, max_bytes=max_bytes)

    def listdir(self, path: str) -> list[Entry]:
        return self._ensure().listdir(path)

    def stat(self, path: str) -> Entry | None:
        return self._ensure().stat(path)


class SquashfsBackend(RootFS):
    """squashfs view using ``unsquashfs`` (no extraction, no mount)."""

    backend = "unsquashfs"

    def __init__(self, image_path: Path, offset: int, runner: ToolRunner) -> None:
        self.image_path = image_path
        self.offset = offset
        self.runner = runner
        self.fstype = "squashfs"
        self.files: dict[str, int] = {}
        self.dirs: set[str] = {"/"}
        self.links: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        rc, out, err = self.runner.run(
            ["unsquashfs", "-l", "-offset", str(self.offset), str(self.image_path)]
        )
        if rc != 0:
            raise RootFSError(f"unsquashfs failed: {err[:300]}")
        prefix = "squashfs-root"
        for line in out.splitlines():
            line = line.rstrip()
            if not line:
                continue
            path = line
            if path.startswith(prefix):
                path = path[len(prefix) :]
            if " -> " in path:
                path, target = path.split(" -> ", 1)
                self.links[path] = target
            path = "/" + path.strip("/")
            parent = path.rsplit("/", 1)[0] or "/"
            while True:
                self.dirs.add(parent)
                if parent == "/":
                    break
                parent = parent.rsplit("/", 1)[0] or "/"
            if path not in self.links:
                self.files[path] = 0

    def stat(self, path: str) -> Entry | None:
        path = validate_image_path(path)
        if path in self.links:
            return Entry(os.path.basename(path), path, 0o120777, 0, False, True)
        if path in self.dirs:
            return Entry(os.path.basename(path) or "/", path, 0o040755, 0, True, False)
        if path in self.files:
            return Entry(os.path.basename(path), path, 0o100644, 0, False, False)
        return None

    def exists(self, path: str) -> bool:
        return self.stat(path) is not None

    def is_dir(self, path: str) -> bool:
        entry = self.stat(path)
        return bool(entry and entry.is_dir)

    def readlink(self, path: str) -> str | None:
        return self.links.get(validate_image_path(path))

    def read_bytes(self, path: str, max_bytes: int = 8 * 1024 * 1024) -> bytes | None:
        path = validate_image_path(path)
        rc, out, err = self.runner.run(
            ["unsquashfs", "-cat", "-offset", str(self.offset), str(self.image_path), path.lstrip("/")],
            binary=True,
        )
        if rc != 0:
            return None
        if len(out) > max_bytes:
            raise TooLarge(f"{path} exceeds {max_bytes} bytes")
        return out

    def listdir(self, path: str) -> list[Entry]:
        path = validate_image_path(path)
        prefix = path.rstrip("/") + "/"
        names: set[str] = set()
        for candidate in list(self.files) + list(self.links) + list(self.dirs):
            if candidate.startswith(prefix):
                rest = candidate[len(prefix) :]
                if rest and "/" not in rest:
                    names.add(rest)
        result = []
        for name in sorted(names):
            entry = self.stat(prefix + name)
            if entry:
                result.append(entry)
        return result
