"""First-boot failure/retry cases; every path and native command is isolated."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fnos.integration.image_assembly import product_bootstrap as runtime
from fnos.integration.image_assembly.product_inputs import stage_product_inputs, verify_product_inputs, APP_DIRS
from fnos.integration.image_assembly.product_inputs import RETIRED_GETTY


class ProductBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'root'
        (self.root / 'etc').mkdir(parents=True)
        (self.root / 'etc/os-release').write_text('ID=debian\n')
        self.inputs = self.base / 'inputs'
        self.inputs.mkdir()
        rows = []
        for name, directory in zip(runtime.APPS, APP_DIRS):
            for filename, data in [('manifest', ('appname="' + name + '"\n').encode()),
                                   ('app.tgz', b'isolated-test-payload'), ('cmd/main', b'#!/bin/sh\n')]:
                p = self.inputs / 'apps' / directory / filename
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                rows.append({'path': str(p.relative_to(self.inputs)), 'bytes': len(data), 'sha256': runtime.digest(p)})
        p = self.inputs / 'ai-models/face-model-1.0.1/det.onnx'
        p.parent.mkdir(parents=True)
        p.write_bytes(b'isolated-test-model')
        rows.append({'path': str(p.relative_to(self.inputs)), 'bytes': p.stat().st_size, 'sha256': runtime.digest(p)})
        (self.inputs / 'inputs.json').write_text(json.dumps({'schema': 'fnos-clean-product-inputs/v1', 'provenance': 'test fixture', 'files': rows}))
        old_console = self.root / RETIRED_GETTY
        old_console.parent.mkdir(parents=True)
        old_console.symlink_to('/lib/systemd/system/serial-getty@.service')
        self.report = stage_product_inputs(self.root, self.inputs)
        (self.root / 'usr/lib/fnos-x5/photos-python').mkdir()
        cli = self.root / runtime.CLI
        cli.parent.mkdir(parents=True, exist_ok=True)
        cli.write_bytes(b'fake command never executed')
        self.hash_patch = patch.object(runtime, 'CLI_SHA', runtime.digest(cli))
        self.hash_patch.start()
        # These tests isolate installation/retry orchestration. SQL behavior
        # and choice preservation have separate real SQLite/PostgreSQL tests.
        self.ai_patch = patch.object(runtime.product_ai_defaults, 'seed_ai', return_value={'inserted': 3})
        self.photos_patch = patch.object(runtime.product_ai_defaults, 'seed_photos', return_value={'inserted': 0})
        self.ai_patch.start()
        self.photos_patch.start()
        (self.root / 'proc/self').mkdir(parents=True)
        (self.root / 'proc/self/mountinfo').write_text('')
        (self.root / 'vol1').mkdir()
        self.installed, self.running = set(), set()
        self.calls = []
        self.fail_install = False
        self.fail_start = False

    def tearDown(self):
        self.hash_patch.stop()
        self.ai_patch.stop()
        self.photos_patch.stop()
        self.temp.cleanup()

    def mount_volume(self):
        (self.root / 'proc/self/mountinfo').write_text('31 22 8:3 / /vol1 rw,relatime - btrfs /dev/fake rw\n')

    def runner(self, command, **kwargs):
        args = command[1:]
        self.calls.append(args)
        output, rc = '', 0
        if args[:3] == ['-m', 'venv', '--without-pip']:
            (Path(args[3]) / 'lib/python3.11/site-packages').mkdir(parents=True)
        elif args[:2] == ['-I', '-c']:
            output = 'FNOS_AI_IMPORTS_OK\n'
        elif args[0] == 'default-volume':
            output = '1\n'
        elif args[0] == 'check':
            output = 'Installed\n' if args[1] in self.installed else 'Not Installed\n'
        elif args[0] == 'status':
            output = 'running\n' if args[1] in self.running else 'stopped\n'
        elif args[0] == 'install-local':
            source = Path(args[args.index('-d') + 1])
            name = (source / 'manifest').read_text().split('"')[1]
            self.installed.add(name)
            if self.fail_install:
                rc = 4
            else:
                self.running.add(name)
        elif args[0] in ('start', 'restart'):
            if self.fail_start:
                rc = 5
            else:
                self.running.add(args[1])
        elif args == ['try-restart', 'ai_manager.service']:
            pass
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, rc, output, '')

    def test_unmounted_default_volume_is_waiting_not_root_fallback(self):
        result = runtime.Bootstrap(self.root, self.runner).run(execute=True)
        self.assertEqual(result['status'], 'waiting_for_native_data_volume')
        self.assertFalse((self.root / 'vol1/@sysappmeta').exists())
        self.assertEqual(self.calls, [['default-volume']])

    def test_install_and_durable_completion_respect_later_uninstall(self):
        self.mount_volume()
        result = runtime.Bootstrap(self.root, self.runner).run(execute=True)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.running, set(runtime.APPS))
        self.assertEqual(result['models']['new_files'], 1)
        self.installed.clear()
        count = len(self.calls)
        self.assertEqual(runtime.Bootstrap(self.root, self.runner).run(execute=True)['status'], 'already_complete')
        self.assertEqual(len(self.calls), count)

    def test_preexisting_stopped_apps_are_not_started_or_reinstalled(self):
        self.mount_volume()
        self.installed.update(runtime.APPS)
        result = runtime.Bootstrap(self.root, self.runner).run(execute=True)
        self.assertEqual(result['status'], 'complete')
        self.assertFalse(self.running)
        self.assertFalse(any(c[0] in ('install-local', 'start') for c in self.calls))

    def test_partial_install_failure_resumes_start_instead_of_false_success(self):
        self.mount_volume()
        self.fail_install = True
        with self.assertRaises(RuntimeError):
            runtime.Bootstrap(self.root, self.runner).run(execute=True)
        self.assertFalse((self.root / runtime.STATE / 'complete.json').exists())
        self.fail_install = False
        result = runtime.Bootstrap(self.root, self.runner).run(execute=True)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.running, set(runtime.APPS))
        self.assertIn(['start', runtime.APPS[0]], self.calls)

    def test_model_conflict_retains_existing_file_and_does_not_install(self):
        self.mount_volume()
        target = self.root / 'vol1/@sysappmeta/ai-manager/models/face-model-1.0.1/det.onnx'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'newer user model')
        with self.assertRaisesRegex(RuntimeError, 'differs'):
            runtime.Bootstrap(self.root, self.runner).run(execute=True)
        self.assertEqual(target.read_bytes(), b'newer user model')
        self.assertFalse(self.installed)

    def test_ai_configuration_failure_is_retryable_not_complete(self):
        self.mount_volume()
        with patch.object(runtime.product_ai_defaults, 'seed_ai', side_effect=RuntimeError('database not ready')):
            with self.assertRaisesRegex(RuntimeError, 'database not ready'):
                runtime.Bootstrap(self.root, self.runner).run(execute=True)
        self.assertFalse((self.root / runtime.STATE / 'complete.json').exists())
        self.assertFalse(self.installed)
        self.assertEqual(runtime.Bootstrap(self.root, self.runner).run(execute=True)['status'], 'complete')

    def test_broken_python_runtime_is_not_published_as_installed(self):
        self.mount_volume()
        def fail_import(command, **kwargs):
            if command[1:3] == ['-I', '-c']:
                return subprocess.CompletedProcess(command, 1, '', 'missing module')
            return self.runner(command, **kwargs)
        with self.assertRaisesRegex(RuntimeError, 'import check'):
            runtime.Bootstrap(self.root, fail_import).run(execute=True)
        self.assertFalse((self.root / 'vol1/@sysappmeta/ai-manager/python/3_11_arm').exists())
        self.assertFalse((self.root / runtime.STATE / 'complete.json').exists())
        self.assertFalse(self.installed)

    def test_tampered_source_is_rejected_before_volume_mutation(self):
        self.mount_volume()
        p = self.root / runtime.PAYLOAD / 'ai-models/face-model-1.0.1/det.onnx'
        p.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            runtime.Bootstrap(self.root, self.runner).run(execute=True)
        self.assertFalse((self.root / 'vol1/@sysappmeta').exists())

    def test_data_volume_symlink_is_rejected(self):
        self.mount_volume()
        (self.root / 'vol1').rmdir()
        outside = self.base / 'not-a-data-volume'
        outside.mkdir()
        (self.root / 'vol1').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            runtime.Bootstrap(self.root, self.runner).run(execute=True)
        self.assertEqual(list(outside.iterdir()), [])

    def test_final_image_manifest_detects_removed_timer(self):
        verify_product_inputs(self.root, self.report)
        (self.root / 'etc/systemd/system/timers.target.wants/fnos-product-bootstrap.timer').unlink()
        with self.assertRaisesRegex(ValueError, 'timer'):
            verify_product_inputs(self.root, self.report)

    def test_foreign_console_removed_and_reintroduction_rejected(self):
        old_console = self.root / RETIRED_GETTY
        self.assertFalse(old_console.is_symlink())
        old_console.symlink_to('/lib/systemd/system/serial-getty@.service')
        with self.assertRaisesRegex(ValueError, 'retired source-image'):
            verify_product_inputs(self.root, self.report)


if __name__ == '__main__':
    unittest.main()
