import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from fnos.integration.native_photos.semantic768_backend import MODEL_SHA256, WORKER_SHA256, NATIVE_IMAGE_SHA256
from fnos.integration.native_photos.semantic768_text_compare import load_vectors


class SemanticTextComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.images = [{'path': '/fixture/' + str(i) + '.jpg', 'sha256': str(i)} for i in range(28)]
        fixture = self.root / 'fixtures.json'
        fixture.write_text(json.dumps({'images': self.images}))
        self.manifest_sha = hashlib.sha256(fixture.read_bytes()).hexdigest()
        vectors = np.eye(28, 768, dtype=np.float32)
        archive = self.root / 'vectors.npz'
        np.savez_compressed(archive, cpu=vectors, bpu=vectors)
        self.source = {'schema': 'fnos-native-semantic768-benchmark/v1', 'progress': {'phase': 'complete'},
                       'gates': {'preprocessing': True, 'features': True}, 'fixture_manifest_path': str(fixture),
                       'fixture_manifest_sha256': self.manifest_sha,
                       'vectors': {'file': archive.name, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                                   'images': self.images, 'dimensions': 768, 'native_image_sha256': NATIVE_IMAGE_SHA256,
                                   'bpu_model_sha256': MODEL_SHA256, 'worker_sha256': WORKER_SHA256}}
        self.report = self.root / 'benchmark.json'
        self.report.write_text(json.dumps(self.source))
        self.queries = {'schema': 'fnos-semantic768-chinese-consistency-queries/v1',
                        'fixture_manifest_sha256': self.manifest_sha, 'queries': [{'text': '一只狗'}]}

    def tearDown(self):
        self.temporary.cleanup()

    def test_verified_vectors_can_be_compared_without_reinference(self):
        _, images, cpu, bpu = load_vectors(self.report, self.queries)
        self.assertEqual(len(images), 28)
        np.testing.assert_array_equal(cpu, bpu)

    def test_changed_corpus_labels_or_duplicate_queries_rejected(self):
        for fields in ({'fixture_manifest_sha256': 'wrong'}, {'queries': [{'text': 'dog', 'expected_images': []}]},
                       {'queries': [{'text': 'dog'}, {'text': 'dog'}]}):
            with self.assertRaises(ValueError):
                load_vectors(self.report, {**self.queries, **fields})

    def test_incomplete_benchmark_or_reordered_images_rejected(self):
        for fields in ({'progress': {'phase': 'timing'}}, {'gates': {'preprocessing': False, 'features': True}},
                       {'vectors': {**self.source['vectors'], 'images': list(reversed(self.images))}}):
            self.report.write_text(json.dumps({**self.source, **fields}))
            with self.assertRaises(ValueError):
                load_vectors(self.report, self.queries)

    def test_changed_vector_bytes_rejected_by_hash(self):
        (self.root / 'vectors.npz').write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            load_vectors(self.report, self.queries)


if __name__ == '__main__':
    unittest.main()
