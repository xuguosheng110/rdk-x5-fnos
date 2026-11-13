"""Offline contract/fault tests. Never open a host block device or contact X5.

RAM_DEPLOY_TEST_BINARY names a Linux binary built with -DRAM_DEPLOY_TEST.
That compile-time backend maps EVERY kernel/device path below a fresh fixture.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock
import shlex
import io
import errno

from fnos.integration.hardware.ram_deploy import validate_approval, digest, CARD_BYTES, CID, SERVER
from fnos.integration.hardware import ram_deploy
from fnos.lab.locks import board_lock

BOOT = '11111111-2222-3333-4444-555555555555'
TOKEN = 'a' * 32


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.helper = self.root / 'helper'; self.helper.write_bytes(b'independently-reviewed fixture')
        archive = self.root / 'key-files.tar.gz'; archive.write_bytes(b'private fixture only'); archive.chmod(0o600)
        self.review = self.root / 'review.json'
        self.review.write_text(json.dumps({'status': 'PASS', 'backup_mode': 'selective_backup', 'whole_disk_backup_required': False,
            'checks': {key: True for key in ('archive_integrity_read_complete', 'exact_member_set', 'all_regular_file_sizes_and_sha256',
                                           'all_uid_gid_modes', 'all_symlink_targets', 'local_and_server_archive_identical')},
            'archive': {'bytes': archive.stat().st_size, 'sha256': digest(archive)},
            'private_paths': {'local_directory': str(self.root)}}))
        self.data = {'schema': 'fnos-ram-deploy-approval/v1', 'accepted': True,
            'review_status': 'independently_approved_for_board_deploy', 'reviewer': 'independent fixture reviewer', 'server': SERVER,
            'image': {'path': '/srv/fnos-build/assembly-test/artifacts/final.img', 'bytes': 4096, 'sha256': 'b'*64},
            'target': {'board_id': 'x5-lab-01', 'device': '/dev/mmcblk1', 'cid': CID, 'bytes': CARD_BYTES, 'major': 179, 'minor': 8},
            'rescue_boot_id': BOOT, 'rescue_host': '192.0.2.10', 'known_hosts_sha256': 'c'*64,
            'helper_sha256': digest(self.helper), 'selective_backup_review_sha256': digest(self.review)}
        self.approval = self.root / 'approval.json'

    def tearDown(self): self.temp.cleanup()

    def run_gate(self):
        self.approval.write_text(json.dumps(self.data))
        return validate_approval(self.approval, digest(self.approval), self.helper, self.review)

    def test_selective_backup_pass_is_sufficient_without_full_card_backup(self):
        self.assertEqual(self.run_gate()['image']['bytes'], 4096)

    def test_not_approved_for_deployment_rejected(self):
        self.data['accepted'] = False
        with self.assertRaises(ValueError): self.run_gate()

    def test_sda_or_different_sd_identity_rejected(self):
        for key, wrong in [('device', '/dev/sda'), ('cid', '0'*32), ('bytes', CARD_BYTES-512), ('major', 8)]:
            old = self.data['target'][key]; self.data['target'][key] = wrong
            with self.subTest(key=key), self.assertRaises(ValueError): self.run_gate()
            self.data['target'][key] = old

    def test_unpinned_or_unreviewed_helper_rejected(self):
        self.helper.write_bytes(b'changed')
        with self.assertRaises(ValueError): self.run_gate()

    def test_changed_backup_or_unsafe_permissions_rejected(self):
        archive = self.root/'key-files.tar.gz'
        archive.chmod(0o644)
        with self.assertRaises(ValueError): self.run_gate()
        archive.chmod(0o600); archive.write_bytes(b'changed')
        with self.assertRaises(ValueError): self.run_gate()

    def test_old_skip_backup_evidence_rejected(self):
        self.review.write_text(json.dumps({'ok': True, 'skipped': True, 'authorized_by': 'user'}))
        self.data['selective_backup_review_sha256'] = digest(self.review)
        with self.assertRaises(ValueError): self.run_gate()

    def test_wrong_approval_digest_rejected(self):
        self.run_gate()
        with self.assertRaises(ValueError): validate_approval(self.approval, '0'*64, self.helper, self.review)

    def test_image_outside_server_or_wrong_length_rejected(self):
        for path in ['/tmp/a.img', '/srv/fnos-build/../a.img']:
            self.data['image']['path'] = path
            with self.assertRaises(ValueError): self.run_gate()


class ControllerFailureTests(unittest.TestCase):
    """Regression for independent-review P1: logging must not release ownership."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.helper = self.root/'helper'; self.helper.write_bytes(b'fixture helper')
        self.approval = {'image': {'path': '/fixture.img', 'bytes': 4096, 'sha256': 'b'*64},
            'target': {'cid': CID, 'bytes': CARD_BYTES, 'major': 179, 'minor': 8},
            'rescue_boot_id': BOOT, 'helper_sha256': digest(self.helper)}

    def tearDown(self): self.temp.cleanup()

    def assert_held(self):
        with self.assertRaises(Exception):
            with board_lock(self.root/'locks', 'x5-lab-01'): pass

    def short_source(self, logging_fault):
        source = Mock(stdout=io.BytesIO(json.dumps({'status': 'SOURCE_VERIFIED', 'bytes': 4096, 'sha256': 'b'*64}).encode()+b'\n'), stdin=None)
        source.poll.return_value = 0
        writer = Mock(stdout=io.BytesIO(b'{"status":"READY"}\n'), stdin=io.BytesIO())
        writer.poll.return_value = None
        def terminated(**_kwargs):
            self.assert_held()
            writer.poll.return_value = 1
            return 1
        writer.wait.side_effect = terminated
        staged = subprocess.CompletedProcess([], 0, (digest(self.helper)+'  helper\n').encode(), b'')
        original_write = Path.write_text
        original_print = print

        def write(path, data, *args, **kwargs):
            if path.name == 'result.json' and json.loads(data)['status'] == 'FAILED_no_boot' and logging_fault == 'ENOSPC':
                raise OSError(errno.ENOSPC, 'fixture full evidence disk')
            return original_write(path, data, *args, **kwargs)

        def output(*args, **kwargs):
            if logging_fault == 'BrokenPipe' and args and '"event": "failed"' in str(args[0]):
                raise BrokenPipeError('fixture closed output')
            return original_print(*args, **kwargs)

        with board_lock(self.root/'locks', 'x5-lab-01'):
            with patch.object(ram_deploy.subprocess, 'Popen', side_effect=[source, writer]), \
                 patch.object(ram_deploy, 'remote', return_value=staged) as remote_mock, \
                 patch.object(Path, 'write_text', new=write), patch('builtins.print', side_effect=output):
                # Preserve the causal source failure, not an ENOSPC/BrokenPipe
                # exception that previously bypassed every termination gate.
                with self.assertRaisesRegex(ValueError, 'source image truncated'):
                    ram_deploy.execute(self.approval, self.helper, [], '/fixture.sock', self.root/'evidence', Mock(revalidate=lambda: None))
            self.assertEqual(writer.wait.call_count, 1)
            self.assertEqual(writer.poll(), 1)
            self.assertTrue(writer.stdin.closed)
            self.assertEqual(remote_mock.call_count, 1)

    def test_enospc_in_failed_report_still_waits_for_writer_terminal(self): self.short_source('ENOSPC')
    def test_broken_output_in_failed_report_still_waits_for_writer_terminal(self): self.short_source('BrokenPipe')

    def test_unknown_completion_and_broken_logger_keep_lock_until_remote_terminal(self):
        writer = Mock(stdin=io.BytesIO()); writer.wait.return_value = 255
        calls = []
        def remote_probe(*_args, **_kwargs):
            self.assert_held(); calls.append('probe')
            row = {'status': 'ACTIVE'} if len(calls) == 1 else {'status': 'TERMINAL', 'run': {'token': TOKEN, 'status': 'FAILED'}}
            return subprocess.CompletedProcess([], 0, json.dumps(row), '')
        def bad_logger(*_args, **_kwargs): raise BrokenPipeError('closed diagnostics')
        with board_lock(self.root/'locks', 'x5-lab-01'):
            with patch.object(ram_deploy, 'remote', side_effect=remote_probe), patch.object(ram_deploy.time, 'sleep', side_effect=lambda _: self.assert_held()):
                result = ram_deploy.ensure_writer_terminal(writer, [], '/helper', TOKEN, bad_logger, boot_id=BOOT)
            self.assertEqual(result['status'], 'FAILED')
            self.assertEqual(len(calls), 2)

    def test_unexpected_probe_exception_cannot_skip_cleanup_lock(self):
        writer = Mock(stdin=io.BytesIO()); writer.wait.side_effect = OSError('wait failure')
        def bad_logger(*_args, **_kwargs): raise OSError(errno.ENOSPC, 'full')
        with board_lock(self.root/'locks', 'x5-lab-01'):
            with patch.object(ram_deploy, 'wait_terminal', side_effect=[RuntimeError('probe decoder failed'), {'status': 'FAILED'}]) as probe, \
                 patch.object(ram_deploy.time, 'sleep', side_effect=lambda _: self.assert_held()):
                result = ram_deploy.ensure_writer_terminal(writer, [], '/helper', TOKEN, bad_logger, boot_id=BOOT)
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(result['status'], 'FAILED')

    def test_wait_terminal_accepts_terminal_evidence_despite_bad_logger(self):
        response = subprocess.CompletedProcess([], 0, json.dumps({'status': 'TERMINAL', 'run': {'token': TOKEN, 'status': 'DONE'}}), '')
        def bad_logger(*_args, **_kwargs): raise OSError(errno.ENOSPC, 'full')
        with patch.object(ram_deploy, 'remote', return_value=response):
            self.assertEqual(ram_deploy.wait_terminal([], '/helper', TOKEN, bad_logger, boot_id=BOOT)['status'], 'DONE')

    def test_authenticated_nonzero_exit_does_not_poll_corrupt_remote_state(self):
        source = Mock(stdout=io.BytesIO(json.dumps({'status': 'SOURCE_VERIFIED', 'bytes': 4096, 'sha256': 'b'*64}).encode()+b'\n'+b'X'*4096), stdin=None)
        source.poll.return_value = 0; source.wait.return_value = 0
        writer = Mock(stdout=io.BytesIO(b'{"status":"READY"}\n'), stdin=io.BytesIO(), returncode=2)
        writer.poll.return_value = 2
        staged = subprocess.CompletedProcess([], 0, (digest(self.helper)+'  helper\n').encode(), b'')
        with board_lock(self.root/'locks', 'x5-lab-01'):
            with patch.object(ram_deploy.subprocess, 'Popen', side_effect=[source, writer]), \
                 patch.object(ram_deploy, 'remote', return_value=staged) as remote_mock, \
                 patch.object(ram_deploy, 'wait_terminal', side_effect=AssertionError('known failed exit must not poll')) as probe:
                with self.assertRaisesRegex(RuntimeError, 'remote writer exited unsuccessfully'):
                    ram_deploy.execute(self.approval, self.helper, [], '/fixture.sock', self.root/'evidence', Mock(revalidate=lambda: None))
            self.assertEqual(remote_mock.call_count, 1)
            probe.assert_not_called()


