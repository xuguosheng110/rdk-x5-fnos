"""Boundary and fixture tests for the read-only input audit.

Run from the repository root:

    python3 -m unittest -v fnos.integration.input_audit.tests.test_audit

No test mounts an image, executes image content, uses sudo or touches a board.
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[4]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "rootfs"
sys.path.insert(0, str(ROOT))

from fnos.integration.input_audit.audit import Auditor, _scan_text  # noqa: E402
from fnos.integration.input_audit.elf import ElfError, parse_elf  # noqa: E402
from fnos.integration.input_audit.image import detect_compression, parse_partitions, probe_filesystem  # noqa: E402
from fnos.integration.input_audit.rootfs import (  # noqa: E402
    DirectoryBackend,
    Ext4Backend,
    PathRefused,
    ToolRunner,
    validate_image_path,
)


def build_elf(
    machine: int = 0xB7,
    bits: int = 64,
    interp: bytes = b"/lib/ld-linux-aarch64.so.1",
    needed: tuple[bytes, ...] = (b"libc.so.6",),
    glibc: bytes | None = b"GLIBC_2.34",
) -> bytes:
    """Build a minimal ELF64/ELF32 with PT_INTERP and a dynamic table."""

    if bits == 64:
        ehsize, phentsize = 64, 56
    else:
        ehsize, phentsize = 52, 32
    phnum = 3
    data_off = ehsize + phentsize * phnum
    interp_off = data_off
    dyn_off = (interp_off + len(interp) + 1 + 7) & ~7
    entries: list[tuple[int, int]] = []
    strings = bytearray(b"\x00")
    offsets: dict[bytes, int] = {}

    def add_string(value: bytes) -> int:
        if value in offsets:
            return offsets[value]
        off = len(strings)
        strings.extend(value + b"\x00")
        offsets[value] = off
        return off

    for lib in needed:
        entries.append((1, add_string(lib)))
    glibc_off = add_string(glibc) if glibc else None
    entries.append((5, 0))  # DT_STRTAB, patched after layout
    entries.append((14, add_string(b"libfixture.so")))
    entries.append((10, 0))  # DT_STRSZ, patched
    if glibc:
        entries.append((0x6FFFFFFE, 0))  # DT_VERNEED, patched
        entries.append((0x6FFFFFFF, 1))
    entries.append((0, 0))

    dyn_size = len(entries) * (16 if bits == 64 else 8)
    strtab_off = dyn_off + dyn_size
    verneed_off = (strtab_off + len(strings) + 7) & ~7
    total = verneed_off + (32 if glibc else 0)
    vbase = 0x400000

    # patch string table address/size and verneed address
    patched = []
    for tag, value in entries:
        if tag == 5:
            value = vbase + strtab_off
        elif tag == 10:
            value = len(strings)
        elif tag == 0x6FFFFFFE:
            value = vbase + verneed_off
        patched.append((tag, value))
    entries = patched

    ident = b"\x7fELF" + bytes([2 if bits == 64 else 1, 1, 1, 0]) + b"\x00" * 8
    if bits == 64:
        ehdr = ident + struct.pack(
            "<HHIQQQIHHHHHH",
            2, machine, 1, vbase + 0x100, ehsize, 0, 0,
            ehsize, phentsize, phnum, 64, 0, 0,
        )
    else:
        ehdr = ident + struct.pack(
            "<HHIIIIIHHHHHH",
            2, machine, 1, vbase + 0x100, ehsize, 0, 0,
            ehsize, phentsize, phnum, 40, 0, 0,
        )
    phdrs = bytearray()
    if bits == 64:
        phdrs += struct.pack("<IIQQQQQQ", 1, 5, 0, vbase, vbase, total, total, 0x1000)
        phdrs += struct.pack("<IIQQQQQQ", 3, 4, interp_off, vbase + interp_off, vbase + interp_off, len(interp) + 1, len(interp) + 1, 1)
        phdrs += struct.pack("<IIQQQQQQ", 2, 6, dyn_off, vbase + dyn_off, vbase + dyn_off, dyn_size, dyn_size, 8)
    else:
        phdrs += struct.pack("<IIIIIIII", 1, 0, vbase, vbase, total, total, 5, 0x1000)
        phdrs += struct.pack("<IIIIIIII", 3, interp_off, vbase + interp_off, vbase + interp_off, len(interp) + 1, len(interp) + 1, 4, 1)
        phdrs += struct.pack("<IIIIIIII", 2, dyn_off, vbase + dyn_off, vbase + dyn_off, dyn_size, dyn_size, 6, 4)

    dyn = bytearray()
    for tag, value in entries:
        dyn += struct.pack("<QQ", tag, value) if bits == 64 else struct.pack("<II", tag, value)

    body = bytearray(total)
    body[:ehsize] = ehdr
    body[ehsize : ehsize + len(phdrs)] = phdrs
    body[interp_off : interp_off + len(interp)] = interp
    body[interp_off + len(interp)] = 0
    body[dyn_off : dyn_off + len(dyn)] = dyn
    body[strtab_off : strtab_off + len(strings)] = strings
    if glibc:
        # strings may have grown; recompute if needed
        if strtab_off + len(strings) > verneed_off:
            raise AssertionError("fixture layout overflow")
        body[strtab_off : strtab_off + len(strings)] = strings
        vn = struct.pack("<HHIII", 1, 1, 0, 16, 0)
        aux = struct.pack("<IHHII", 0x12345678, 0, 2, glibc_off, 0)
        body[verneed_off : verneed_off + 32] = vn + aux
    return bytes(body)


def _crc(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def build_gpt_image(path: Path) -> None:
    total_sectors = 4096
    image = bytearray(total_sectors * 512)
    # protective MBR
    image[446 + 4] = 0xEE
    struct.pack_into("<I", image, 446 + 8, 1)
    struct.pack_into("<I", image, 446 + 12, total_sectors - 1)
    image[510:512] = b"\x55\xaa"
    entries = bytearray(128 * 4)
    entries[0:16] = bytes.fromhex("af3dc60f838472478e793d69d8477de4")
    entries[16:32] = bytes(range(16))
    struct.pack_into("<QQQ", entries, 32, 2048, 4095, 0)
    entries[56:128] = "root".encode("utf-16-le").ljust(72, b"\x00")
    header = bytearray(92)
    header[0:8] = b"EFI PART"
    struct.pack_into("<I", header, 8, 0x00010000)
    struct.pack_into("<I", header, 12, 92)
    struct.pack_into("<Q", header, 24, 1)
    struct.pack_into("<Q", header, 32, total_sectors - 1)
    struct.pack_into("<Q", header, 40, 34)
    struct.pack_into("<Q", header, 48, total_sectors - 34)
    header[56:72] = bytes(range(16, 32))
    struct.pack_into("<Q", header, 72, 2)
    struct.pack_into("<I", header, 80, 4)
    struct.pack_into("<I", header, 84, 128)
    struct.pack_into("<I", header, 88, _crc(entries))
    struct.pack_into("<I", header, 16, _crc(bytes(header)))
    image[512 : 512 + 92] = header
    image[1024 : 1024 + len(entries)] = entries
    # ext4 superblock magic in partition 1
    sb = 2048 * 512 + 1024
    struct.pack_into("<H", image, sb + 0x38, 0xEF53)
    struct.pack_into("<I", image, sb + 0x18, 0)  # 1KiB blocks
    struct.pack_into("<I", image, sb + 0x60, 0x40)  # extents -> ext4
    path.write_bytes(image)


ASYM_UUID = "00112233-4455-6677-8899-aabbccddeeff"


def build_ext4_uuid_image(path: Path, value: str) -> None:
    image = bytearray(4096)
    sb = 1024
    image[sb + 0x38 : sb + 0x3A] = b"\x53\xef"
    image[sb + 0x60 : sb + 0x64] = struct.pack("<I", 0x40)  # extents -> ext4
    image[sb + 0x68 : sb + 0x78] = uuid.UUID(value).bytes
    image[sb + 0x78 : sb + 0x88] = b"UUIDTEST".ljust(16, b"\x00")
    path.write_bytes(image)


def build_btrfs_uuid_image(path: Path, value: str) -> None:
    image = bytearray(0x12000)
    image[0x10040 : 0x10048] = b"_BHRfS_M"
    image[0x10020 : 0x10030] = uuid.UUID(value).bytes
    image[0x1012B : 0x1022B] = b"UUIDTEST".ljust(256, b"\x00")
    path.write_bytes(image)


def blkid_uuid(path: Path) -> str:
    proc = subprocess.run(
        ["blkid", "-p", "-o", "value", "-s", "UUID", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise AssertionError(f"blkid failed: {proc.stderr}")
    return proc.stdout.strip()


class UuidTests(unittest.TestCase):
    def test_ext4_superblock_uuid_is_raw_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "ext4.img"
            build_ext4_uuid_image(image, ASYM_UUID)
            fs = probe_filesystem(image, 0, image.stat().st_size)
            self.assertEqual(fs["type"], "ext4")
            self.assertEqual(fs["uuid"], ASYM_UUID)

    def test_btrfs_fsid_is_raw_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "btrfs.img"
            build_btrfs_uuid_image(image, ASYM_UUID)
            fs = probe_filesystem(image, 0, image.stat().st_size)
            self.assertEqual(fs["type"], "btrfs")
            self.assertEqual(fs["uuid"], ASYM_UUID)

    def test_gpt_guid_keeps_mixed_endian(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "gpt.img"
            build_gpt_image(image)
            parsed = parse_partitions(image)
            self.assertEqual(parsed["meta"]["disk_guid"], "13121110-1514-1716-1819-1a1b1c1d1e1f")
            self.assertEqual(parsed["partitions"][0]["type_guid"], "0fc63daf-8483-4772-8e79-3d69d8477de4")
            self.assertEqual(parsed["partitions"][0]["unique_guid"], "03020100-0504-0706-0809-0a0b0c0d0e0f")
            self.assertEqual(
                len({parsed["meta"]["disk_guid"], parsed["partitions"][0]["type_guid"], parsed["partitions"][0]["unique_guid"]}),
                3,
            )

    @unittest.skipUnless(
        os.environ.get("FNOS_AUDIT_REAL_IMAGE") and shutil.which("sfdisk"),
        "set FNOS_AUDIT_REAL_IMAGE to the raw GPT image; sfdisk required",
    )
    def test_real_gpt_unique_guid_matches_sfdisk(self) -> None:
        image = Path(os.environ["FNOS_AUDIT_REAL_IMAGE"])
        parsed = parse_partitions(image)
        self.assertEqual(parsed["table"], "gpt")
        for part in parsed["partitions"]:
            proc = subprocess.run(
                ["sfdisk", "--part-uuid", str(image), str(part["index"])],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(part["unique_guid"], proc.stdout.strip().lower(), part["name"])
        if shutil.which("blkid"):
            proc = subprocess.run(
                ["blkid", "-p", "-o", "value", "-s", "PTUUID", str(image)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(parsed["meta"]["disk_guid"], proc.stdout.strip().lower())

    @unittest.skipUnless(shutil.which("mke2fs") and shutil.which("blkid"), "mke2fs/blkid not installed")
    def test_ext4_uuid_matches_blkid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            image = Path(tmp) / "ext4.img"
            subprocess.run(
                ["mke2fs", "-q", "-t", "ext4", "-U", ASYM_UUID, "-d", str(root), str(image), "8192"],
                check=True,
            )
            self.assertEqual(probe_filesystem(image, 0, image.stat().st_size)["uuid"], ASYM_UUID)
            self.assertEqual(blkid_uuid(image), ASYM_UUID)

    @unittest.skipUnless(shutil.which("mkfs.btrfs") and shutil.which("blkid"), "mkfs.btrfs/blkid not installed")
    def test_btrfs_uuid_matches_blkid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "btrfs.img"
            with open(image, "wb") as f:
                f.truncate(256 * 1024 * 1024)
            subprocess.run(
                ["mkfs.btrfs", "-q", "-f", "-U", ASYM_UUID, "-L", "UUIDTEST", str(image)],
                check=True,
            )
            self.assertEqual(probe_filesystem(image, 0, image.stat().st_size)["uuid"], ASYM_UUID)
            self.assertEqual(blkid_uuid(image), ASYM_UUID)

    @unittest.skipUnless(
        os.environ.get("FNOS_AUDIT_REAL_PARTITIONS") and shutil.which("blkid"),
        "set FNOS_AUDIT_REAL_PARTITIONS to a directory with rootfs-p1.ext4/rootfs-p2.btrfs",
    )
    def test_real_partition_uuids_match_blkid(self) -> None:
        directory = Path(os.environ["FNOS_AUDIT_REAL_PARTITIONS"])
        for name in ("rootfs-p1.ext4", "rootfs-p2.btrfs"):
            path = directory / name
            self.assertTrue(path.is_file(), path)
            fs = probe_filesystem(path, 0, path.stat().st_size)
            self.assertEqual(fs["uuid"], blkid_uuid(path), name)


class AssemblyTests(unittest.TestCase):
    def test_root_argument_uses_resolved_uuids(self) -> None:
        auditor = Auditor(Path("/nonexistent/image"), Path("/tmp"), Path("/tmp"))
        facts = {
            "uuid": ASYM_UUID,
            "unique_guid": ASYM_UUID,
            "partition_index": 2,
            "image": {
                "disk_guid": "39d4fd48-5058-41be-a5ad-1b4bf8c0f517",
                "partition_names": ["BOOT", "rootfs"],
                "pre_partition_bytes": 32 * 1024 * 1024,
            },
            "fstype": "btrfs",
            "boot_partition": {},
            "network": {},
            "storage": {"samba": {}},
            "modules": {},
            "scripts": {},
            "systemd": {},
            "elf": {},
        }
        boot = auditor.build_assembly(facts)["boot"]
        self.assertEqual(boot["root_argument"], f"root=UUID={ASYM_UUID}")
        self.assertEqual(boot["root_argument_partuuid"], f"root=PARTUUID={ASYM_UUID}")
        self.assertNotIn("39d4fd48-5058-41be-a5ad-1b4bf8c0f517" + "-02", boot["root_argument_partuuid"])
        self.assertNotIn("<rootfs-uuid>", boot["root_argument"])

    def test_missing_unique_guid_does_not_guess(self) -> None:
        auditor = Auditor(Path("/nonexistent/image"), Path("/tmp"), Path("/tmp"))
        facts = {
            "uuid": ASYM_UUID,
            "unique_guid": None,
            "partition_index": 2,
            "image": {"disk_guid": "39d4fd48-5058-41be-a5ad-1b4bf8c0f517", "partition_names": ["rootfs"]},
            "fstype": "btrfs",
            "boot_partition": {},
            "network": {},
            "storage": {"samba": {}},
            "modules": {},
            "scripts": {},
            "systemd": {},
            "elf": {},
        }
        boot = auditor.build_assembly(facts)["boot"]
        self.assertIsNone(boot["root_argument_partuuid"])


class ElfTests(unittest.TestCase):
    def test_aarch64_with_interp_needed_and_glibc(self) -> None:
        info = parse_elf(build_elf())
        self.assertTrue(info.is_aarch64)
        self.assertEqual(info.machine_name, "AArch64")
        self.assertEqual(info.interpreter, "/lib/ld-linux-aarch64.so.1")
        self.assertIn("libc.so.6", info.needed)
        self.assertIn("GLIBC_2.34", info.glibc_versions)

    def test_foreign_machine_is_not_aarch64(self) -> None:
        info = parse_elf(build_elf(machine=0x3E))
        self.assertFalse(info.is_aarch64)
        self.assertEqual(info.machine_name, "x86-64")

    def test_truncated_elf_raises(self) -> None:
        with self.assertRaises(ElfError):
            parse_elf(b"\x7fELF\x02\x01\x01")
        with self.assertRaises(ElfError):
            parse_elf(build_elf()[:80])

    def test_not_elf_raises(self) -> None:
        with self.assertRaises(ElfError):
            parse_elf(b"MZ" + b"\x00" * 100)

    def test_32bit_elf(self) -> None:
        info = parse_elf(build_elf(bits=32, machine=0x28))
        self.assertEqual(info.bits, 32)
        self.assertEqual(info.machine_name, "ARM")


class PathTests(unittest.TestCase):
    def test_rejects_parent_quote_and_relative(self) -> None:
        for bad in ("/etc/../etc/passwd", "../etc/passwd", "/etc/\"x", "/etc/\nx", ""):
            with self.assertRaises(PathRefused):
                validate_image_path(bad)

    def test_accepts_normal_paths(self) -> None:
        self.assertEqual(validate_image_path("/etc//os-release"), "/etc/os-release")

    def test_tool_runner_refuses_mount_and_sudo(self) -> None:
        runner = ToolRunner()
        for tool in ("mount", "sudo", "losetup", "systemctl"):
            with self.assertRaises(Exception):
                runner.run([tool, "--version"])
        self.assertEqual(runner.commands, [])

    def test_directory_backend_confines_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "etc").mkdir()
            (root / "etc" / "os-release").write_text("ID=test\n", encoding="utf-8")
            (root / "inside").write_text("ok", encoding="utf-8")
            (root / "escape").symlink_to("../../../../etc/passwd")
            backend = DirectoryBackend(root)
            self.assertEqual(backend.read_text("/inside"), "ok")
            self.assertIsNone(backend.read_bytes("/escape"))
            self.assertEqual(backend.readlink("/escape"), "../../../../etc/passwd")


class KeywordTests(unittest.TestCase):
    def test_short_keywords_use_word_boundaries_and_skip_comments(self) -> None:
        text = "# spi should be ignored\nspindown_time = 24\nSPICE listen = 127.0.0.1\nactive spi flash\nUPDATEMGR_BOOT_FAMILY\n"
        hits = _scan_text("/etc/example.conf", text, ("spi", "boot_family"), 20)
        self.assertEqual([h["line"] for h in hits if h["keyword"] == "spi"], [4])
        self.assertTrue(any(h["keyword"] == "boot_family" for h in hits))


class ImageTests(unittest.TestCase):
    def test_gpt_and_ext4_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "gpt.img"
            build_gpt_image(image)
            parsed = parse_partitions(image)
            self.assertEqual(parsed["table"], "gpt")
            self.assertTrue(parsed["meta"]["header_crc_ok"])
            self.assertTrue(parsed["meta"]["entries_crc_ok"])
            self.assertEqual(len(parsed["partitions"]), 1)
            part = parsed["partitions"][0]
            self.assertEqual(part["start_bytes"], 2048 * 512)
            self.assertEqual(part["size_bytes"], 2048 * 512)
            self.assertEqual(part["name"], "root")
            self.assertEqual(part["filesystem"]["type"], "ext4")

    def test_mbr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mbr.img"
            data = bytearray(4096 * 512)
            data[446 + 4] = 0x83
            struct.pack_into("<I", data, 446 + 8, 2048)
            struct.pack_into("<I", data, 446 + 12, 1024)
            data[510:512] = b"\x55\xaa"
            path.write_bytes(data)
            parsed = parse_partitions(path)
            self.assertEqual(parsed["table"], "mbr")
            self.assertEqual(parsed["partitions"][0]["type"], "linux")

    def test_corrupt_gpt_crc_is_reported_not_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "badgpt.img"
            build_gpt_image(image)
            raw = bytearray(image.read_bytes())
            raw[512 + 16] ^= 0xFF  # header CRC field, not recomputed
            image.write_bytes(raw)
            parsed = parse_partitions(image)
            self.assertFalse(parsed["meta"]["header_crc_ok"])

    def test_gzip_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.img.gz"
            path.write_bytes(gzip.compress(b"hello"))
            self.assertEqual(detect_compression(path), "gzip")

    def test_no_partition_table_whole_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "root.sqfs"
            path.write_bytes(b"hsqs" + b"\x00" * 100)
            parsed = parse_partitions(path)
            self.assertEqual(parsed["table"], "none")
            self.assertEqual(parsed["meta"]["whole_image_filesystem"]["type"], "squashfs")


@unittest.skipUnless(shutil.which("debugfs") and shutil.which("mke2fs"), "debugfs/mke2fs not installed")
class Ext4BackendTests(unittest.TestCase):
    def _make_ext4(self, tmp: Path) -> tuple[Path, int, int]:
        root = tmp / "root"
        (root / "etc").mkdir(parents=True)
        (root / "etc" / "os-release").write_text("ID=fnos\n", encoding="utf-8")
        (root / "etc" / "fstab").write_text("UUID=x / ext4 defaults 0 1\n", encoding="utf-8")
        (root / "lib").mkdir()
        (root / "lib" / "ld.so").symlink_to("/lib/ld-real.so")
        ext = tmp / "root.ext4"
        subprocess.run(["mke2fs", "-q", "-t", "ext4", "-d", str(root), str(ext), "8192"], check=True)
        offset = 1024 * 1024
        whole = tmp / "disk.img"
        with open(whole, "wb") as out:
            out.write(b"\x00" * offset)
            out.write(ext.read_bytes())
        return whole, offset, ext.stat().st_size

    def test_read_only_ext4_at_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            whole, offset, size = self._make_ext4(Path(tmp))
            runner = ToolRunner()
            backend = Ext4Backend(whole, offset, size, Path(tmp) / "work", runner, 1)
            self.assertTrue(backend.exists("/etc/os-release"))
            self.assertEqual(backend.read_text("/etc/os-release"), "ID=fnos\n")
            self.assertEqual(backend.readlink("/lib/ld.so"), "/lib/ld-real.so")
            names = {e.name for e in backend.listdir("/etc")}
            self.assertIn("fstab", names)
            self.assertTrue(all(cmd[0] == "debugfs" for cmd in runner.commands))
            with self.assertRaises(PathRefused):
                backend.read_bytes("/etc/../etc/os-release")

    def test_missing_file_and_bad_offset_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            whole, offset, size = self._make_ext4(Path(tmp))
            backend = Ext4Backend(whole, offset, size, Path(tmp) / "work", ToolRunner(), 1)
            self.assertIsNone(backend.read_bytes("/etc/does-not-exist"))
            self.assertFalse(backend.exists("/etc/does-not-exist"))
            bad = Ext4Backend(whole, offset + 4096, 4096, Path(tmp) / "bad", ToolRunner(), 2)
            self.assertFalse(bad.exists("/etc"))


class FixtureAuditTests(unittest.TestCase):
    def test_end_to_end_fixture_json_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "audit.json"
            md = Path(tmp) / "audit.md"
            env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "fnos.integration.input_audit",
                    "audit",
                    "--rootfs-dir",
                    str(FIXTURE),
                    "--work-dir",
                    str(Path(tmp) / "work"),
                    "--out-json",
                    str(out),
                    "--out-md",
                    str(md),
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            doc = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(doc["schema"], "fnos-input-audit/v1")
            self.assertEqual(doc["mode"], "static-readonly-fixture")
            self.assertEqual(doc["rootfs"]["fstype"], "fixture")
            self.assertEqual(doc["rootfs"]["os_release"]["ID"], "fnos")
            self.assertEqual(doc["rootfs"]["storage"]["samba"]["shares"], ["data"])
            self.assertTrue(doc["rootfs"]["scripts"]["resize_scripts"])
            self.assertFalse(doc["safety"]["mounted"])
            self.assertFalse(doc["safety"]["image_code_executed"])
            self.assertFalse(doc["safety"]["sudo_used"])
            self.assertTrue(all("mount" not in " ".join(cmd) for cmd in doc["safety"]["host_tools_used"]))
            self.assertEqual(doc["verdict"]["fnos_bootable_on_x5"], "not_tested")
            self.assertIn("fnOS ARM 输入只读审计", md.read_text(encoding="utf-8"))
            ids = {f["id"] for f in doc["findings"]}
            self.assertIn("boot-chain-must-be-x5", ids)
            self.assertIn("resize-must-be-adapted", ids)

    def _run_audit(self, rootfs: Path, tmp: Path) -> dict:
        out = tmp / "audit.json"
        env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.run(
            [
                sys.executable, "-m", "fnos.integration.input_audit", "audit",
                "--rootfs-dir", str(rootfs), "--work-dir", str(tmp / "work"),
                "--out-json", str(out),
            ],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(out.read_text(encoding="utf-8"))

    def test_real_aarch64_elf_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "root"
            shutil.copytree(FIXTURE, root, symlinks=True)
            elf = build_elf()
            (root / "lib" / "systemd" / "systemd").write_bytes(elf)
            (root / "usr" / "sbin" / "smbd").write_bytes(elf)
            (root / "lib" / "ld-2.36.so").write_bytes(elf)
            (root / "lib" / "aarch64-linux-gnu" / "libc-2.36.so").write_bytes(elf)
            doc = self._run_audit(root, tmp_path)
            self.assertGreaterEqual(doc["rootfs"]["elf"]["sample_count"], 2)
            self.assertEqual(doc["rootfs"]["elf"]["foreign"], [])
            self.assertEqual(doc["rootfs"]["elf"]["loader"]["elf"]["machine_name"], "AArch64")
            self.assertIn("GLIBC_2.34", doc["rootfs"]["elf"]["max_glibc_requirement"])

    def test_foreign_elf_is_p0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "root"
            shutil.copytree(FIXTURE, root, symlinks=True)
            (root / "lib" / "systemd" / "systemd").write_bytes(build_elf(machine=0x3E))
            (root / "usr" / "sbin" / "smbd").write_bytes(build_elf(machine=0x3E))
            doc = self._run_audit(root, tmp_path)
            ids = {f["id"] for f in doc["findings"]}
            self.assertIn("foreign-elf", ids)
            foreign = doc["rootfs"]["elf"]["foreign"]
            self.assertTrue(any(x["machine"] == "x86-64" for x in foreign))

    def test_fixture_scripts_are_not_executed(self) -> None:
        marker = FIXTURE / "etc" / "SHOULD_NOT_EXIST"
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
