"""Small, testable MBR layout helper for the X5 candidate image."""

from __future__ import annotations

import struct


def _chs(lba: int, heads: int = 255, sectors: int = 63) -> bytes:
    """Encode LBA as legacy CHS. LBA is authoritative; CHS is best effort."""
    if lba >= 1024 * heads * sectors:
        # Common "too large" encoding for CHS; BIOS/Linux use LBA.
        return bytes([0xFE, 0xFF, 0xFF])
    cylinder = lba // (heads * sectors)
    rem = lba % (heads * sectors)
    head = rem // sectors
    sector = (rem % sectors) + 1
    return bytes([head & 0xFF, (sector & 0x3F) | ((cylinder >> 2) & 0xC0), cylinder & 0xFF])


def _entry(boot: int, ptype: int, start: int, sectors: int) -> bytes:
    if not 0 <= start <= 0xFFFFFFFF or not 0 < sectors <= 0xFFFFFFFF:
        raise ValueError("MBR entry out of range")
    if start + sectors - 1 > 0xFFFFFFFF:
        raise ValueError("MBR entry end LBA out of range")
    out = bytearray(16)
    out[0] = boot
    out[1:4] = _chs(start)
    out[4] = ptype
    out[5:8] = _chs(start + sectors - 1)
    struct.pack_into("<I", out, 8, start)
    struct.pack_into("<I", out, 12, sectors)
    return bytes(out)


def build_mbr(base: bytes, p1_start: int, p1_sectors: int, p2_start: int, p2_sectors: int) -> bytes:
    """Preserve the X5 MBR boot code/signature and replace the two LBA entries."""
    if len(base) < 512:
        raise ValueError("base MBR is shorter than 512 bytes")
    out = bytearray(base[:512])
    out[446:510] = b"\x00" * 64
    out[446:462] = _entry(0x00, 0x0C, p1_start, p1_sectors)  # FAT32 CONFIG
    out[462:478] = _entry(0x80, 0x83, p2_start, p2_sectors)  # bootable ext4 rootfs
    out[510:512] = b"\x55\xaa"
    return bytes(out)


def parse_mbr(data: bytes) -> list[dict]:
    if len(data) < 512 or data[510:512] != b"\x55\xaa":
        raise ValueError("not an MBR")
    out = []
    for i in range(4):
        e = data[446 + i * 16 : 446 + (i + 1) * 16]
        out.append(
            {
                "index": i + 1,
                "bootable": e[0] == 0x80,
                "type": e[4],
                "start_lba": struct.unpack_from("<I", e, 8)[0],
                "sectors": struct.unpack_from("<I", e, 12)[0],
            }
        )
    return out
