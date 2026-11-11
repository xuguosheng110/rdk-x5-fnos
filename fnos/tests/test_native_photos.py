"""Access and process failure regressions for the native Photos bridge."""
import os
import hashlib
import sys
from pathlib import Path
import tempfile
import unittest

from fnos.integration.native_photos.backend import ClipWorker, read_allowed_image, normalize_embedding


class NativePhotosTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.allowed = self.root / "allowed"
        self.allowed.mkdir()
        self.image = self.allowed / "image.jpg"
        self.image.write_bytes(b"fixture")

    def tearDown(self):
        self.tmp.cleanup()

    def test_allowed_regular_file(self):
        self.assertEqual(read_allowed_image(str(self.image), [self.allowed]), b"fixture")

    def test_outside_and_prefix_collision_rejected(self):
        other = self.root / "allowed-other"
        other.mkdir()
        image = other / "image.jpg"
        image.write_bytes(b"private")
        for path in (str(image), str(self.allowed / ".." / "allowed-other" / "image.jpg")):
            with self.assertRaises((ValueError, PermissionError)):
                read_allowed_image(path, [self.allowed])

    def test_symlink_file_and_parent_rejected(self):
        for name, target in (("link.jpg", self.image), ("dir", self.allowed)):
            (self.allowed / name).symlink_to(target)
        for path in (self.allowed / "link.jpg", self.allowed / "dir" / "image.jpg"):
            with self.assertRaises(OSError):
                read_allowed_image(str(path), [self.allowed])

    def test_special_file_rejected_without_blocking(self):
        fifo = self.allowed / "fifo"
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            read_allowed_image(str(fifo), [self.allowed])

    def test_empty_and_oversized_rejected(self):
        self.image.write_bytes(b"")
        with self.assertRaises(ValueError):
            read_allowed_image(str(self.image), [self.allowed])
        self.image.write_bytes(b"0123456789")
        with self.assertRaises(ValueError):
            read_allowed_image(str(self.image), [self.allowed], max_bytes=9)

    def test_embedding_contract(self):
        result = normalize_embedding([1.0] * 512)
        self.assertEqual(len(result), 512)
        self.assertAlmostEqual(sum(v*v for v in result), 1.0)
        for bad in ([0.0] * 512, [1.0] * 511, [float("nan")] * 512,
                    [float("inf")] * 512, [1e308] * 512):
            with self.assertRaises(ValueError):
                normalize_embedding(bad)

    def fake_worker(self, program):
        model = self.root / 'model.bin'
        model.write_bytes(b'test-only model')
        script = self.root / 'worker'
        script.write_text('#!' + sys.executable + '\n' + program)
        script.chmod(0o700)
        class FixtureWorker(ClipWorker):
            model_sha256 = hashlib.sha256(b'test-only model').hexdigest()
        return FixtureWorker(script, model, timeout=2.0)

    def test_worker_crash_is_reported_and_reaped(self):
        worker = self.fake_worker('import os\nos.write(1,b"X5CL")\n')
        with self.assertRaises((RuntimeError, BrokenPipeError)):
            worker.infer(bytes(worker.input_bytes))
        self.assertIsNone(worker.process)

    def test_worker_timeout_is_bounded_and_reaped(self):
        worker = self.fake_worker('import os,time\nos.write(1,b"X5CL")\ntime.sleep(30)\n')
        with self.assertRaises(TimeoutError):
            worker.infer(bytes(worker.input_bytes))
        self.assertIsNone(worker.process)

    def test_wrong_worker_protocol_is_rejected(self):
        worker = self.fake_worker('import os\nos.write(1,b"WRNG")\n')
        with self.assertRaisesRegex(RuntimeError, 'protocol'):
            worker.infer(bytes(worker.input_bytes))
        self.assertIsNone(worker.process)


if __name__ == "__main__":
    unittest.main()
