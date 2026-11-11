"""Real native-RPC/detection/BPU test on owned public-image fixtures."""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser()
    for name in ('worker', 'model', 'det-model', 'source-image', 'output-root'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    import cv2
    import numpy as np

    root = Path(tempfile.mkdtemp(prefix='face-pipeline-', dir=args.output_root))
    sock = root / 'rpc.sock'
    original = cv2.imread(str(args.source_image))
    assert original is not None
    fixtures = {
        'original': original,
        'brighter': np.minimum(original.astype(np.int16) + 10, 255).astype(np.uint8),
        'rotated': cv2.rotate(original, cv2.ROTATE_90_CLOCKWISE),
        'two_faces': np.hstack([original, original]),
        'no_face': np.zeros_like(original),
    }
    for name, data in fixtures.items():
        assert cv2.imwrite(str(root / (name + '.png')), data)
    command = [sys.executable, '-m', 'fnos.integration.native_photos.face_service',
               '--worker', str(args.worker), '--model', str(args.model),
               '--det-model', str(args.det_model), '--allowed-root', str(root),
               '--socket-path', str(sock)]
    results = {}
    with (root / 'service.log').open('w') as log:
        proc = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True)
        try:
            for _ in range(150):
                if sock.exists():
                    break
                if proc.poll() is not None:
                    raise RuntimeError('service exited before ready')
                time.sleep(.2)

            def request(body):
                run = subprocess.run(['curl', '-sS', '--max-time', '25', '--unix-socket', str(sock),
                                      'http://localhost/', '-H', 'Content-Type: application/json',
                                      '--data-binary', '@-'],
                                     input=json.dumps({'method': 'inferFaceDet', 'body': body}),
                                     text=True, capture_output=True, check=True)
                return json.loads(run.stdout)

            for name in fixtures:
                start = time.monotonic()
                response = request({'image': str(root / (name + '.png'))})
                assert response['code'] == 0, (name, response)
                faces = response['data']['embeddingList']
                # Rotated images are a measured gap if the native detector
                # returns no face; do not make up automatic rotation support.
                expected = {'original': 1, 'brighter': 1, 'two_faces': 2, 'no_face': 0}
                if name in expected:
                    assert len(faces) == expected[name], (name, len(faces))
                for face in faces:
                    vector = face['embedding']
                    assert len(vector) == 512 and all(math.isfinite(v) for v in vector)
                    assert abs(sum(v*v for v in vector) - 1) < 1e-5
                    assert len(face['bbox']) == 4
                results[name] = {'faces': len(faces), 'seconds': time.monotonic() - start}
                (root / (name + '.response.json')).write_text(json.dumps(response))
            original_vector = json.loads((root / 'original.response.json').read_text())['data']['embeddingList'][0]['embedding']
            bright_vector = json.loads((root / 'brighter.response.json').read_text())['data']['embeddingList'][0]['embedding']
            similarity = sum(a*b for a, b in zip(original_vector, bright_vector))
            assert similarity > .9, similarity
            results['same_image_brightness_cosine'] = similarity
            assert request({})['code'] != 0
            assert request({'image': '/etc/passwd'})['code'] != 0
            results['invalid_and_outside_rejected'] = True
        finally:
            os.kill(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        assert proc.returncode == 0, proc.returncode
    results['service_exit'] = proc.returncode
    results['scope'] = 'real detector + BPU + native RPC; not native Photos task registration or clustering'
    (root / 'results.json').write_text(json.dumps(results, indent=2))
    print(json.dumps({'stage': str(root), 'results': results}, indent=2))


if __name__ == '__main__':
    main()
