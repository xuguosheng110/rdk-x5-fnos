from pathlib import Path
import tempfile
import unittest

from fnos.integration.image_assembly.home_storage import stage_home_storage, verify_home_storage, UNITS


class HomeStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "root"
        (self.root / "etc").mkdir(parents=True)
        (self.root / "etc/os-release").write_text("ID=debian\n")
        elf = bytearray(64)
        elf[:6] = b"\x7fELF\x02\x01"
        elf[16:18] = (3).to_bytes(2, "little")
        elf[18:20] = (183).to_bytes(2, "little")
        self.library = self.base / "test-only.so"
        self.library.write_bytes(elf)

    def tearDown(self):
        self.tmp.cleanup()

    def test_storage_services_share_one_policy_without_global_preload(self):
        report = stage_home_storage(self.root, self.library)
        self.assertEqual(verify_home_storage(self.root, report)["units"], len(UNITS))
        self.assertIn("FNOS_HOME_DATA_ROOT=auto", (self.root / "etc/fnos-x5/home-storage.env").read_text())
        self.assertFalse((self.root / "etc/ld.so.preload").exists())
        for unit in ("trim_upload", "sysinfo_service", "smbd", "trim_app_center", "backup_service"):
            conf = (self.root / f"etc/systemd/system/{unit}.service.d/80-fnos-home-storage.conf").read_text()
            self.assertIn("EnvironmentFile=", conf)
            self.assertNotIn("UMask", conf)

    def test_tampered_payload_rejected(self):
        report = stage_home_storage(self.root, self.library)
        (self.root / report["files"][0]["path"]).write_bytes(b"changed")
        with self.assertRaises(ValueError):
            verify_home_storage(self.root, report)

    def test_ftp_view_is_scoped_to_native_ftp_and_preserves_shared_policy(self):
        report = stage_home_storage(self.root, self.library, self.library)
        self.assertEqual(report["ftp_view"], "private-mount-namespace-chroot")
        env = (self.root / "etc/fnos-x5/ftp-storage.env").read_text()
        self.assertIn("libftp_storage-", env)
        self.assertIn(":/usr/lib/fnos-x5/libhome_acl-", env)
        self.assertIn("FNOS_HOME_DATA_ROOT=auto", env)
        for unit in UNITS:
            dropin = (self.root / f"etc/systemd/system/{unit}.service.d/80-fnos-home-storage.conf").read_text()
            self.assertEqual("ftp-storage.env" in dropin, unit == "smbftpd")
        verify_home_storage(self.root, report)

    def test_invalid_ftp_library_rejected_before_any_staging_write(self):
        invalid = self.base / "wrong-ftp.so"
        invalid.write_bytes(b"wrong architecture")
        with self.assertRaises(ValueError):
            stage_home_storage(self.root, self.library, invalid)
        self.assertFalse((self.root / "etc/fnos-x5").exists())

    def test_symlink_destination_rejected_before_writes(self):
        (self.root / "usr").mkdir()
        (self.root / "usr/lib").symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(ValueError):
            stage_home_storage(self.root, self.library)
        self.assertFalse((self.root / "etc/fnos-x5").exists())

    def test_non_arm_payload_rejected(self):
        self.library.write_bytes(b"not an ARM shared library")
        with self.assertRaises(ValueError):
            stage_home_storage(self.root, self.library)

    def test_executable_instead_of_library_rejected(self):
        data = bytearray(self.library.read_bytes())
        data[16:18] = (2).to_bytes(2, "little")
        self.library.write_bytes(data)
        with self.assertRaises(ValueError):
            stage_home_storage(self.root, self.library)

    def test_live_root_rejected(self):
        with self.assertRaises(ValueError):
            stage_home_storage(Path("/"), self.library)


if __name__ == "__main__":
    unittest.main()
