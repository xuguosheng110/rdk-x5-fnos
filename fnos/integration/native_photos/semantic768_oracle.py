"""Verify candidate preprocessing against the original native CPU RPC first."""
import argparse
import json
from pathlib import Path

from .semantic768_backend import NATIVE_IMAGE_SHA256, checked_digest, normalize_embedding, prepare_image
from .semantic768_benchmark import embedding


def evaluate(session, cpu_socket, rows):
    import numpy as np
    results = []
    for row in rows:
        checked_digest(row['path'], row['sha256'])
        result = {'image': row['path']}
        try:
            native = embedding(cpu_socket, 'inferImg2Vec', {'image': row['path']})
            raw = prepare_image(Path(row['path']).read_bytes())
            candidate = np.asarray(normalize_embedding(session.run(None, {
                'image': np.frombuffer(raw, dtype='<f4').reshape(1, 3, 224, 224)})[0]), dtype=np.float64)
            result.update(cosine=float(np.dot(native, candidate)),
                          max_abs=float(np.max(np.abs(native - candidate))))
            result['passed'] = result['cosine'] >= .999999 and result['max_abs'] <= 2e-5
        except Exception as error:
            result.update(passed=False, error=repr(error))
        results.append(result)
        print(json.dumps(result), flush=True)
    return results


def main():
    import onnxruntime as ort
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('cpu-socket', 'native-onnx', 'manifest', 'report'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--threads', type=int, default=1)
    parser.add_argument('--edge-cases-only', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.threads <= 64:
        parser.error('invalid thread count')
    checked_digest(args.native_onnx, NATIVE_IMAGE_SHA256)
    source = json.loads(args.manifest.read_text())
    rows = source['images'][:1] if args.edge_cases_only else list(source['images'])
    rows += source.get('oracle_images', [])
    options = ort.SessionOptions()
    options.intra_op_num_threads = args.threads
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    session = ort.InferenceSession(str(args.native_onnx), sess_options=options, providers=['CPUExecutionProvider'])
    results = evaluate(session, args.cpu_socket, rows)
    passed = bool(results) and all(row['passed'] for row in results)
    report = {'status': 'native_preprocessing_pass' if passed else 'native_preprocessing_fail',
              'scope': 'original native CPU RPC vs identical native ONNX with candidate preprocessing; no BPU quantization',
              'samples': results, 'gates': {'cosine_min': .999999, 'max_abs_max': 2e-5}}
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    return int(not passed)


if __name__ == '__main__':
    raise SystemExit(main())