@unittest.skipUnless(os.environ.get('RAM_DEPLOY_TEST_BINARY'), 'Linux fixture binary required')
class WriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.binary = os.environ['RAM_DEPLOY_TEST_BINARY']
        self.length = 4096; self.card = 16384
        self.image = bytes(range(256))*16
        self.put('/root-type', b'ramfs\n')
        self.put('/proc/sys/kernel/random/boot_id', (BOOT+'\n').encode())
        self.put('/proc/self/mountinfo', b'1 0 0:1 / / rw - rootfs rootfs rw\n2 1 0:2 / /proc rw - proc proc rw\n')
        self.put('/proc/swaps', b'Filename\tType\tSize\tUsed\tPriority\n')
        self.put('/sys/block/mmcblk1/device/cid', (CID+'\n').encode())
        self.put('/sys/block/mmcblk1/size', (str(self.card//512)+'\n').encode())
        self.put('/sys/block/mmcblk1/dev', b'179:8\n')
        for name in ['run', 'sys/block/mmcblk1/holders', 'sys/block/mmcblk1/mmcblk1p2/holders']:
            (self.root/name).mkdir(parents=True, exist_ok=True)
        self.original = b'X'*self.card
        self.put('/dev/mmcblk1', self.original); self.put('/dev/sda', b'protected external disk')

    def tearDown(self): self.temp.cleanup()

    def put(self, name, data):
        p = self.root / name.lstrip('/'); p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)

    def argv(self, sha=None):
        return [self.binary, '--execute', TOKEN, BOOT, CID, str(self.card), '179', '8', str(self.length),
                sha or hashlib.sha256(self.image).hexdigest()]

    def run_writer(self, *, data=None, fault='', sha=None):
        env = {**os.environ, 'RAM_DEPLOY_TEST_ROOT': str(self.root), 'RAM_DEPLOY_TEST_FAULT': fault}
        result = subprocess.run(self.argv(sha), input=self.image if data is None else data, capture_output=True, env=env, timeout=15)
        self.assertEqual((self.root/'dev/sda').read_bytes(), b'protected external disk')
        return result

    def refused(self, result, *, untouched=False):
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b'"status":"PASS"', result.stdout)
        if untouched: self.assertEqual((self.root/'dev/mmcblk1').read_bytes(), self.original)

    def test_full_write_readback_exact_sha_and_tail_preserved(self):
        r = self.run_writer(); self.assertEqual(r.returncode, 0, r.stderr.decode())
        result = json.loads(r.stdout.splitlines()[-1])
        self.assertEqual(result['written'], self.length); self.assertEqual(result['readback'], self.length)
        self.assertEqual(result['sha256'], hashlib.sha256(self.image).hexdigest())
        self.assertEqual((self.root/'dev/mmcblk1').read_bytes(), self.image + self.original[self.length:])

    def test_short_input_fails(self): self.refused(self.run_writer(data=self.image[:-1]), untouched=True)
    def test_extra_input_fails(self): self.refused(self.run_writer(data=self.image+b'extra'))
    def test_wrong_source_hash_fails(self): self.refused(self.run_writer(sha='0'*64))
    def test_short_write_fails(self): self.refused(self.run_writer(fault='short_write'))
    def test_short_read_fails(self): self.refused(self.run_writer(fault='short_read'))
    def test_fsync_failure_fails(self): self.refused(self.run_writer(fault='fsync'))
    def test_cache_invalidation_failure_fails(self): self.refused(self.run_writer(fault='cache_flush'))
    def test_readback_hash_mismatch_fails(self): self.refused(self.run_writer(fault='readback_corruption'))

    def test_open_identity_races_refuse_before_write(self):
        for fault in ['identity_race', 'cid_race', 'capacity_race', 'mount_after_open']:
            with self.subTest(fault=fault): self.refused(self.run_writer(fault=fault), untouched=True)

    def test_mounted_root_swap_and_holders_each_refuse_before_write(self):
        cases = [('/root-type', b'ext4\n'), ('/proc/swaps', b'Filename\tType\n/dev/mmcblk1p3 partition\n'),
                 ('/proc/self/mountinfo', b'1 0 179:10 / /vol1 rw - ext4 /dev/mmcblk1p2 rw\n'),
                 ('/sys/block/mmcblk1/holders/dm-0', b'')]
        for name, value in cases:
            p = self.root/name.lstrip('/'); old = p.read_bytes() if p.exists() else None
            self.put(name, value)
            with self.subTest(path=name): self.refused(self.run_writer(), untouched=True)
            p.unlink() if old is None else p.write_bytes(old)

    def test_symlink_target_is_rejected(self):
        target = self.root/'dev/mmcblk1'; target.unlink(); target.symlink_to(self.root/'dev/sda')
        self.refused(self.run_writer())

    def test_remote_writer_interlock_and_terminal_status(self):
        env = {**os.environ, 'RAM_DEPLOY_TEST_ROOT': str(self.root)}
        first = subprocess.Popen(self.argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.assertEqual(json.loads(first.stdout.readline())['status'], 'READY')
        try:
            self.refused(self.run_writer(), untouched=True)
            status = subprocess.run([self.binary, '--status', TOKEN], capture_output=True, env=env, timeout=5)
            self.assertEqual(json.loads(status.stdout)['status'], 'ACTIVE')
            first.communicate(self.image, timeout=10)
            self.assertEqual(first.returncode, 0)
            status = subprocess.run([self.binary, '--status', TOKEN], capture_output=True, env=env, timeout=5)
            self.assertEqual(json.loads(status.stdout)['run']['status'], 'DONE')
        finally:
            if first.poll() is None: first.kill(); first.communicate()

    def test_multi_chunk_write_and_readback(self):
        self.card = 4*1024*1024; self.length = 2*1024*1024+512
        self.image = bytes(range(256))*(self.length//256); self.original = b'X'*self.card
        self.put('/dev/mmcblk1', self.original); self.put('/sys/block/mmcblk1/size', (str(self.card//512)+'\n').encode())
        r = self.run_writer(); self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual((self.root/'dev/mmcblk1').read_bytes(), self.image+self.original[self.length:])

    def test_controller_streams_verified_source_to_fixture_and_checks_receipt(self):
        source = self.root/'image.img'; source.write_bytes(self.image)
        helper = Path(self.binary)
        approval = {'image': {'path': str(source), 'bytes': self.length, 'sha256': digest(source)},
            'target': {'cid': CID, 'bytes': self.card, 'major': 179, 'minor': 8},
            'rescue_boot_id': BOOT, 'helper_sha256': digest(helper)}
        env = {**os.environ, 'RAM_DEPLOY_TEST_ROOT': str(self.root)}
        original_popen, original_run = subprocess.Popen, subprocess.run

        def fake_popen(argv, **kwargs):
            if argv[0] == self.binary:
                return original_popen(argv, **kwargs)
            actual = shlex.split(argv[-1])
            if actual[0] == 'python3':
                return original_popen(actual, **kwargs)
            self.assertEqual(actual[1], '--execute')
            return original_popen([self.binary, *actual[1:]], env=env, **kwargs)

        def fake_remote(_ssh, command, **kwargs):
            if command.startswith('set -eu;'):
                self.assertIn(BOOT, command)
                self.assertEqual(kwargs['input'], helper.read_bytes())
                return subprocess.CompletedProcess([], 0, (digest(helper)+'  helper\n').encode(), b'')
            actual = shlex.split(command)
            self.assertEqual(actual[1], '--status')
            return original_run([self.binary, *actual[1:]], env=env, capture_output=True, **kwargs)

        with board_lock(self.root/'controller-locks', 'x5-lab-01') as held:
            with patch.object(ram_deploy.subprocess, 'Popen', side_effect=fake_popen), patch.object(ram_deploy, 'remote', side_effect=fake_remote):
                result = ram_deploy.execute(approval, helper, ['fixture-ssh'], '/fixture/server.sock', self.root/'evidence', held)
            held.revalidate()
        self.assertEqual(result['status'], 'PASS_write_and_full_readback_only_not_booted')
        self.assertEqual((self.root/'dev/mmcblk1').read_bytes(), self.image+self.original[self.length:])

    def test_transport_uncertainty_retains_physical_lock_until_terminal(self):
        events = []
        replies = [subprocess.CompletedProcess([], 0, '{"status":"ACTIVE"}', ''),
                   subprocess.CompletedProcess([], 0, json.dumps({'status': 'TERMINAL', 'run': {'token': TOKEN, 'status': 'FAILED'}}), '')]
        with board_lock(self.root/'controller-locks', 'x5-lab-01') as held:
            def pause(_seconds):
                held.revalidate()
                with self.assertRaises(Exception):
                    with board_lock(self.root/'controller-locks', 'x5-lab-01'): pass
            with patch.object(ram_deploy, 'remote', side_effect=replies), patch.object(ram_deploy.time, 'sleep', side_effect=pause):
                result = ram_deploy.wait_terminal([], '/fixture/helper', TOKEN, lambda name, **kw: events.append(name), boot_id=BOOT)
            self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(events, ['completion_unknown_lock_retained_no_power', 'remote_terminal'])


if __name__ == '__main__': unittest.main()
