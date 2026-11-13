"""First-boot failure/retry cases; every path and native command is isolated."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
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
        self.report = stage_product_inputs(self.root, self.inputs, require_accel_complete=False)
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

    def bundle_dirs(self, root):
        accel = root / 'usr/share/fnos-x5/accel'
        return [accel, *(p for p in accel.rglob('*')
                         if p.is_dir() and not p.is_symlink())]

    def verify_strict_as_root(self, root, report):
        # fakeroot makes the staging tree root-owned on the build server.  This
        # keeps the real modes and simulates only that ownership view.
        real_lstat = Path.lstat

        def root_lstat(path):
            info = real_lstat(path)
            return types.SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_gid=0)

        with patch.object(Path, 'lstat', new=root_lstat):
            return verify_product_inputs(root, report, require_root_owner=True)

    def mount_volume(self):
        (self.root / 'proc/self/mountinfo').write_text('31 22 8:3 / /vol1 rw,relatime - btrfs /dev/fake rw\n')

    def runner(self, command, **kwargs):
        args = command[1:]
        self.calls.append(args)
        output, rc = '', 0
        if args[:3] == ['-m', 'venv', '--without-pip']:
            (Path(args[3]) / 'lib/python3.11/site-packages').mkdir(parents=True)
        elif args[:2] == ['-S', '-c']:
            self.assertEqual(kwargs['cwd'], str(self.root))
            output = 'FNOS_AI_EMBEDDED_IMPORTS_OK\n'
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
        elif args[0] == 'stop':
            self.running.discard(args[1])
        elif args[0] == 'start':
            if self.fail_start:
                rc = 5
            else:
                self.running.add(args[1])
        elif args == ['try-restart', 'ai_manager.service']:
            pass
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, rc, output, '')

    def test_emergency_readonly_volume_is_not_ready(self):
        for options in ('rw,relatime,emergency_ro', 'ro,relatime'):
            with self.subTest(options=options):
                (self.root / 'proc/self/mountinfo').write_text(
                    '31 22 253:0 / /vol1 rw,relatime - ext4 /dev/dm-0 ' + options + '\n')
                self.assertIsNone(runtime.Bootstrap(self.root, self.runner).volume())

    def test_runtime_repair_lock_busy_returns_explicit_skip(self):
        import fcntl
        state = self.root / 'var/lib/fnos-x5/product-bootstrap'
        state.mkdir(parents=True)
        with (state / 'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = runtime.Bootstrap(self.root, self.runner).repair_runtime()
        self.assertEqual(result['status'], 'skipped_busy')
        self.assertIn('lock', result['reason'])
        self.assertFalse((state / 'runtime-health.json').exists())

    def test_module_health_marks_image_timer_as_not_owner(self):
        health = self.root / 'var/lib/fnos-x5/accel/module-health.json'
        health.parent.mkdir(parents=True)
        for status, owned in (('installed', True), ('waiting_for_native_apps', True),
                              ('uninstalled', False), ('uninstalled_with_preserved_changes', False)):
            health.write_text(json.dumps({'status': status}))
            self.assertEqual(runtime._module_owned(self.root), owned, status)
        health.write_text('{}')
        self.assertFalse(runtime._module_owned(self.root))
        health.unlink()
        self.assertFalse(runtime._module_owned(self.root))

    def test_runtime_unit_skips_when_module_owns_repair(self):
        unit = (self.root / 'etc/systemd/system/fnos-product-runtime.service').read_text()
        self.assertIn('--module-owned-check', unit)

    def test_unmounted_default_volume_is_waiting_not_root_fallback(self):
        result = runtime.Bootstrap(self.root, self.runner).run(execute=True)
        self.assertEqual(result['status'], 'waiting_for_native_data_volume')
        self.assertFalse((self.root / 'vol1/@sysappmeta').exists())
        self.assertEqual(self.calls, [['default-volume']])

    def test_web_install_volume_when_cli_default_is_unset(self):
        self.mount_volume()
        for name in runtime.APPS:
            meta = self.root / 'var/apps' / name / 'meta'
            meta.parent.mkdir(parents=True)
            target = self.root / 'vol1/@appmeta' / name
            target.mkdir(parents=True)
            meta.symlink_to('/vol1/@appmeta/' + name)
        with patch.object(runtime.Bootstrap, 'cli', return_value='0'):
            self.assertEqual(runtime.Bootstrap(self.root).volume(), ('1', self.root / 'vol1'))

    def test_unset_cli_does_not_guess_from_mounted_disks(self):
        self.mount_volume()
        with patch.object(runtime.Bootstrap, 'cli', return_value='0'):
            self.assertIsNone(runtime.Bootstrap(self.root).volume())

    def test_web_metadata_cannot_override_explicit_missing_default(self):
        self.mount_volume()
        meta = self.root / 'var/apps/trim.photos/meta'
        meta.parent.mkdir(parents=True)
        meta.symlink_to('/vol1/@appmeta/trim.photos')
        with patch.object(runtime.Bootstrap, 'cli', return_value='2'):
            self.assertIsNone(runtime.Bootstrap(self.root).volume())

    def test_web_metadata_disagreement_stays_waiting(self):
        self.mount_volume()
        for index, name in enumerate(runtime.APPS, 1):
            meta = self.root / 'var/apps' / name / 'meta'
            meta.parent.mkdir(parents=True)
            target = self.root / ('vol' + str(index)) / '@appmeta' / name
            target.mkdir(parents=True)
            meta.symlink_to('/vol' + str(index) + '/@appmeta/' + name)
        with patch.object(runtime.Bootstrap, 'cli', return_value='0'):
            self.assertIsNone(runtime.Bootstrap(self.root).volume())

    def test_web_metadata_unmounted_volume_stays_waiting(self):
        meta = self.root / 'var/apps/trim.photos/meta'
        meta.parent.mkdir(parents=True)
        (self.root / 'vol1/@appmeta/trim.photos').mkdir(parents=True)
        meta.symlink_to('/vol1/@appmeta/trim.photos')
        with patch.object(runtime.Bootstrap, 'cli', return_value='0'):
            self.assertIsNone(runtime.Bootstrap(self.root).volume())

    def test_embedded_runtime_packages_visible_without_pth_processing(self):
        deps = self.root / 'usr/lib/fnos-x5/photos-python'
        (deps / 'example_package').mkdir()
        (deps / 'example_package/__init__.py').write_text('VALUE = 42\n')
        runtime.Bootstrap(self.root, self.runner).seed_python(self.root / 'vol1')
        site = self.root / 'vol1/@sysappmeta/ai-manager/python/3_11_arm/lib/python3.11/site-packages'
        import os, sys
        result = subprocess.run([sys.executable, '-S', '-c',
                                 'import example_package; assert example_package.VALUE == 42'],
                                env={**os.environ, 'PYTHONPATH': str(site)}, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_embedded_import_failure_prevents_success_state(self):
        def fail_embedded(command, **kwargs):
            if command[1:3] == ['-S', '-c']:
                return subprocess.CompletedProcess(command, 1, '', 'ModuleNotFoundError: numpy')
            return self.runner(command, **kwargs)
        bootstrap = runtime.Bootstrap(self.root, fail_embedded)
        with self.assertRaisesRegex(RuntimeError, 'embedded'):
            bootstrap.seed_python(self.root / 'vol1')
        self.assertNotIn('python', bootstrap.state)

    def test_install_and_durable_completion_respect_later_uninstall(self):
        self.mount_volume()
        result = runtime.Bootstrap(self.root, self.runner).run(execute=True)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.running, set(runtime.APPS))
        self.assertIn(['stop', 'trim.photos'], self.calls)
        self.assertIn(['start', 'trim.photos'], self.calls)
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

    def test_staged_bundle_directories_are_0755_and_strictly_verified(self):
        for path in self.bundle_dirs(self.root):
            self.assertEqual(path.stat().st_mode & 0o777, 0o755, path)
        result = self.verify_strict_as_root(self.root, self.report)
        self.assertEqual(result['apps'], list(runtime.APPS))

    def test_group_write_umask_is_not_inherited_by_staged_bundle(self):
        root = self.base / 'umask-root'
        (root / 'etc').mkdir(parents=True)
        (root / 'etc/os-release').write_text('ID=debian\n')
        old_umask = os.umask(0o002)
        try:
            report = stage_product_inputs(root, self.inputs, require_accel_complete=False)
        finally:
            os.umask(old_umask)
        for path in self.bundle_dirs(root):
            self.assertEqual(path.stat().st_mode & 0o777, 0o755, path)
        self.verify_strict_as_root(root, report)

    def test_strict_verifier_reports_relative_path_uid_gid_and_mode(self):
        accel = self.root / 'usr/share/fnos-x5/accel'
        accel.chmod(0o775)
        with self.assertRaises(ValueError) as raised:
            self.verify_strict_as_root(self.root, self.report)
        message = str(raised.exception)
        self.assertIn('unsafe product bootstrap ownership or mode', message)
        self.assertIn('usr/share/fnos-x5/accel', message)
        self.assertRegex(message, r'uid=0 gid=0 mode=0775')

    def test_strict_verifier_checks_empty_bundle_directory_mode(self):
        empty = self.root / 'usr/share/fnos-x5/accel/empty'
        empty.mkdir()
        empty.chmod(0o775)
        with self.assertRaises(ValueError) as raised:
            self.verify_strict_as_root(self.root, self.report)
        message = str(raised.exception)
        self.assertIn('usr/share/fnos-x5/accel/empty', message)
        self.assertRegex(message, r'uid=0 gid=0 mode=0775')

    def test_staged_bootstrap_runs_without_repository_import(self):
        import os, sys
        script = self.root / 'usr/lib/fnos-x5/product_bootstrap.py'
        self.assertTrue(script.is_file())
        result = subprocess.run([sys.executable, str(script), '--help'],
                                cwd=str(self.root / 'usr/lib/fnos-x5'), capture_output=True,
                                env={**os.environ, 'PYTHONPATH': ''})
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn('--repair-runtime', result.stdout.decode())

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
