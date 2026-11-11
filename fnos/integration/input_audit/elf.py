"""Minimal, read-only ELF metadata parser.

The audit never executes a binary from the image.  It only parses bytes that
were copied out of a read-only filesystem view.  The parser deliberately
supports just enough of ELF64/ELF32 to answer the questions that matter for a
userspace transplant:

* is the object AArch64 (and little endian)?
* which program interpreter does it request?
* which shared libraries (DT_NEEDED) and glibc symbol versions does it need?

Anything malformed raises ``ElfError`` instead of guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import struct

ELF_MAGIC = b"\x7fELF"
EM_AARCH64 = 0xB7
EM_X86_64 = 0x3E
EM_386 = 0x03
EM_ARM = 0x28
EM_RISCV = 0xF3

MACHINES = {
    EM_AARCH64: "AArch64",
    EM_X86_64: "x86-64",
    EM_386: "i386",
    EM_ARM: "ARM",
    EM_RISCV: "RISC-V",
}

PT_LOAD = 1
PT_DYNAMIC = 2
PT_INTERP = 3

DT_NULL = 0
DT_NEEDED = 1
DT_STRTAB = 5
DT_STRSZ = 10
DT_SONAME = 14
DT_RPATH = 15
DT_RUNPATH = 29
DT_VERNEED = 0x6FFFFFFE
DT_VERNEEDNUM = 0x6FFFFFFF

MAX_STRING = 4096
MAX_ENTRIES = 4096


class ElfError(ValueError):
    """Raised when bytes are not a complete, supported ELF object."""


@dataclass
class ElfInfo:
    bits: int
    endian: str
    machine: int
    machine_name: str
    etype: int
    osabi: int
    interpreter: str | None = None
    needed: list[str] = field(default_factory=list)
    soname: str | None = None
    rpath: list[str] = field(default_factory=list)
    runpath: list[str] = field(default_factory=list)
    glibc_versions: list[str] = field(default_factory=list)

    @property
    def is_aarch64(self) -> bool:
        return self.bits == 64 and self.endian == "little" and self.machine == EM_AARCH64

    def as_dict(self) -> dict:
        return {
            "bits": self.bits,
            "endian": self.endian,
            "machine": self.machine,
            "machine_name": self.machine_name,
            "is_aarch64": self.is_aarch64,
            "type": self.etype,
            "osabi": self.osabi,
            "interpreter": self.interpreter,
            "needed": self.needed,
            "soname": self.soname,
            "rpath": self.rpath,
            "runpath": self.runpath,
            "glibc_versions": self.glibc_versions,
        }


def _unpack(fmt: str, data: bytes, offset: int):
    size = struct.calcsize(fmt)
    if offset < 0 or offset + size > len(data):
        raise ElfError(f"truncated ELF: need {size} bytes at {offset}")
    return struct.unpack_from(fmt, data, offset)


def _cstring(data: bytes, offset: int, limit: int | None = None) -> str:
    if offset < 0 or offset >= len(data):
        return ""
    end = data.find(b"\x00", offset, min(len(data), offset + MAX_STRING))
    if end < 0:
        end = min(len(data), offset + MAX_STRING)
    raw = data[offset:end]
    if limit is not None:
        raw = raw[:limit]
    return raw.decode("utf-8", "replace")


def _vaddr_to_offset(segments: list[tuple[int, int, int]], vaddr: int) -> int | None:
    for p_vaddr, p_filesz, p_offset in segments:
        if p_vaddr <= vaddr < p_vaddr + p_filesz:
            return p_offset + (vaddr - p_vaddr)
    return None


def parse_elf(data: bytes) -> ElfInfo:
    """Parse an ELF header and, when present, dynamic metadata."""

    if len(data) < 16 or data[:4] != ELF_MAGIC:
        raise ElfError("not an ELF object")
    ei_class = data[4]
    ei_data = data[5]
    if ei_class not in (1, 2):
        raise ElfError(f"unsupported ELF class {ei_class}")
    if ei_data not in (1, 2):
        raise ElfError(f"unsupported ELF byte order {ei_data}")
    bits = 32 if ei_class == 1 else 64
    endian = "little" if ei_data == 1 else "big"
    prefix = "<" if ei_data == 1 else ">"
    min_header = 52 if bits == 32 else 64
    if len(data) < min_header:
        raise ElfError("truncated ELF header")

    e_type, e_machine = _unpack(prefix + "HH", data, 16)
    osabi = data[7]
    if bits == 64:
        e_phoff = _unpack(prefix + "Q", data, 32)[0]
        e_phentsize = _unpack(prefix + "H", data, 54)[0]
        e_phnum = _unpack(prefix + "H", data, 56)[0]
    else:
        e_phoff = _unpack(prefix + "I", data, 28)[0]
        e_phentsize = _unpack(prefix + "H", data, 42)[0]
        e_phnum = _unpack(prefix + "H", data, 44)[0]

    if e_phnum > 1024:
        raise ElfError("implausible program header count")
    if e_phnum and e_phentsize < (56 if bits == 64 else 32):
        raise ElfError("bad program header entry size")
    if e_phoff + e_phnum * e_phentsize > len(data):
        raise ElfError("truncated program header table")

    interp: str | None = None
    dynamic_offset: int | None = None
    dynamic_size = 0
    loads: list[tuple[int, int, int]] = []
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        if bits == 64:
            p_type, _p_flags = _unpack(prefix + "II", data, off)
            p_offset = _unpack(prefix + "Q", data, off + 8)[0]
            p_vaddr = _unpack(prefix + "Q", data, off + 16)[0]
            p_filesz = _unpack(prefix + "Q", data, off + 32)[0]
        else:
            p_type = _unpack(prefix + "I", data, off)[0]
            p_offset = _unpack(prefix + "I", data, off + 4)[0]
            p_vaddr = _unpack(prefix + "I", data, off + 8)[0]
            p_filesz = _unpack(prefix + "I", data, off + 16)[0]
        if p_type == PT_LOAD:
            loads.append((p_vaddr, p_filesz, p_offset))
        elif p_type == PT_INTERP:
            if p_offset + p_filesz > len(data):
                raise ElfError("truncated PT_INTERP")
            interp = _cstring(data, p_offset, p_filesz)
        elif p_type == PT_DYNAMIC:
            dynamic_offset = p_offset
            dynamic_size = p_filesz

    info = ElfInfo(
        bits=bits,
        endian=endian,
        machine=e_machine,
        machine_name=MACHINES.get(e_machine, f"EM_{e_machine}"),
        etype=e_type,
        osabi=osabi,
        interpreter=interp,
    )

    if dynamic_offset is None or dynamic_size == 0:
        return info
    if dynamic_offset + dynamic_size > len(data):
        raise ElfError("truncated PT_DYNAMIC")

    entsize = 16 if bits == 64 else 8
    entries: list[tuple[int, int]] = []
    for i in range(min(dynamic_size // entsize, MAX_ENTRIES)):
        off = dynamic_offset + i * entsize
        if bits == 64:
            tag, val = _unpack(prefix + "QQ", data, off)
        else:
            tag, val = _unpack(prefix + "II", data, off)
        entries.append((tag, val))
        if tag == DT_NULL:
            break

    strtab = next((v for t, v in entries if t == DT_STRTAB), None)
    strsz = next((v for t, v in entries if t == DT_STRSZ), 0)
    if strtab is None:
        return info
    strtab_off = _vaddr_to_offset(loads, strtab)
    if strtab_off is None or strtab_off >= len(data):
        raise ElfError("DT_STRTAB is not backed by a PT_LOAD segment")
    strtab_end = min(len(data), strtab_off + (strsz or (len(data) - strtab_off)))

    def dyn_string(value: int) -> str:
        return _cstring(data[:strtab_end], strtab_off + value)

    for tag, value in entries:
        if tag == DT_NEEDED:
            s = dyn_string(value)
            if s:
                info.needed.append(s)
        elif tag == DT_SONAME:
            info.soname = dyn_string(value) or None
        elif tag == DT_RPATH:
            info.rpath = [p for p in dyn_string(value).split(":") if p]
        elif tag == DT_RUNPATH:
            info.runpath = [p for p in dyn_string(value).split(":") if p]

    verneed = next((v for t, v in entries if t == DT_VERNEED), None)
    verneednum = next((v for t, v in entries if t == DT_VERNEEDNUM), 0)
    if verneed is not None:
        info.glibc_versions = _parse_verneed(data, prefix, bits, loads, verneed, verneednum, strtab_off)
    return info


def _parse_verneed(data, prefix, bits, loads, verneed_vaddr, count, strtab_off=0) -> list[str]:
    offset = _vaddr_to_offset(loads, verneed_vaddr)
    if offset is None:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for _ in range(min(count or 1, 256)):
        if offset + 16 > len(data):
            break
        _version, vn_cnt, _vn_file, vn_aux, vn_next = _unpack(prefix + "HHIII", data, offset)
        aux_off = offset + vn_aux
        for _ in range(min(vn_cnt, 256)):
            if aux_off + 16 > len(data):
                break
            _hash, _flags, _other, vna_name, vna_next = _unpack(prefix + "IHHII", data, aux_off)
            name_off = strtab_off + vna_name
            if 0 <= name_off < len(data):
                name = _cstring(data, name_off)
                if name and name not in seen:
                    seen.add(name)
                    found.append(name)
            if not vna_next:
                break
            aux_off += vna_next
        if not vn_next:
            break
        offset += vn_next
    return sorted(found)


def max_glibc_version(versions: list[str]) -> str | None:
    """Return the highest ``GLIBC_X.Y`` requirement, sorting numerically."""

    best: tuple[int, ...] | None = None
    best_s: str | None = None
    for v in versions:
        if not v.startswith("GLIBC_"):
            continue
        try:
            parts = tuple(int(p) for p in v[6:].split("."))
        except ValueError:
            continue
        if best is None or parts > best:
            best = parts
            best_s = v
    return best_s
