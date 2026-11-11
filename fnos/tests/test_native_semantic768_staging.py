"""Offline staging and native model-ready dispatch failure boundaries."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fnos.integration.image_assembly import native_semantic768 as stage
from fnos.integration.image_assembly import semantic768_ready as ready


def sha(value):
    return hashlib.sha256(value).hexdigest()


def complete_review():
    return {'schema': 'fnos-semantic768-activation-review/v1', 'reviewer': 'Codex-independent',
            'status': 'PASS', 'artifacts': ready.artifact_identity(),
            'gates': {name: {'status': 'PASS', 'evidence_sha256': 'a' * 64}
                      for name in ('native_preprocess_oracle', 'equal_budget_performance',
                                   'production_default_performance', 'native_chinese_search',
                                   'mixed_cpu_bpu_index')}}


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'root'
        self.root.mkdir()
        self.assets = Path(self.tmp.name) / 'assets'
        self.assets.mkdir()
        self.put('etc/os-release', b'fixture OS')
        self.original = b'fixture vendor ELF'
        self.put(stage.ENTRY, self.original, 0o755)
        self.put('vol1/@appdata/trim.photos/existing-vectors.db', b'old native CPU vectors')
        photos_files = []
        for relative in [*(stage.NATIVE_MODULES / name for name in ('__init__.py', 'backend.py', 'storage_roots.py')),
                         'usr/lib/fnos-x5/photos-python/numpy/__init__.py',
                         'usr/lib/fnos-x5/photos-python/PIL/__init__.py']:
            self.put(relative, b'shared dependency')
            photos_files.append({'path': str(relative), 'sha256': sha(b'shared dependency'), 'mode': 0o644})
        photos = {'schema': 'fnos-native-photos/v1', 'namespace': 'fixture', 'files': photos_files}
        self.put(stage.PHOTOS_MANIFEST, json.dumps(photos).encode())
        self.patchers = [patch.object(ready, 'NATIVE_SHA', sha(self.original)),
                         patch.object(ready, 'MODEL_SHA', sha(b'fixture model')),
                         patch.object(ready, 'WORKER_SHA', sha(b'fixture worker'))]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)
        assets = {}
        rows = []
        for name, data in [('native_cn_image.bin', b'fixture model'), ('semantic768-worker', b'fixture worker')]:
            (self.assets / name).write_bytes(data)
            _, target, mode = stage.ASSETS[name]
            assets[name] = (sha(data), target, mode)
            rows.append({'path': name, 'sha256': sha(data)})
        p = patch.object(stage, 'ASSETS', assets)
        p.start()
        self.addCleanup(p.stop)
        (self.assets / 'assets.json').write_text(json.dumps({'schema': 'fnos-semantic768-assets/v1', 'files': rows}))
        self.review = Path(self.tmp.name) / 'activation.json'
        self.review.write_text(json.dumps(complete_review()))

    def put(self, relative, value, mode=0o644):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        path.chmod(mode)

    def install(self):
        return stage.stage_native_semantic768(self.root, self.assets, activation_review=self.review,
                                             activation_review_sha256=ready.digest(self.review))

    def test_stage_disabled_preserves_cpu_shared_payload_and_existing_vectors(self):
        before = (self.root / stage.PHOTOS_MANIFEST).read_bytes()
        result = stage.stage_native_semantic768(self.root)
        self.assertFalse(result['enabled'])
        self.assertFalse((self.root / stage.ORIGINAL).exists())
        self.assertEqual((self.root / stage.ENTRY).read_bytes(), self.original)
        self.assertFalse((self.root / stage.READY).exists())
        self.assertFalse((self.root / stage.POLICY).exists())
        self.assertEqual((self.root / stage.PHOTOS_MANIFEST).read_bytes(), before)
        self.assertEqual((self.root / 'vol1/@appdata/trim.photos/existing-vectors.db').read_bytes(), b'old native CPU vectors')
        self.assertTrue(stage.verify_native_semantic768(self.root, result)['native_cpu_preserved'])

    def test_enabled_staging_preserves_original_before_replacing_entry(self):
        result = self.install()
        self.assertTrue(result['enabled'])
        self.assertEqual((self.root / stage.ORIGINAL).read_bytes(), self.original)
        self.assertEqual((self.root / stage.ENTRY).read_bytes(), stage.LAUNCHER)
        self.assertTrue(stage.verify_native_semantic768(self.root, result)['native_cpu_preserved'])

    def test_disabled_staging_rejects_dormant_experimental_payload(self):
        self.put('usr/share/fnos-x5/models/semantic768/native_cn_image.bin', b'old candidate')
        with self.assertRaisesRegex(ValueError, 'dormant payload'):
            stage.stage_native_semantic768(self.root)
        self.assertFalse((self.root / stage.MANIFEST).exists())

    def test_bad_asset_rejected_before_any_mutation(self):
        (self.assets / 'native_cn_image.bin').write_bytes(b'bad')
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual((self.root / stage.ENTRY).read_bytes(), self.original)
        self.assertFalse((self.root / stage.ORIGINAL).exists())

    def test_symlink_destination_rejected_before_preserving_original(self):
        (self.root / 'etc/fnos-x5').mkdir()
        (self.root / stage.POLICY).symlink_to('/tmp/outside')
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse((self.root / stage.ORIGINAL).exists())

    def test_shared_dependency_corruption_rejected(self):
        self.put(stage.NATIVE_MODULES / 'backend.py', b'changed')
        with self.assertRaises(ValueError):
            self.install()

    def test_frozen_runtime_drift_rejected(self):
        digest = ready.digest
        def changed(path):
            return '0' * 64 if path.name == 'semantic768_entry.py' else digest(path)
        with patch.object(ready, 'digest', side_effect=changed):
            with self.assertRaises(ValueError):
                self.install()
        self.assertFalse((self.root / stage.ORIGINAL).exists())

    def test_wrong_preserved_cpu_not_overwritten(self):
        self.put(stage.ORIGINAL, b'unknown ELF')
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual((self.root / stage.ORIGINAL).read_bytes(), b'unknown ELF')

    def test_cpu_entry_remains_intact_if_atomic_replacement_fails(self):
        replace = os.replace
        def fail_entry(source, target):
            if target == self.root / stage.ENTRY:
                raise OSError('simulated rename error')
            return replace(source, target)
        with patch.object(stage.os, 'replace', side_effect=fail_entry):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual((self.root / stage.ENTRY).read_bytes(), self.original)
        self.assertEqual((self.root / stage.ORIGINAL).read_bytes(), self.original)

    def test_final_readback_detects_payload_tampering(self):
        report = self.install()
        self.put(stage.READY, b'corrupt')
        with self.assertRaises(ValueError):
            stage.verify_native_semantic768(self.root, report)

    def test_complete_independent_review_is_required_and_pinned(self):
        review = Path(self.tmp.name) / 'review.json'
        value = complete_review()
        value['gates'].pop('production_default_performance')
        review.write_text(json.dumps(value))
        self.assertFalse(stage.activation_policy(review, ready.digest(review))['enabled'])
        value = complete_review()
        review.write_text(json.dumps(value))
        self.assertTrue(stage.activation_policy(review, ready.digest(review))['enabled'])
        with self.assertRaises(ValueError):
            stage.activation_policy(review, '0' * 64)

    def test_activation_bound_to_exact_runtime_model_and_original(self):
        review = complete_review()
        review['artifacts']['model_sha256'] = '0' * 64
        self.assertFalse(ready.review_passed(review))
        review = complete_review()
        review['reviewer'] = 'benchmark-self-report'
        self.assertFalse(ready.review_passed(review))


class ReadyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.image = Path('/vol7/@sysappmeta/ai-manager/models/img2vec-medium-model-1.0.0/img2vec.onnx')
        self.text = self.image.parents[1] / 'txt2vec-medium-model-1.0.0/txt2vec.onnx'
        self.tokenizer = self.text.with_name('sp.model')
        self.argv = ['--onnx_model', str(self.image), '--socket_path', '/run/actual-native.sock',
                     '--cpu_usage', '50', '--num_threads_onnx', '3', '--edition', 'domestic']

    def model_digest(self, path):
        return {self.image: ready.IMAGE_SHA, self.text: ready.TEXT_SHA,
                self.tokenizer: ready.TOKENIZER_SHA}[path]

    def test_actual_native_launch_pairs_same_volume_without_guessing_socket(self):
        with patch.object(ready, 'digest', side_effect=self.model_digest):
            config = ready.config_for_launch(self.argv, {'enabled': False})
        self.assertEqual(config['socket_path'], '/run/actual-native.sock')
        self.assertEqual(config['native_text_model'], str(self.text))
        self.assertFalse(config['enabled'])

    def test_enabled_boolean_without_full_independent_review_stays_cpu(self):
        with patch.object(ready, 'digest', side_effect=self.model_digest):
            config = ready.config_for_launch(self.argv, {'enabled': True})
        self.assertFalse(config['enabled'])
        self.assertIsNone(config['activation_acceptance'])

    def test_valid_evidence_and_all_native_models_allow_explicit_activation(self):
        with patch.object(ready, 'digest', side_effect=self.model_digest):
            config = ready.config_for_launch(self.argv, {'enabled': True, 'activation_review': complete_review()})
        self.assertTrue(config['enabled'])
        self.assertEqual(config['activation_acceptance'], ready.ACCEPTANCE)

    def test_wrong_tokenizer_rejected(self):
        def digest(path):
            return '0' * 64 if path == self.tokenizer else self.model_digest(path)
        with patch.object(ready, 'digest', side_effect=digest):
            with self.assertRaises(ValueError):
                ready.config_for_launch(self.argv, {'enabled': True})

    def test_non_native_cache_or_other_model_not_selected(self):
        for path in ('/tmp/img2vec.onnx', '/vol1/@sysappmeta/ai-manager/models/img2vec-large-model-1.0.0/img2vec.onnx'):
            with self.assertRaises(ValueError):
                ready.model_pair(Path(path))

    def test_unknown_arguments_and_other_edition_keep_native_behavior(self):
        for extra in (['--accelerator', 'unknown'], ['--edition', 'international'], ['--runtime-config', '/tmp/forged.json']):
            with self.assertRaises(ValueError):
                ready.native_selection(self.argv + extra)

    def test_missing_policy_delegates_exact_arguments_to_cpu(self):
        with patch.object(ready, 'POLICY', self.base / 'absent.json'), patch.object(ready, 'cpu_exec') as cpu:
            ready.main(self.argv)
        cpu.assert_called_once_with(self.argv)

    def test_missing_model_delegates_without_clearing_index(self):
        policy = self.base / 'policy.json'
        policy.write_text('{"enabled":false}')
        with patch.object(ready, 'POLICY', policy), patch.object(ready, 'cpu_exec') as cpu:
            ready.main(self.argv)
        cpu.assert_called_once_with(self.argv)

    def test_runtime_write_private_atomic_and_distinct_socket(self):
        runtime = self.base / 'run/semantic768'
        runtime.parent.mkdir(mode=0o700)
        with patch.object(ready, 'RUNTIME', runtime):
            first = ready.write_runtime({'socket_path': '/run/a', 'enabled': False})
            second = ready.write_runtime({'socket_path': '/run/b', 'enabled': False})
        self.assertNotEqual(first, second)
        self.assertEqual(first.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(first.read_text())['socket_path'], '/run/a')
        self.assertFalse(list(runtime.glob('.ready-*')))

    def test_runtime_symlink_parent_rejected(self):
        real = self.base / 'real'
        real.mkdir()
        (self.base / 'linked').symlink_to(real, target_is_directory=True)
        with patch.object(ready, 'RUNTIME', self.base / 'linked/semantic768'):
            with self.assertRaises(ValueError):
                ready.write_runtime({'socket_path': '/run/a'})

    def test_runtime_write_failure_delegates_exact_args(self):
        policy = self.base / 'policy.json'
        policy.write_text('{"enabled":false}')
        with patch.object(ready, 'POLICY', policy), patch.object(ready, 'digest', side_effect=self.model_digest), \
                patch.object(ready, 'write_runtime', side_effect=OSError(28, 'disk full')), \
                patch.object(ready, 'cpu_exec') as cpu:
            ready.main(self.argv)
        cpu.assert_called_once_with(self.argv)

    def test_running_service_failure_does_not_start_a_second_cpu_service(self):
        from fnos.integration.native_photos import semantic768_entry
        policy = self.base / 'policy.json'
        policy.write_text('{"enabled":false}')
        with patch.object(ready, 'POLICY', policy), patch.object(ready, 'digest', side_effect=self.model_digest), \
                patch.object(ready, 'write_runtime', return_value=self.base / 'runtime.json'), \
                patch.object(ready, 'cpu_exec') as cpu, \
                patch.object(semantic768_entry, 'main', side_effect=RuntimeError('already bound')):
            with self.assertRaisesRegex(RuntimeError, 'already bound'):
                ready.main(self.argv)
        cpu.assert_not_called()


if __name__ == '__main__':
    unittest.main()
