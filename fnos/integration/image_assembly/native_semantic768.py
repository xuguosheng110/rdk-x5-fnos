"""Offline semantic768 staging, reusing the existing Photos runtime payload.

This API does not run inference or install a complete OS. Activation is a
separate, independently pinned review; absent evidence keeps vendor CPU.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from .platform_runtime import _destination, _prepare_parent
from .native_photos import MANIFEST as PHOTOS_MANIFEST, verify_native_photos
from . import semantic768_ready as ready

ENTRY = Path('usr/trim/lib/ai_manager/com.trim.img2vec/cp311/img2vec.bin')
ORIGINAL = ENTRY.with_name('img2vec.native.bin')
MANIFEST = Path('usr/share/fnos-x5/native-semantic768-manifest.json')
POLICY = Path('etc/fnos-x5/semantic768.json')
READY = Path('usr/lib/fnos-x5/semantic768_ready.py')
NATIVE_MODULES = Path('usr/lib/fnos-x5/fnos/integration/native_photos')
DORMANT_PATHS = (ORIGINAL, POLICY, READY, Path('usr/lib/fnos-x5/semantic768-worker'),
                 Path('usr/share/fnos-x5/models/semantic768'),
                 *(NATIVE_MODULES / name for name in ready.RUNTIME_HASHES))
ASSETS = {
    'native_cn_image.bin': (ready.MODEL_SHA, Path('usr/share/fnos-x5/models/semantic768/native_cn_image.bin'), 0o644),
    'semantic768-worker': (ready.WORKER_SHA, Path('usr/lib/fnos-x5/semantic768-worker'), 0o755),
}
LAUNCHER = b'''#!/usr/bin/python3
import hashlib
import os
from pathlib import Path
import runpy
import sys

entry = Path('/usr/lib/fnos-x5/semantic768_ready.py')
try:
    # run_name avoids entering main within this recovery boundary.
    loaded = runpy.run_path(str(entry), run_name='semantic768_ready')
except Exception:
    original = Path('/usr/trim/lib/ai_manager/com.trim.img2vec/cp311/img2vec.native.bin')
    if original.is_symlink() or hashlib.sha256(original.read_bytes()).hexdigest() != '3399764ba791ec678f10b96d66558146001f3dbcafe8e6acf1e281fe2461a534':
        raise RuntimeError('verified original semantic executable unavailable')
    os.execv(str(original), [str(original), *sys.argv[1:]])
raise SystemExit(loaded['main']())
'''


def atomic_payload(root, relative, *, source=None, payload=None, mode=0o644):
    target = _destination(root, relative)
    if target.is_symlink():
        raise ValueError('symlink at semantic destination')
    _prepare_parent(root, target.parent)
    fd, name = tempfile.mkstemp(prefix='.semantic768-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            if source is not None:
                with source.open('rb') as original:
                    shutil.copyfileobj(original, stream, 1024 * 1024)
            else:
                stream.write(payload)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(name, target)
        parent_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return {'path': str(relative), 'sha256': ready.digest(target), 'mode': mode}


def activation_policy(review_path=None, expected_sha256=None):
    """Missing or non-PASS review is staged disabled; a wrong pin is an error."""
    policy = {'schema': 'fnos-semantic768-policy/v1', 'enabled': False,
              'native_executable': str(ready.NATIVE), 'activation_review': None,
              'activation_review_sha256': None}
    if review_path is None:
        if expected_sha256 is not None:
            raise ValueError('review hash without review file')
        return policy
    path = Path(review_path)
    if path.is_symlink() or not expected_sha256 or ready.digest(path) != expected_sha256:
        raise ValueError('independently pinned activation review required')
    review = json.loads(path.read_text())
    policy.update(activation_review=review, activation_review_sha256=expected_sha256,
                  enabled=ready.review_passed(review))
    return policy


def require_no_dormant_payload(root):
    for relative in DORMANT_PATHS:
        # _destination deliberately rejects directories; for this absence
        # check both a directory and a file at the candidate path must fail.
        path = root / relative
        if path.exists() or path.is_symlink():
            raise ValueError('disabled semantic staging contains dormant payload: ' + str(relative))


def stage_native_semantic768(root, assets=None, *, activation_review=None, activation_review_sha256=None):
    root = Path(root).resolve()
    if root == Path('/') or not (root / 'etc/os-release').is_file():
        raise ValueError('offline root filesystem required')
    original = _destination(root, ENTRY)
    if original.is_symlink() or ready.digest(original) != ready.NATIVE_SHA:
        raise ValueError('unreviewed native semantic executable')
    policy = activation_policy(activation_review, activation_review_sha256)
    if not policy['enabled']:
        # CPU is a complete supported product path, not a half-installed BPU
        # candidate. Do not ship unused experimental models or replace its
        # executable merely to report a disabled feature.
        require_no_dormant_payload(root)
        report = {'schema': 'fnos-native-semantic768/v1', 'artifacts': ready.artifact_identity(),
                  'enabled': False, 'activation_review_sha256': policy['activation_review_sha256'],
                  'dimensions': 768, 'index_action': 'preserve-existing-native-vectors',
                  'native_executable_sha256': ready.NATIVE_SHA, 'files': [],
                  'acceptance': 'vendor CPU unchanged; BPU activation evidence incomplete'}
        atomic_payload(root, MANIFEST, payload=(json.dumps(report, indent=2) + '\n').encode())
        verify_native_semantic768(root, report)
        return report
    if assets is None:
        raise ValueError('accepted BPU staging requires pinned assets')
    assets = Path(assets).resolve()
    preserved = _destination(root, ORIGINAL)
    if preserved.exists() and (preserved.is_symlink() or ready.digest(preserved) != ready.NATIVE_SHA):
        raise ValueError('existing preserved semantic executable mismatch')
    photos_path = _destination(root, PHOTOS_MANIFEST)
    photos = json.loads(photos_path.read_text())
    verify_native_photos(root, photos)
    shared = {row['path'] for row in photos['files']}
    if (not {str(NATIVE_MODULES / name) for name in ('__init__.py', 'backend.py', 'storage_roots.py')} <= shared
            or not any(path.startswith('usr/lib/fnos-x5/photos-python/numpy/') for path in shared)
            or not any(path.startswith('usr/lib/fnos-x5/photos-python/PIL/') for path in shared)):
        raise ValueError('existing Photos shared dependencies required')
    inventory_path = _destination(assets, Path('assets.json'))
    inventory = json.loads(inventory_path.read_text())
    if inventory.get('schema') != 'fnos-semantic768-assets/v1':
        raise ValueError('semantic asset inventory required')
    rows = inventory.get('files', [])
    if len(rows) != len(ASSETS) or {row.get('path') for row in rows} != set(ASSETS):
        raise ValueError('exact semantic model and worker required')
    inputs = []
    for row in rows:
        expected, target, mode = ASSETS[row['path']]
        source = _destination(assets, Path(row['path']))
        if source.is_symlink() or row['sha256'] != expected or ready.digest(source) != expected:
            raise ValueError('unreviewed semantic model/worker')
        inputs.append((source, target, mode))
    source_root = Path(__file__).parents[1] / 'native_photos'
    for name, expected in ready.RUNTIME_HASHES.items():
        source = source_root / name
        if source.is_symlink() or ready.digest(source) != expected:
            raise ValueError('semantic runtime differs from frozen review: ' + name)
        inputs.append((source, NATIVE_MODULES / name, 0o644))
    inputs.append((Path(ready.__file__), READY, 0o644))
    # No mutations until every input, dependency, policy and destination passes.
    for path in [ORIGINAL, MANIFEST, POLICY, ENTRY, *(target for _, target, _ in inputs)]:
        if _destination(root, path).is_symlink():
            raise ValueError('symlink at semantic destination: ' + str(path))
    report = {'schema': 'fnos-native-semantic768/v1', 'artifacts': ready.artifact_identity(),
              'enabled': policy['enabled'], 'activation_review_sha256': policy['activation_review_sha256'],
              'reused_photos_manifest_sha256': ready.digest(photos_path),
              'model_ready_event': 'native-ai-manager-launch', 'dimensions': 768,
              'index_action': 'preserve-existing-native-vectors', 'files': [],
              'acceptance': 'offline payload only; final image clean boot and native product acceptance are separate'}
    report['files'].append(atomic_payload(root, ORIGINAL, source=original, mode=0o755))
    for source, target, mode in inputs:
        report['files'].append(atomic_payload(root, target, source=source, mode=mode))
    report['files'].append(atomic_payload(root, POLICY, payload=(json.dumps(policy, indent=2) + '\n').encode()))
    # Only now replace the vendor entry, atomically. Original ELF is durable.
    report['files'].append(atomic_payload(root, ENTRY, payload=LAUNCHER, mode=0o755))
    atomic_payload(root, MANIFEST, payload=(json.dumps(report, indent=2) + '\n').encode())
    verify_native_semantic768(root, report)
    return report


def verify_native_semantic768(root, report, *, require_root_owner=False):
    root = Path(root).resolve()
    path = _destination(root, MANIFEST)
    if path.is_symlink() or json.loads(path.read_text()) != report:
        raise ValueError('semantic manifest mismatch')
    if (report.get('schema') != 'fnos-native-semantic768/v1'
            or report.get('artifacts') != ready.artifact_identity()
            or report.get('index_action') != 'preserve-existing-native-vectors'):
        raise ValueError('semantic identity/compatibility mismatch')
    if report.get('enabled') is False:
        require_no_dormant_payload(root)
        entry = _destination(root, ENTRY)
        if (report.get('files') != [] or entry.is_symlink()
                or ready.digest(entry) != ready.NATIVE_SHA):
            raise ValueError('disabled semantic staging must preserve vendor CPU')
        if require_root_owner:
            for target in (entry, _destination(root, MANIFEST)):
                while target != root:
                    info = target.lstat()
                    if info.st_uid or info.st_gid or info.st_mode & 0o022:
                        raise ValueError('unsafe semantic runtime path')
                    target = target.parent
        return {'verified_files': 0, 'enabled': False, 'dimensions': 768,
                'native_cpu_preserved': True, 'candidate_payload_installed': False}
    photos_path = _destination(root, PHOTOS_MANIFEST)
    if ready.digest(photos_path) != report['reused_photos_manifest_sha256']:
        raise ValueError('shared Photos manifest changed')
    verify_native_photos(root, json.loads(photos_path.read_text()), require_root_owner=require_root_owner)
    required = {str(ORIGINAL), str(POLICY), str(ENTRY), str(READY),
                *(str(target) for _, target, _ in ASSETS.values()),
                *(str(NATIVE_MODULES / name) for name in ready.RUNTIME_HASHES)}
    if len(report['files']) != len(required) or {row['path'] for row in report['files']} != required:
        raise ValueError('semantic payload inventory incomplete')
    rows = [*report['files'], {'path': str(MANIFEST), 'sha256': ready.digest(path), 'mode': 0o644}]
    for row in rows:
        path = _destination(root, Path(row['path']))
        if path.is_symlink() or ready.digest(path) != row['sha256'] or path.stat().st_mode & 0o777 != row['mode']:
            raise ValueError('semantic payload mismatch: ' + row['path'])
        if require_root_owner:
            while path != root:
                info = path.lstat()
                if info.st_uid or info.st_gid or info.st_mode & 0o022:
                    raise ValueError('unsafe semantic runtime path')
                path = path.parent
    policy = json.loads((root / POLICY).read_text())
    if policy.get('enabled') != report['enabled'] or (policy['enabled'] and not ready.review_passed(policy['activation_review'])):
        raise ValueError('semantic activation evidence mismatch')
    if ready.digest(root / ORIGINAL) != ready.NATIVE_SHA or (root / ENTRY).read_bytes() != LAUNCHER:
        raise ValueError('semantic CPU fallback or launcher mismatch')
    return {'verified_files': len(report['files']), 'enabled': report['enabled'],
            'dimensions': 768, 'native_cpu_preserved': True}
