"""768D contract and failure recovery; hardware accuracy has separate evidence."""
import hashlib
import http.server
import io
import json
from pathlib import Path
import socket
import socketserver
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

import numpy as np
from PIL import Image

from fnos.integration.native_photos.semantic768_backend import (
    Semantic768Backend, Semantic768Worker, SemanticInferenceError, SemanticStartupError,
    PREPROCESS_REVISION, NATIVE_IMAGE_SHA256,
    checked_digest, normalize_embedding, prepare_image)
from fnos.integration.native_photos.semantic768_entry import claim_socket, main as entry_main, NATIVE_ENTRY_SHA256
from fnos.integration.native_photos.semantic768_benchmark import (
    cpu_delta, native_health, rpc_response, rpc, NativeProtocolError, require_continuity)


class Semantic768Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def image_bytes(self, image):
        output = io.BytesIO()
        image.save(output, format='PNG')
        return output.getvalue()

    def test_embedding_space_is_768_only_and_float32_normalized(self):
        expected = np.arange(768, dtype=np.float32).reshape(1, 768)
        expected = (expected / np.linalg.norm(expected, axis=1, keepdims=True))[0]
        actual = normalize_embedding(np.arange(768, dtype=np.float32))
        np.testing.assert_array_equal(actual, expected)
        for bad in ([1] * 512, [0] * 768, [float('nan')] * 768, [float('inf')] * 768,
                    [1e38] * 768, np.ones((2, 384))):
            with self.assertRaises(ValueError):
                normalize_embedding(bad)

    def test_rgb_tensor_matches_calibration_preprocessing_without_crop(self):
        rng = np.random.default_rng(42)
        image = Image.fromarray(rng.integers(0, 256, (31, 89, 3), dtype=np.uint8))
        expected = np.asarray(image.convert('RGB').resize((224, 224), Image.Resampling.BICUBIC), dtype=np.float32) / 255
        actual = np.frombuffer(prepare_image(self.image_bytes(image)), dtype='<f4').reshape(1, 3, 224, 224)
        np.testing.assert_array_equal(actual, expected.transpose(2, 0, 1)[None])

    def test_gray_and_rgba_keep_candidate_channel_contract(self):
        gray = Image.fromarray(np.arange(300, dtype=np.uint8).reshape(15, 20))
        tensor = np.frombuffer(prepare_image(self.image_bytes(gray)), dtype='<f4').reshape(3, 224, 224)
        np.testing.assert_array_equal(tensor[0], tensor[1])
        np.testing.assert_array_equal(tensor[1], tensor[2])
        rgba = Image.new('RGBA', (11, 17), (80, 120, 200, 128))
        raw = prepare_image(self.image_bytes(rgba))
        self.assertEqual(len(raw), Semantic768Worker.input_bytes)

    def test_corrupt_and_unsupported_two_channel_image_rejected(self):
        for data in (b'not an image', self.image_bytes(Image.new('LA', (20, 20)))):
            with self.assertRaises((ValueError, OSError)):
                prepare_image(data)

    def test_dynamic_roots_allow_new_mount_without_restarting_worker(self):
        image = self.root / 'new.png'
        image.write_bytes(self.image_bytes(Image.new('RGB', (16, 16), 'red')))
        roots = []
        class Worker:
            def infer(self, tensor):
                return [1.0] + [0.0] * 767
        backend = Semantic768Backend(Worker(), lambda: roots)
        with self.assertRaises(PermissionError):
            backend.infer({'image': str(image)})
        roots.append(self.root)
        self.assertEqual(len(backend.infer({'image': str(image)})['embedding']), 768)
        for body in ({'image': str(image), 'other': 1}, {}, [], {'image': '../file'}):
            with self.assertRaises(ValueError):
                backend.infer(body)

    def test_worker_io_errors_are_server_errors_not_bad_image_requests(self):
        image = self.root / 'valid.png'
        image.write_bytes(self.image_bytes(Image.new('RGB', (16, 16), 'red')))
        for error in (TimeoutError('timeout'), BrokenPipeError('dead worker'), ValueError('bad output')):
            class Worker:
                def infer(self, tensor):
                    raise error
            with self.assertRaises(SemanticInferenceError) as caught:
                Semantic768Backend(Worker(), [self.root]).infer({'image': str(image)})
            self.assertIs(caught.exception.__cause__, error)

    def fixture_worker(self, program, timeout=1):
        model = self.root / 'model'
        model.write_bytes(b'fixture-only model')
        script = self.root / 'worker'
        script.write_text('#!' + sys.executable + '\n' + program)
        script.chmod(0o700)
        class FixtureWorker(Semantic768Worker):
            model_sha256 = hashlib.sha256(model.read_bytes()).hexdigest()
            worker_sha256 = hashlib.sha256(script.read_bytes()).hexdigest()
        return FixtureWorker(script, model, timeout=timeout)

    def test_persistent_worker_handles_two_full_requests(self):
        worker = self.fixture_worker('import sys,struct\nsys.stdout.buffer.write(b"X5CN");sys.stdout.buffer.flush()\n'
            'while True:\n data=sys.stdin.buffer.read(602112)\n if not data: break\n'
            ' sys.stdout.buffer.write(struct.pack("<768f",*([1.0]*768)));sys.stdout.buffer.flush()\n')
        try:
            first = worker.infer(bytes(worker.input_bytes))
            pid = worker.process.pid
            second = worker.infer(bytes(worker.input_bytes))
            self.assertEqual(first, second)
            self.assertEqual(pid, worker.process.pid)
            self.assertEqual(len(first), 768)
        finally:
            worker.close()
        self.assertIsNone(worker.process)

    def test_failed_worker_protocol_timeout_and_short_output_are_reaped(self):
        programs = ['import os\nos.write(1,b"X5CL")\n',
                    'import os,time\nos.write(1,b"X5CN");time.sleep(20)\n',
                    'import sys\nsys.stdout.buffer.write(b"X5CN");sys.stdout.buffer.flush()\n'
                    'sys.stdin.buffer.read(602112);sys.stdout.buffer.write(b"short");sys.stdout.buffer.flush()\n']
        for program in programs:
            with self.subTest(program=program):
                worker = self.fixture_worker(program)
                with self.assertRaises((RuntimeError, TimeoutError, BrokenPipeError)):
                    worker.infer(bytes(worker.input_bytes))
                self.assertIsNone(worker.process)

    def test_unreviewed_executable_or_model_is_rejected(self):
        path = self.root / 'artifact'
        path.write_bytes(b'different')
        with self.assertRaisesRegex(ValueError, 'artifact'):
            checked_digest(path, '0' * 64)

    def test_next_request_recovers_after_child_crash(self):
        program = ('import sys,struct\nfrom pathlib import Path\n'
                   'marker=Path(sys.argv[1]+".started")\n'
                   'sys.stdout.buffer.write(b"X5CN");sys.stdout.buffer.flush()\n'
                   'sys.stdin.buffer.read(602112)\n'
                   'if not marker.exists(): marker.touch();sys.exit(3)\n'
                   'sys.stdout.buffer.write(struct.pack("<768f",*([1.0]*768)));sys.stdout.buffer.flush()\n')
        worker = self.fixture_worker(program)
        try:
            with self.assertRaises(RuntimeError):
                worker.infer(bytes(worker.input_bytes))
            self.assertIsNone(worker.process)
            self.assertEqual(len(worker.infer(bytes(worker.input_bytes))), 768)
        finally:
            worker.close()

    def test_disabled_entry_delegates_original_arguments_without_loading_bpu(self):
        original = self.root / 'original.bin'
        original.write_bytes(b'fixture')
        config = self.root / 'config.json'
        config.write_text(json.dumps({'native_executable': str(original), 'enabled': False}))
        class Delegated(Exception):
            pass
        with patch('fnos.integration.native_photos.semantic768_entry.digest', return_value=NATIVE_ENTRY_SHA256), \
             patch('fnos.integration.native_photos.semantic768_entry.os.execv', side_effect=Delegated) as execute:
            with self.assertRaises(Delegated):
                entry_main(['--runtime-config', str(config), '--edition', 'international', '--unknown-native-option'])
        execute.assert_called_once_with(str(original), [str(original), '--edition', 'international', '--unknown-native-option'])

    def test_enabled_candidate_with_incomplete_acceptance_falls_back_to_native(self):
        original = self.root / 'original.bin'
        original.write_bytes(b'fixture')
        model = self.root / 'native.onnx'
        model.write_bytes(b'fixture model')
        config = self.root / 'config.json'
        config.write_text(json.dumps({'native_executable': str(original), 'enabled': True,
                                      'python_root': str(self.root), 'preprocess_revision': 'wrong'}))
        class Delegated(Exception):
            pass
        def fixture_digest(path):
            return NATIVE_IMAGE_SHA256 if Path(path) == model else NATIVE_ENTRY_SHA256
        before_path = list(sys.path)
        try:
            with patch('fnos.integration.native_photos.semantic768_entry.digest', side_effect=fixture_digest), \
                 patch('fnos.integration.native_photos.semantic768_entry.os.execv', side_effect=Delegated) as execute:
                with self.assertRaises(Delegated):
                    entry_main(['--runtime-config', str(config), '--onnx_model', str(model)])
            execute.assert_called_once_with(str(original), [str(original), '--onnx_model', str(model)])
        finally:
            sys.path[:] = before_path

    def test_first_inference_failure_delegates_cpu_and_releases_socket_lock(self):
        original, model = self.root / 'original.bin', self.root / 'native.onnx'
        original.write_bytes(b'fixture')
        model.write_bytes(b'fixture model')
        endpoint = self.root / 'rpc.sock'
        config = self.root / 'config.json'
        config.write_text(json.dumps({'native_executable': str(original), 'enabled': True,
            'python_root': str(self.root), 'preprocess_revision': PREPROCESS_REVISION,
            'activation_acceptance': 'native-preprocess-search-performance-passed',
            'native_text_model': 'fixture-text', 'native_tokenizer': 'fixture-sp',
            'worker': 'fixture-worker', 'model': 'fixture-model', 'socket_path': str(endpoint)}))
        class Delegated(Exception):
            pass
        def fixture_digest(path):
            return NATIVE_IMAGE_SHA256 if Path(path) == model else NATIVE_ENTRY_SHA256
        before_path, before_argv = list(sys.path), list(sys.argv)
        try:
            with patch('fnos.integration.native_photos.semantic768_entry.digest', side_effect=fixture_digest), \
                 patch('fnos.integration.native_photos.semantic768_backend.checked_digest'), \
                 patch('fnos.integration.native_photos.semantic768_backend.Semantic768Worker', return_value=Mock()), \
                 patch('fnos.integration.native_photos.semantic768_service.main', side_effect=SemanticStartupError('warmup')), \
                 patch('fnos.integration.native_photos.semantic768_entry.os.execv', side_effect=Delegated) as execute:
                with self.assertRaises(Delegated):
                    entry_main(['--runtime-config', str(config), '--onnx_model', str(model), '--socket_path', str(endpoint)])
            self.assertEqual(execute.call_count, 1)
            self.assertFalse(endpoint.exists())
            with claim_socket(endpoint):
                pass
        finally:
            sys.path[:], sys.argv[:] = before_path, before_argv

    def test_live_socket_cannot_be_claimed_and_stale_socket_can(self):
        path = self.root / 'rpc.sock'
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen()
        with self.assertRaisesRegex(RuntimeError, 'already active'):
            claim_socket(path)
        server.close()
        with claim_socket(path):
            self.assertFalse(path.exists())
            with self.assertRaises(BlockingIOError):
                claim_socket(path)

    def test_socket_symlink_or_regular_file_is_never_removed(self):
        path = self.root / 'rpc.sock'
        path.write_text('preserve')
        with self.assertRaises(RuntimeError):
            claim_socket(path)
        self.assertEqual(path.read_text(), 'preserve')
        path.unlink()
        path.symlink_to(self.root / 'nonexistent')
        with self.assertRaises(RuntimeError):
            claim_socket(path)
        self.assertTrue(path.is_symlink())

    def test_cpu_sampling_rejects_restart_instead_of_underreporting(self):
        before = [{'pid': 5, 'start_ticks': 100, 'cpu_s': 3.0}]
        self.assertAlmostEqual(cpu_delta(before, [{'pid': 5, 'start_ticks': 100, 'cpu_s': 3.7}]), .7)
        with self.assertRaises(RuntimeError):
            cpu_delta(before, [{'pid': 5, 'start_ticks': 101, 'cpu_s': .1}])

    def test_real_native_health_boolean_fixture_and_unknown_shapes(self):
        self.assertTrue(native_health(json.loads('{"code":0,"data":true}')))
        self.assertFalse(native_health({'code': 0, 'data': False}))
        for response in ({'code': 0, 'data': {'ready': True}}, {'code': 0, 'data': 1},
                         {'code': 0, 'data': 'true'}, {'code': 1, 'data': True}, {}, True):
            with self.assertRaises(NativeProtocolError):
                native_health(response)

    def test_unix_http_health_bool_does_not_relax_inference_object_contract(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                data = True if request['method'] == 'healthCheck' else {'embedding': [1.] + [0.] * 767}
                payload = json.dumps({'code': 0, 'data': data}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *_args):
                pass
        endpoint = self.root / 'rpc-health.sock'
        server = socketserver.UnixStreamServer(str(endpoint), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            import os
            response = rpc_response(endpoint, 'healthCheck', {}, expected_pid=os.getpid())
            self.assertTrue(native_health(response))
            with self.assertRaises(NativeProtocolError):
                rpc(endpoint, 'healthCheck', {})
            self.assertEqual(len(rpc(endpoint, 'inferImg2Vec', {'image': '/fixture'})['embedding']), 768)
            with self.assertRaisesRegex(NativeProtocolError, 'PID'):
                rpc_response(endpoint, 'healthCheck', {}, expected_pid=os.getpid() + 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_cross_request_restart_and_thread_budget_escape_are_rejected(self):
        initial = [{'pid': 5, 'start_ticks': 100, 'affinity': [7], 'thread_affinities': {'5': [7], '6': [7]}}]
        require_continuity(initial, initial, [7])
        for change in ({'pid': 9}, {'start_ticks': 101}, {'affinity': list(range(8))},
                       {'thread_affinities': {'5': [7], '6': [0, 7]}}):
            with self.assertRaises(RuntimeError):
                require_continuity(initial, [{**initial[0], **change}], [7])

    def test_missing_or_corrupt_runtime_json_uses_verified_fixed_anchor(self):
        original = self.root / 'img2vec.native.bin'
        original.write_bytes(b'fixture')
        config = self.root / 'missing.json'
        class Delegated(Exception):
            pass
        for corrupt in (False, True):
            if corrupt:
                config.write_text('not-json')
            with patch('fnos.integration.native_photos.semantic768_entry.NATIVE_FALLBACK_PATHS', (original,)), \
                 patch('fnos.integration.native_photos.semantic768_entry.digest', return_value=NATIVE_ENTRY_SHA256), \
                 patch('fnos.integration.native_photos.semantic768_entry.os.execv', side_effect=Delegated) as execute:
                with self.assertRaises(Delegated):
                    entry_main(['--runtime-config', str(config), '--edition', 'domestic'])
                execute.assert_called_once_with(str(original), [str(original), '--edition', 'domestic'])


if __name__ == '__main__':
    unittest.main()
