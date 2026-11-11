"""Flashing backends and the write/full-read-back transaction.

Two implementations share one contract:

* ``FileFlashBackend`` writes to an ordinary temporary file.  It is the only
  backend used by offline tests and by ``--backends fake``;
* ``MacDiskutilBackend`` performs read-only ``diskutil`` identity queries and,
  behind the real-hardware gate plus root, raw ``/dev/rdiskN`` writes.

Every round re-identifies the whole external USB target (device, serial,
model, exact byte capacity, protocol, topology, mount state).  A target that
changes, a mounted/internal disk, a short read, or a full-length hash mismatch
stops the transaction without a cold boot.  No ``/dev`` path is opened by the
file backend or by any test.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import plistlib
import stat
import subprocess
from typing import Any, Callable, Protocol

from .authorization import require_real_gate
from .errors import HardwareFailure, Refused
from .util import CHUNK, read_json

IO_CHUNK = 4 * 1024 * 1024


class FlashFailure(HardwareFailure):
    def __init__(self, code: str, message: str, *, manual_recovery: bool = False):
        super().__init__(code, message, manual_recovery=manual_recovery)


@dataclass(frozen=True)
class TargetIdentity:
    device: str
    serial: str | None
    model: str | None
    size_bytes: int
    bus: str
    whole_disk: bool
    internal: bool
    mounted: bool
    read_only: bool
    virtual: bool
    topology: str | None = None
    mount_points: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "serial": self.serial,
            "model": self.model,
            "size_bytes": self.size_bytes,
            "bus": self.bus,
            "whole_disk": self.whole_disk,
            "internal": self.internal,
            "mounted": self.mounted,
            "read_only": self.read_only,
            "virtual": self.virtual,
            "topology": self.topology,
            "mount_points": list(self.mount_points),
        }


@dataclass(frozen=True)
class ImageHandle:
    path: Path
    fd: int
    sha256: str
    size_bytes: int
    mtime_ns: int

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


def parse_identity(raw: dict[str, Any]) -> TargetIdentity:
    if not isinstance(raw, dict):
        raise Refused("target identity must be a JSON object")
    device = raw.get("device")
    if not isinstance(device, str) or not device.startswith("/dev/"):
        raise Refused("target identity has no absolute /dev device")
    size = raw.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise Refused("target identity has no positive byte size")
    mounts = raw.get("mount_points") or []
    if not isinstance(mounts, list) or not all(isinstance(x, str) for x in mounts):
        raise Refused("target mount_points must be strings")
    return TargetIdentity(
        device=device,
        serial=raw.get("serial") if isinstance(raw.get("serial"), str) else None,
        model=raw.get("model") if isinstance(raw.get("model"), str) else None,
        size_bytes=size,
        bus=str(raw.get("bus") or "unknown"),
        whole_disk=raw.get("whole_disk") is True,
        internal=raw.get("internal") is not False,
        mounted=bool(mounts) or raw.get("mounted") is True,
        read_only=raw.get("read_only") is True,
        virtual=raw.get("virtual") is True,
        topology=raw.get("topology") if isinstance(raw.get("topology"), str) else None,
        mount_points=tuple(mounts),
    )


def expected_identity(plan: dict[str, Any]) -> TargetIdentity:
    target = plan.get("target") or {}
    pre = plan.get("preconditions") or {}
    if not isinstance(target, dict):
        raise Refused("plan target is missing")
    return TargetIdentity(
        device=str(target.get("device")),
        serial=target.get("serial"),
        model=target.get("model"),
        size_bytes=int(target.get("size_bytes") or 0),
        bus=str(target.get("bus") or "unknown"),
        whole_disk=pre.get("whole_disk") is True,
        # The plan preconditions store these as "must be false"; missing is
        # treated as unsafe/unknown and therefore rejected by compare_identity.
        internal=pre.get("internal_or_system_disk", True) is not False,
        mounted=pre.get("mounted", True) is not False,
        read_only=pre.get("read_only", True) is not False,
        virtual=False,
        topology=target.get("topology"),
    )



def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def compare_identity(actual: TargetIdentity, expected: TargetIdentity) -> None:
    problems: list[str] = []
    if actual.device != expected.device:
        problems.append(f"device changed ({expected.device} -> {actual.device})")
    if actual.serial != expected.serial:
        problems.append("serial changed")
    if _norm(actual.model) != _norm(expected.model):
        problems.append("model/media name changed")
    if actual.size_bytes != expected.size_bytes:
        problems.append("byte capacity changed")
    if _norm(actual.bus) != _norm(expected.bus):
        problems.append("bus protocol changed")
    if actual.topology != expected.topology:
        problems.append("topology changed")
    if not actual.whole_disk:
        problems.append("not a whole disk")
    if actual.internal:
        problems.append("internal/system disk")
    if actual.mounted or actual.mount_points:
        problems.append("target or a child volume is mounted")
    if actual.read_only:
        problems.append("target is read-only")
    if actual.virtual:
        problems.append("virtual media is not a stable physical target")
    if not actual.serial:
        problems.append("serial number is missing")
    if not actual.model:
        problems.append("model/media name is missing")
    if problems:
        raise FlashFailure(
            "target_changed",
            "target identity is not the reviewed target: " + "; ".join(problems),
        )


def open_trusted_image(plan: dict[str, Any]) -> ImageHandle:
    """Open and hash the exact image file, proving it did not change."""
    image = plan.get("image") or {}
    path = image.get("path")
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise Refused("plan image path is missing or not absolute")
    image_path = Path(path)
    if image_path.is_symlink():
        raise Refused("image path must not be a symlink")
    try:
        fd = os.open(image_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise Refused(f"cannot open image: {exc.strerror}") from None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise Refused("image is not a regular file")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(fd)
    except Refused:
        os.close(fd)
        raise
    except OSError as exc:
        os.close(fd)
        raise Refused(f"cannot read image: {exc.strerror}") from None
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        os.close(fd)
        raise Refused("image changed while it was being hashed")
    if before.st_nlink != 1:
        os.close(fd)
        raise Refused("image has extra hard links; refusing an ambiguous source")
    try:
        path_stat = os.stat(image_path, follow_symlinks=False)
    except OSError:
        os.close(fd)
        raise Refused("image path disappeared while it was being hashed") from None
    if (path_stat.st_dev, path_stat.st_ino) != (after.st_dev, after.st_ino):
        os.close(fd)
        raise Refused("image path was replaced while it was being hashed")
    if total != after.st_size:
        os.close(fd)
        raise Refused("image read length does not match its stat size")
    if total != image.get("size_bytes"):
        os.close(fd)
        raise Refused("image size changed after the plan was created")
    if digest.hexdigest() != image.get("sha256"):
        os.close(fd)
        raise Refused("image SHA-256 changed after the plan was created")
    return ImageHandle(
        path=image_path,
        fd=fd,
        sha256=digest.hexdigest(),
        size_bytes=total,
        mtime_ns=after.st_mtime_ns,
    )


def read_exact(fd: int, offset: int, length: int) -> bytes:
    result = bytearray()
    while len(result) < length:
        chunk = os.pread(fd, length - len(result), offset + len(result))
        if not chunk:
            raise FlashFailure(
                "short_io", f"short read at byte {offset + len(result)}"
            )
        result.extend(chunk)
    return bytes(result)


def write_exact(fd: int, offset: int, data: bytes) -> None:
    view = memoryview(data)
    written = 0
    while view:
        count = os.pwrite(fd, view, offset + written)
        if count <= 0:
            raise FlashFailure("short_io", f"short write at byte {offset + written}")
        written += count
        view = view[count:]


class FlashBackend(Protocol):
    name: str

    def identify(self) -> TargetIdentity: ...

    def open_target(self) -> int: ...

    def close_target(self, fd: int) -> None: ...

    def write(self, fd: int, offset: int, data: bytes) -> None: ...

    def read(self, fd: int, offset: int, length: int) -> bytes: ...

    def flush(self, fd: int) -> None: ...

    def sync(self) -> None: ...


class FileFlashBackend:
    """Regular-file substitute.  It never accepts a ``/dev`` path."""

    name = "file"

    def __init__(
        self,
        target_path: Path,
        *,
        identity: TargetIdentity | dict[str, Any] | None = None,
        metadata_path: Path | None = None,
        write_hook: Callable[[int], None] | None = None,
        read_hook: Callable[[int], None] | None = None,
    ):
        self.target_path = Path(target_path)
        if str(self.target_path).startswith("/dev/"):
            raise Refused("file flash backend refuses a /dev path")
        self.metadata_path = Path(metadata_path) if metadata_path else None
        self.write_hook = write_hook
        self.read_hook = read_hook
        if identity is None and self.metadata_path is None:
            raise Refused("file flash backend needs an identity or metadata_path")
        self._identity = (
            identity if isinstance(identity, TargetIdentity) else parse_identity(identity)
            if isinstance(identity, dict)
            else identity
        )

    def identify(self) -> TargetIdentity:
        if self.metadata_path is not None:
            raw = read_json(self.metadata_path)
            return parse_identity(raw)
        if self._identity is None:
            raise Refused("file flash backend has no identity")
        return self._identity

    def open_target(self) -> int:
        if self.target_path.is_symlink():
            raise Refused("file target must not be a symlink")
        try:
            fd = os.open(
                self.target_path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
            )
        except OSError as exc:
            raise FlashFailure("target_open_failed", f"cannot open target: {exc.strerror}") from None
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            raise Refused("file target is not a regular file")
        return fd

    def close_target(self, fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass

    def write(self, fd: int, offset: int, data: bytes) -> None:
        write_exact(fd, offset, data)
        if self.write_hook is not None:
            self.write_hook(offset + len(data))

    def read(self, fd: int, offset: int, length: int) -> bytes:
        data = read_exact(fd, offset, length)
        if self.read_hook is not None:
            self.read_hook(offset + len(data))
        return data

    def flush(self, fd: int) -> None:
        os.fsync(fd)

    def sync(self) -> None:
        return None


class MacDiskutilBackend:
    """Real macOS USB whole-disk backend.  Not exercised in this batch."""

    name = "mac-diskutil"

    def __init__(self, device: str):
        require_real_gate()
        if not isinstance(device, str) or not device.startswith("/dev/disk"):
            raise Refused("real backend needs a whole /dev/diskN device")
        self.device = device

    @staticmethod
    def _disk_info(device: str) -> dict[str, Any]:
        try:
            proc = subprocess.run(
                ["diskutil", "info", "-plist", device],
                capture_output=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}
        if proc.returncode != 0:
            return {}
        try:
            value = plistlib.loads(proc.stdout)
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _mount_points(device: str) -> tuple[str, ...]:
        try:
            proc = subprocess.run(
                ["diskutil", "list", "-plist", device],
                capture_output=True,
                check=False,
                timeout=15,
            )
            data = plistlib.loads(proc.stdout) if proc.returncode == 0 else {}
        except (OSError, subprocess.TimeoutExpired, plistlib.InvalidFileException):
            return ()
        disk_name = Path(device).name
        found: set[str] = set()

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, child in node.items():
                    if key in ("MountPoint", "MountPoints"):
                        if isinstance(child, str) and child:
                            found.add(child)
                        elif isinstance(child, list):
                            found.update(x for x in child if isinstance(x, str) and x)
                    else:
                        walk(child)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        if isinstance(data, dict):
            for disk in data.get("AllDisksAndPartitions") or []:
                if isinstance(disk, dict) and disk.get("DeviceIdentifier") == disk_name:
                    walk(disk)
        return tuple(sorted(found))

    def identify(self) -> TargetIdentity:
        info = self._disk_info(self.device)
        if not info:
            raise FlashFailure(
                "target_missing", "diskutil could not read the reviewed target"
            )
        mounts = self._mount_points(self.device)
        info_mount = info.get("MountPoint")
        if isinstance(info_mount, str) and info_mount:
            mounts = tuple(sorted(set(mounts) | {info_mount}))
        serial = info.get("SerialNumber") or info.get("MediaSerialNumber")
        model = info.get("MediaName") or info.get("DeviceModel")
        size = info.get("TotalSize")
        if not isinstance(size, int) or isinstance(size, bool):
            size = 0
        device_node = str(info.get("DeviceNode") or self.device)
        topology_id = Path(device_node).name
        return TargetIdentity(
            device=device_node,
            serial=serial if isinstance(serial, str) else None,
            model=model if isinstance(model, str) else None,
            size_bytes=size,
            bus=str(info.get("BusProtocol") or "unknown"),
            whole_disk=info.get("WholeDisk") is True or info.get("Whole") is True,
            internal=info.get("Internal") is not False,
            mounted=bool(mounts),
            read_only=bool(
                info.get("ReadOnlyMedia")
                or info.get("MediaReadOnly")
                or info.get("Writable") is False
            ),
            virtual=info.get("VirtualOrPhysical") == "Virtual",
            topology=f"{info.get('BusProtocol')}:{model}:{topology_id}",
            mount_points=mounts,
        )

    def open_target(self) -> int:
        if os.geteuid() != 0:
            raise Refused("real raw-device writes require root; no write was attempted")
        raw = self.device.replace("/dev/disk", "/dev/rdisk", 1)
        try:
            fd = os.open(raw, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise FlashFailure(
                "target_open_failed", f"cannot open raw target: {exc.strerror}"
            ) from None
        if not stat.S_ISCHR(os.fstat(fd).st_mode):
            os.close(fd)
            raise Refused("raw target is not a character device")
        return fd

    def close_target(self, fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass

    def write(self, fd: int, offset: int, data: bytes) -> None:
        write_exact(fd, offset, data)

    def read(self, fd: int, offset: int, length: int) -> bytes:
        return read_exact(fd, offset, length)

    def flush(self, fd: int) -> None:
        os.fsync(fd)

    def sync(self) -> None:
        try:
            subprocess.run(["sync"], check=False, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            pass


@dataclass
class WriteResult:
    bytes_written: int
    complete: bool
    cancelled: bool


def write_image(
    backend: FlashBackend,
    target_fd: int,
    image: ImageHandle,
    *,
    cancel: Callable[[], bool],
    on_progress: Callable[[int, int], None] | None = None,
    chunk: int = IO_CHUNK,
) -> WriteResult:
    """Write exact image bytes; cancellation stops only at a chunk boundary."""
    offset = 0
    while offset < image.size_bytes:
        if cancel():
            return WriteResult(offset, False, True)
        length = min(chunk, image.size_bytes - offset)
        data = read_exact(image.fd, offset, length)
        backend.write(target_fd, offset, data)
        offset += length
        if on_progress is not None:
            on_progress(offset, image.size_bytes)
    backend.flush(target_fd)
    backend.sync()
    return WriteResult(offset, offset == image.size_bytes, False)


def readback_image(
    backend: FlashBackend,
    target_fd: int,
    image: ImageHandle,
    *,
    cancel: Callable[[], bool],
    on_progress: Callable[[int, int], None] | None = None,
    chunk: int = IO_CHUNK,
) -> tuple[str, int, bool]:
    """Return ``(sha256, bytes_read, complete)``.  Cancellation never claims a pass."""
    digest = hashlib.sha256()
    offset = 0
    while offset < image.size_bytes:
        if cancel():
            return digest.hexdigest(), offset, False
        length = min(chunk, image.size_bytes - offset)
        data = backend.read(target_fd, offset, length)
        if len(data) != length:
            raise FlashFailure(
                "readback_short",
                f"read-back returned {len(data)} of {length} bytes at {offset}",
            )
        digest.update(data)
        offset += length
        if on_progress is not None:
            on_progress(offset, image.size_bytes)
    if cancel():
        # A cancel that arrives just after the last chunk is still a cancel;
        # never turn it into a verified pass.
        return digest.hexdigest(), offset, False
    return digest.hexdigest(), offset, True


def require_full_readback(
    actual_sha: str, image: ImageHandle, *, bytes_read: int, complete: bool
) -> None:
    if not complete:
        raise FlashFailure("readback_incomplete", "full-length read-back did not complete")
    if bytes_read != image.size_bytes:
        raise FlashFailure(
            "readback_short",
            f"read-back covered {bytes_read} bytes, expected {image.size_bytes}",
        )
    if actual_sha != image.sha256:
        raise FlashFailure(
            "hash_mismatch",
            "full read-back SHA-256 differs from the trusted image; boot is blocked",
        )

