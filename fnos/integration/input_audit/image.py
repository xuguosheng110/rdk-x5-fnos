"""Read-only image/partition probing without mounting.

The module only reads bytes, or writes a private copy under the caller's work
directory.  It never calls ``mount``, ``losetup``, ``sudo`` or an installer from
the image.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import bz2
import gzip
import hashlib
import json
import lzma
import os
from pathlib import Path
import shutil
import struct
import subprocess
import uuid
import sys
import time
import zlib

CHUNK = 8 * 1024 * 1024
SECTOR = 512

COMPRESSION_MAGIC = {
    b"\x1f\x8b": "gzip",
    b"\xfd7zXZ\x00": "xz",
    b"\x28\xb5\x2f\xfd": "zstd",
    b"BZh": "bzip2",
}

GPT_LINUX_FS = "0fc63daf-8483-4772-8e79-3d69d8477de4"
GPT_EFI = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
GPT_LINUX_LVM = "e6d6d379-f507-44c2-a23c-238f2a3df928"
GPT_LINUX_RAID = "a19d880f-05fc-4d3b-a006-743f0f84911e"
GPT_LINUX_SWAP = "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f"

GPT_TYPE_NAMES = {
    GPT_LINUX_FS: "linux-filesystem",
    GPT_EFI: "efi-system",
    GPT_LINUX_LVM: "linux-lvm",
    GPT_LINUX_RAID: "linux-raid",
    GPT_LINUX_SWAP: "linux-swap",
}

MBR_TYPE_NAMES = {
    0x01: "fat12",
    0x04: "fat16",
    0x06: "fat16",
    0x07: "ntfs/exfat",
    0x0B: "fat32",
    0x0C: "fat32",
    0x0E: "fat16",
    0x0F: "extended",
    0x82: "linux-swap",
    0x83: "linux",
    0x85: "linux-extended",
    0x8E: "linux-lvm",
    0xEE: "gpt-protective",
    0xEF: "efi-system",
}


class ImageError(RuntimeError):
    """Unsupported or malformed image input."""


@dataclass
class InputImage:
    source: Path
    size: int
    sha256: str
    kind: str
    image_path: Path
    decompressed_size: int
    decompressed_sha256: str
    commands: list[list[str]] = field(default_factory=list)
    reused_decompressed: bool = False

    def as_dict(self) -> dict:
        return {
            "path": str(self.source),
            "size": self.size,
            "sha256": self.sha256,
            "compression": self.kind,
            "decompressed": {
                "path": str(self.image_path),
                "size": self.decompressed_size,
                "sha256": self.decompressed_sha256,
                "reused": self.reused_decompressed,
            },
        }


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def detect_compression(path: Path, probe: int = 8) -> str:
    with open(path, "rb") as f:
        head = f.read(probe)
    for magic, name in COMPRESSION_MAGIC.items():
        if head.startswith(magic):
            return name
    return "raw"


def _read_at(f, offset: int, size: int) -> bytes:
    f.seek(offset)
    return f.read(size)


def _u32(data: bytes, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


def _u64(data: bytes, off: int) -> int:
    return struct.unpack_from("<Q", data, off)[0]


def _uuid_raw(raw: bytes) -> str:
    """Canonical UUID from a filesystem superblock (network/big-endian bytes).

    ext4 ``s_uuid`` and btrfs ``fsid`` are stored as the 16 bytes of the
    textual UUID, not as the mixed-endian GUID layout used by GPT entries.
    """
    if len(raw) != 16:
        return ""
    return str(uuid.UUID(bytes=raw))


def _uuid_gpt(raw: bytes) -> str:
    """Canonical UUID from GPT's mixed-endian 16-byte GUID layout."""
    if len(raw) != 16:
        return ""
    a, b, c = struct.unpack_from("<IHH", raw, 0)
    d = raw[8:10].hex()
    e = raw[10:16].hex()
    return f"{a:08x}-{b:04x}-{c:04x}-{d}-{e}"


def _decode_label(raw: bytes) -> str:
    raw = raw.split(b"\x00", 1)[0]
    return raw.decode("utf-8", "replace").strip()


