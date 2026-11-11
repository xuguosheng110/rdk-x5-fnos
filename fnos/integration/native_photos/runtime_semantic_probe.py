"""Run the qualified 768D candidate against fixed tensors on an actual X5.

This isolated probe does not register an AI service, alter a Photos index, or
claim native preprocessing/search compatibility. --validate-only checks the
bundle on the server without executing the ARM worker.
"""
import argparse
import hashlib
import json
import platform
from pathlib import Path
import subprocess
import time


MODEL_SHA256 = '3aad66e225b8f6ea5edf7db08ef46d5babf8ac9fa23634242e6c979ec09b1bab'
WORKER_SHA256 = '2d08cda15f2905251b4006c3608b5fe394a4f946c4f7a7409d9d7653cd49184e'
GATES = {'cosine_min': .99, 'cosine_mean': .995, 'reference_retrieval_top1': .95}


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def load_bundle(root):
    import numpy as np
    root = root.resolve(strict=True)
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest['schema'] != 'fnos-semantic-runtime-probe/v1':
        raise ValueError('unsupported bundle schema')
    if len(manifest['fixtures']) != 28:
        raise ValueError('all 28 held-out fixtures are required')
    if len({row['name'] for row in manifest['fixtures']}) != 28:
        raise ValueError('duplicate fixtures')

    def checked(name, expected):
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file() or digest(path) != expected:
            raise ValueError('bundle file mismatch: ' + name)
        return path

    worker = checked('semantic768-worker', WORKER_SHA256)
    model = checked('native_cn_image.bin', MODEL_SHA256)
    tensors, references = [], []
    for row in manifest['fixtures']:
        if row['split'] != 'held_out':
            raise ValueError('calibration data cannot count as held-out acceptance')
        raw = checked(row['input'], row['input_sha256']).read_bytes()
        a = np.frombuffer(raw, dtype='<f4')
        if a.size != 3 * 224 * 224 or not np.isfinite(a).all() or np.min(a) < 0 or np.max(a) > 1:
            raise ValueError('invalid input tensor')
        ref = np.load(checked(row['reference'], row['reference_sha256']), allow_pickle=False)
        if ref.shape != (1, 768) or not np.isfinite(ref).all() or np.linalg.norm(ref) < 1e-12:
            raise ValueError('invalid reference embedding')
        tensors.append(raw)
        references.append(ref.reshape(768).astype(np.float64))
    return manifest, worker, model, tensors, np.stack(references)


def main():
    import numpy as np
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--validate-only', action='store_true')
    args = parser.parse_args()
    manifest, worker, model, tensors, reference = load_bundle(args.bundle)
    if args.validate_only:
        report = {'status': 'bundle_integrity_pass_not_run', 'fixtures': len(tensors),
                  'model_sha256': MODEL_SHA256, 'worker_sha256': WORKER_SHA256}
    else:
        board = Path('/proc/device-tree/model').read_bytes().rstrip(b'\0').decode()
        if platform.machine() != 'aarch64' or 'RDK X5' not in board:
            raise ValueError('this runtime probe requires the actual RDK X5')
        started = time.monotonic()
        result = subprocess.run([str(worker), str(model)], input=b''.join(tensors),
                                capture_output=True, timeout=600)
        args.report.with_suffix('.worker.log').write_bytes(result.stderr)
        expected_size = 4 + len(tensors) * 768 * 4
        if result.returncode or len(result.stdout) != expected_size or result.stdout[:4] != b'X5CN':
            raise RuntimeError('worker failed or returned the wrong tensor contract; see worker log')
        actual = np.frombuffer(result.stdout[4:], dtype='<f4').reshape(len(tensors), 768).astype(np.float64)
        if not np.isfinite(actual).all() or np.any(np.linalg.norm(actual, axis=1) < 1e-12):
            raise ValueError('worker produced nonfinite or zero embeddings')
        normalized = actual / np.linalg.norm(actual, axis=1, keepdims=True)
        expected = reference / np.linalg.norm(reference, axis=1, keepdims=True)
        cosines = np.sum(normalized * expected, axis=1)
        top1 = float(np.mean(np.argmax(normalized @ expected.T, axis=1) == np.arange(len(tensors))))
        report = {'status': 'pending', 'scope': 'X5 mixed CPU/BPU tensor inference; native Photos search and end-to-end speed are separate',
                  'board': board, 'kernel': platform.release(), 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                  'model_sha256': MODEL_SHA256, 'worker_sha256': WORKER_SHA256,
                  'fixtures': len(tensors), 'cosine_min': float(cosines.min()), 'cosine_mean': float(cosines.mean()),
                  'reference_retrieval_top1': top1, 'gates': GATES,
                  'elapsed_including_worker_start_s': time.monotonic() - started,
                  'samples': [{'name': row['name'], 'cosine': float(cos)} for row, cos in zip(manifest['fixtures'], cosines)]}
        report['status'] = 'tensor_runtime_pass' if all(report[key] >= threshold for key, threshold in GATES.items()) else 'tensor_runtime_fail'
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'samples'}))
    return int(report['status'] == 'tensor_runtime_fail')


if __name__ == '__main__':
    raise SystemExit(main())
