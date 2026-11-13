"""Lifecycle-level tests for the standalone module installer (no board access)."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fnos.integration.acceleration.module.app import module_install
from fnos.integration.image_assembly import product_accel
from fnos.tests.test_product_accel import Fixture


class ModuleInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.f = Fixture(self.base)
        self.package = self.base / 'package'
        self.package.mkdir()
        for name in ('module_install.py', *module_install.RUNTIME_NAMES):
            source = Path(__file__).parents[1] / 'integration/image_assembly' / name
            if name == 'module_install.py':
                source = Path(module_install.__file__)
            (self.package / name).write_bytes(source.read_bytes())
        (self.package / 'payload').mkdir()
        (self.package / 'payload/accel').mkdir(parents=True)
        for name in ('bundle.json', 'compat.json'):
            (self.package / 'payload/accel' / name).write_bytes((self.f.bundle / name).read_bytes())
        (self.package / 'payload/accel/files').mkdir()
        for source in (self.f.bundle / 'files').rglob('*'):
            if source.is_file():
                dest = self.package / 'payload/accel/files' / source.relative_to(self.f.bundle / 'files')
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(source.read_bytes())
        models = self.package / 'payload/product-inputs/ai-models/test-model'
        models.mkdir(parents=True)
        (models / 'model.bin').write_bytes(b'model\n')
        (self.package / 'payload/product-inputs/inputs.json').write_text(json.dumps({
            'schema': 'fnos-product-inputs/v1', 'provenance': 'test',
            'apps': [], 'files': [{'path': 'ai-models/test-model/model.bin',
                                   'bytes': 6, 'sha256': product_accel.digest(models / 'model.bin')}]}))
        self.ready = {'machine': 'aarch64', 'kernel': '6.18.0',
                      'modules': ['bpu_framework', 'bpu_cores', 'bpu_hw_io_x5'],
                      'devices': {'/dev/bpu_core0': True},
                      'paths': {p: True for p in (
                          '/usr/lib/hbbpu/libdnn.so', '/usr/lib/hbbpu/libhbrt_bayes_aarch64.so',
                          '/usr/lib/hbbpu/libhbrt_bayes_aarch64.so',
                          '/usr/hobot/lib/firmware/libmm/chagall.bin')}}
        self.apps = {'trim.photos': 'Installed', 'trim.media': 'Installed'}

    def tearDown(self):
        self.temp.cleanup()

    def patch_install(self, **overrides):
        values = {
            'platform_status': {'machine': 'aarch64', 'kernel': '6.18.0',
                                'modules': [], 'blocked': [], 'photos': 'ready', 'media': 'ready'},
            'apps': self.apps,
            'volume': ('1', self.f.root / 'vol1'),
        }
        values.update(overrides)
        stack = [
            patch.object(module_install.product_accel, 'platform_status', return_value=values['platform_status']),
            patch.object(module_install, '_package_apps', return_value=values['apps']),
            patch.object(module_install.product_bootstrap.Bootstrap, 'volume', return_value=values['volume']),
            patch.object(module_install.product_bootstrap.Bootstrap, 'seed_models'),
            patch.object(module_install.product_bootstrap.Bootstrap, 'seed_python'),
            patch.object(module_install.product_ai_defaults, 'check_binary'),
            patch.object(module_install.product_ai_defaults, 'seed_ai', return_value={'inserted': 3}),
            patch.object(module_install.product_ai_defaults, 'ensure_postgres_access', return_value={'changed': False}),
            patch.object(module_install.product_ai_defaults, 'seed_photos', return_value={'inserted': 2}),
            patch.object(module_install, '_reload_if_running', return_value=[]),
            patch.object(module_install.product_accel, '_platform_probe', return_value=self.ready),
        ]
        for item in stack:
            item.start()
            self.addCleanup(item.stop)

    def test_install_is_transactional_and_writes_persistent_tool(self):
        self.patch_install()
        result, code = module_install.install(self.f.root, self.package, require_complete=False)
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'installed')
        self.assertEqual((self.f.root / product_accel.PHOTOS_ENTRY).read_bytes(), b'wrapper-v1\n')
        self.assertTrue((self.f.root / 'var/lib/fnos-x5/accel/tool/module_install.py').is_file())
        self.assertTrue((self.f.root / 'usr/share/fnos-x5/accel/bundle.json').is_file())
        self.assertTrue((self.f.root / 'usr/lib/fnos-x5/product_bootstrap.py').is_file())
        self.assertTrue((self.f.root / 'etc/systemd/system/fnos-x5-accel-runtime.timer').is_file())
        self.assertTrue((self.f.root / 'etc/systemd/system/timers.target.wants/fnos-x5-accel-runtime.timer').is_symlink())
        worker = self.f.root / 'usr/lib/fnos-x5/face128_worker'
        worker.unlink()
        repaired = module_install.product_bootstrap.Bootstrap(self.f.root).repair_accel({}, require_complete=False)
        self.assertEqual(repaired['status'], 'installed')
        self.assertEqual(worker.read_bytes(), b'worker-v1\n')
        health = json.loads((self.f.root / 'var/lib/fnos-x5/accel/module-health.json').read_text())
        self.assertEqual(health['status'], 'installed')
        self.assertEqual(self.f.user_db.read_bytes(), b'user-photo-index-never-touched\n')

    def test_runtime_profile_still_seeds_face_model_selection(self):
        (self.package / 'payload/profile.json').write_text(
            '{"schema":"fnos-x5-accel-profile/v1","profile":"runtime"}\n')
        self.patch_install()
        result, code = module_install.install(self.f.root, self.package, require_complete=False)
        self.assertEqual(code, 0)
        self.assertEqual(result['profile'], 'runtime')
        call = module_install.product_ai_defaults.seed_ai.call_args
        self.assertNotIn('include_face_model', call.kwargs)

    def test_unknown_photos_upgrade_neither_seeds_models_nor_restarts_app(self):
        self.patch_install()
        (self.f.root / 'var/apps/trim.photos/target/trim-photos').write_bytes(b'new native app\n')
        result, code = module_install.install(self.f.root, self.package, require_complete=False)
        self.assertEqual(code, 0)
        self.assertEqual(result['photos']['status'], 'preserved_native_no_patch')
        module_install.product_bootstrap.Bootstrap.seed_models.assert_not_called()
        module_install.product_bootstrap.Bootstrap.seed_python.assert_not_called()
        module_install._reload_if_running.assert_called_once_with(
            self.f.root, [product_accel.MEDIA], result)

    def test_waiting_for_native_apps_does_not_install_or_touch_files(self):
        self.patch_install(apps={'trim.photos': 'Not Installed', 'trim.media': 'Not Installed'})
        result, code = module_install.install(self.f.root, self.package, require_complete=False)
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'waiting_for_native_apps')
        self.assertEqual(self.f.entry_path.read_bytes(), self.f.native_entry)
        self.assertTrue((self.f.root / 'var/lib/fnos-x5/accel/tool/module_install.py').is_file())
        self.assertTrue((self.f.root / 'etc/systemd/system/fnos-x5-accel-runtime.timer').is_file())
        self.assertTrue((self.f.root / 'etc/systemd/system/timers.target.wants/fnos-x5-accel-runtime.timer').is_symlink())

    def test_runtime_repeat_install_preserves_create_ownership_and_uninstall_cleans(self):
        state = self.f.root / 'var/lib/fnos-x5/accel'
        first = module_install._install_runtime_files(self.f.root, self.package, state)
        self.assertEqual(first['status'], 'installed')
        records = json.loads((state / 'runtime-files.json').read_text())['files']
        self.assertTrue(all(row['action'] == 'create' for row in records if row['path'].startswith('usr/lib/')))
        second = module_install._install_runtime_files(self.f.root, self.package, state)
        self.assertEqual(second['status'], 'installed')
        records = json.loads((state / 'runtime-files.json').read_text())['files']
        self.assertTrue(all(row['action'] == 'create' for row in records if row['path'].startswith('usr/lib/')))
        module_install._uninstall_runtime_files(self.f.root, state)
        self.assertFalse((self.f.root / 'etc/systemd/system/fnos-x5-accel-runtime.timer').exists())
        self.assertFalse((self.f.root / 'etc/systemd/system/timers.target.wants/fnos-x5-accel-runtime.timer').exists())
        self.assertFalse((self.f.root / 'usr/lib/fnos-x5/product_bootstrap.py').exists())

    def test_failed_upgrade_overlay_restores_previous_timer_runtime_and_bundle(self):
        self.patch_install()
        module_install.install(self.f.root, self.package, require_complete=False)
        v1_boot = (self.f.root / 'usr/lib/fnos-x5/product_bootstrap.py').read_bytes()
        v1_compat = (self.f.root / 'usr/share/fnos-x5/accel/compat.json').read_bytes()
        self.assertTrue((self.f.root / 'etc/systemd/system/fnos-x5-accel-runtime.timer').exists())
        (self.package / 'product_bootstrap.py').write_bytes(b'v2-bootstrap\n')
        new = json.loads(v1_compat)
        new['module']['version'] = '0.3.2'
        (self.package / 'payload/accel/compat.json').write_text(json.dumps(new))
        bundle = json.loads((self.package / 'payload/accel/bundle.json').read_text())
        bundle['compat'] = new
        (self.package / 'payload/accel/bundle.json').write_text(json.dumps(bundle))
        from unittest.mock import patch
        with patch.object(module_install.product_accel, 'apply_bundle',
                          side_effect=module_install.product_accel.AccelError('overlay_failed')):
            result, code = module_install.install(self.f.root, self.package, require_complete=False)
        self.assertEqual(code, 2)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual((self.f.root / 'usr/lib/fnos-x5/product_bootstrap.py').read_bytes(), v1_boot)
        self.assertEqual((self.f.root / 'usr/share/fnos-x5/accel/compat.json').read_bytes(), v1_compat)
        self.assertTrue((self.f.root / 'etc/systemd/system/fnos-x5-accel-runtime.timer').exists())
        self.assertTrue((self.f.root / 'etc/systemd/system/timers.target.wants/fnos-x5-accel-runtime.timer').is_symlink())

    def test_runtime_upgrade_failure_restores_previous_module_file(self):
        state = self.f.root / 'var/lib/fnos-x5/accel'
        module_install._install_runtime_files(self.f.root, self.package, state)
        target = self.f.root / 'usr/lib/fnos-x5/product_bootstrap.py'
        original = target.read_bytes()
        (self.package / 'product_bootstrap.py').write_bytes(b'v2-bootstrap\n')
        real = module_install._write_bytes
        def fail_timer(path, data, mode, uid=None, gid=None):
            if Path(path).name == module_install.RUNTIME_SERVICE.name:
                raise OSError('simulated timer write failure')
            return real(path, data, mode, uid, gid)
        from unittest.mock import patch
        with patch.object(module_install, 'SERVICE_TEXT', module_install.SERVICE_TEXT + '#v2\n'):
            with patch.object(module_install, '_write_bytes', side_effect=fail_timer):
                with self.assertRaises(OSError):
                    module_install._install_runtime_files(self.f.root, self.package, state)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(json.loads((state / 'runtime-files.json').read_text())['files'][0]['sha256'],
                         product_accel.digest(target))

    def test_pointer_upgrade_refreshes_compat_but_leaves_image_pointer(self):
        state = self.f.root / 'var/lib/fnos-x5/accel'
        module_install._install_runtime_files(self.f.root, self.package, state)
        module_install._stage_bundle_pointer(self.f.root, self.package, require_complete=False)
        pointer = self.f.root / 'usr/share/fnos-x5/accel/compat.json'
        before = pointer.read_bytes()
        new = json.loads(before)
        new['module']['version'] = '0.3.1'
        (self.package / 'payload/accel/compat.json').write_text(json.dumps(new))
        module_install._stage_bundle_pointer(self.f.root, self.package, require_complete=False)
        self.assertEqual(json.loads(pointer.read_text())['module']['version'], '0.3.1')
        image = self.f.base / 'image-root'
        (image / 'usr/share/fnos-x5/accel').mkdir(parents=True)
        (image / 'usr/share/fnos-x5/accel/bundle.json').write_text('{"image": true}\n')
        result = module_install._stage_bundle_pointer(image, self.package, require_complete=False)
        self.assertEqual(result['status'], 'image_owned')
        self.assertEqual((image / 'usr/share/fnos-x5/accel/bundle.json').read_text(), '{"image": true}\n')

    def test_incomplete_release_payload_rolls_back_runtime_and_timer(self):
        self.patch_install()
        result, code = module_install.install(self.f.root, self.package, require_complete=True)
        self.assertEqual(code, 2)
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse((self.f.root / 'etc/systemd/system/fnos-x5-accel-runtime.timer').exists())
        self.assertFalse((self.f.root / 'usr/lib/fnos-x5/product_bootstrap.py').exists())
        self.assertEqual(self.f.entry_path.read_bytes(), self.f.native_entry)

    def test_later_native_app_install_is_picked_up_by_persisted_repair_chain(self):
        self.patch_install(apps={'trim.photos': 'Not Installed', 'trim.media': 'Not Installed'})
        result, code = module_install.install(self.f.root, self.package, require_complete=False)
        self.assertEqual(result['status'], 'waiting_for_native_apps')
        applied = module_install.product_bootstrap.Bootstrap(self.f.root).repair_accel(
            {}, require_complete=False)
        self.assertEqual(applied['status'], 'installed')
        self.assertEqual(self.f.entry_path.read_bytes(), b'wrapper-v1\n')

    def test_blocked_platform_reports_readable_status_and_writes_nothing(self):
        self.patch_install(platform_status={'machine': 'aarch64', 'kernel': '5.15.0',
                                            'modules': [], 'blocked': ['unsupported_kernel'],
                                            'photos': 'blocked_kernel', 'media': 'blocked_kernel'})
        result, code = module_install.install(self.f.root, self.package, require_complete=False)
        self.assertEqual(code, 2)
        self.assertEqual(result['status'], 'blocked_platform')
        self.assertIn('未替换内核', result['recovery'])
        self.assertEqual(self.f.entry_path.read_bytes(), self.f.native_entry)

    def test_uninstall_restores_native_and_keeps_user_data(self):
        image_unit = self.f.root / 'etc/systemd/system/fnos-product-runtime.service'
        image_unit.parent.mkdir(parents=True, exist_ok=True)
        image_unit.write_text('image-owned runtime unit\n')
        self.patch_install()
        module_install.install(self.f.root, self.package, require_complete=False)
        self.assertTrue(module_install.product_bootstrap._module_owned(self.f.root))
        result, code = module_install.uninstall(self.f.root)
        self.assertFalse(module_install.product_bootstrap._module_owned(self.f.root))
        self.assertEqual(image_unit.read_text(), 'image-owned runtime unit\n')
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'uninstalled')
        self.assertEqual(self.f.entry_path.read_bytes(), self.f.native_entry)
        self.assertEqual(self.f.user_db.read_bytes(), b'user-photo-index-never-touched\n')
        self.assertFalse((self.f.root / 'usr/lib/fnos-x5/face128_worker').exists())
        self.assertFalse((self.f.root / 'usr/share/fnos-x5/accel/bundle.json').exists())
        self.assertFalse((self.f.root / 'etc/systemd/system/fnos-x5-accel-runtime.timer').exists())
        self.assertFalse((self.f.root / 'usr/lib/fnos-x5/product_bootstrap.py').exists())

    def test_uninstall_restores_preinstalled_image_runtime_file(self):
        image_file = self.f.root / 'usr/lib/fnos-x5/product_bootstrap.py'
        image_file.parent.mkdir(parents=True, exist_ok=True)
        image_file.write_bytes(b'image-original-bootstrap\n')
        self.patch_install()
        module_install.install(self.f.root, self.package, require_complete=False)
        self.assertNotEqual(image_file.read_bytes(), b'image-original-bootstrap\n')
        module_install.install(self.f.root, self.package, require_complete=False)
        records = json.loads((self.f.root / 'var/lib/fnos-x5/accel/runtime-files.json').read_text())['files']
        self.assertEqual(next(r for r in records if r['path'].endswith('product_bootstrap.py'))['action'],
                         'replace')
        module_install.uninstall(self.f.root)
        self.assertEqual(image_file.read_bytes(), b'image-original-bootstrap\n')

    def test_activation_failure_is_not_reported_as_installed(self):
        self.patch_install()
        from unittest.mock import patch
        with patch.object(module_install, '_activate_timer', return_value=False):
            result, code = module_install.install(self.f.root, self.package, require_complete=False)
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'installed_repair_required')
        self.assertIn('systemctl enable', result['recovery'])

    def test_status_exposes_health_and_recovery(self):
        self.patch_install()
        module_install.install(self.f.root, self.package, require_complete=False)
        result = module_install.status(self.f.root)
        self.assertEqual(result['status'], 'installed')
        self.assertIn('recovery', result)

    def test_stopped_app_is_not_restarted(self):
        calls = []

        def cli(command, *args, **kwargs):
            calls.append(command)
            return 'stopped' if command == 'status' else ''
        with patch.object(module_install.product_bootstrap.Bootstrap, 'cli', side_effect=cli):
            module_install._reload_if_running(self.f.root, ['trim.photos'], {})
        self.assertEqual(calls, ['status'])
        self.assertNotIn('stop', calls)
        self.assertNotIn('start', calls)


if __name__ == '__main__':
    unittest.main()
