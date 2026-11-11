"""Provenance reuse must reject changed inputs, code, dependencies or gates."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fnos.integration.native_photos import semantic768_backend as backend
from fnos.integration.native_photos import semantic768_evidence as evidence
from fnos.integration.native_photos.semantic768_benchmark import publish_report


class SemanticEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.package, self.run = self.root / 'package', self.root / 'run'
        self.run.mkdir()
        (self.package / 'fixtures').mkdir(parents=True)
        code = self.package / 'code/fnos/integration/native_photos/semantic768_backend.py'
        code.parent.mkdir(parents=True)
        code.write_bytes(Path(backend.__file__).read_bytes())
        self.model, self.executable = self.root / 'model', self.root / 'native.bin'
        self.model.write_bytes(b'fixture ONNX')
        self.executable.write_bytes(b'fixture native executable')
        self.images = []
        for index in range(34):
            image = self.package / 'fixtures' / ('image-' + str(index))
            image.write_bytes(('fixture image ' + str(index)).encode())
            self.images.append({'path': str(image), 'sha256': evidence.sha(image)})
        self.manifest = self.package / 'fixtures/manifest.json'
        self.manifest.write_text(json.dumps({'images': self.images[:28], 'oracle_images': self.images[28:]}))
        files = [code, self.manifest] + [Path(row['path']) for row in self.images]
        package_manifest = self.package / 'package-manifest.json'
        package_manifest.write_text(json.dumps({'files': [{'path': str(path.relative_to(self.package)),
                                                          'sha256': evidence.sha(path)} for path in files]}))
        self.report = {'status': 'native_preprocessing_pass', 'gates': {'cosine_min': .999999, 'max_abs_max': 2e-5},
                       'samples': [{'image': row['path'], 'passed': True, 'cosine': 1.0, 'max_abs': 0.0} for row in self.images]}
        (self.run / 'oracle.json').write_text(json.dumps(self.report))
        state = {'status': 'oracle_only_pass', 'cleanup_complete': True, 'native_onnx_threads': 1,
                 'services': [{'name': 'cpu', 'last_health_response': {'code': 0, 'data': True},
                               'command': [str(self.executable), '--onnx_model', str(self.model), '--model_format', 'ONNX',
                                           '--edition', 'domestic', '--num_threads_onnx', '1']}]}
        (self.run / 'state.json').write_text(json.dumps(state))
        self.runtime = self.run / 'runtime.json'
        self.runtime.write_text('{"fixture":"fingerprint is tested separately"}')
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(evidence, 'R3_PACKAGE_MANIFEST_SHA256', evidence.sha(package_manifest)))
        self.stack.enter_context(patch.object(evidence, 'NATIVE_IMAGE_SHA256', evidence.sha(self.model)))
        self.stack.enter_context(patch.object(evidence, 'NATIVE_ENTRY_SHA256', evidence.sha(self.executable)))
        self.runtime_check = self.stack.enter_context(patch.object(evidence, 'checked_runtime', return_value={'fingerprint_sha256': 'fixture'}))
        self.output = self.run / 'attestation.json'

    def tearDown(self):
        self.stack.close()
        self.temporary.cleanup()

    def attest(self):
        return evidence.attest(self.run, self.package, self.manifest, self.runtime, self.output)

    def test_complete_evidence_reuses_all_34_samples_without_reinference(self):
        self.attest()
        result = evidence.verify_reuse(self.output, self.manifest, self.model, expected_threads=1)
        self.assertEqual(len(result['samples']), 34)
        self.assertTrue(self.runtime_check.called)

    def test_changed_image_or_thread_profile_cannot_reuse(self):
        self.attest()
        with self.assertRaisesRegex(ValueError, 'thread'):
            evidence.verify_reuse(self.output, self.manifest, self.model, expected_threads=2)
        Path(self.images[0]['path']).write_bytes(b'different image')
        with self.assertRaises(ValueError):
            evidence.verify_reuse(self.output, self.manifest, self.model)

    def test_failed_gate_rejected_even_if_report_hash_is_updated(self):
        self.attest()
        self.report['samples'][0]['cosine'] = .999998
        (self.run / 'oracle.json').write_text(json.dumps(self.report))
        changed = json.loads(self.output.read_text())
        changed['source_report_sha256'] = evidence.sha(self.run / 'oracle.json')
        self.output.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'sample'):
            evidence.verify_reuse(self.output, self.manifest, self.model)

    def test_partial_oracle_not_attested(self):
        self.report['samples'].pop()
        (self.run / 'oracle.json').write_text(json.dumps(self.report))
        with self.assertRaisesRegex(ValueError, 'sample'):
            self.attest()
        self.assertFalse(self.output.exists())

    def test_dependency_change_or_changed_preprocessing_rejects_reuse(self):
        self.attest()
        with patch.object(evidence, 'checked_runtime', side_effect=ValueError('dependency changed')):
            with self.assertRaisesRegex(ValueError, 'dependency'):
                evidence.verify_reuse(self.output, self.manifest, self.model)
        wrong = self.root / 'changed_backend.py'
        wrong.write_text('# another preprocessing implementation')
        with patch.object(backend, '__file__', str(wrong)):
            with self.assertRaisesRegex(ValueError, 'preprocessing'):
                evidence.verify_reuse(self.output, self.manifest, self.model)

    def test_progress_snapshot_is_complete_json_and_replaced_atomically(self):
        path = self.root / 'progress.json'
        publish_report(path, {'progress': {'completed_requests': 1}})
        publish_report(path, {'progress': {'completed_requests': 2}})
        self.assertEqual(json.loads(path.read_text())['progress']['completed_requests'], 2)
        self.assertFalse(path.with_name(path.name + '.tmp').exists())


if __name__ == '__main__':
    unittest.main()