def probe_filesystem(image_path: Path, offset: int, size: int) -> dict:
    """Identify a filesystem from magic values; never mounts it."""

    out: dict = {"type": "unknown", "offset": offset, "size": size}
    if size <= 0:
        out["type"] = "empty"
        return out
    with open(image_path, "rb") as f:
        head = _read_at(f, offset, min(size, 4096))
        if len(head) >= 4 and head[:4] == b"hsqs":
            out.update(type="squashfs", version=struct.unpack_from("<H", head, 4)[0] if len(head) >= 6 else None)
            return out
        if len(head) >= 6 and head[:6] == b"LUKS\xba\xbe":
            out.update(type="luks", note="encrypted; static audit cannot inspect")
            return out
        if len(head) >= 2 and head[0:2] == b"\x1f\x8b":
            out.update(type="gzip-compressed-filesystem", note="nested compression")
            return out

        # ext2/3/4 superblock at +1024
        if len(head) >= 2048:
            sb = _read_at(f, offset + 1024, 1024)
            if len(sb) >= 0x100 and struct.unpack_from("<H", sb, 0x38)[0] == 0xEF53:
                incompat = _u32(sb, 0x60)
                out.update(
                    type="ext4" if incompat & 0x40 or incompat & 0x80 else "ext2/3",
                    label=_decode_label(sb[0x78:0x88]),
                    uuid=_uuid_raw(sb[0x68:0x78]),
                    block_size=1024 << _u32(sb, 0x18),
                    inodes=_u32(sb, 0x00),
                    blocks=_u32(sb, 0x04),
                    state="clean" if struct.unpack_from("<H", sb, 0x3A)[0] == 1 else "unclean",
                    details={"compat": hex(_u32(sb, 0x5C)), "incompat": hex(incompat), "ro_compat": hex(_u32(sb, 0x64))},
                )
                return out

        # btrfs superblock at +0x10000
        if size > 0x10000 + 0x1000:
            btr = _read_at(f, offset + 0x10000, 0x1000)
            if len(btr) >= 0x1000 and btr[0x40:0x48] == b"_BHRfS_M":
                out.update(
                    type="btrfs",
                    label=_decode_label(btr[0x12B:0x22B]),
                    uuid=_uuid_raw(btr[0x20:0x30]),
                    total_bytes=_u64(btr, 0x70),
                    bytes_used=_u64(btr, 0x78),
                    sectorsize=_u32(btr, 0x90),
                    nodesize=_u32(btr, 0x94),
                )
                return out

        if head[:4] == b"XFSB":
            out.update(type="xfs")
            return out
        if len(head) >= 4 and struct.unpack_from("<I", head, 0)[0] == 0xF2F52010:
            out.update(type="f2fs")
            return out
        if len(head) >= 2048 and head[1024:1028] == b"\xe2\xe1\xf5\xe0":
            out.update(type="erofs")
            return out
        if len(head) >= 0x100 and head[0x1FE:0x200] == b"\x55\xaa":
            fat = head[0x36:0x3E]
            if fat.startswith(b"FAT"):
                out.update(type="vfat", label=_decode_label(head[0x47:0x52]))
                return out
            if head[0x52:0x57] == b"FAT32":
                out.update(type="vfat", label=_decode_label(head[0x47:0x52]))
                return out
        if len(head) >= 0x8006 and head[0x8001:0x8006] == b"CD001":
            out.update(type="iso9660")
            return out
    return out


