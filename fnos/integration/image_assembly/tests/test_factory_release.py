"""The full image must not claim the updater's target without its hotfix."""
from pathlib import Path
from contextlib import contextmanager
import hashlib
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fnos.integration.image_assembly import assemble, factory_release
from fnos.integration.in_place_update import update, build


def digest(data):
    return hashlib.sha256(data).hexdigest()


class FactoryReleaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        self.root = base / 'root'
        self.root.mkdir()
        self.original = b'original module for the factory kernel'
        self.patched = b'patched module for the factory kernel'
        self.kernel = b'factory kernel'
        self.module = base / 'patched.ko'
        self.module.write_bytes(self.patched)
        self.target = update.WifiTarget(digest(self.kernel), digest(self.original), digest(self.patched))
        self.manifest = base / 'target.json'
        self.manifest.write_text(self.target.to_json())
        for rel, data in ((Path('boot/Image'), self.kernel), (update.DRIVER, self.original),
                          (Path('etc/passwd'), b'account unchanged'),
                          (Path('vol1/photos/original.jpg'), b'photo unchanged')):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for rel in ('etc/modprobe.d', 'etc/systemd/system/multi-user.target.wants'):
            (self.root / rel).mkdir(parents=True, exist_ok=True)
        lstat = Path.lstat

        def root_owned(path):
            info = lstat(path)
            return SimpleNamespace(st_uid=0, st_gid=0, st_mode=info.st_mode)

        for mock in (patch.object(Path, 'lstat', root_owned),
                     patch.object(factory_release.os, 'chown'),
                     patch.object(factory_release.os, 'fchown'),
                     patch.object(factory_release.os, 'lchown'), patch.object(assemble, 'run')):
            mock.start()
            self.addCleanup(mock.stop)

    def test_v033_without_reviewed_wifi_inputs_is_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(assemble, 'run'):
                with self.assertRaisesRegex(ValueError, 'Wi-Fi'):
                    assemble.stage_x5_release(root)
            self.assertFalse((root / 'etc/fnos-x5/release').exists())

    def test_factory_transaction_is_complete_and_supports_apply_rollback_reapply(self):
        result = assemble.stage_x5_release(self.root, wifi_module=self.module, wifi_target=self.manifest)
        self.assertTrue(result['rollback_baselines'])
        self.assertEqual(factory_release.verify(self.root, 'v0.3.3', self.manifest)['status'], 'verified')
        install = update.Installation(self.root, factory_release.CURRENT_HELPER.read_bytes(), self.patched,
                                      driver_baseline=self.target.base_driver_sha256)
        with install.locked():
            install.apply()
            self.assertEqual(install.status(), 'v0.3.3')
            install.rollback()
            self.assertEqual(install.status(), 'v0.3.2')
            self.assertEqual((self.root / update.DRIVER).read_bytes(), self.original)
            self.assertEqual(digest((self.root / update.HELPER).read_bytes()), update.BASE_DIGEST)
            install.apply()
            self.assertEqual(install.status(), 'v0.3.3')
        self.assertEqual((self.root / 'etc/passwd').read_bytes(), b'account unchanged')
        self.assertEqual((self.root / 'vol1/photos/original.jpg').read_bytes(), b'photo unchanged')

    def test_wrong_kernel_or_module_never_labels_the_image_v033(self):
        for path, data in ((self.root / 'boot/Image', b'wrong kernel'),
                           (self.root / update.DRIVER, b'wrong original module'),
                           (self.module, b'wrong replacement module')):
            with self.subTest(path=str(path)):
                previous = path.read_bytes()
                path.write_bytes(data)
                with self.assertRaises(ValueError):
                    assemble.stage_x5_release(self.root, wifi_module=self.module, wifi_target=self.manifest)
                self.assertFalse((self.root / update.RELEASE).exists())
                path.write_bytes(previous)

    def test_finished_image_verifier_rejects_missing_performance_policy(self):
        assemble.stage_x5_release(self.root, wifi_module=self.module, wifi_target=self.manifest)
        (self.root / update.WIFI_POLICY).unlink()
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            factory_release.verify(self.root, 'v0.3.3', self.manifest)

    def test_v032_marker_is_honored_and_keeps_its_original_driver(self):
        assemble.stage_x5_release(self.root, 'v0.3.2')
        factory_release.verify(self.root, 'v0.3.2')
        self.assertEqual((self.root / update.DRIVER).read_bytes(), self.original)
        self.assertFalse((self.root / update.WIFI_POLICY).exists())

    def test_update_package_embeds_the_same_explicit_kernel_target(self):
        from zipfile import ZipFile
        output = self.module.parent / 'update.pyz'
        build.build(output, self.module, self.target)
        with ZipFile(output) as archive:
            target = update.WifiTarget.from_json(archive.read('wifi-target.json'))
            self.assertEqual(target, self.target)
            self.assertEqual(digest(archive.read('aic8800_fdrv.ko')), target.driver_sha256)

    def test_symlink_state_parent_is_rejected_before_changing_the_image(self):
        outside = self.module.parent / 'outside'
        outside.mkdir()
        sentinel = outside / 'sentinel'
        sentinel.write_bytes(b'untouched')
        (self.root / 'var/lib').mkdir(parents=True)
        (self.root / 'var/lib/fnos-x5').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            assemble.stage_x5_release(self.root, wifi_module=self.module, wifi_target=self.manifest)
        self.assertFalse((self.root / update.RELEASE).exists())
        self.assertEqual(sentinel.read_bytes(), b'untouched')

    def test_live_host_is_never_a_factory_destination(self):
        with self.assertRaisesRegex(ValueError, 'Offline'):
            assemble.stage_x5_release(Path('/'))

    def test_factory_reentry_after_module_replacement_resumes_the_transaction(self):
        with patch.object(update.Installation, 'set_release', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                assemble.stage_x5_release(self.root, wifi_module=self.module, wifi_target=self.manifest)
        self.assertEqual((self.root / update.DRIVER).read_bytes(), self.patched)
        for _ in range(2):
            assemble.stage_x5_release(self.root, wifi_module=self.module, wifi_target=self.manifest)
            factory_release.verify(self.root, 'v0.3.3', self.manifest)

    def test_competing_factory_call_does_not_write_before_acquiring_the_lock(self):
        assemble.stage_x5_release(self.root, wifi_module=self.module, wifi_target=self.manifest)
        before = {p: (self.root / p).read_bytes() for p in (update.RELEASE, update.HELPER, update.DRIVER)}
        install = update.Installation(self.root, factory_release.CURRENT_HELPER.read_bytes(), self.patched,
                                      driver_baseline=self.target.base_driver_sha256)
        with install.locked():
            with self.assertRaises(BlockingIOError):
                assemble.stage_x5_release(self.root, wifi_module=self.module, wifi_target=self.manifest)
        self.assertEqual(before, {p: (self.root / p).read_bytes() for p in before})

    def test_factory_verification_rejects_unusable_update_state_mode(self):
        assemble.stage_x5_release(self.root, wifi_module=self.module, wifi_target=self.manifest)
        (self.root / update.STATE).chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'ownership or mode'):
            factory_release.verify(self.root, 'v0.3.3', self.manifest)

    def test_early_factory_helper_without_backup_is_recovered_from_the_pinned_baseline(self):
        helper = self.root / update.HELPER
        helper.parent.mkdir(parents=True)
        helper.write_bytes(factory_release.CURRENT_HELPER.read_bytes())
        assemble.stage_x5_release(self.root, wifi_module=self.module, wifi_target=self.manifest)
        self.assertEqual(digest((self.root / update.STATE / 'original-helper.py').read_bytes()), update.BASE_DIGEST)
        factory_release.verify(self.root, 'v0.3.3', self.manifest)

    def test_factory_system_directories_are_not_group_writable_under_builder_umask(self):
        old = os.umask(0o002)
        try:
            assemble.stage_x5_release(self.root, wifi_module=self.module, wifi_target=self.manifest)
        finally:
            os.umask(old)
        for relative, mode in ((update.STATE, 0o700), (update.STATE.parent, 0o755),
                               (update.RELEASE.parent, 0o755), (update.HELPER.parent, 0o755)):
            self.assertEqual((self.root / relative).stat().st_mode & 0o777, mode)
        factory_release.verify(self.root, 'v0.3.3', self.manifest)


    def test_factory_normalizes_reviewed_kernel_artifact_modes_without_relaxing_updater(self):
        driver = self.root / update.DRIVER
        driver.chmod(0o664)
        kernel = self.root / 'usr/lib/modules/6.18.0/kernel'
        for parent in driver.parents:
            if parent == kernel.parent:
                break
            parent.chmod(0o775)
        unrelated = self.root / 'vol1/photos'
        unrelated.chmod(0o777)
        assemble.stage_x5_release(self.root, wifi_module=self.module, wifi_target=self.manifest)
        self.assertEqual(driver.stat().st_mode & 0o777, 0o644)
        for parent in driver.parents:
            if parent == kernel.parent:
                break
            self.assertEqual(parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(unrelated.stat().st_mode & 0o777, 0o777)
        factory_release.verify(self.root, 'v0.3.3', self.manifest)

    def test_factory_rejects_unowned_driver_parents_before_normalizing_modes(self):
        driver = self.root / update.DRIVER
        driver.chmod(0o664)
        original_lstat = Path.lstat
        unowned_parent = driver.parent.resolve()
        def unowned(path):
            info = original_lstat(path)
            return SimpleNamespace(st_uid=1000 if path == unowned_parent else info.st_uid,
                                   st_gid=info.st_gid, st_mode=info.st_mode)
        with patch.object(Path, 'lstat', unowned):
            with self.assertRaisesRegex(ValueError, 'metadata'):
                assemble.stage_x5_release(self.root, wifi_module=self.module, wifi_target=self.manifest)
        self.assertEqual(driver.stat().st_mode & 0o777, 0o664)
        self.assertFalse((self.root / update.RELEASE).exists())

    def test_busy_factory_lock_does_not_normalize_driver_modes(self):
        assemble.stage_x5_release(self.root, wifi_module=self.module, wifi_target=self.manifest)
        driver = self.root / update.DRIVER
        driver.chmod(0o664)
        install = update.Installation(self.root, factory_release.CURRENT_HELPER.read_bytes(), self.patched,
                                      driver_baseline=self.target.base_driver_sha256)
        with install.locked():
            with self.assertRaises(BlockingIOError):
                assemble.stage_x5_release(self.root, wifi_module=self.module, wifi_target=self.manifest)
        self.assertEqual(driver.stat().st_mode & 0o777, 0o664)

    def test_changed_driver_at_lock_entry_is_rejected_before_mode_or_marker_changes(self):
        driver = self.root / update.DRIVER
        driver.chmod(0o664)
        locked = update.Installation.locked
        @contextmanager
        def replace_before_yield(installation):
            with locked(installation):
                driver.write_bytes(b'unreviewed concurrent replacement')
                yield
        with patch.object(update.Installation, 'locked', replace_before_yield):
            with self.assertRaisesRegex(ValueError, 'module'):
                assemble.stage_x5_release(self.root, wifi_module=self.module, wifi_target=self.manifest)
        self.assertEqual(driver.stat().st_mode & 0o777, 0o664)
        self.assertFalse((self.root / update.RELEASE).exists())

    def test_v032_does_not_normalize_a_driver_without_an_explicit_trusted_target(self):
        driver = self.root / update.DRIVER
        driver.chmod(0o664)
        driver.parent.chmod(0o775)
        assemble.stage_x5_release(self.root, 'v0.3.2')
        self.assertEqual(driver.stat().st_mode & 0o777, 0o664)
        self.assertEqual(driver.parent.stat().st_mode & 0o777, 0o775)


if __name__ == '__main__':
    unittest.main()
