import errno
from pathlib import Path
import signal
import subprocess
import unittest
from unittest.mock import Mock, patch

from fnos.integration.native_photos import semantic768_run_text_validation as controller


class NativeTextControllerTests(unittest.TestCase):
    def test_native_command_keeps_domestic_medium_tokenizer_contract(self):
        command = controller.native_command('/native/text.bin', '/models/text.onnx', '/models/sp.model', '/tmp/test/text.sock', 1)
        self.assertEqual(command[command.index('--sp') + 1], '/models/sp.model')
        self.assertEqual(command[command.index('--edition') + 1], 'domestic')
        self.assertEqual(command[command.index('--socket_path') + 1], '/tmp/test/text.sock')
        self.assertNotIn('--tokenizer', command)
        for threads in (0, 65, True):
            with self.assertRaises(ValueError):
                controller.native_command('/text', '/model', '/sp', '/socket', threads)

    def test_health_bool_false_then_true_binds_expected_pid(self):
        process = Mock(pid=123); process.poll.return_value = None
        row = {}; save = Mock()
        with patch.object(controller, 'rpc_response', side_effect=[{'code': 0, 'data': False}, {'code': 0, 'data': True}]) as rpc, \
             patch.object(controller.time, 'sleep'):
            controller.wait_ready(process, Path('/tmp/isolated.sock'), row, save)
        self.assertEqual(rpc.call_count, 2)
        self.assertEqual(rpc.call_args.kwargs['expected_pid'], 123)
        self.assertIn('startup_to_ready_s', row)
        self.assertEqual(row['last_health_response'], {'code': 0, 'data': True})

    def test_unknown_health_protocol_fails_immediately(self):
        process = Mock(pid=123); process.poll.return_value = None
        row = {}
        with patch.object(controller, 'rpc_response', return_value={'code': 0, 'data': {'ready': True}}) as rpc:
            with self.assertRaises(controller.NativeProtocolError):
                controller.wait_ready(process, Path('/tmp/isolated.sock'), row, Mock())
        self.assertEqual(rpc.call_count, 1)
        self.assertIn('last_readiness_error', row)

    def test_permission_failure_is_not_initializing_retry(self):
        process = Mock(pid=123); process.poll.return_value = None
        with patch.object(controller, 'rpc_response', side_effect=PermissionError(errno.EACCES, 'fixture')) as rpc:
            with self.assertRaises(PermissionError):
                controller.wait_ready(process, Path('/tmp/isolated.sock'), {}, Mock())
        self.assertEqual(rpc.call_count, 1)

    def test_cleanup_waits_and_kills_only_owned_process_group(self):
        process = Mock(pid=123)
        process.wait.side_effect = [subprocess.TimeoutExpired('fixture', 10), 0]
        with patch.object(controller.os, 'killpg') as kill:
            controller.stop_owned(process)
        self.assertEqual(kill.call_args_list[0].args, (123, signal.SIGTERM))
        self.assertEqual(kill.call_args_list[1].args, (123, signal.SIGKILL))
        self.assertEqual(process.wait.call_count, 2)


if __name__ == '__main__':
    unittest.main()