def _crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _parse_gpt(f, image_path: Path) -> tuple[list[dict], dict]:
    header = _read_at(f, SECTOR, 512)
    if len(header) < 92 or header[:8] != b"EFI PART":
        raise ImageError("GPT signature missing")
    header_size = _u32(header, 12)
    if not 92 <= header_size <= 512:
        raise ImageError(f"invalid GPT header size {header_size}")
    stored_crc = _u32(header, 16)
    zeroed = bytearray(header[:header_size])
    zeroed[16:20] = b"\x00" * 4
    header_crc_ok = _crc32(bytes(zeroed)) == stored_crc
    entries_lba = _u64(header, 72)
    num_entries = _u32(header, 80)
    entry_size = _u32(header, 84)
    entries_crc = _u32(header, 88)
    if entry_size < 128 or entry_size > 4096 or num_entries > 1024:
        raise ImageError("implausible GPT entry table")
    raw = _read_at(f, entries_lba * SECTOR, num_entries * entry_size)
    if len(raw) != num_entries * entry_size:
        raise ImageError("truncated GPT entry table")
    entries_crc_ok = _crc32(raw) == entries_crc
    parts: list[dict] = []
    for index in range(num_entries):
        entry = raw[index * entry_size : (index + 1) * entry_size]
        type_guid = _uuid_gpt(entry[0:16])
        if type_guid == "00000000-0000-0000-0000-000000000000":
            continue
        unique_guid = _uuid_gpt(entry[16:32])
        if unique_guid == "00000000-0000-0000-0000-000000000000":
            unique_guid = None
        first = _u64(entry, 32)
        last = _u64(entry, 40)
        if last < first:
            continue
        name = entry[56:128].decode("utf-16-le", "replace").split("\x00", 1)[0]
        parts.append(
            {
                "index": index + 1,
                "start_lba": first,
                "last_lba": last,
                "start_bytes": first * SECTOR,
                "size_bytes": (last - first + 1) * SECTOR,
                "type_guid": type_guid,
                "unique_guid": unique_guid,
                "type": GPT_TYPE_NAMES.get(type_guid, "gpt-other"),
                "name": name,
                "attributes": _u64(entry, 48),
            }
        )
    meta = {
        "table": "gpt",
        "header_crc_ok": header_crc_ok,
        "entries_crc_ok": entries_crc_ok,
        "disk_guid": _uuid_gpt(header[56:72]),
        "first_usable_lba": _u64(header, 40),
        "last_usable_lba": _u64(header, 48),
    }
    return parts, meta


def _parse_mbr(f) -> tuple[list[dict], dict]:
    sector = _read_at(f, 0, 512)
    if len(sector) < 512 or sector[510:512] != b"\x55\xaa":
        return [], {"table": "none"}
    parts: list[dict] = []
    for i in range(4):
        off = 446 + i * 16
        status, ptype = sector[off], sector[off + 4]
        lba = struct.unpack_from("<I", sector, off + 8)[0]
        count = struct.unpack_from("<I", sector, off + 12)[0]
        if ptype == 0 or count == 0:
            continue
        parts.append(
            {
                "index": i + 1,
                "start_lba": lba,
                "last_lba": lba + count - 1,
                "start_bytes": lba * SECTOR,
                "size_bytes": count * SECTOR,
                "mbr_type": ptype,
                "type": MBR_TYPE_NAMES.get(ptype, f"mbr-0x{ptype:02x}"),
                "unique_guid": None,
                "bootable": status == 0x80,
                "name": "",
            }
        )
    return parts, {"table": "mbr"}


def parse_partitions(image_path: Path) -> dict:
    """Return GPT/MBR partition records with filesystem probes."""

    size = image_path.stat().st_size
    with open(image_path, "rb") as f:
        head = _read_at(f, 0, 512)
        parts: list[dict] = []
        meta: dict = {"table": "none"}
        if len(head) >= 512 and head[510:512] == b"\x55\xaa":
            is_gpt = any(head[446 + i * 16 + 4] == 0xEE for i in range(4))
            if is_gpt or (len(head) >= 520 and _read_at(f, SECTOR, 8) == b"EFI PART"):
                parts, meta = _parse_gpt(f, image_path)
                meta["protective_mbr"] = True
            else:
                parts, meta = _parse_mbr(f)
        elif len(head) >= 520 and _read_at(f, SECTOR, 8) == b"EFI PART":
            parts, meta = _parse_gpt(f, image_path)
            meta["protective_mbr"] = False
        else:
            fs = probe_filesystem(image_path, 0, size)
            meta = {"table": "none", "whole_image_filesystem": fs}
            return {
                "sector_size": SECTOR,
                "table": "none",
                "meta": meta,
                "partitions": [],
                "unpartitioned_bytes": size,
            }

    for p in parts:
        p["start_bytes"] = p["start_lba"] * SECTOR
        p["size_bytes"] = min(p["size_bytes"], max(0, size - p["start_bytes"]))
        p["filesystem"] = probe_filesystem(image_path, p["start_bytes"], p["size_bytes"])
    first_start = min((p["start_bytes"] for p in parts), default=size)
    return {
        "sector_size": SECTOR,
        "table": meta.get("table", "none"),
        "meta": meta,
        "partitions": parts,
        "pre_partition_bytes": first_start,
        "unpartitioned_bytes": max(0, size - max((p["start_bytes"] + p["size_bytes"] for p in parts), default=0)),
    }


