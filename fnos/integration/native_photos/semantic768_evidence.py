"""Strict reuse of a completed native oracle; never weakens its 34-image gate."""
import argparse
import hashlib
import importlib
import json
import math
from pathlib import Path
import sys

from .semantic768_backend import NATIVE_IMAGE_SHA256, checked_digest
from .semantic768_entry import NATIVE_ENTRY_SHA256

R3_PACKAGE_MANIFEST_SHA256 = 'a665ddb75676470673dfa42aba016be684ef457be1e266d0f1d42c0fb343f85f'


def sha(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def runtime_fingerprint():
    packages, files = {}, {}
    for name, siblings in (('numpy', ('numpy.libs',)), ('PIL', ('pillow.libs', 'Pillow.libs')),
                           ('onnxruntime', ('onnxruntime.libs',))):
        module = importlib.import_module(name)
        root = Path(module.__file__).resolve().parent
        packages[name] = {'version': module.__version__, 'path': str(root)}
        roots = [root] + [root.parent / sibling for sibling in siblings if (root.parent / sibling).is_dir()]
        for folder in roots:
            for path in sorted(folder.rglob('*')):
                if path.is_file() and (path.suffix in ('.py', '.so') or '.so.' in path.name):
                    files[str(path.resolve())] = sha(path)
    executable = Path(sys.executable).resolve()
    payload = {'schema': 'fnos-semantic768-runtime-fingerprint/v1',
               'python_version': list(sys.version_info[:3]), 'python_executable': str(executable),
               'python_sha256': sha(executable), 'packages': packages, 'files': files}
    payload['fingerprint_sha256'] = hashlib.sha256(canonical(payload)).hexdigest()
    return payload


def checked_runtime(path):
    expected = json.loads(Path(path).read_text())
    payload = {key: value for key, value in expected.items() if key != 'fingerprint_sha256'}
    if hashlib.sha256(canonical(payload)).hexdigest() != expected.get('fingerprint_sha256'):
        raise ValueError('runtime fingerprint document is inconsistent')
    current = runtime_fingerprint()
    if current != expected:
        raise ValueError('Python/NumPy/Pillow/ORT paths or source/library hashes changed since oracle')
    return expected


def attest(run_dir, package_dir, manifest, runtime_identity, output):
    from . import semantic768_backend
    run_dir, package_dir = Path(run_dir), Path(package_dir)
    package_manifest = package_dir / 'package-manifest.json'
    checked_digest(package_manifest, R3_PACKAGE_MANIFEST_SHA256)
    source = json.loads(package_manifest.read_text())
    for item in source['files']:
        file = (package_dir / item['path']).resolve()
        if not file.is_relative_to(package_dir.resolve()):
            raise ValueError('invalid oracle package path')
        checked_digest(file, item['sha256'])
    source_backend = package_dir / 'code/fnos/integration/native_photos/semantic768_backend.py'
    if sha(source_backend) != sha(semantic768_backend.__file__):
        raise ValueError('current preprocessing or normalization differs from oracle')
    state_path, report_path = run_dir / 'state.json', run_dir / 'oracle.json'
    state, report = json.loads(state_path.read_text()), json.loads(report_path.read_text())
    if state.get('status') != 'oracle_only_pass' or state.get('cleanup_complete') is not True:
        raise ValueError('source oracle run is not completed and cleaned up')
    if report.get('status') != 'native_preprocessing_pass' or report.get('gates') != {'cosine_min': .999999, 'max_abs_max': 2e-5}:
        raise ValueError('source oracle did not pass unchanged strict gates')
    fixture_manifest = json.loads(Path(manifest).read_text())
    if sha(manifest) != sha(package_dir / 'fixtures/manifest.json'):
        raise ValueError('fixture manifest differs from the immutable source package')
    if len(fixture_manifest['images']) != 28 or len(fixture_manifest.get('oracle_images', [])) != 6:
        raise ValueError('all 28 held-out and six mode fixtures required')
    rows = fixture_manifest['images'] + fixture_manifest['oracle_images']
    samples = report['samples']
    if len(samples) != 34 or [row['image'] for row in samples] != [row['path'] for row in rows]:
        raise ValueError('oracle sample list differs from complete fixture set')
    for sample, row in zip(samples, rows):
        checked_digest(row['path'], row['sha256'])
        if (sample.get('passed') is not True or not math.isfinite(sample.get('cosine', float('nan')))
                or sample['cosine'] < .999999 or not 0 <= sample.get('max_abs', float('nan')) <= 2e-5):
            raise ValueError('an oracle sample does not meet the original gate')
    services = state['services']
    if len(services) != 1 or services[0]['name'] != 'cpu' or services[0].get('last_health_response') != {'code': 0, 'data': True}:
        raise ValueError('oracle source lacks observed native CPU health identity')
    command = services[0]['command']
    model = command[command.index('--onnx_model') + 1]
    if (command[command.index('--model_format') + 1] != 'ONNX'
            or command[command.index('--edition') + 1] != 'domestic'
            or int(command[command.index('--num_threads_onnx') + 1]) != state['native_onnx_threads']):
        raise ValueError('oracle CPU model profile differs from original domestic contract')
    checked_digest(command[0], NATIVE_ENTRY_SHA256)
    checked_digest(model, NATIVE_IMAGE_SHA256)
    runtime = checked_runtime(runtime_identity)
    evidence = {'schema': 'fnos-semantic768-verified-oracle/v1', 'status': 'complete_oracle_verified_for_reuse',
                'source_package_manifest': str(package_manifest.resolve()), 'source_package_manifest_sha256': sha(package_manifest),
                'source_report': str(report_path.resolve()), 'source_report_sha256': sha(report_path),
                'source_state': str(state_path.resolve()), 'source_state_sha256': sha(state_path),
                'fixture_manifest': str(Path(manifest).resolve()), 'fixture_manifest_sha256': sha(manifest),
                'runtime_identity': str(Path(runtime_identity).resolve()), 'runtime_identity_sha256': sha(runtime_identity),
                'runtime_fingerprint_sha256': runtime['fingerprint_sha256'],
                'preprocessing_module_sha256': sha(semantic768_backend.__file__),
                'native_onnx_threads': state['native_onnx_threads'],
                'native_executable': command[0], 'native_executable_sha256': NATIVE_ENTRY_SHA256,
                'native_onnx': model, 'native_onnx_sha256': NATIVE_IMAGE_SHA256,
                'samples': samples, 'gates': report['gates']}
    output = Path(output)
    with output.open('x') as stream:
        stream.write(json.dumps(evidence, indent=2) + '\n')
    return evidence


def verify_reuse(path, manifest, native_onnx, *, expected_threads=None):
    from . import semantic768_backend
    evidence = json.loads(Path(path).read_text())
    if evidence.get('schema') != 'fnos-semantic768-verified-oracle/v1' or evidence.get('status') != 'complete_oracle_verified_for_reuse':
        raise ValueError('unsupported oracle reuse evidence')
    if expected_threads is not None and evidence['native_onnx_threads'] != expected_threads:
        raise ValueError('ONNX thread count changed since original oracle')
    for field in ('source_package_manifest', 'source_report', 'source_state', 'fixture_manifest', 'runtime_identity'):
        checked_digest(evidence[field], evidence[field + '_sha256'])
    if evidence['source_package_manifest_sha256'] != R3_PACKAGE_MANIFEST_SHA256:
        raise ValueError('unreviewed oracle source package')
    original_files = {row['path']: row['sha256'] for row in json.loads(Path(evidence['source_package_manifest']).read_text())['files']}
    if evidence['fixture_manifest_sha256'] != original_files['fixtures/manifest.json']:
        raise ValueError('evidence fixtures differ from original source package')
    if evidence['preprocessing_module_sha256'] != original_files['code/fnos/integration/native_photos/semantic768_backend.py']:
        raise ValueError('evidence preprocessing differs from original source package')
    if sha(manifest) != evidence['fixture_manifest_sha256'] or sha(semantic768_backend.__file__) != evidence['preprocessing_module_sha256']:
        raise ValueError('fixtures or preprocessing code changed since oracle')
    checked_digest(native_onnx, NATIVE_IMAGE_SHA256)
    checked_digest(evidence['native_executable'], NATIVE_ENTRY_SHA256)
    checked_runtime(evidence['runtime_identity'])
    # Re-evaluate data in the hashed source report, not untrusted cached booleans.
    report = json.loads(Path(evidence['source_report']).read_text())
    state = json.loads(Path(evidence['source_state']).read_text())
    fixtures = json.loads(Path(manifest).read_text())
    rows = fixtures['images'] + fixtures['oracle_images']
    if (state.get('status') != 'oracle_only_pass' or state.get('cleanup_complete') is not True
            or report.get('status') != 'native_preprocessing_pass' or len(rows) != 34
            or len(fixtures['images']) != 28 or len(report['samples']) != 34
            or report.get('gates') != {'cosine_min': .999999, 'max_abs_max': 2e-5}):
        raise ValueError('oracle source is incomplete or gates differ')
    for row, sample in zip(rows, report['samples']):
        checked_digest(row['path'], row['sha256'])
        if (sample['image'] != row['path'] or sample.get('passed') is not True
                or not math.isfinite(sample['cosine']) or sample['cosine'] < .999999
                or not 0 <= sample['max_abs'] <= 2e-5):
            raise ValueError('oracle sample changed or failed')
    return {'evidence_path': str(Path(path).resolve()), 'evidence_sha256': sha(path),
            'source_report_sha256': evidence['source_report_sha256'],
            'runtime_fingerprint_sha256': evidence['runtime_fingerprint_sha256'], 'samples': report['samples']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    fingerprint = sub.add_parser('fingerprint')
    fingerprint.add_argument('--report', type=Path, required=True)
    create = sub.add_parser('attest')
    for name in ('run-dir', 'package-dir', 'manifest', 'runtime-identity', 'report'):
        create.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    if args.action == 'fingerprint':
        result = runtime_fingerprint()
        with args.report.open('x') as stream:
            stream.write(json.dumps(result, indent=2) + '\n')
        print(json.dumps({'fingerprint_sha256': result['fingerprint_sha256'], 'files': len(result['files']), 'packages': result['packages']}))
    else:
        result = attest(args.run_dir, args.package_dir, args.manifest, args.runtime_identity, args.report)
        print(json.dumps({'status': result['status'], 'samples': len(result['samples']), 'report': str(args.report)}))


if __name__ == '__main__':
    main()
