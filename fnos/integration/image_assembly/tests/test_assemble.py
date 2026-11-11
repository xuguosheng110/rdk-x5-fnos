"""Pure/boundary tests for the X5 image assembly helpers."""

from __future__ import annotations

import sys
from pathlib import Path
import unittest
import tempfile
import errno
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from fnos.integration.image_assembly.mbr import build_mbr, parse_mbr  # noqa: E402
from fnos.integration.image_assembly.assemble import adapt_boot_cmd, harden_boot_cmd, validate_product_kernel, PRODUCT_KERNEL_OPTIONS  # noqa: E402
from fnos.integration.image_assembly.assemble import copy_sparse_partition, MIB


def base_mbr() -> bytes:
    b = bytearray(512)
    b[440:444] = bytes.fromhex("05722edb")
    b[510:512] = b"\x55\xaa"
    return bytes(b)


class BootCmdTests(unittest.TestCase):
    def test_original_docker_nft_failure_is_rejected_before_product_assembly(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config'
            # Observed board: NF_TABLES=m, but all three IP families disabled.
            content = '\n'.join(name + '=' + ('m' if 'm' in allowed else 'y')
                                for name, allowed in PRODUCT_KERNEL_OPTIONS.items()
                                if name not in ('CONFIG_NF_TABLES_INET', 'CONFIG_NF_TABLES_IPV4', 'CONFIG_NF_TABLES_IPV6'))
            config.write_text(content + '\n')
            with self.assertRaisesRegex(ValueError, 'CONFIG_NF_TABLES_IPV4'):
                validate_product_kernel(config)
            with config.open('a') as out:
                out.write('CONFIG_NF_TABLES_INET=y\nCONFIG_NF_TABLES_IPV4=y\nCONFIG_NF_TABLES_IPV6=y\n')
            self.assertEqual(validate_product_kernel(config)['CONFIG_NF_TABLES_IPV4'], 'y')

    def test_raw_initrd_uses_address_size_and_fails_closed(self) -> None:
        src = 'imagefile="Image"\n...\nbooti ${kernel_addr_r} - ${fdt_addr_r}\n'
        out = adapt_boot_cmd(src)
        self.assertIn("setenv ramdisk_addr_r 0xc0000000", out)
        # r3 needed manually persisted settings to relocate the large initrd.
        # A fresh board must receive these from the image's own boot script.
        self.assertIn("setenv initrd_high 0xffffffff", out)
        self.assertIn("setenv bootm_low 0x84000000", out)
        self.assertIn("setenv bootm_size 0x100000000", out)
        self.assertIn('${ramdisk_addr_r}:${initrd_size}', out)
        self.assertIn("ERROR: cannot load", out)
        self.assertIn("exit", out)
        self.assertIn("0x20000000", out)
        self.assertIn("0x${filesize}", out)
        self.assertNotIn("booti ${kernel_addr_r} - ${fdt_addr_r}", out)
        self.assertIn("booti ${kernel_addr_r} ${initrd_load_addr} ${fdt_addr_r}", out)

    def test_harden_uses_echo_not_colon(self) -> None:
        src = (
            'imagefile="Image"\n'
            'ext4load ${devtype} ${devnum}:${devplist} ${fdt_addr_r} ${prefix}hobot/${fdtfile}\n'
            'ext4load ${devtype} ${devnum}:${devplist} ${kernel_addr_r} ${prefix}${imagefile}\n'
            'booti ${kernel_addr_r} - ${fdt_addr_r}\n'
        )
        out = harden_boot_cmd(src)
        self.assertIn('echo "Loaded FDT"', out)
        self.assertIn('echo "Loaded kernel"', out)
        self.assertIn('ERROR: cannot load FDT', out)
        self.assertIn('ERROR: cannot load kernel', out)
        self.assertNotIn("then :", out)
        self.assertNotIn("if :", out)


class MbrTests(unittest.TestCase):
    def test_two_partitions_preserve_disk_signature_and_boot_flag(self) -> None:
        mbr = build_mbr(base_mbr(), 8192, 524288, 532480, 19767296)
        self.assertEqual(mbr[440:444], bytes.fromhex("05722edb"))
        entries = parse_mbr(mbr)
        self.assertEqual(entries[0]["start_lba"], 8192)
        self.assertEqual(entries[0]["sectors"], 524288)
        self.assertEqual(entries[0]["type"], 0x0C)
        self.assertFalse(entries[0]["bootable"])
        self.assertEqual(entries[1]["start_lba"], 532480)
        self.assertEqual(entries[1]["sectors"], 19767296)
        self.assertEqual(entries[1]["type"], 0x83)
        self.assertTrue(entries[1]["bootable"])
        self.assertEqual(entries[2]["type"], 0)
        self.assertEqual(entries[3]["type"], 0)

    def test_does_not_create_gpt_protective_entry(self) -> None:
        mbr = build_mbr(base_mbr(), 8192, 524288, 532480, 1000000)
        self.assertFalse(any(e["type"] == 0xEE for e in parse_mbr(mbr)))
        self.assertEqual(len(parse_mbr(mbr)), 4)

    def test_out_of_range_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_mbr(base_mbr(), 8192, 524288, 0xFFFFFFFF, 0xFFFFFFFF)

    def test_short_base_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_mbr(b"\x00" * 100, 1, 1, 2, 1)

    def test_mbr_magic(self) -> None:
        mbr = build_mbr(base_mbr(), 8192, 524288, 532480, 1000)
        self.assertEqual(mbr[510:512], b"\x55\xaa")


class SparseImageTests(unittest.TestCase):
    def test_partition_offsets_holes_and_zero_bytes_match_dense_baseline(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / 'source'
                output = Path(directory) / 'image'
                # Leading/trailing holes, a data run straddling a copy chunk,
                # allocated zeros, and a second data run near the end.
                with source.open('wb') as stream:
                    stream.seek(8 * MIB - 17)
                    stream.write(bytes(range(256)) * 24)
                    stream.seek(16 * MIB)
                    stream.write(b'\0' * MIB)
                    stream.seek(25 * MIB + 13)
                    stream.write(b'end-of-data')
                    stream.truncate(32 * MIB)
                expected = source.read_bytes()
                offset = 12345
                with output.open('w+b') as target, source.open('rb') as original:
                    target.truncate(offset + len(expected) + 4096)
                    target.write(b'MBR-marker')
                    if fallback:
                        with patch('os.lseek', side_effect=OSError(errno.EINVAL, 'unsupported')):
                            written = copy_sparse_partition(original, target, offset)
                    else:
                        written = copy_sparse_partition(original, target, offset)
                    target.flush()
                    target.seek(0)
                    self.assertEqual(target.read(10), b'MBR-marker')
                    target.seek(offset)
                    self.assertEqual(target.read(len(expected)), expected)
                    self.assertEqual(target.read(), b'\0' * 4096)
                    self.assertLess(written, len(expected))


if __name__ == "__main__":
    unittest.main()
