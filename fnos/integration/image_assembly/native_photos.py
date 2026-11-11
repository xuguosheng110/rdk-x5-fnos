"""Offline, hash-verified official BPU detector + FaceID128 Photos payload.

This embeds runtime dependencies, the model and launcher. Native Photos app
installation/default activation and model-cache seeding remain separate steps.
Never derive an image from the live user's Photos database or login settings.
"""
import hashlib
import json
from pathlib import Path
import shutil

from .platform_runtime import _destination, _prepare_parent

ENTRY = Path('usr/trim/lib/ai_manager/com.trim.face_det/cp311/face_det.bin')
ENTRY_SHA = 'b7fe842b4b6dd1a4f1af84afcb5b5972086633b02ee4555aa051d522031860c4'
MODEL_SHA = 'aa3ab3230f0957bc60a9b0c3dbd21e7900a14fe7aa7b1031f3b78fb0e0ab7ce1'
WORKER_SHA = 'a05bd0d2748bde0b38738052f99a7a6b76c22406f65c0b3c4989e47eee82c48e'
DETECTOR_SHA = '5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91'
BOX_SHA = 'bae0c2ec8c1c66d2c2ff4e51877dbe2514a5aea3fb714856897c4c696d6d0729'
LANDMARK_SHA = 'bf0316c870ddd7854af368f310b03bc33c8a756d8ddae4d3aba5e2a1b564a102'
DETECTION_WORKER_SHA = '05831b336cc63b2deb526f860a73b05c8fd1f8cf2611b87f858886ede1e13cf3'
MANIFEST = Path('usr/share/fnos-x5/native-photos-manifest.json')
NAMESPACE = 'drobotics-faceid128-aa3ab3230f0957bc'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def stage_native_photos(root, assets):
    root, assets = Path(root).resolve(), Path(assets).resolve()
    if root == Path('/') or not (root / 'etc/os-release').is_file():
        raise ValueError('offline root filesystem required')
    original = _destination(root, ENTRY)
    if original.is_symlink() or digest(original) != ENTRY_SHA:
        raise ValueError('unreviewed native face launcher')
    inventory = json.loads((assets / 'assets.json').read_text())
    if inventory.get('schema') != 'fnos-photos-assets/v1':
        raise ValueError('photos assets manifest required')
    expected = {'face128_worker': WORKER_SHA, 'faceID.hbm': MODEL_SHA, 'det.onnx': DETECTOR_SHA,
                'face_detection_worker': DETECTION_WORKER_SHA,
                'face-detector.hbm': BOX_SHA, 'landmark106.hbm': LANDMARK_SHA}
    names = set()
    inputs = []
    for row in inventory['files']:
        name = row['path']
        relative = Path(name)
        if name in names or not relative.parts or str(relative) != name or relative.is_absolute() or '..' in relative.parts:
            raise ValueError('invalid or repeated asset path')
        names.add(name)
        if name not in expected and (relative.parts[0] != 'python' or len(relative.parts) < 2):
            raise ValueError('unexpected photos asset: ' + name)
        source = _destination(assets, relative)
        if source.is_symlink() or not source.is_file() or digest(source) != row['sha256']:
            raise ValueError('photos asset hash mismatch: ' + name)
        if name in expected and row['sha256'] != expected[name]:
            raise ValueError('unreviewed model or worker: ' + name)
        target = ({'face128_worker': 'usr/lib/fnos-x5/face128_worker',
                   'face_detection_worker': 'usr/lib/fnos-x5/face_detection_worker',
                   'face-detector.hbm': 'usr/share/fnos-x5/models/faceid/face-detector.hbm',
                   'landmark106.hbm': 'usr/share/fnos-x5/models/faceid/landmark106.hbm',
                   'faceID.hbm': 'usr/share/fnos-x5/models/faceid/faceID.hbm',
                   'det.onnx': 'usr/share/fnos-x5/models/faceid/det.onnx'}.get(name)
                  or 'usr/lib/fnos-x5/photos-python/' + str(relative.relative_to('python')))
        inputs.append((source, Path(target), 0o755 if name in ('face128_worker', 'face_detection_worker') else 0o644))
    if not expected.keys() <= names or not any(n.startswith('python/onnxruntime/') for n in names):
        raise ValueError('missing photos model, worker or Python dependencies')
    module_root = Path(__file__).parents[1] / 'native_photos'
    for name in ('__init__.py', 'backend.py', 'face.py', 'face_backend.py', 'face_service.py', 'native_face_entry.py', 'storage_roots.py', 'official_detector.py'):
        inputs.append((module_root / name, Path('usr/lib/fnos-x5/fnos/integration/native_photos') / name, 0o644))
    inputs.extend([(original, Path('usr/lib/fnos-x5/native-face-original.bin'), 0o755),
                   (module_root / 'model-sources.json', Path('usr/share/fnos-x5/photos-model-sources.json'), 0o644),
                   (module_root / 'native_face_entry.py', ENTRY, 0o755)])
    config = {'encoder': 'faceid128', 'embedding_namespace': NAMESPACE,
              'worker': '/usr/lib/fnos-x5/face128_worker',
              'model': '/usr/share/fnos-x5/models/faceid/faceID.hbm',
              'detector': 'official-x5',
              'detection_worker': '/usr/lib/fnos-x5/face_detection_worker',
              'box_model': '/usr/share/fnos-x5/models/faceid/face-detector.hbm',
              'landmark_model': '/usr/share/fnos-x5/models/faceid/landmark106.hbm',
              'python_root': '/usr/lib/fnos-x5', 'python_deps': '/usr/lib/fnos-x5/photos-python',
              'detector_sha256': DETECTOR_SHA, 'native_entry_sha256': ENTRY_SHA,
              'require_index_namespace': True}
    configs = {Path('etc/fnos-x5/faceid.json'): (json.dumps(config, indent=2) + '\n').encode()}
    # Validate the entire plan before the first mutation.
    for path in [*(target for _, target, _ in inputs), *configs, MANIFEST]:
        if _destination(root, path).is_symlink():
            raise ValueError('symlink at photos destination: ' + str(path))
    result = {'schema': 'fnos-native-photos/v1', 'namespace': NAMESPACE,
              'detector': 'official-x5', 'landmarks': 'official-x5-106',
              'learned_dimensions': 128, 'rpc_dimensions': 512,
              'native_entry_sha256': ENTRY_SHA, 'files': [],
              'acceptance': 'offline payload only; native app installation, first activation and clean boot require separate acceptance'}
    for source, target, mode in inputs:
        dest = root / target
        _prepare_parent(root, dest.parent)
        shutil.copyfile(source, dest)
        dest.chmod(mode)
        result['files'].append({'path': str(target), 'sha256': digest(dest), 'mode': mode})
    for target, payload in configs.items():
        dest = root / target
        _prepare_parent(root, dest.parent)
        dest.write_bytes(payload)
        dest.chmod(0o644)
        result['files'].append({'path': str(target), 'sha256': digest(dest), 'mode': 0o644})
    path = root / MANIFEST
    _prepare_parent(root, path.parent)
    path.write_text(json.dumps(result, indent=2) + '\n')
    path.chmod(0o644)
    verify_native_photos(root, result)
    return result


def verify_native_photos(root, report, *, require_root_owner=False):
    root = Path(root).resolve()
    manifest = _destination(root, MANIFEST)
    if manifest.is_symlink() or json.loads(manifest.read_text()) != report:
        raise ValueError('photos manifest mismatch')
    rows = [*report['files'], {'path': str(MANIFEST), 'sha256': digest(manifest), 'mode': 0o644}]
    for row in rows:
        path = _destination(root, Path(row['path']))
        if path.is_symlink() or digest(path) != row['sha256'] or path.stat().st_mode & 0o777 != row['mode']:
            raise ValueError('photos payload mismatch: ' + row['path'])
        if require_root_owner:
            while path != root:
                st = path.lstat()
                if st.st_uid or st.st_gid or st.st_mode & 0o022:
                    raise ValueError('unsafe photos runtime path')
                path = path.parent
    return {'verified_files': len(report['files']), 'namespace': report['namespace']}
