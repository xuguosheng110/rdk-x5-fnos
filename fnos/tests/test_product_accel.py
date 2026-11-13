"""Offline transaction/compatibility tests for the shared acceleration overlay."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from fnos.integration.image_assembly import product_accel


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


class Fixture:
    def __init__(self, base):
        self.base = Path(base)
        self.root = self.base / 'root'
        self.bundle = self.base / 'bundle'
        (self.root / 'etc/systemd/system/mediasrv.service.d').mkdir(parents=True)
        (self.root / 'var/apps/trim.photos/target').mkdir(parents=True)
        (self.root / 'var/apps/trim.photos/var/db').mkdir(parents=True)
        (self.root / 'usr/trim/lib/ai_manager/com.trim.face_det/cp311').mkdir(parents=True)
        (self.root / 'usr/trim/lib/mediasrv/lib').mkdir(parents=True)
        (self.root / 'usr/trim/bin').mkdir(parents=True)
        (self.root / 'usr/lib/fnos-x5/photos-python').mkdir(parents=True)
        self.native_entry = b'native-face-det-entry\n'
        self.entry_path = self.root / product_accel.PHOTOS_ENTRY
        self.entry_path.write_bytes(self.native_entry)
        self.unit = b'native-mediasrv-unit\n'
        self.unit_path = self.root / product_accel.MEDIA_UNIT
        self.unit_path.write_bytes(self.unit)
        (self.root / 'usr/trim/lib/mediasrv/lib/libavcodec.so.62').write_bytes(b'native-codec\n')
        (self.root / 'usr/trim/bin/ai_manager').write_bytes(b'ai-manager\n')
        (self.root / 'var/apps/trim.photos/target/trim-photos').write_bytes(b'photos-binary\n')
        (self.root / 'var/apps/trim.photos/manifest').write_text('version=0.9.14\n')
        self.user_db = self.root / 'var/apps/trim.photos/var/db/photo.db'
        self.user_db.write_bytes(b'user-photo-index-never-touched\n')
        self.compat = {
            'schema': product_accel.COMPAT_SCHEMA,
            'module': {'name': 'fnos-x5-accel', 'version': '0.3.0'},
            'platform': {
                'machine': 'aarch64', 'kernel_releases': ['6.18.0'],
                'photos': {'modules': ['bpu_framework', 'bpu_cores', 'bpu_hw_io_x5'],
                           'devices': ['/dev/bpu_core0'],
                           'paths': ['/usr/lib/hbbpu/libdnn.so',
                                     '/usr/lib/hbbpu/libhbrt_bayes_aarch64.so']},
                'media': {'paths': ['/usr/hobot/lib/firmware/libmm/chagall.bin']},
            },
            'native': {
                'photos_entry': {'path': str(product_accel.PHOTOS_ENTRY),
                                 'sha256': digest_bytes(self.native_entry)},
                'ai_manager': {'path': 'usr/trim/bin/ai_manager',
                               'sha256': digest_bytes(b'ai-manager\n')},
                'media_codec': {'path': 'usr/trim/lib/mediasrv/lib/libavcodec.so.62',
                                'sha256': digest_bytes(b'native-codec\n')},
                'media_unit': {'path': str(product_accel.MEDIA_UNIT),
                               'sha256': digest_bytes(self.unit)},
            },
            'apps': {'trim.photos': {'versions': [
                {'version': '0.9.14',
                 'binary_sha256': digest_bytes(b'photos-binary\n'),
                 'defaults': 'supported'}]},
                     'trim.media': {'versions': [{'version': '0.9.8-1'}]}},
        }
        self.compat_path = self.bundle / 'compat.json'
        self.compat_path.parent.mkdir(parents=True)
        self.compat_path.write_text(json.dumps(self.compat))
        self.probe = {'machine': 'aarch64', 'kernel': '6.18.0',
                      'modules': ['bpu_framework', 'bpu_cores', 'bpu_hw_io_x5'],
                      'devices': {'/dev/bpu_core0': True},
                      'paths': {'/usr/lib/hbbpu/libdnn.so': True,
                                '/usr/lib/hbbpu/libhbrt_bayes_aarch64.so': True,
                                '/usr/hobot/lib/firmware/libmm/chagall.bin': True}}
        self.state = self.root / product_accel.STATE
        self.make_bundle()

    def make_bundle(self, *, worker=b'worker-v1\n', wrapper=b'wrapper-v1\n', version='0.3.0'):
        files = self.bundle / 'files'
        rows = []

        def add(relative, data, *, target=None, kind='additive', known=()):
            path = files / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            rows.append({'path': relative, 'target': target or relative,
                         'sha256': digest_bytes(data), 'mode': 0o644,
                         'subsystem': 'photos' if 'photos' in relative or 'face' in relative else 'media',
                         'kind': kind, 'known_native_sha256': list(known)})

        add('usr/lib/fnos-x5/photos-python/testpkg.py', b'VALUE = 1\n')
        add('usr/lib/fnos-x5/face128_worker', worker)
        add('usr/lib/fnos-x5/native_face_entry.py', wrapper, target=str(product_accel.PHOTOS_ENTRY),
            kind='overlay', known=(digest_bytes(self.native_entry),))
        add('etc/systemd/system/mediasrv.service.d/90-fnos-vpu.conf', b'[Service]\n',
            kind='config')
        self.bundle_manifest = {
            'schema': product_accel.BUNDLE_SCHEMA,
            'module': {'name': 'fnos-x5-accel', 'version': version},
            'compat_sha256': digest_bytes(self.compat_path.read_bytes()),
            'compat': self.compat, 'files': rows}
        (self.bundle / 'bundle.json').write_text(json.dumps(self.bundle_manifest))

    def apply(self, **kwargs):
        kwargs.setdefault('platform_probe', self.probe)
        return product_accel.apply_bundle(self.root, self.bundle,
                                          state_dir=self.state, **kwargs)


class ProductAccelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.f = Fixture(Path(self.temp.name).resolve())

    def tearDown(self):
        self.temp.cleanup()

    def test_apply_and_uninstall_restore_native_and_preserve_user_data(self):
        result = self.f.apply()
        self.assertEqual(result['status'], 'installed')
        self.assertEqual((self.f.root / product_accel.PHOTOS_ENTRY).read_bytes(), b'wrapper-v1\n')
        self.assertEqual((self.f.root / 'usr/lib/fnos-x5/face128_worker').read_bytes(), b'worker-v1\n')
        self.assertTrue((self.f.root / 'etc/systemd/system/mediasrv.service.d/90-fnos-vpu.conf').is_file())
        self.assertEqual(self.f.user_db.read_bytes(), b'user-photo-index-never-touched\n')
        self.assertEqual(product_accel.installed_status(self.f.root, self.f.state)['status'], 'installed')
        state = json.loads((self.f.state / 'state.json').read_text())
        self.assertEqual(state['files'][str(product_accel.PHOTOS_ENTRY)]['uid'],
                         self.f.entry_path.stat().st_uid)
        result = product_accel.uninstall_bundle(self.f.root, state_dir=self.f.state)
        self.assertEqual(result['status'], 'uninstalled')
        self.assertEqual((self.f.root / product_accel.PHOTOS_ENTRY).read_bytes(), self.f.native_entry)
        self.assertFalse((self.f.root / 'usr/lib/fnos-x5/face128_worker').exists())
        self.assertFalse((self.f.root / 'etc/systemd/system/mediasrv.service.d/90-fnos-vpu.conf').exists())
        self.assertEqual(self.f.user_db.read_bytes(), b'user-photo-index-never-touched\n')

    def test_repeat_install_is_idempotent(self):
        self.f.apply()
        calls = len(list((self.f.state / 'backup').rglob('*')))
        result = self.f.apply()
        self.assertEqual(result['status'], 'already_installed')
        self.assertEqual(result['changed'], [])
        self.assertEqual(len(list((self.f.state / 'backup').rglob('*'))), calls)
        self.assertEqual((self.f.root / product_accel.PHOTOS_ENTRY).read_bytes(), b'wrapper-v1\n')

    def test_unknown_native_entry_is_preserved(self):
        self.f.entry_path.write_bytes(b'future-vendor-entry\n')
        result = self.f.apply()
        self.assertEqual(result['status'], 'installed')
        self.assertEqual(self.f.entry_path.read_bytes(), b'future-vendor-entry\n')
        self.assertIn('unknown_native', {row['reason'] for row in result['skipped']})
        self.assertIn(b'future-vendor-entry', self.f.entry_path.read_bytes())

    def test_low_space_fails_before_any_write(self):
        with self.assertRaises(product_accel.AccelError) as raised:
            self.f.apply(free_bytes=1)
        self.assertEqual(raised.exception.code, 'insufficient_space')
        self.assertFalse((self.f.root / 'usr/lib/fnos-x5/face128_worker').exists())
        self.assertEqual(self.f.entry_path.read_bytes(), self.f.native_entry)

    def test_midway_failure_rolls_back_every_changed_file(self):
        real = product_accel._write_target
        count = {'n': 0}

        def fail_last(source, target, mode, **kwargs):
            count['n'] += 1
            if target.name == '90-fnos-vpu.conf':
                raise OSError('simulated disk failure')
            return real(source, target, mode, **kwargs)

        with patch.object(product_accel, '_write_target', side_effect=fail_last):
            with self.assertRaises(OSError):
                self.f.apply()
        self.assertEqual(self.f.entry_path.read_bytes(), self.f.native_entry)
        self.assertFalse((self.f.root / 'usr/lib/fnos-x5/face128_worker').exists())
        self.assertFalse((self.f.root / 'usr/lib/fnos-x5/photos-python/testpkg.py').exists())
        self.assertEqual(product_accel.installed_status(self.f.root, self.f.state)['status'], 'failed')

    def test_unknown_kernel_is_blocked_without_writes(self):
        with self.assertRaises(product_accel.AccelError) as raised:
            self.f.apply(platform_probe={**self.f.probe, 'kernel': '5.15.0'})
        self.assertEqual(raised.exception.code, 'platform_blocked')
        self.assertEqual(self.f.entry_path.read_bytes(), self.f.native_entry)
        self.assertFalse((self.f.root / 'usr/lib/fnos-x5/face128_worker').exists())

    def test_unknown_media_unit_keeps_native_media_path(self):
        self.f.unit_path.write_bytes(b'future-mediasrv-unit\n')
        result = self.f.apply()
        self.assertEqual(result['status'], 'installed')
        self.assertFalse((self.f.root / 'etc/systemd/system/mediasrv.service.d/90-fnos-vpu.conf').exists())
        self.assertIn('unknown_native_unit', {row['reason'] for row in result['skipped']})
        self.assertTrue((self.f.root / 'usr/lib/fnos-x5/face128_worker').is_file())

    def test_upgrade_updates_worker_and_uninstall_restores_original_native(self):
        self.f.apply()
        self.f.make_bundle(worker=b'worker-v2\n', wrapper=b'wrapper-v2\n', version='0.3.1')
        result = self.f.apply(module_version='0.3.1')
        self.assertEqual(result['status'], 'installed')
        self.assertEqual((self.f.root / 'usr/lib/fnos-x5/face128_worker').read_bytes(), b'worker-v2\n')
        self.assertEqual(self.f.entry_path.read_bytes(), b'wrapper-v2\n')
        product_accel.uninstall_bundle(self.f.root, state_dir=self.f.state)
        self.assertEqual(self.f.entry_path.read_bytes(), self.f.native_entry)
        self.assertFalse((self.f.root / 'usr/lib/fnos-x5/face128_worker').exists())

    def test_failed_upgrade_restores_v1_worker_and_wrapper_then_uninstall_restores_native(self):
        self.f.apply()
        self.f.make_bundle(worker=b'worker-v2\n', wrapper=b'wrapper-v2\n', version='0.3.1')
        real = product_accel._write_target
        worker = self.f.root / 'usr/lib/fnos-x5/face128_worker'
        owner = product_accel._owner(worker)
        restored_owners = []

        def fail_entry(source, target, mode, **kwargs):
            if target.name == 'face_det.bin':
                raise OSError('simulated v2 write failure')
            if target == worker and source.read_bytes() == b'worker-v1\n':
                restored_owners.append((kwargs.get('uid'), kwargs.get('gid')))
            return real(source, target, mode, **kwargs)

        with patch.object(product_accel, '_write_target', side_effect=fail_entry):
            with self.assertRaises(OSError):
                self.f.apply(module_version='0.3.1')
        self.assertEqual((self.f.root / 'usr/lib/fnos-x5/face128_worker').read_bytes(), b'worker-v1\n')
        self.assertEqual(restored_owners, [owner])
        self.assertEqual(self.f.entry_path.read_bytes(), b'wrapper-v1\n')
        self.assertEqual(product_accel.installed_status(self.f.root, self.f.state)['status'], 'installed')
        product_accel.uninstall_bundle(self.f.root, state_dir=self.f.state)
        self.assertFalse((self.f.root / 'usr/lib/fnos-x5/face128_worker').exists())
        self.assertEqual(self.f.entry_path.read_bytes(), self.f.native_entry)

    def test_uninstall_preserves_vendor_change_after_install(self):
        self.f.apply()
        worker = self.f.root / 'usr/lib/fnos-x5/face128_worker'
        worker.write_bytes(b'vendor-updated-worker\n')
        result = product_accel.uninstall_bundle(self.f.root, state_dir=self.f.state)
        self.assertEqual(result['status'], 'uninstalled_with_preserved_changes')
        self.assertEqual(worker.read_bytes(), b'vendor-updated-worker\n')
        self.assertEqual(self.f.entry_path.read_bytes(), self.f.native_entry)

    def test_in_place_image_bundle_is_adopted_without_duplicating_payload(self):
        worker = self.f.root / 'usr/lib/fnos-x5/face128_worker'
        worker.write_bytes(b'worker-v1\n')
        self.f.entry_path.write_bytes(b'wrapper-v1\n')
        source_wrapper = self.f.root / 'usr/lib/fnos-x5/fnos/integration/native_photos/native_face_entry.py'
        source_wrapper.parent.mkdir(parents=True)
        source_wrapper.write_bytes(b'wrapper-v1\n')
        (self.f.root / product_accel.MEDIA_DROPIN).write_bytes(b'[Service]\n')
        bundle = self.f.base / 'in-place'
        collected = product_accel.collect_bundle(self.f.root, bundle,
                                                 compat_path=self.f.compat_path, reference_only=True)
        overlay = next(row for row in collected['files'] if row['target'] == str(product_accel.PHOTOS_ENTRY))
        self.assertEqual(overlay['mode'], 0o755)
        self.assertFalse((bundle / 'files/usr/lib/fnos-x5/face128_worker').exists())
        result = product_accel.apply_bundle(self.f.root, bundle, platform_probe=self.f.probe,
                                            state_dir=self.f.state)
        self.assertEqual(result['status'], 'already_installed')
        state = product_accel.installed_status(self.f.root, self.f.state)
        self.assertEqual(state['status'], 'installed')
        result = product_accel.uninstall_bundle(self.f.root, state_dir=self.f.state)
        self.assertEqual(result['status'], 'uninstalled_with_preserved_changes')
        self.assertEqual(worker.read_bytes(), b'worker-v1\n')
        self.assertEqual(self.f.entry_path.read_bytes(), b'wrapper-v1\n')

    def test_collected_bundle_directories_are_0755_under_group_umask(self):
        source = self.f.root / product_accel.MEDIA_DROPIN
        source.write_bytes(b'[Service]\n')
        bundle = self.f.base / 'umask-bundle'
        old_umask = os.umask(0o002)
        try:
            product_accel.collect_bundle(self.f.root, bundle,
                                         compat_path=self.f.compat_path)
        finally:
            os.umask(old_umask)
        dirs = [bundle, *(p for p in bundle.rglob('*')
                          if p.is_dir() and not p.is_symlink())]
        self.assertIn(bundle / 'files/etc/systemd/system/mediasrv.service.d', dirs)
        for path in dirs:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755, path)

    def test_preexisting_files_symlink_is_rejected_without_touching_outside(self):
        source = self.f.root / product_accel.MEDIA_DROPIN
        source.write_bytes(b'[Service]\n')
        for reference_only in (False, True):
            with self.subTest(reference_only=reference_only):
                bundle = self.f.base / ('symlink-bundle-' + str(reference_only))
                bundle.mkdir()
                outside = self.f.base / ('outside-files-' + str(reference_only))
                outside.mkdir()
                outside.chmod(0o700)
                (bundle / 'files').symlink_to(outside, target_is_directory=True)
                with self.assertRaises(product_accel.AccelError) as raised:
                    product_accel.collect_bundle(
                        self.f.root, bundle, compat_path=self.f.compat_path,
                        reference_only=reference_only)
                self.assertEqual(raised.exception.code, 'symlink_path')
                self.assertTrue((bundle / 'files').is_symlink())
                self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o700)

    def test_bundle_directory_policy_keeps_file_modes_and_links(self):
        bundle = self.f.base / 'policy-bundle'
        (bundle / 'sub').mkdir(parents=True)
        (bundle / 'sub').chmod(0o775)
        script = bundle / 'script'
        script.write_bytes(b'#!/bin/sh\n')
        script.chmod(0o700)
        link = bundle / 'link'
        link.symlink_to('/tmp')
        product_accel.enforce_bundle_dirs(bundle)
        self.assertEqual(stat.S_IMODE((bundle / 'sub').stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o700)
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), '/tmp')

    def test_empty_or_incomplete_bundle_is_rejected_for_release(self):
        empty = self.f.base / 'empty-root'
        empty.mkdir()
        with self.assertRaises(product_accel.AccelError) as raised:
            product_accel.collect_bundle(empty, self.f.base / 'empty-bundle',
                                         compat_path=self.f.compat_path,
                                         require_complete=True)
        self.assertEqual(raised.exception.code, 'incomplete_bundle')

    def test_crash_journal_is_rolled_back_before_retry(self):
        self.f.state.mkdir(parents=True)
        backup = self.f.state / 'backup/tx/entry'
        backup.parent.mkdir(parents=True)
        backup.write_bytes(self.f.native_entry)
        self.f.entry_path.write_bytes(b'half-written-wrapper\n')
        worker = self.f.root / 'usr/lib/fnos-x5/face128_worker'
        worker.write_bytes(b'half-written-worker\n')
        (self.f.state / 'transaction.json').write_text(json.dumps({
            'schema': product_accel.STATE_SCHEMA, 'status': 'applying', 'txid': 'tx',
            'files': {
                str(product_accel.PHOTOS_ENTRY): {'action': 'replace', 'backup': 'backup/tx/entry',
                                                   'sha256': digest_bytes(b'wrapper-v1\n')},
                'usr/lib/fnos-x5/face128_worker': {'action': 'create', 'backup': None,
                                                    'sha256': digest_bytes(b'worker-v1\n')}}}))
        self.assertTrue(product_accel._recover_transaction(self.f.root, self.f.state))
        self.assertEqual(self.f.entry_path.read_bytes(), self.f.native_entry)
        self.assertFalse(worker.exists())
        self.assertFalse((self.f.state / 'transaction.json').exists())

    def test_compat_status_distinguishes_known_and_unknown(self):
        known = product_accel.compat_status(self.f.root, self.f.compat)
        self.assertEqual(known['photos']['status'], 'supported')
        self.assertEqual(known['media']['status'], 'supported')
        (self.f.root / 'var/apps/trim.photos/target/trim-photos').write_bytes(b'future\n')
        unknown = product_accel.compat_status(self.f.root, self.f.compat)
        self.assertEqual(unknown['photos']['status'], 'unknown_native')
        self.assertIn('recovery', unknown['photos'])


if __name__ == '__main__':
    unittest.main()
