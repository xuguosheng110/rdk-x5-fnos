"""Factory image identity boundaries; uses only temporary offline roots."""
from pathlib import Path
import tempfile
import unittest

from fnos.integration.image_assembly.image_identity import stage_image_identity, verify_image_identity, REMOVED


class FactoryIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'image'
        (self.root / 'etc').mkdir(parents=True)
        (self.root / 'etc/os-release').write_text('ID=debian\n')
        for name in ['etc/machine-id', 'var/lib/dbus/machine-id', *map(str, REMOVED)]:
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('fixed source image identity\n')
        (self.root / 'etc/machine-id').chmod(0o444)
        (self.root / 'etc/shadow').write_text('sentinel-not-a-real-password')

    def tearDown(self):
        self.temp.cleanup()

    def test_cloned_identities_removed_and_accounts_preserved(self):
        report = stage_image_identity(self.root)
        self.assertEqual((self.root / 'etc/machine-id').read_bytes(), b'')
        self.assertEqual((self.root / 'etc/machine-id').stat().st_mode & 0o777, 0o444)
        self.assertEqual(str((self.root / 'var/lib/dbus/machine-id').readlink()), '/etc/machine-id')
        for name in REMOVED:
            self.assertFalse((self.root / name).exists())
        self.assertEqual((self.root / 'etc/shadow').read_text(), 'sentinel-not-a-real-password')
        verify_image_identity(self.root, report)

    def test_host_key_reintroduced_into_final_image_is_rejected(self):
        report = stage_image_identity(self.root)
        (self.root / 'etc/ssh/ssh_host_ed25519_key').write_text('unexpected identity')
        with self.assertRaisesRegex(ValueError, 'identity retained'):
            verify_image_identity(self.root, report)

    def test_symlink_rejected_before_any_identity_is_removed(self):
        outside = self.base / 'outside'
        outside.write_text('keep')
        seed = self.root / 'var/lib/systemd/random-seed'
        seed.unlink()
        seed.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            stage_image_identity(self.root)
        self.assertEqual(outside.read_text(), 'keep')
        self.assertTrue((self.root / 'etc/ssh/ssh_host_ed25519_key').exists())

    def test_live_root_is_not_a_factory_image_target(self):
        with self.assertRaisesRegex(ValueError, 'offline root'):
            stage_image_identity(Path('/'))

    def test_modified_network_defaults_fail_final_readback(self):
        report = stage_image_identity(self.root)
        (self.root / 'etc/NetworkManager/conf.d/80-fnos-x5-ethernet.conf').write_text('[connection]\n')
        with self.assertRaisesRegex(ValueError, 'identity file mismatch'):
            verify_image_identity(self.root, report)


if __name__ == '__main__':
    unittest.main()
