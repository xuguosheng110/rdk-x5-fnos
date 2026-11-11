"""BPU image embeddings with bounded local input and a supervised C worker.

This adapter is not automatically registered with ai_manager: its paired CLIP
model must first pass semantic compatibility and native Photos indexing gates.
"""
from __future__ import annotations

import hashlib
import io
import math
import os
from pathlib import Path
import select
import stat
import struct
import subprocess
import threading
import time

IMAGE_SHA256 = "99f54b25c74e3a9d19aaf0d258d8a6939f46e2752d43673881565015ef8ae1e1"
TEXT_SHA256 = "d2a53b45c90c4e66c139143d19ff76799288d6114ed981726f45364d1cad9298"


def read_allowed_image(path: str, roots, *, max_bytes=32 * 1024 * 1024) -> bytes:
    """Open a regular file under explicit roots, rejecting every symlink component.

    Walk directory descriptors to avoid a realpath/check/open race. Does not
    replace native Photos user authorization; the native RPC is root-peer only.
    """
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
        raise ValueError("image must be an absolute local path")
    parts = path.split("/")[1:]
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise ValueError("noncanonical image path")
    candidate = Path(path)
    if callable(roots):
        roots = roots()
    if not any(candidate.is_relative_to(Path(r)) and candidate != Path(r) for r in roots):
        raise PermissionError("image outside configured roots")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        image_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(image_fd)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= max_bytes:
                raise ValueError("image is not a bounded regular file")
            chunks, size = [], 0
            while True:
                chunk = os.read(image_fd, min(1024 * 1024, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("image grew beyond size limit")
            if size != info.st_size:
                raise ValueError("image changed while reading")
            return b"".join(chunks)
        finally:
            os.close(image_fd)
    finally:
        os.close(fd)


def normalize_embedding(values):
    if len(values) != 512 or not all(math.isfinite(v) for v in values):
        raise ValueError("invalid embedding shape or nonfinite values")
    norm = math.sqrt(sum(v * v for v in values))
    if not math.isfinite(norm) or norm < 1e-12:
        raise ValueError("invalid embedding norm")
    return [float(v / norm) for v in values]


def prepare_image(data: bytes) -> bytes:
    """Published X5 CLIP RGB bicubic/center-crop [0,1] F32 NCHW contract."""
    import cv2
    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(data)) as header:
        w, h = header.size
        if min(w, h) < 1 or w * h > 40_000_000 or max(w, h) > 32768:
            raise ValueError("image dimensions exceed limit")
    bgr = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if bgr is None or bgr.shape[:2] != (h, w):
        raise ValueError("image decode failed or dimensions changed")
    if h < w:
        new_h, new_w = 224, round(w * 224 / h)
    else:
        new_h, new_w = round(h * 224 / w), 224
    if new_h * new_w > 16_000_000:
        raise ValueError("image aspect ratio exceeds limit")
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    resized = cv2.resize(rgb, (new_w, new_h), interpolation=cv2.INTER_CUBIC)
    y, x = (new_h - 224) // 2, (new_w - 224) // 2
    crop = resized[y:y+224, x:x+224].astype(np.float32) / np.float32(255)
    return np.ascontiguousarray(crop.transpose(2, 0, 1)[None], dtype="<f4").tobytes()


class ClipWorker:
    """One persistent model; serialized requests and bounded worker I/O."""
    model_sha256 = IMAGE_SHA256
    magic = b"X5CL"
    input_bytes = 3 * 224 * 224 * 4

    def __init__(self, executable, model, *, timeout=20.0, stderr=None):
        self.executable, self.model = Path(executable), Path(model)
        with self.model.open("rb") as f:
            if hashlib.file_digest(f, "sha256").hexdigest() != self.model_sha256:
                raise ValueError("model does not match the reviewed X5 encoder")
        self.timeout, self.stderr = timeout, stderr
        self.lock = threading.Lock()
        self.process = None

    def _start(self):
        self.process = subprocess.Popen([str(self.executable), str(self.model)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.stderr, bufsize=0)
        os.set_blocking(self.process.stdin.fileno(), False)
        os.set_blocking(self.process.stdout.fileno(), False)
        if self._read(4, time.monotonic() + self.timeout) != self.magic:
            raise RuntimeError("worker protocol mismatch")

    def _read(self, count, deadline):
        fd = self.process.stdout.fileno()
        chunks = []
        while count:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], max(0, left))[0]:
                raise TimeoutError("BPU worker response timeout")
            data = os.read(fd, count)
            if not data:
                raise RuntimeError("BPU worker exited before response")
            chunks.append(data)
            count -= len(data)
        return b"".join(chunks)

    def infer(self, tensor: bytes):
        if len(tensor) != self.input_bytes:
            raise ValueError("invalid input tensor byte count")
        with self.lock:
            try:
                if self.process is None:
                    self._start()
                deadline = time.monotonic() + self.timeout
                pending = memoryview(tensor)
                fd = self.process.stdin.fileno()
                while pending:
                    left = deadline - time.monotonic()
                    if left <= 0 or not select.select([], [fd], [], max(0, left))[1]:
                        raise TimeoutError("BPU worker input timeout")
                    pending = pending[os.write(fd, pending):]
                values = struct.unpack("<512f", self._read(512 * 4, deadline))
                return normalize_embedding(values)
            except Exception:
                self._stop()
                raise

    def _stop(self):
        p, self.process = self.process, None
        if p is not None:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=5)
            p.stdin.close()
            p.stdout.close()

    def close(self):
        with self.lock:
            self._stop()


class ImageBackend:
    def __init__(self, worker, roots):
        self.worker, self.roots = worker, tuple(Path(r) for r in roots)
        if not self.roots or any(not r.is_absolute() or r == Path("/") for r in self.roots):
            raise ValueError("explicit image roots required")

    def infer(self, body):
        if not isinstance(body, dict) or set(body) != {"image"}:
            raise ValueError("expected image request")
        data = read_allowed_image(body["image"], self.roots)
        return {"embedding": self.worker.infer(prepare_image(data))}
