"""Measure same/different identity separation on real detection and BPU output.

The JSON fixtures contain explicit test labels and local image paths. This test
does not infer the identity of a person or change the native Photos database.
Native upload, indexing and UI grouping remain separate acceptance gates.
"""
import argparse
import itertools
import json
from pathlib import Path
import sys

from .face import FaceId128Worker, InsightFaceWorker
from .face_backend import FaceBackend


def main():
    p = argparse.ArgumentParser()
    for name in ('worker', 'model', 'det-model', 'fixtures', 'output'):
        p.add_argument('--' + name, required=True, type=Path)
    p.add_argument('--allowed-root', required=True, type=Path, action='append')
    p.add_argument('--encoder', choices=['faceid128', 'insightface'], default='faceid128')
    p.add_argument('--eps', type=float, default=.36)
    args = p.parse_args()
    if not 0 < args.eps < 1:
        p.error('eps must be between zero and one')
    fixtures = json.loads(args.fixtures.read_text())
    if not 4 <= len(fixtures) <= 128 or len({r['label'] for r in fixtures}) < 2:
        p.error('at least two labels and four fixtures required; maximum128')
    sys.path.insert(0, '/usr/trim/lib/ai_manager/com.trim.face_det/cp311')
    from lib.insight import Det
    detector = Det(model_file=str(args.det_model), num_threads_onnx=3)
    detector.prepare(ctx_id=-1, det_thresh=.7, input_size=(640, 640))
    worker_cls = FaceId128Worker if args.encoder == 'faceid128' else InsightFaceWorker
    worker = worker_cls(args.worker, args.model)
    backend = FaceBackend(detector, worker, args.allowed_root)
    vectors = []
    try:
        for row in fixtures:
            faces = backend.infer({'image': row['image']})['embeddingList']
            if len(faces) != 1:
                raise ValueError('fixture must have exactly one detected face: ' + row['image'])
            vector = faces[0]['embedding']
            if args.encoder == 'faceid128' and any(vector[128:]):
                raise ValueError('nonzero values outside the128 learned features')
            vectors.append(vector)
    finally:
        worker.close()
    same, different, pairs = [], [], []
    for a, b in itertools.combinations(range(len(fixtures)), 2):
        cosine = sum(x*y for x, y in zip(vectors[a], vectors[b]))
        match = fixtures[a]['label'] == fixtures[b]['label']
        (same if match else different).append(cosine)
        pairs.append({'a': a, 'b': b, 'same_label': match, 'cosine': cosine})
    if not same or not different:
        raise ValueError('both same-identity and different-identity pairs required')
    threshold = 1 - args.eps
    report = {'scope': 'real detector/alignment/BPU feature separation; native clustering is separate',
              'encoder': args.encoder, 'model_sha256': worker_cls.model_sha256,
              'fixtures': fixtures, 'pairs': pairs,
              'same_min': min(same), 'different_max': max(different),
              'native_default_cosine_threshold': threshold,
              'passed': min(same) > threshold > max(different)}
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k not in ('fixtures', 'pairs')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
