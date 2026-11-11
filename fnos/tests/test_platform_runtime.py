"""Offline image staging regressions; no devices or live rootfs are touched."""
from pathlib import Path
import subprocess
import tempfile
import tarfile
import unittest
from unittest.mock import patch

from fnos.integration.image_assembly import platform_runtime as runtime


class RuntimeStagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "rootfs"
        (self.root / "etc").mkdir(parents=True)
        (self.root / "etc/os-release").write_text('ID=fnos\n')
        (self.root / "etc/native.conf").write_text('keep native configuration\n')
        self.sources = self.base / "sources"
        self.pins = {}
        for repo in runtime.SOURCES:
            folder = self.sources / repo
            folder.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(folder)], check=True)
        wifi = self.sources / "x5-hobot-wifi/debian/vendor/etc/firmware"
        wifi.mkdir(parents=True)
        for name in runtime.WIFI_REQUIRED:
            (wifi / name).write_bytes(b'firmware')
        mm = self.sources / "x5-hobot-multimedia/debian/usr/hobot/lib"
        (mm / "firmware/libmm").mkdir(parents=True)
        (mm / "firmware/libmm/chagall.bin").write_bytes(b'codec firmware')
        for pattern in runtime.MM_LIBS:
            name = pattern[:-1]
            (mm / (name + '.1')).write_bytes(b'library')
            (mm / name).symlink_to(name + '.1')
        dnn = self.sources / "x5-hobot-dnn/x5/usr/lib"
        dnn.mkdir(parents=True)
        for name in ('libdnn.so', 'libhbrt_bayes_aarch64.so'):
            (dnn / name).write_bytes(b'bpu library')
        for repo in runtime.SOURCES:
            self.commit(repo)
        self.addCleanup(patch.stopall)
        patch.object(runtime, 'SOURCES', self.pins).start()

    def commit(self, repo):
        cwd = self.sources / repo
        subprocess.run(['git', '-C', str(cwd), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(cwd), '-c', 'user.name=Fixture', '-c',
                        'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture'], check=True)
        self.pins[repo] = subprocess.check_output(['git', '-C', str(cwd), 'rev-parse', 'HEAD'], text=True).strip()

    def test_repeat_staging_preserves_native_config_and_verified_payload(self):
        import os
        previous = os.umask(0o002)
        try:
            first = runtime.stage_runtime(self.root, self.sources)
        finally:
            os.umask(previous)
        second = runtime.stage_runtime(self.root, self.sources)
        self.assertEqual(first, second)
        self.assertEqual(runtime.verify_runtime(self.root, first)['verified_files'], len(first['files']))
        self.assertEqual((self.root / 'etc/native.conf').read_text(), 'keep native configuration\n')
        for path in self.root.rglob('*'):
            if not path.is_symlink():
                self.assertFalse(path.stat().st_mode & 0o022, str(path))

    def test_missing_firmware_fails_before_output_changes(self):
        (self.sources / 'x5-hobot-wifi/debian/vendor/etc/firmware' / runtime.WIFI_REQUIRED[0]).unlink()
        self.commit('x5-hobot-wifi')
        with self.assertRaisesRegex(ValueError, 'missing X5 firmware'):
            runtime.stage_runtime(self.root, self.sources)
        self.assertFalse((self.root / 'vendor').exists())

    def test_untracked_source_is_rejected(self):
        (self.sources / 'x5-hobot-wifi/unreviewed.bin').write_bytes(b'extra')
        with self.assertRaisesRegex(ValueError, 'modified source'):
            runtime.stage_runtime(self.root, self.sources)
        self.assertFalse((self.root / 'vendor').exists())

    def test_unknown_bpu_blob_is_rejected_before_staging(self):
        blob = self.base / 'unknown.ko'
        blob.write_bytes(b'unknown module')
        with self.assertRaisesRegex(ValueError, 'unreviewed BPU'):
            runtime.stage_runtime(self.root, self.sources, blob)
        self.assertFalse((self.root / 'vendor').exists())

    def test_config_parent_cannot_escape_staging_tree(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (self.root / 'etc/ld.so.conf.d').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symlink in destination'):
            runtime.stage_runtime(self.root, self.sources)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.root / 'vendor').exists())

    def test_final_image_verifier_detects_config_and_payload_tampering(self):
        report = runtime.stage_runtime(self.root, self.sources)
        target = self.root / 'etc/ld.so.conf.d/fnos-x5-runtime.conf'
        target.write_text('/wrong/path\n')
        with self.assertRaisesRegex(ValueError, 'incorrect final runtime file'):
            runtime.verify_runtime(self.root, report)

    def test_final_image_verifier_detects_replaced_library_link(self):
        report = runtime.stage_runtime(self.root, self.sources)
        target = self.root / 'usr/hobot/lib/libmultimedia.so'
        target.unlink()
        target.symlink_to('/tmp/wrong.so')
        with self.assertRaisesRegex(ValueError, 'incorrect final runtime link'):
            runtime.verify_runtime(self.root, report)

    def test_bundle_uses_root_metadata_and_excludes_host_identity(self):
        report = runtime.stage_runtime(self.root, self.sources)
        output = self.base / 'runtime.tar.gz'
        runtime.create_validation_bundle(self.root, report, output)
        with tarfile.open(output) as archive:
            self.assertNotIn('etc/os-release', archive.getnames())
            self.assertNotIn('etc/native.conf', archive.getnames())
            for member in archive.getmembers():
                self.assertEqual((member.uid, member.gid), (0, 0), member.name)
                if not member.issym():
                    self.assertFalse(member.mode & 0o022, member.name)

    def test_final_owner_gate_rejects_build_user_metadata(self):
        report = runtime.stage_runtime(self.root, self.sources)
        import os
        if os.getuid() == 0:
            self.skipTest('fixture belongs to root on this runner')
        with self.assertRaisesRegex(ValueError, 'non-root runtime ownership'):
            runtime.verify_runtime(self.root, report, require_root_owner=True)


if __name__ == '__main__':
    unittest.main()
