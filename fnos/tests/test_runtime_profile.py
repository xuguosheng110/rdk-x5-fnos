"""Shared FPK/image runtime profile policy tests."""
import json
from pathlib import Path
import tempfile
import unittest

from fnos.integration.image_assembly import runtime_profile


class RuntimeProfileTests(unittest.TestCase):
    def test_runtime_inventory_is_one_onnx_set_and_tokenizer(self):
        data = runtime_profile.inventory('runtime')
        paths = [row['path'] for row in data['required_files']]
        self.assertEqual(len(paths), 6)
        self.assertTrue(all(path.endswith(('.onnx', '/sp.model')) for path in paths))
        self.assertFalse(any(path.endswith(('.bin', '.xml')) for path in paths))
        self.assertEqual([p for p in paths if 'face-model-1.0.1' in p],
                         ['ai-models/face-model-1.0.1/det.onnx',
                          'ai-models/face-model-1.0.1/genderage.onnx',
                          'ai-models/face-model-1.0.1/rec.onnx'])

    def test_runtime_python_filter_excludes_build_tools_and_tests(self):
        for path in ('python/pip/pip.py', 'python/skimage/__init__.py',
                     'python/numpy/tests/test_core.py', 'python/networkx/x.py'):
            self.assertFalse(runtime_profile.python_asset_allowed(path, 'runtime'), path)
        for path in ('python/onnxruntime/capi/x.so', 'python/numpy/core.py',
                     'python/scipy/special/x.py'):
            self.assertTrue(runtime_profile.python_asset_allowed(path, 'runtime'), path)
        self.assertTrue(runtime_profile.python_asset_allowed('python/pip/x.py', 'full'))

    def test_removed_distinfo_license_is_relocated(self):
        path = 'python/scikit_image-0.24.0.dist-info/LICENSE.txt'
        self.assertFalse(runtime_profile.python_asset_allowed(path, 'runtime'))
        self.assertEqual(runtime_profile.distinfo_license_source(path),
                         Path('usr/share/fnos-x5/licenses/scikit_image-0.24.0.dist-info/LICENSE.txt'))
        self.assertIsNone(runtime_profile.distinfo_license_source(
            'python/scikit_image-0.24.0.dist-info/METADATA'))

    def test_prune_root_removes_models_python_tests_and_metadata_but_keeps_license(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'etc').mkdir()
            (root / 'etc/os-release').write_text('ID=fnos\n')
            py = root / 'usr/lib/fnos-x5/photos-python'
            files = {
                'pip/x.py': b'pip',
                'skimage/x.py': b'skimage',
                'numpy/core.py': b'numpy',
                'numpy/tests/test_core.py': b'test',
                'scikit_image-1.dist-info/METADATA': b'metadata',
                'scikit_image-1.dist-info/LICENSE': b'license',
            }
            for name, data in files.items():
                path = py / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            models = root / 'usr/share/fnos-x5/product-inputs/ai-models'
            for name in ('img2vec-medium-model-1.0.0/img2vec.onnx',
                         'img2vec-medium-model-1.0.0/img2vec.bin'):
                path = models / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(name.encode())
            result = runtime_profile.prune_root(root)
            self.assertGreater(result['removed_files'], 0)
            self.assertFalse((py / 'pip').exists())
            self.assertFalse((py / 'skimage').exists())
            self.assertFalse((py / 'numpy/tests').exists())
            self.assertTrue((py / 'numpy/core.py').is_file())
            self.assertFalse((py / 'scikit_image-1.dist-info/METADATA').exists())
            self.assertTrue((root / 'usr/share/fnos-x5/licenses/scikit_image-1.dist-info/LICENSE').is_file())
            self.assertTrue((models / 'img2vec-medium-model-1.0.0/img2vec.onnx').is_file())
            self.assertFalse((models / 'img2vec-medium-model-1.0.0/img2vec.bin').exists())
            self.assertTrue(runtime_profile.audit_root(root)['ok'])


if __name__ == '__main__':
    unittest.main()
