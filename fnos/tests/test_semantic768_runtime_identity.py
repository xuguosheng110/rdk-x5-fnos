"""Offline controlled-process receipt tests; never run a native ARM model."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fnos.integration.native_photos import semantic768_runtime_identity as identity


class RuntimeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.model, self.endpoint = self.root / 'native.onnx', self.root / 'cpu.sock'
        self.model.write_bytes(b'fixture native model')
        self.command = [str(Path(sys.executable).resolve()), '-c', 'import time; time.sleep(30)',
                        '--model_format', 'ONNX', '--edition', 'domestic', '--onnx_model', str(self.model),
                        '--num_threads_onnx', '1', '--socket_path', str(self.endpoint)]
        self.process = subprocess.Popen(self.command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.log = self.root / 'cpu.log'
        self.log.write_text('fixture INFO: model_format:ONNX\nfixture INFO: edition:domestic\n'
                            'fixture INFO: onnx_model:' + str(self.model) + '\nfixture INFO: num_threads_onnx:1\n')
        self.receipt = self.root / 'receipt.json'
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(identity, 'peer_pid', return_value=self.process.pid))
        self.stack.enter_context(patch.object(identity, 'NATIVE_ENTRY_SHA256', hashlib.sha256(Path(self.command[0]).read_bytes()).hexdigest()))
        self.stack.enter_context(patch.object(identity, 'NATIVE_IMAGE_SHA256', hashlib.sha256(self.model.read_bytes()).hexdigest()))

    def tearDown(self):
        self.stack.close()
        self.process.terminate()
        self.process.wait(timeout=5)
        self.temporary.cleanup()

    def capture(self):
        return identity.capture_receipt(self.process, self.command, self.endpoint, self.log, 'cpu', self.receipt)

    def test_actual_popen_child_identity_and_launch_log_are_sufficient_without_proc_argv(self):
        self.capture()
        original = Path.read_bytes
        def argv_is_rewritten(path):
            if path.name == 'cmdline':
                return b'trim.img2vec\0'
            return original(path)
        with patch.object(Path, 'read_bytes', argv_is_rewritten):
            result = identity.validate_receipt(self.receipt, 'cpu', self.endpoint, expected_pid=self.process.pid)
        self.assertEqual(result['num_threads_onnx'], 1)
        self.assertEqual(result['model'], str(self.model))

    def test_socket_peer_mismatch_rejected(self):
        self.capture()
        with patch.object(identity, 'peer_pid', return_value=self.process.pid + 1):
            with self.assertRaisesRegex(ValueError, 'peer'):
                identity.validate_receipt(self.receipt, 'cpu', self.endpoint)

    def test_process_restart_or_foreign_controller_rejected(self):
        self.capture()
        data = json.loads(self.receipt.read_text())
        data['process']['start_ticks'] += 1
        self.receipt.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'identity'):
            identity.validate_receipt(self.receipt, 'cpu', self.endpoint)
        with patch.object(identity.os, 'getpid', return_value=9999991), patch.object(identity.os, 'getppid', return_value=9999992):
            with self.assertRaisesRegex(ValueError, 'controller'):
                identity.validate_receipt(self.receipt, 'cpu', self.endpoint)

    def test_changed_model_or_startup_log_rejected(self):
        self.capture()
        self.model.write_bytes(b'other model')
        with self.assertRaises(ValueError):
            identity.validate_receipt(self.receipt, 'cpu', self.endpoint)
        self.model.write_bytes(b'fixture native model')
        self.log.write_text('unrelated startup log')
        with self.assertRaisesRegex(ValueError, 'startup'):
            identity.validate_receipt(self.receipt, 'cpu', self.endpoint)

    def test_append_only_logs_allowed_but_unconfirmed_threads_are_not(self):
        self.capture()
        with self.log.open('a') as stream:
            stream.write('fixture INFO: inference completed\n')
        identity.validate_receipt(self.receipt, 'cpu', self.endpoint)
        self.receipt.unlink()
        self.log.write_text(self.log.read_text().replace('num_threads_onnx:1', 'num_threads_onnx:2'))
        with self.assertRaisesRegex(ValueError, 'startup log'):
            self.capture()

    def test_real_text_log_without_model_format_keeps_model_and_tokenizer_gates(self):
        config = {'model': '/native/text.onnx', 'tokenizer': '/native/sp.model', 'num_threads_onnx': 1}
        log = b'INFO: onnx_model:/native/text.onnx\nINFO: sp:/native/sp.model\nINFO: num_threads_onnx:1\nINFO: edition:domestic\nINFO: device:CPU\n'
        identity.verify_log(log, config)
        with self.assertRaises(ValueError):
            identity.verify_log(log.replace(b'sp:/native/sp.model', b'sp:/wrong/sp.model'), config)
        with self.assertRaises(ValueError):
            identity.verify_log(log.replace(b'device:CPU', b'device:CUDA'), config)
        with self.assertRaises(ValueError):
            identity.verify_log(log, {'model': '/native/text.onnx', 'num_threads_onnx': 1})


if __name__ == '__main__':
    unittest.main()
