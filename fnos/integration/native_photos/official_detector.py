"""Official X5 multi-task face detector and 106-point landmark BPU adapter.

Pinned upstream models, no ROS daemon or CPU neural detector. Resize, NV12
packing and landmark coordinate restoration remain ordinary CPU preprocessing.
"""
import hashlib
import os
from pathlib import Path
import select
import struct
import subprocess
import threading
import time

from .backend import ClipWorker

BOX_SHA256 = 'bae0c2ec8c1c66d2c2ff4e51877dbe2514a5aea3fb714856897c4c696d6d0729'
LANDMARK_SHA256 = 'bf0316c870ddd7854af368f310b03bc33c8a756d8ddae4d3aba5e2a1b564a102'
WIDTH, HEIGHT = 960, 544
POINTS = 106
RECORD_FLOATS = 5 + POINTS * 3
# This pinned 106-point layout: pupils, nose tip and outer mouth corners.
# 74/83 are upper eyelid points, not eye centers: using them split one subject
# after resizing. Numbered overlays plus cross-scale identity tests verify
# 104/105. This is not the unrelated InsightFace 2d106 numbering scheme.
FIVE_POINT_INDICES = (104, 105, 54, 84, 90)


class OfficialBpuDetector(ClipWorker):
    magic = b'X5FD'
    input_bytes = WIDTH * HEIGHT * 3 // 2

    def __init__(self, executable, box_model, landmark_model, *, confidence=.7,
                 timeout=20.0, stderr=None):
        self.executable = Path(executable)
        self.box_model, self.landmark_model = Path(box_model), Path(landmark_model)
        for path, expected in ((self.box_model, BOX_SHA256), (self.landmark_model, LANDMARK_SHA256)):
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                    raise ValueError('model does not match the reviewed official X5 detector')
        if not 0 < confidence < 1 or not 0 < timeout <= 120:
            raise ValueError('invalid detector threshold or timeout')
        self.confidence, self.timeout, self.stderr = confidence, timeout, stderr
        self.process = None
        self.lock = threading.Lock()

    def _start(self):
        self.process = subprocess.Popen([str(self.executable), str(self.box_model), str(self.landmark_model)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.stderr, bufsize=0)
        os.set_blocking(self.process.stdin.fileno(), False)
        os.set_blocking(self.process.stdout.fileno(), False)
        if self._read(4, time.monotonic() + self.timeout) != self.magic:
            raise RuntimeError('official detector worker protocol mismatch')

    def infer_raw(self, tensor):
        if len(tensor) != self.input_bytes:
            raise ValueError('invalid detector input byte count')
        with self.lock:
            try:
                if self.process is None:
                    self._start()
                deadline = time.monotonic() + self.timeout
                pending = memoryview(tensor)
                fd = self.process.stdin.fileno()
                while pending:
                    left = deadline - time.monotonic()
                    if left <= 0 or not select.select([], [fd], [], left)[1]:
                        raise TimeoutError('official detector input timeout')
                    written = os.write(fd, pending)
                    if not written:
                        raise RuntimeError('official detector input closed')
                    pending = pending[written:]
                count, = struct.unpack('<I', self._read(4, deadline))
                if count > 30:
                    raise ValueError('official detector output count exceeds model capacity')
                return self._read(count * RECORD_FLOATS * 4, deadline), count
            except Exception:
                self._stop()
                raise

    def detect(self, image, input_size=None):
        import cv2
        import numpy as np
        if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
            raise ValueError('expected uint8 BGR image')
        h, w = image.shape[:2]
        if min(w, h) < 1 or w*h > 40_000_000 or max(w,h) > 32768:
            raise ValueError('image dimensions exceed detector limit')
        scale = min(WIDTH / w, HEIGHT / h)
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
        x, y = (WIDTH - nw) // 2, (HEIGHT - nh) // 2
        canvas = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
        canvas[y:y+nh, x:x+nw] = cv2.resize(image, (nw, nh), interpolation=cv2.INTER_LINEAR)
        flat = cv2.cvtColor(canvas, cv2.COLOR_BGR2YUV_I420).reshape(-1)
        n = WIDTH * HEIGHT
        uv = np.empty(n // 2, np.uint8)
        uv[0::2], uv[1::2] = flat[n:n*5//4], flat[n*5//4:]
        payload, count = self.infer_raw(flat[:n].tobytes() + uv.tobytes())
        rows = np.frombuffer(payload, dtype='<f4').reshape(count, RECORD_FLOATS)
        if not np.isfinite(rows).all():
            raise ValueError('nonfinite official detector result')
        boxes, landmarks = [], []
        # Use exact rounded resize factors, not an assumed common scale.
        sx, sy = nw / w, nh / h
        for row in rows:
            if not self.confidence <= row[4] <= 1:
                continue
            points = row[5:].reshape(POINTS, 3)[list(FIVE_POINT_INDICES)].copy()
            if (points[:, 2] < .1).any():
                continue
            points[:, 0] = (points[:, 0] - x) / sx
            points[:, 1] = (points[:, 1] - y) / sy
            if (points[:, :2] < 0).any() or (points[:, 0] >= w).any() or (points[:, 1] >= h).any():
                continue
            box = row[:5].copy()
            box[[0, 2]] = np.clip((box[[0, 2]] - x) / sx, 0, w-1)
            box[[1, 3]] = np.clip((box[[1, 3]] - y) / sy, 0, h-1)
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            boxes.append(box)
            landmarks.append(points[:, :2])
        return np.asarray(boxes, dtype=np.float32).reshape(-1, 5), np.asarray(landmarks, dtype=np.float32).reshape(-1, 5, 2)
