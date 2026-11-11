"""Pinned D-Robotics faceid InsightFace encoder, independent of ROS 2.

Feature extraction only: detection, landmark quality, native Photos model
registration and identity clustering require separate end-to-end validation.
"""
from .backend import ClipWorker

FACE_SHA256 = "bd9ff56e7b14e26bad16d0ee0f98f39d41261c3cf7ba85cad9dfdafbc9332d61"


class InsightFaceWorker(ClipWorker):
    model_sha256 = FACE_SHA256
    magic = b"X5IF"
    input_bytes = 112 * 112 * 3 // 2


class FaceId128Worker(ClipWorker):
    """Upstream S32 FaceID: 128 learned values in a zero-padded 512-value RPC."""
    model_sha256 = "aa3ab3230f0957bc60a9b0c3dbd21e7900a14fe7aa7b1031f3b78fb0e0ab7ce1"
    magic = b"X5F1"
    input_bytes = 112 * 112 * 3 // 2


def aligned_bgr_to_nv12(bgr):
    """Convert an already aligned 112x112 BGR crop to unpadded NV12."""
    import cv2
    import numpy as np
    if bgr.shape != (112, 112, 3) or bgr.dtype != np.uint8:
        raise ValueError("expected aligned 112x112 uint8 BGR face")
    yuv = cv2.cvtColor(np.ascontiguousarray(bgr), cv2.COLOR_BGR2YUV_I420).reshape(-1)
    pixels = 112 * 112
    uv = np.empty(pixels // 2, dtype=np.uint8)
    uv[0::2], uv[1::2] = yuv[pixels:pixels * 5 // 4], yuv[pixels * 5 // 4:]
    return yuv[:pixels].tobytes() + uv.tobytes()


def align_face(bgr, landmarks):
    """Similarity-fit five landmarks to the standard InsightFace 112px template.

    Landmark order: left eye, right eye, nose, left mouth, right mouth, in image
    coordinates. Least squares fits scale/rotation/translation without reflection.
    Reference: deepinsight/insightface python-package/insightface/utils/face_align.py.
    """
    import cv2
    import numpy as np
    if bgr.ndim != 3 or bgr.shape[2] != 3 or bgr.dtype != np.uint8:
        raise ValueError("expected uint8 BGR image")
    src = np.asarray(landmarks, dtype=np.float64)
    if src.shape != (5, 2) or not np.isfinite(src).all():
        raise ValueError("expected five finite face landmarks")
    if (src < 0).any() or (src[:, 0] >= bgr.shape[1]).any() or (src[:, 1] >= bgr.shape[0]).any():
        raise ValueError("landmarks outside image")
    dst = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                    [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float64)
    design = np.zeros((10, 4), dtype=np.float64)
    design[0::2, 0], design[0::2, 1], design[0::2, 2] = src[:, 0], -src[:, 1], 1
    design[1::2, 0], design[1::2, 1], design[1::2, 3] = src[:, 1], src[:, 0], 1
    params, _, rank, _ = np.linalg.lstsq(design, dst.reshape(-1), rcond=None)
    a, b, tx, ty = params
    if rank != 4 or not np.isfinite(params).all() or a*a + b*b < 1e-12:
        raise ValueError("degenerate face alignment")
    matrix = np.array([[a, -b, tx], [b, a, ty]], dtype=np.float64)
    return cv2.warpAffine(bgr, matrix, (112, 112), flags=cv2.INTER_LINEAR, borderValue=0)