def _decompress(src: Path, dst: Path, kind: str, commands: list[list[str]]) -> None:
    tmp = dst.with_suffix(dst.suffix + ".part")
    with open(tmp, "wb") as out:
        if kind == "gzip":
            with gzip.open(src, "rb") as f:
                shutil.copyfileobj(f, out, CHUNK)
        elif kind == "xz":
            with lzma.open(src, "rb") as f:
                shutil.copyfileobj(f, out, CHUNK)
        elif kind == "bzip2":
            with bz2.open(src, "rb") as f:
                shutil.copyfileobj(f, out, CHUNK)
        elif kind == "zstd":
            cmd = ["zstd", "-d", "-c", str(src)]
            commands.append(cmd)
            proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.PIPE)
            _, err = proc.communicate()
            if proc.returncode != 0:
                raise ImageError(f"zstd failed: {err.decode('utf-8', 'replace')[:500]}")
        else:
            raise ImageError(f"unsupported compression {kind}")
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, dst)


def prepare_image(source: Path, work_dir: Path, force: bool = False) -> InputImage:
    """Hash the original and materialise a raw image under ``work_dir``."""

    source = source.resolve()
    if not source.is_file():
        raise ImageError(f"not a regular file: {source}")
    work_dir.mkdir(parents=True, exist_ok=True)
    kind = detect_compression(source)
    size = source.stat().st_size
    digest = sha256_file(source)
    if kind == "raw":
        return InputImage(source, size, digest, kind, source, size, digest)

    safe_name = source.name.replace("/", "_")[:120]
    dst = work_dir / f"{safe_name}.{digest[:16]}.raw"
    sidecar = dst.with_suffix(dst.suffix + ".meta.json")
    if dst.exists() and sidecar.exists() and not force:
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
            if meta.get("source_sha256") == digest and dst.stat().st_size == meta.get("size"):
                return InputImage(
                    source, size, digest, kind, dst, meta["size"], meta["sha256"], reused_decompressed=True
                )
        except (ValueError, OSError):
            pass
    free = shutil.disk_usage(work_dir).free
    estimate = size * 3 + (2 << 30)
    if free < estimate:
        raise ImageError(f"insufficient work space: free={free} estimate={estimate}")
    print(f"[audit] decompressing {kind} {source.name} ({size} bytes) -> {dst}", file=sys.stderr, flush=True)
    started = time.time()
    commands: list[list[str]] = []
    _decompress(source, dst, kind, commands)
    dsize = dst.stat().st_size
    ddigest = sha256_file(dst)
    sidecar.write_text(
        json.dumps(
            {
                "source_sha256": digest,
                "source_size": size,
                "compression": kind,
                "size": dsize,
                "sha256": ddigest,
                "seconds": round(time.time() - started, 2),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return InputImage(source, size, digest, kind, dst, dsize, ddigest, commands=commands)


def extract_range(source: Path, offset: int, size: int, dest: Path) -> dict:
    """Copy a partition to a private file; used because debugfs has no offset option."""

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha256()
    written = 0
    with open(source, "rb") as src, open(tmp, "wb") as out:
        src.seek(offset)
        remaining = size
        while remaining > 0:
            block = src.read(min(CHUNK, remaining))
            if not block:
                raise ImageError("unexpected EOF while extracting partition")
            out.write(block)
            h.update(block)
            written += len(block)
            remaining -= len(block)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, dest)
    return {"path": str(dest), "size": written, "sha256": h.hexdigest()}
