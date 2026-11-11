"""Exercise the actual variable-length detector IPC at its failure boundaries."""
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from fnos.integration.native_photos import official_detector as mod

class OfficialDetectorProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.model=self.root/'model';self.model.write_bytes(b'test-model')
        self.sha=hashlib.sha256(b'test-model').hexdigest()
    def tearDown(self):self.tmp.cleanup()
    def worker(self,output):
        executable=self.root/'worker'
        executable.write_text('#!'+sys.executable+'\nimport sys,struct\nsys.stdout.buffer.write(b"X5FD");sys.stdout.buffer.flush()\nsys.stdin.buffer.read(783360)\n'+output+'\n')
        executable.chmod(0o700)
        with patch.object(mod,'BOX_SHA256',self.sha),patch.object(mod,'LANDMARK_SHA256',self.sha):
            return mod.OfficialBpuDetector(executable,self.model,self.model,timeout=10)
    def test_empty_result_is_valid(self):
        w=self.worker('sys.stdout.buffer.write(struct.pack("<I",0));sys.stdout.buffer.flush()')
        try:self.assertEqual(w.infer_raw(bytes(w.input_bytes)),(b'',0))
        finally:w.close()
    def test_count_overflow_rejected_before_payload_read(self):
        w=self.worker('sys.stdout.buffer.write(struct.pack("<I",31));sys.stdout.buffer.flush()')
        with self.assertRaisesRegex(ValueError,'capacity'):w.infer_raw(bytes(w.input_bytes))
        self.assertIsNone(w.process)
    def test_truncated_landmarks_fail_and_worker_reaped(self):
        w=self.worker('sys.stdout.buffer.write(struct.pack("<I",1)+b"short");sys.stdout.buffer.flush()')
        with self.assertRaisesRegex(RuntimeError,'exited'):w.infer_raw(bytes(w.input_bytes))
        self.assertIsNone(w.process)
    def test_bounded_timeout_does_not_fallback_to_cpu(self):
        w=self.worker('import time;time.sleep(30)')
        # Let interpreter startup finish before exercising the response deadline.
        w._start()
        w.timeout=.2
        with self.assertRaises(TimeoutError):w.infer_raw(bytes(w.input_bytes))
        self.assertIsNone(w.process)
    def test_model_pin_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError,'reviewed'):
            mod.OfficialBpuDetector('/missing',self.model,self.model)
    def test_partial_frame_rejected_before_process(self):
        w=self.worker('')
        with self.assertRaisesRegex(ValueError,'byte count'):w.infer_raw(b'bad')
        self.assertIsNone(w.process)

if __name__=='__main__':unittest.main()
