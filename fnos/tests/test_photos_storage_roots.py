"""Photos must follow volumes mounted after the native AI worker starts."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fnos.integration.native_photos.backend import read_allowed_image
from fnos.integration.native_photos.storage_roots import mounted_data_roots


class PhotosStorageRootsTests(unittest.TestCase):
    def test_all_native_namespaces_and_escaped_mounts(self):
        paths = ['/vol1', '/vol12', '/vol00/usb', '/vol01/cloud',
                 '/vol02/1000-0-share', '/vol02/name\\040with\\040spaces',
                 '/vol02', '/vol03/unknown', '/vol1-other', '/etc']
        mounts = '\n'.join(f'{i+1} 1 8:1 / {p} rw - ext4 /dev/test rw'
                           for i, p in enumerate(paths))
        with patch.object(Path, 'read_text', return_value=mounts):
            self.assertEqual(mounted_data_roots(), sorted([
                '/vol1', '/vol12', '/vol00/usb', '/vol01/cloud',
                '/vol02/1000-0-share', '/vol02/name with spaces']))

    def test_empty_volumes_do_not_prevent_worker_start(self):
        with patch.object(Path, 'read_text', return_value='1 0 8:0 / / rw - ext4 /dev/root rw'):
            self.assertEqual(mounted_data_roots(), [])

    def test_running_reader_observes_mount_and_unmount(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = str(Path(folder).resolve())
            path = Path(folder) / 'photo.jpg'
            path.write_bytes(b'public image fixture')
            roots = []
            current = lambda: roots[:]
            with self.assertRaises(PermissionError):
                read_allowed_image(str(path), current)
            roots.append(folder)
            self.assertEqual(read_allowed_image(str(path), current), path.read_bytes())
            roots.clear()
            with self.assertRaises(PermissionError):
                read_allowed_image(str(path), current)


if __name__ == '__main__':
    unittest.main()
