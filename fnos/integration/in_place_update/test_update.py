import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from fnos.integration.in_place_update import build, update


OLD = b'original compatibility script'
NEW = b'updated compatibility script'
OLD_DRIVER = b'original aic8800 module'
NEW_DRIVER = b'updated aic8800 module\r\n'


class UpdateTest(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        lstat = Path.lstat

        def root_owned(path):
            info = lstat(path)
            return SimpleNamespace(st_uid=0, st_gid=0, st_mode=info.st_mode)

        ownership = patch.object(Path, 'lstat', root_owned)
        for method in ('fchown', 'lchown'):
            owner_call = patch.object(update.os, method)
            owner_call.start()
            self.addCleanup(owner_call.stop)
        ownership.start()
        self.addCleanup(ownership.stop)
        self.old_digest = hashlib.sha256(OLD).hexdigest()
        self.patcher = patch.object(update, 'BASE_DIGEST', self.old_digest)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        driver_patcher = patch.object(update, 'BASE_DRIVER_DIGEST', hashlib.sha256(OLD_DRIVER).hexdigest())
        driver_patcher.start()
        self.addCleanup(driver_patcher.stop)
        for rel, data in ((update.RELEASE, update.BASE), (update.HELPER, OLD),
                          (update.DRIVER, OLD_DRIVER),
                          (Path('vol1/@homeassistant/config/configuration.yaml'), b'config'),
                          (Path('etc/passwd'), b'account')):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (self.root / 'etc/modprobe.d').mkdir()
        (self.root / 'etc/systemd/system/multi-user.target.wants').mkdir(parents=True)
        self.install = update.Installation(self.root, NEW, NEW_DRIVER)
        self.install.state.mkdir(mode=0o700, parents=True)

    def test_upgrade_rollback_and_reapply(self):
        self.assertEqual(self.install.status(), 'v0.3.2')
        self.install.apply()
        self.install.apply()
        self.assertEqual(self.install.status(), 'v0.3.3')
        self.assertEqual((self.root / update.HELPER).read_bytes(), NEW)
        self.assertEqual((self.root / update.DRIVER).read_bytes(), NEW_DRIVER)
        self.install.rollback()
        self.install.rollback()
        self.assertEqual(self.install.status(), 'v0.3.2')
        self.assertEqual((self.root / update.HELPER).read_bytes(), OLD)
        self.assertEqual((self.root / update.DRIVER).read_bytes(), OLD_DRIVER)
        self.assertEqual((self.root / 'etc/passwd').read_bytes(), b'account')
        self.assertEqual((self.root / 'vol1/@homeassistant/config/configuration.yaml').read_bytes(), b'config')
        self.install.apply()
        self.assertEqual(self.install.status(), 'v0.3.3')

    def test_performance_policy_is_installed_enabled_and_removed(self):
        wifi = self.root / 'etc/modprobe.d/fnos-x5-aic8800-performance.conf'
        unit = self.root / 'etc/systemd/system/fnos-x5-performance.service'
        link = unit.parent / 'multi-user.target.wants' / unit.name
        self.install.apply()
        self.install.apply()
        self.assertIn(b'options aic8800_fdrv ps_on=N\n', wifi.read_bytes())
        self.assertIn(b'echo performance > "$policy"', unit.read_bytes())
        self.assertEqual(link.readlink(), Path('/etc/systemd/system') / unit.name)
        link.unlink()
        self.assertEqual(self.install.status(), 'interrupted')
        self.install.apply()
        self.assertEqual(self.install.status(), 'v0.3.3')
        self.install.rollback()
        self.assertFalse(wifi.exists())
        self.assertFalse(unit.exists())
        self.assertFalse(link.is_symlink())

    def test_existing_v033_without_performance_policy_can_be_updated(self):
        self.install.apply()
        wifi = self.root / 'etc/modprobe.d/fnos-x5-aic8800-performance.conf'
        unit = self.root / 'etc/systemd/system/fnos-x5-performance.service'
        link = unit.parent / 'multi-user.target.wants' / unit.name
        link.unlink()
        unit.unlink()
        wifi.unlink()
        self.assertEqual(self.install.status(), 'interrupted')
        self.install.apply()
        self.assertEqual(self.install.status(), 'v0.3.3')

    def test_unrecognized_performance_policy_or_enable_link_aborts_before_changes(self):
        wifi = self.root / 'etc/modprobe.d/fnos-x5-aic8800-performance.conf'
        link = self.root / 'etc/systemd/system/multi-user.target.wants/fnos-x5-performance.service'
        wifi.write_bytes(b'local policy')
        with self.assertRaises(ValueError):
            self.install.apply()
        self.assertEqual(wifi.read_bytes(), b'local policy')
        wifi.unlink()
        link.symlink_to('/opt/local/custom.service')
        with self.assertRaises(ValueError):
            self.install.apply()
        self.assertEqual(link.readlink(), Path('/opt/local/custom.service'))
        self.assertEqual((self.root / update.HELPER).read_bytes(), OLD)
        self.assertFalse(self.install.backup.exists())

    def test_interrupted_upgrade_can_be_rolled_back_or_resumed(self):
        for resume in (self.install.rollback, self.install.apply):
            with patch.object(self.install, 'set_release', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):
                    self.install.apply()
            self.assertEqual(self.install.status(), 'interrupted')
            resume()
            self.assertNotEqual(self.install.status(), 'interrupted')
        self.assertEqual(self.install.status(), 'v0.3.3')

    def test_foreign_version_or_helper_is_rejected(self):
        release = self.root / update.RELEASE
        release.write_bytes(b'v0.3.1\n')
        with self.assertRaises(ValueError):
            self.install.apply()
        release.write_bytes(update.BASE)
        (self.root / update.HELPER).write_bytes(b'local modification')
        with self.assertRaises(ValueError):
            self.install.apply()
        self.assertEqual(release.read_bytes(), update.BASE)
        self.assertFalse(self.install.backup.exists())

    def test_already_updated_board_imports_verified_driver_backup(self):
        self.install.apply()
        legacy = self.root / update.LEGACY_DRIVER_BACKUP
        legacy.parent.mkdir(mode=0o700)
        self.install.driver_backup.rename(legacy)
        self.install.apply()
        self.install.rollback()
        self.assertEqual((self.root / update.DRIVER).read_bytes(), OLD_DRIVER)

    def test_invalid_driver_never_changes_helper_or_release(self):
        (self.root / update.DRIVER).write_bytes(b'foreign driver')
        with self.assertRaises(ValueError):
            self.install.apply()
        self.assertEqual((self.root / update.HELPER).read_bytes(), OLD)
        self.assertEqual((self.root / update.RELEASE).read_bytes(), update.BASE)
        self.assertFalse(self.install.backup.exists())

    def test_corrupt_driver_backup_aborts_rollback_before_any_write(self):
        self.install.apply()
        self.install.driver_backup.write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            self.install.rollback()
        self.assertEqual((self.root / update.HELPER).read_bytes(), NEW)
        self.assertEqual((self.root / update.DRIVER).read_bytes(), NEW_DRIVER)
        self.assertEqual((self.root / update.RELEASE).read_bytes(), update.TARGET)

    def test_existing_v033_without_wifi_module_can_be_updated(self):
        self.install.apply()
        self.install.driver_backup.unlink()
        (self.root / update.DRIVER).write_bytes(OLD_DRIVER)
        self.install.apply()
        self.assertEqual((self.root / update.DRIVER).read_bytes(), NEW_DRIVER)
        self.install.rollback()
        self.assertEqual((self.root / update.DRIVER).read_bytes(), OLD_DRIVER)

    def test_builder_embeds_module_bytes_without_text_conversion(self):
        module = self.root / 'module.ko'
        package = self.root / 'update.pyz'
        module.write_bytes(NEW_DRIVER)
        with patch.object(build, 'DRIVER_DIGEST', hashlib.sha256(NEW_DRIVER).hexdigest()):
            build.build(package, module)
        with ZipFile(package) as archive:
            self.assertEqual(archive.read('aic8800_fdrv.ko'), NEW_DRIVER)
