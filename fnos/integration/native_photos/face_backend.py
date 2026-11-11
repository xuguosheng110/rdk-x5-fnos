"""Native inferFaceDet payload using the installed detector and X5 faceid.

Detection is supplied by the configured versioned detector. FaceID extraction
uses the BPU; official-x5 additionally uses BPU box and landmark models.
The faceid embedding namespace must not be mixed with native model embeddings.
"""
import io
import threading

from .backend import read_allowed_image
from .face import align_face, aligned_bgr_to_nv12


class FaceBackend:
    def __init__(self, detector, worker, roots, *, max_faces=64):
        self.detector, self.worker, self.roots = detector, worker, roots
        self.max_faces = max_faces
        self.lock = threading.Lock()

    def infer(self, body):
        import cv2
        import numpy as np
        from PIL import Image

        if not isinstance(body, dict) or not isinstance(body.get('image'), str):
            raise ValueError('image path required')
        data = read_allowed_image(body['image'], self.roots)
        with Image.open(io.BytesIO(data)) as header:
            w, h = header.size
            if min(w, h) < 1 or max(w, h) > 32768 or w * h > 40_000_000:
                raise ValueError('image dimensions exceed limit')
        # The native detector uses imread's orientation convention. OpenCV
        # applies EXIF before returning BGR; boxes therefore follow that image.
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.size > 3 * 40_000_000:
            raise ValueError('image decode failed')
        with self.lock:
            boxes, landmarks = self.detector.detect(image, input_size=(640, 640))
            if len(boxes) > self.max_faces:
                raise ValueError('face count exceeds limit')
            if len(boxes) and (landmarks is None or len(landmarks) != len(boxes)):
                raise ValueError('detector landmark contract failed')
            output = []
            for box, points in zip(boxes, [] if landmarks is None else landmarks):
                if len(box) != 5 or not np.isfinite(box).all():
                    raise ValueError('detector returned invalid box')
                crop = align_face(image, points)
                vector = self.worker.infer(aligned_bgr_to_nv12(crop))
                output.append({'embedding': vector, 'bbox': [float(v) for v in box[:4]]})
            return {'embeddingList': output}
