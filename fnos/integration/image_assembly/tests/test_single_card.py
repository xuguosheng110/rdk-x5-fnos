import copy
from pathlib import Path
import tempfile
import subprocess
import unittest

from fnos.integration.image_assembly import quark_log_compat, root_expand


class RootSizeTests(unittest.TestCase):
    def setUp(self):
        self.root = '/dev/mmcblk1p2'
        self.layout = {
            'label': 'dos', 'device': '/dev/mmcblk1', 'sectorsize': 512,
            'partitions': [
                {'node': '/dev/mmcblk1p1', 'start': 8192, 'size': 524288, 'type': 'c'},
                {'node': self.root, 'start': 532480, 'size': 12 << 21,
                 'type': '83', 'bootable': True},
            ],
        }

    def test_32gb_card_keeps_space_for_data(self):
        original = copy.deepcopy(self.layout)
        self.assertEqual(root_expand.target_size(self.layout, self.root, 61440000), 16 << 21)
        self.assertEqual(self.layout, original)

    def test_smaller_card_and_existing_larger_root(self):
        small = 14 << 21
        self.assertEqual(root_expand.target_size(self.layout, self.root, small + 532480), small)
        self.layout['partitions'][1]['size'] = 20 << 21
        self.assertEqual(root_expand.target_size(self.layout, self.root, 61440000), 20 << 21)

    def test_existing_data_partition_is_rejected(self):
        self.layout['partitions'].append({'node': '/dev/mmcblk1p3'})
        with self.assertRaises(ValueError):
            root_expand.target_size(self.layout, self.root, 61440000)


class QuarkTests(unittest.TestCase):
    def test_unknown_launcher_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'main'
            path.write_bytes(b'new vendor version')
            self.assertFalse(quark_log_compat.repair(path))
            self.assertEqual(path.read_bytes(), b'new vendor version')


class QBittorrentTests(unittest.TestCase):
    def test_service_log_uses_writable_private_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            script = quark_log_compat.QBIT_REPLACEMENT.decode()
            subprocess.run(['sh', '-ec', script + '\nprintf stopped >> "$LOG_FILE"'],
                           env={'TRIM_PKGVAR': directory, 'LOG_DIR': ''}, check=True)
            self.assertEqual((Path(directory) / 'logs/service.log').read_text(), 'stopped')
