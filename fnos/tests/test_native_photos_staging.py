"""Offline staging regression for runtime-profile license relocation."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fnos.integration.image_assembly import native_photos


def digest(data):
    return hashlib.sha256(data).hexdigest()


class NativePhotosStagingTests(unittest.TestCase):
    def test_removed_distinfo_license_is_staged_before_python_pruning(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / 'root'
            (root / 'etc').mkdir(parents=True)
            (root / 'etc/os-release').write_text('ID=debian\n')
            entry = root / native_photos.ENTRY
            entry.parent.mkdir(parents=True)
            entry.write_bytes(b'native-entry\n')

            assets = base / 'assets'
            payloads = {
                'face128_worker': b'worker\n',
                'faceID.hbm': b'model\n',
                'det.onnx': b'detector\n',
                'face_detection_worker': b'detection-worker\n',
                'face-detector.hbm': b'box\n',
                'landmark106.hbm': b'landmark\n',
                'python/scikit_image-1.dist-info/LICENSE': b'license\n',
                'python/onnxruntime/capi/libtest.so': b'ort\n',
            }
            rows = []
            for name, data in payloads.items():
                path = assets / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                rows.append({'path': name, 'bytes': len(data), 'sha256': digest(data)})
            (assets / 'assets.json').write_text(json.dumps({
                'schema': 'fnos-photos-assets/v1', 'files': rows}))

            with patch.multiple(
                    native_photos,
                    ENTRY_SHA=digest(entry.read_bytes()),
                    WORKER_SHA=digest(payloads['face128_worker']),
                    MODEL_SHA=digest(payloads['faceID.hbm']),
                    DETECTOR_SHA=digest(payloads['det.onnx']),
                    DETECTION_WORKER_SHA=digest(payloads['face_detection_worker']),
                    BOX_SHA=digest(payloads['face-detector.hbm']),
                    LANDMARK_SHA=digest(payloads['landmark106.hbm'])):
                report = native_photos.stage_native_photos(root, assets, profile='runtime')

            license_path = root / 'usr/share/fnos-x5/licenses/scikit_image-1.dist-info/LICENSE'
            self.assertEqual(license_path.read_bytes(), payloads['python/scikit_image-1.dist-info/LICENSE'])
            for path in (license_path.parent, license_path.parent.parent,
                         license_path.parent.parent.parent):
                self.assertEqual(path.stat().st_mode & 0o777, 0o755, path)
            self.assertEqual((root / 'usr/lib/fnos-x5/face128_worker').stat().st_mode & 0o777, 0o755)
            self.assertIn(str(license_path.relative_to(root)),
                          {row['path'] for row in report['files']})


if __name__ == '__main__':
    unittest.main()
