"""Native domestic Medium image encoder, with its original 768D coordinates.

No registration or text-model replacement is performed here. The preprocessing
candidate must pass the original native CPU RPC oracle before activation.
"""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import select
import subprocess
import threading
import time

from .backend import read_allowed_image

DIMENSIONS = 768
MODEL_SHA256 = 'dd605bf507f259df824d22a199b55ca423f7020c51766edb599e2d709e384939'
WORKER_SHA256 = '2d08cda15f2905251b4006c3608b5fe394a4f946c4f7a7409d9d7653cd49184e'
NATIVE_IMAGE_SHA256 = '73a177536d297a3e3767ee1cd55f4ab9045bd0c2a4ac9a584fb77eaf3b7e1988'
NATIVE_TEXT_SHA256 = 'd4712c45f5c5fba9e5603beba03415f06f1d6755d098e781ffcb0a79f4d65a44'
NATIVE_TOKENIZER_SHA256 = 'b7fe3bcc8d284fcb782691411e8b6fd4f45d7245565b094de6ab795e66bcd32f'
PREPROCESS_REVISION = 'domestic-medium-pil-resize224-v1'


class SemanticInferenceError(RuntimeError):
    """Worker failure, distinct from unreadable/invalid user image input."""


class SemanticStartupError(RuntimeError):
    """Failure before binding a candidate socket; CPU fallback is still safe."""


def checked_digest(path, expected):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        raise ValueError('unreviewed semantic artifact: ' + str(path))
    return actual


def normalize_embedding(values):
    """Match native float32 L2 normalization; reject shape/NaN/zero vectors."""
    import numpy as np
    values = np.asarray(values, dtype=np.float32)
    if values.shape not in ((DIMENSIONS,), (1, DIMENSIONS)) or not np.isfinite(values).all():
        raise ValueError('expected a finite 768D embedding')
    values = values.reshape(1, DIMENSIONS)
    with np.errstate(over='ignore', invalid='ignore'):
        norm = np.linalg.norm(values, axis=1, keepdims=True)
    if not np.isfinite(norm).all() or float(norm[0, 0]) < 1e-12:
        raise ValueError('invalid embedding norm')
    return (values / norm)[0].tolist()


def prepare_image(data):
    """Domestic candidate: PIL bicubic stretch, then channel handling, F32/255.

    This deliberately has no CLIP center crop, EXIF transpose, or mean/std
    subtraction. The native ONNX already contains its own mean/std transform.
    Palette, alpha, grayscale and EXIF parity are tested against native RPC,
    not inferred from ordinary RGB calibration images.
    """
    import numpy as np
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        if min(width, height) < 1 or width * height > 40_000_000 or max(width, height) > 32768:
            raise ValueError('image dimensions exceed limit')
        image = image.resize((224, 224), Image.Resampling.BICUBIC)
        values = np.array(image, dtype=np.float32)
    if values.ndim == 2:
        values = np.stack((values, values, values), axis=-1)
    if values.ndim == 3 and values.shape[2] == 4:
        values = values[:, :, :3]
    if values.shape != (224, 224, 3) or not np.isfinite(values).all():
        raise ValueError('unsupported native image channel layout')
    values = values / np.float32(255)
    return np.ascontiguousarray(values.transpose(2, 0, 1)[None], dtype='<f4').tobytes()


class Semantic768Worker:
    """One persistent, serialized worker with checked artifacts and bounded I/O."""
    model_sha256 = MODEL_SHA256
    worker_sha256 = WORKER_SHA256
    input_bytes = 3 * 224 * 224 * 4
    output_bytes = DIMENSIONS * 4

    def __init__(self, executable, model, *, timeout=30.0, stderr=None):
        self.executable, self.model = Path(executable), Path(model)
        checked_digest(self.executable, self.worker_sha256)
        checked_digest(self.model, self.model_sha256)
        if not 0 < timeout <= 120:
            raise ValueError('invalid worker timeout')
        self.timeout, self.stderr = timeout, stderr
        self.lock, self.process = threading.Lock(), None

    def _read(self, count, deadline):
        chunks = []
        fd = self.process.stdout.fileno()
        while count:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                raise TimeoutError('semantic worker response timeout')
            chunk = os.read(fd, count)
            if not chunk:
                raise RuntimeError('semantic worker exited before completing response')
            chunks.append(chunk)
            count -= len(chunk)
        return b''.join(chunks)

    def _start(self):
        self.process = subprocess.Popen([str(self.executable), str(self.model)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.stderr, bufsize=0)
        os.set_blocking(self.process.stdin.fileno(), False)
        os.set_blocking(self.process.stdout.fileno(), False)
        if self._read(4, time.monotonic() + self.timeout) != b'X5CN':
            raise RuntimeError('semantic worker protocol mismatch')

    def infer(self, tensor):
        import numpy as np
        if len(tensor) != self.input_bytes:
            raise ValueError('invalid image tensor byte count')
        with self.lock:
            try:
                if self.process is None:
                    self._start()
                deadline = time.monotonic() + self.timeout
                fd, pending = self.process.stdin.fileno(), memoryview(tensor)
                while pending:
                    left = deadline - time.monotonic()
                    if left <= 0 or not select.select([], [fd], [], left)[1]:
                        raise TimeoutError('semantic worker input timeout')
                    count = os.write(fd, pending)
                    if not count:
                        raise RuntimeError('semantic worker stopped accepting input')
                    pending = pending[count:]
                return normalize_embedding(np.frombuffer(self._read(self.output_bytes, deadline), dtype='<f4'))
            except BaseException:
                self._stop()
                raise

    def _stop(self):
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()

    def close(self):
        with self.lock:
            self._stop()


class Semantic768Backend:
    def __init__(self, worker, roots):
        self.worker, self.roots = worker, roots
        if not callable(roots):
            self.roots = tuple(Path(root) for root in roots)
            if not self.roots or any(not root.is_absolute() or root == Path('/') for root in self.roots):
                raise ValueError('explicit image roots required')

    def infer(self, body):
        if not isinstance(body, dict) or set(body) != {'image'}:
            raise ValueError('expected native image request')
        image = read_allowed_image(body['image'], self.roots)
        tensor = prepare_image(image)
        try:
            return {'embedding': self.worker.infer(tensor)}
        except Exception as error:
            raise SemanticInferenceError('semantic inference worker failed') from error
