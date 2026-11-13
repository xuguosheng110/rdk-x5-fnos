"""Hash-pinned acceleration overlay shared by the image and the standalone .fpk.

The full image stages the reviewed workers, models, Python packages and media
adapter.  The standalone ARM64 module packs the *same* files (collected from an
assembled root) and applies them through this module.  Nothing here installs or
replaces the native Photos/Media applications, a kernel, a driver or user data.

Compatibility is deliberately a closed list.  An unknown native binary keeps
its native path, is recorded as ``unknown`` and is never patched by guesswork.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
import shutil
import tempfile
import uuid

BUNDLE_SCHEMA = 'fnos-x5-accel-bundle/v1'
STATE_SCHEMA = 'fnos-x5-accel-state/v1'
COMPAT_SCHEMA = 'fnos-x5-accel-compat/v1'
STATE = Path('var/lib/fnos-x5/accel')
BUNDLE_ROOT = Path('usr/share/fnos-x5/accel')
LOCK_NAME = 'lock'
MARGIN = 64 * 1024 * 1024
PHOTOS_ENTRY = Path('usr/trim/lib/ai_manager/com.trim.face_det/cp311/face_det.bin')
MEDIA_UNIT = Path('etc/systemd/system/mediasrv.service')
MEDIA_DROPIN = Path('etc/systemd/system/mediasrv.service.d/90-fnos-vpu.conf')
PHOTOS = 'trim.photos'
MEDIA = 'trim.media'

# Every path is either a reviewed asset that the full image already stages, or
# an overlay whose original native hash is in compat.json.  Do not add a path
# here without a real-image readback and a board test.
PHOTOS_FILES = (
    'usr/lib/fnos-x5/face128_worker',
    'usr/lib/fnos-x5/face_detection_worker',
    'usr/lib/fnos-x5/native-face-original.bin',
    'usr/share/fnos-x5/models/faceid/face-detector.hbm',
    'usr/share/fnos-x5/models/faceid/landmark106.hbm',
    'usr/share/fnos-x5/models/faceid/faceID.hbm',
    'usr/share/fnos-x5/models/faceid/det.onnx',
    'etc/fnos-x5/faceid.json',
    'usr/share/fnos-x5/photos-model-sources.json',
)
PHOTOS_MODULES = (
    'usr/lib/fnos-x5/fnos/integration/native_photos/__init__.py',
    'usr/lib/fnos-x5/fnos/integration/native_photos/backend.py',
    'usr/lib/fnos-x5/fnos/integration/native_photos/face.py',
    'usr/lib/fnos-x5/fnos/integration/native_photos/face_backend.py',
    'usr/lib/fnos-x5/fnos/integration/native_photos/face_service.py',
    'usr/lib/fnos-x5/fnos/integration/native_photos/native_face_entry.py',
    'usr/lib/fnos-x5/fnos/integration/native_photos/storage_roots.py',
    'usr/lib/fnos-x5/fnos/integration/native_photos/official_detector.py',
)
MEDIA_FILES = (
    'usr/lib/fnos-x5/native_media_launcher.py',
    'usr/lib/fnos-x5/quality_switch.js',
)
PYTHON_ROOT = 'usr/lib/fnos-x5/photos-python'
PYTHON_PACKAGES = ('numpy', 'onnxruntime', 'cv2', 'sklearn', 'faiss', 'sentencepiece')
PHOTOS_REQUIRED = {
    'usr/lib/fnos-x5/face128_worker',
    'usr/lib/fnos-x5/face_detection_worker',
    'usr/lib/fnos-x5/native-face-original.bin',
    'usr/share/fnos-x5/models/faceid/face-detector.hbm',
    'usr/share/fnos-x5/models/faceid/landmark106.hbm',
    'usr/share/fnos-x5/models/faceid/faceID.hbm',
    'usr/share/fnos-x5/models/faceid/det.onnx',
    'etc/fnos-x5/faceid.json',
    'usr/share/fnos-x5/photos-model-sources.json',
}
MEDIA_REQUIRED = {
    'usr/lib/fnos-x5/native_media_launcher.py',
    str(MEDIA_DROPIN),
}
MODE = {PHOTOS_ENTRY: 0o755, Path('usr/lib/fnos-x5/face128_worker'): 0o755,
        Path('usr/lib/fnos-x5/face_detection_worker'): 0o755,
        Path('usr/lib/fnos-x5/native-face-original.bin'): 0o755}


class AccelError(RuntimeError):
    """User-readable, retryable or blocking acceleration failure."""

    def __init__(self, code, detail=''):
        self.code = code
        self.detail = detail
        super().__init__(code + (': ' + detail if detail else ''))


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_path(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise AccelError('unsafe_path', str(relative))
    path = Path(root)
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise AccelError('symlink_path', str(relative))
    return path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _load_json(path):
    return json.loads(Path(path).read_text())


def state_paths(state_dir):
    state_dir = Path(state_dir)
    return state_dir / 'state.json', state_dir / 'transaction.json'


def load_compat(path):
    data = _load_json(path)
    if data.get('schema') != COMPAT_SCHEMA:
        raise AccelError('unreviewed_compat_schema')
    return data


def _platform_probe(root):
    root = Path(root)
    modules = []
    for name in ('bpu_framework', 'bpu_cores', 'bpu_hw_io_x5'):
        if (root / 'sys/module' / name).is_dir():
            modules.append(name)
    try:
        proc_modules = (root / 'proc/modules').read_text()
    except OSError:
        proc_modules = ''
    modules = sorted(set(modules) | {line.split()[0] for line in proc_modules.splitlines()
                                     if line.split() and line.split()[0] in
                                     ('bpu_framework', 'bpu_cores', 'bpu_hw_io_x5')})
    devices = {'/dev/bpu_core0': (root / 'dev/bpu_core0').exists()}
    paths = {}
    for name in ('usr/lib/hbbpu/libdnn.so', 'usr/lib/hbbpu/libhbrt_bayes_aarch64.so',
                 'usr/hobot/lib/firmware/libmm/chagall.bin'):
        paths['/' + name] = (root / name).exists()
    return {'machine': os.uname().machine, 'kernel': os.uname().release,
            'modules': modules, 'devices': devices, 'paths': paths}


def platform_status(root, compat, *, probe=None):
    """Read-only kernel/architecture gate.  Never loads or replaces a module."""
    info = _platform_probe(root) if probe is None else probe
    platform = compat.get('platform', {})
    result = {'machine': info.get('machine'), 'kernel': info.get('kernel'),
              'modules': sorted(info.get('modules', [])), 'blocked': [],
              'photos': 'ready', 'media': 'ready', 'checked_at': now()}
    if info.get('machine') != platform.get('machine', 'aarch64'):
        result['blocked'].append('unsupported_architecture')
        result['photos'] = result['media'] = 'blocked_platform'
    if info.get('kernel') not in platform.get('kernel_releases', []):
        result['blocked'].append('unsupported_kernel')
        result['photos'] = result['media'] = 'blocked_kernel'
    if result['blocked']:
        return result
    photos = platform.get('photos', {})
    missing_modules = [m for m in photos.get('modules', []) if m not in result['modules']]
    missing_devices = [d for d in photos.get('devices', [])
                       if not info.get('devices', {}).get(d, False)]
    missing_paths = [p for p in photos.get('paths', [])
                     if not info.get('paths', {}).get(p, False)]
    if missing_modules or missing_devices or missing_paths:
        result['photos'] = 'blocked_driver'
        result['photos_missing'] = {'modules': missing_modules, 'devices': missing_devices,
                                    'paths': missing_paths}
    media = platform.get('media', {})
    missing_paths = [p for p in media.get('paths', [])
                     if not info.get('paths', {}).get(p, False)]
    if missing_paths:
        result['media'] = 'blocked_driver'
        result['media_missing'] = {'paths': missing_paths}
    return result


def app_version(root, name):
    """Read the native manifest's version; absence is unknown, never invented."""
    for candidate in (Path(root) / 'var/apps' / name / 'manifest',
                      Path(root) / 'var/apps' / name / 'meta' / 'manifest'):
        try:
            for line in candidate.read_text().splitlines():
                match = re.match(r'\s*version\s*=\s*"?([^"\s]+)"?\s*$', line)
                if match:
                    return match.group(1)
        except OSError:
            pass
    return None


def compat_status(root, compat):
    """Classify the native app/runtime without modifying anything."""
    apps = compat.get('apps', {})
    result = {'photos': {'app': PHOTOS}, 'media': {'app': MEDIA}, 'checked_at': now()}
    photos = apps.get(PHOTOS, {})
    binary = Path(root) / 'var/apps' / PHOTOS / 'target' / 'trim-photos'
    version = app_version(root, PHOTOS)
    result['photos']['version'] = version
    if binary.is_file():
        actual = digest(binary)
        result['photos']['sha256'] = actual
        known = {row.get('binary_sha256'): row for row in photos.get('versions', [])}
        if actual in known:
            result['photos']['status'] = 'supported'
            result['photos']['known_version'] = known[actual].get('version')
            result['photos']['defaults'] = known[actual].get('defaults', 'supported')
        else:
            result['photos']['status'] = 'unknown_native'
            result['photos']['defaults'] = 'preserved_native_no_patch'
            result['photos']['recovery'] = '更新模块兼容清单后再安装；当前保留原生可用路径'
    else:
        result['photos']['status'] = 'not_installed'
    native = compat.get('native', {})
    media = apps.get(MEDIA, {})
    result['media']['version'] = app_version(root, MEDIA)
    checks = {}
    for key, label in (('media_codec', 'codec'), ('media_unit', 'unit')):
        row = native.get(key, {})
        relative = Path(row.get('path', ''))
        if not row or relative.is_absolute() or '..' in relative.parts:
            checks[label] = {'status': 'missing'}
            continue
        path = Path(root) / relative
        if not path.is_file():
            checks[label] = {'status': 'missing'}
            continue
        actual = digest(path)
        checks[label] = {'status': 'supported' if actual == row.get('sha256') else 'unknown_native',
                         'sha256': actual, 'expected': row.get('sha256')}
    result['media']['checks'] = checks
    result['media']['status'] = ('supported' if all(v['status'] == 'supported' for v in checks.values())
                                 else 'unknown_native')
    if result['media']['status'] != 'supported':
        result['media']['recovery'] = '保留原生软件路径；更新模块兼容清单后再安装适配层'
    entry = native.get('photos_entry', {})
    path = Path(root) / entry.get('path', '')
    if path.is_file():
        result['photos']['entry_sha256'] = digest(path)
    return result


def _bundle_files(bundle_dir, root=None):
    manifest = _load_json(Path(bundle_dir) / 'bundle.json')
    if manifest.get('schema') != BUNDLE_SCHEMA:
        raise AccelError('unreviewed_accel_bundle')
    files_root = manifest.get('files_root', 'files')
    if files_root == '/':
        if root is None:
            raise AccelError('in_place_bundle_requires_root')
        base = Path(root)
    else:
        base = Path(bundle_dir) / files_root
    for row in manifest.get('files', []):
        relative = row['path']
        row_root = row.get('files_root', files_root)
        if row_root == '/':
            if root is None:
                raise AccelError('in_place_bundle_requires_root')
            row_base = Path(root)
        elif Path(row_root).is_absolute():
            row_base = Path(row_root)
        else:
            row_base = Path(bundle_dir) / row_root
        source = safe_path(row_base, relative)
        if not source.is_file() or source.is_symlink():
            raise AccelError('accel_payload_missing', relative)
        if digest(source) != row['sha256']:
            raise AccelError('accel_payload_checksum', relative)
    return manifest


def verify_bundle(bundle_dir, *, root=None, require_complete=True):
    manifest = _bundle_files(bundle_dir, root)
    rows = manifest.get('files', [])
    if not require_complete:
        return {'schema': manifest['schema'], 'module': manifest.get('module'),
                'files': len(rows),
                'photos_files': sum(1 for r in rows if r.get('subsystem') == 'photos'),
                'media_files': sum(1 for r in rows if r.get('subsystem') == 'media')}
    compat = manifest.get('compat') or load_compat(Path(bundle_dir) / 'compat.json')
    if compat.get('schema') != COMPAT_SCHEMA:
        raise AccelError('incomplete_bundle', 'compat schema')
    apps = compat.get('apps', {})
    if not apps.get(PHOTOS, {}).get('versions'):
        raise AccelError('incomplete_bundle', 'trim.photos compatibility')
    if not apps.get(MEDIA, {}).get('versions'):
        raise AccelError('incomplete_bundle', 'trim.media compatibility')
    paths = {row['path'] for row in rows}
    targets = {row.get('target', row['path']) for row in rows}
    missing = sorted(PHOTOS_REQUIRED - paths)
    missing += sorted(MEDIA_REQUIRED - targets - paths)
    if str(PHOTOS_ENTRY) not in targets:
        missing.append(str(PHOTOS_ENTRY))
    if not any(path.startswith(PYTHON_ROOT + '/' + pkg + '/') for path in paths for pkg in PYTHON_PACKAGES):
        missing.append(PYTHON_ROOT + '/dependencies')
    for package in PYTHON_PACKAGES:
        if not any(path.startswith(PYTHON_ROOT + '/' + package + '/') for path in paths):
            missing.append(PYTHON_ROOT + '/' + package)
    if not any(path.startswith('usr/lib/fnos-x5/libavcodec-hobot-') and path.endswith('.so')
               for path in paths):
        missing.append('libavcodec-hobot adapter')
    if missing:
        raise AccelError('incomplete_bundle', ','.join(sorted(set(missing))))
    return {'schema': manifest['schema'], 'module': manifest.get('module'),
            'files': len(rows),
            'photos_files': sum(1 for r in rows if r.get('subsystem') == 'photos'),
            'media_files': sum(1 for r in rows if r.get('subsystem') == 'media')}


def _bundle_dir(path, boundary):
    """Create/chmod only directories inside one controlled bundle root."""
    path, boundary = Path(path), Path(boundary)
    chain = []
    current = path
    while current != boundary:
        if boundary not in current.parents:
            raise AccelError('unsafe_bundle_path', str(path))
        chain.append(current)
        current = current.parent
    for item in reversed(chain):
        if item.is_symlink():
            raise AccelError('symlink_path', str(item))
        item.mkdir(exist_ok=True, mode=0o755)
        item.chmod(0o755)


def enforce_bundle_dirs(bundle_dir):
    """Force bundle-owned directories to 0755; never touch files or links."""
    bundle_dir = Path(bundle_dir)
    if bundle_dir.is_symlink() or not bundle_dir.is_dir():
        raise AccelError('unsafe_bundle_root', str(bundle_dir))
    count = 1
    bundle_dir.chmod(0o755)
    for current, dirnames, _ in os.walk(bundle_dir, followlinks=False):
        for name in list(dirnames):
            path = Path(current) / name
            if path.is_symlink():
                dirnames.remove(name)
                continue
            path.chmod(0o755)
            count += 1
    return count


def collect_bundle(root, output, *, compat_path, module_version='0.3.0', reference_only=False,
                   require_complete=False):
    """Collect reviewed files from an assembled root into a self-contained bundle."""
    root, output = Path(root), Path(output)
    if output.is_symlink():
        raise AccelError('symlink_path', str(output))
    compat = load_compat(compat_path)
    files = []
    output.mkdir(parents=True, exist_ok=True, mode=0o755)
    output.chmod(0o755)
    if not reference_only:
        _bundle_dir(output / 'files', output)

    def add(relative, *, subsystem, kind='additive', target=None, known_native=()):
        source = Path(root) / relative
        if not source.is_file() or source.is_symlink():
            return False
        mode = MODE.get(Path(target or relative), MODE.get(Path(relative), 0o644))
        expected = digest(source)
        row = {'path': relative, 'target': target or relative, 'sha256': expected,
               'mode': mode, 'subsystem': subsystem, 'kind': kind,
               'known_native_sha256': list(known_native)}
        if not reference_only:
            dest = output / 'files' / relative
            _bundle_dir(dest.parent, output)
            shutil.copyfile(source, dest)
            dest.chmod(mode)
            if digest(dest) != expected:
                raise AccelError('collect_checksum', relative)
        elif kind == 'config' and relative == (target or relative):
            # A drop-in that has no independent source must travel with the
            # bundle so it can be recreated after an app update removes it.
            dest = output / 'files' / relative
            _bundle_dir(dest.parent, output)
            shutil.copyfile(source, dest)
            dest.chmod(mode)
            row['files_root'] = 'files'
        files.append(row)
        return True

    for relative in PHOTOS_FILES:
        add(relative, subsystem='photos')
    for relative in PHOTOS_MODULES:
        add(relative, subsystem='photos')
    if Path(root, PYTHON_ROOT).is_dir():
        for source in sorted(Path(root, PYTHON_ROOT).rglob('*')):
            if (source.is_file() and not source.is_symlink()
                    and '__pycache__' not in source.parts and source.suffix != '.pyc'
                    and not source.name.startswith('.')):
                add(str(source.relative_to(root)), subsystem='photos')
    native = compat.get('native', {})
    entry = native.get('photos_entry', {})
    add('usr/lib/fnos-x5/fnos/integration/native_photos/native_face_entry.py',
        subsystem='photos', kind='overlay',
        target=str(entry.get('path', PHOTOS_ENTRY)), known_native=(entry.get('sha256'),))
    for relative in sorted(Path(root).glob('usr/lib/fnos-x5/libavcodec-hobot-*.so')):
        add(str(relative.relative_to(root)), subsystem='media')
    for relative in MEDIA_FILES:
        add(relative, subsystem='media')
    add(str(MEDIA_DROPIN), subsystem='media', kind='config',
        target=str(MEDIA_DROPIN), known_native=())
    # The reviewed native media unit itself is never copied; only its exact hash
    # is recorded so the launcher/installer can refuse an unrecognised ABI.
    bundle = {'schema': BUNDLE_SCHEMA, 'module': {'name': 'fnos-x5-accel',
                                                   'version': module_version},
              'compat_sha256': digest(compat_path), 'compat': compat,
              'files_root': '/' if reference_only else 'files',
              'created_at': now(), 'files': files}
    shutil.copyfile(compat_path, output / 'compat.json')
    (output / 'compat.json').chmod(0o644)
    (output / 'bundle.json').write_text(json.dumps(bundle, indent=2, sort_keys=True) + '\n')
    (output / 'bundle.json').chmod(0o644)
    enforce_bundle_dirs(output)
    if require_complete:
        verify_bundle(output, root=root if reference_only else None, require_complete=True)
    return bundle


def _prior_state(state_file):
    try:
        return _load_json(state_file)
    except (OSError, ValueError):
        return {}


def _recover_transaction(root, state_dir):
    """Roll back a transaction left by a crash/power loss before any new write.

    A completed transaction (state already installed and every target hash
    matches) is kept; only the stale journal is removed.  Anything else is
    reverted to the recorded backups / removed created files.
    """
    root, state_dir = Path(root), Path(state_dir)
    state_file, journal_file = state_paths(state_dir)
    if not journal_file.is_file():
        return False
    try:
        journal = _load_json(journal_file)
    except (OSError, ValueError):
        journal = {}
    try:
        state = _load_json(state_file)
    except (OSError, ValueError):
        state = {}
    rows = journal.get('files', {})
    try:
        complete = state.get('status') in ('installed', 'upgrading') and rows and all(
            (safe_path(root, relative).is_file() and not safe_path(root, relative).is_symlink()
             and digest(safe_path(root, relative)) == row.get('sha256'))
            for relative, row in rows.items())
    except (OSError, AccelError):
        complete = False
    if complete:
        journal_file.unlink(missing_ok=True)
        return True
    for relative, row in reversed(list(rows.items())):
        target = safe_path(root, relative)
        try:
            if row.get('action') == 'create':
                if target.is_file() and not target.is_symlink():
                    target.unlink()
            elif row.get('rollback_backup') or row.get('backup'):
                source = state_dir / (row.get('rollback_backup') or row.get('backup'))
                if source.is_file():
                    _write_target(source, target, source.stat().st_mode & 0o777,
                                  uid=row.get('uid'), gid=row.get('gid'))
        except OSError:
            pass
    for relative in reversed(journal.get('created_dirs', [])):
        try:
            (root / relative).rmdir()
        except OSError:
            pass
    journal_file.unlink(missing_ok=True)
    return True


def _owner(path):
    try:
        info = Path(path).stat()
        return info.st_uid, info.st_gid
    except OSError:
        return os.geteuid(), os.getegid()


def _write_target(source, target, mode, *, uid=None, gid=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.accel-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as out, source.open('rb') as stream:
            shutil.copyfileobj(stream, out, 1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, mode)
        if uid is not None and gid is not None:
            os.chown(name, uid, gid)
        os.replace(name, target)
        _fsync_dir(target.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _rel(root, target):
    return Path(target).relative_to(Path(root))


def apply_bundle(root, bundle_dir, *, platform_probe=None, state_dir=None,
                 free_bytes=None, module_version=None, require_complete=False):
    """Apply the overlay transactionally; never patch an unknown native binary.

    Returns a report.  Raises AccelError for blocked platform, insufficient
    space, payload corruption and write failures.  A failed write restores every
    file changed in this transaction before raising.
    """
    root = Path(root).resolve()
    bundle_dir = Path(bundle_dir).resolve()
    bundle = _bundle_files(bundle_dir, root)
    compat = bundle.get('compat') or load_compat(bundle_dir / 'compat.json')
    if require_complete:
        verify_bundle(bundle_dir, root=root, require_complete=True)
    platform = platform_status(root, compat, probe=platform_probe)
    if platform['blocked']:
        raise AccelError('platform_blocked', ','.join(platform['blocked']))
    native = compat_status(root, compat)
    state_dir = Path(state_dir) if state_dir else safe_path(root, STATE)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (state_dir / LOCK_NAME).open('a')
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AccelError('install_busy')
        _recover_transaction(root, state_dir)
        state_file, journal_file = state_paths(state_dir)
        prior = _prior_state(state_file)
        prior_files = prior.get('files', {}) if prior.get('status') in ('installed', 'upgrading') else {}
        version = module_version or bundle.get('module', {}).get('version')
        plan, skipped, changed = [], [], []
        for row in bundle['files']:
            relative = row['path']
            target = safe_path(root, row['target'])
            row_root = row.get('files_root', bundle.get('files_root', 'files'))
            source = (safe_path(root, relative) if row_root == '/'
                      else safe_path(Path(row_root) if Path(row_root).is_absolute()
                                     else bundle_dir / row_root, relative))
            subsystem = row.get('subsystem', 'photos')
            eligible = platform.get(subsystem) == 'ready'
            reason = None
            if subsystem == 'media' and native['media']['status'] != 'supported':
                eligible = False
                reason = 'unknown_native_unit'
            current = target.is_file() and not target.is_symlink()
            current_sha = digest(target) if current else None
            if current_sha == row['sha256']:
                continue
            if not eligible:
                skipped.append({'path': row['target'],
                                'reason': reason or 'platform_' + platform.get(subsystem, 'blocked')})
                continue
            prior_hash = prior_files.get(row['target'], {}).get('sha256')
            if current:
                if current_sha == prior_hash:
                    pass
                elif row.get('kind') == 'overlay' and current_sha in row.get('known_native_sha256', []):
                    pass
                elif row.get('kind') == 'overlay':
                    skipped.append({'path': row['target'], 'reason': 'unknown_native'})
                    continue
                else:
                    skipped.append({'path': row['target'], 'reason': 'existing_different'})
                    continue
            elif row.get('kind') == 'overlay' and row.get('target') == str(PHOTOS_ENTRY):
                skipped.append({'path': row['target'], 'reason': 'native_missing'})
                continue
            action = 'replace' if current else 'create'
            plan.append((row, target, source, action, current_sha))
            changed.append(row)
        if not plan:
            matched = {}
            for row in bundle['files']:
                target = safe_path(root, row['target'])
                if target.is_file() and not target.is_symlink() and digest(target) == row['sha256']:
                    prior_row = prior.get('files', {}).get(row['target'], {})
                    matched[row['target']] = {
                        'sha256': row['sha256'], 'mode': int(row.get('mode', 0o644)),
                        'backup': prior_row.get('backup'),
                        'created': bool(prior_row.get('created', False)),
                        'restore': prior_row.get('restore', 'leave'),
                        'uid': prior_row.get('uid', target.stat().st_uid),
                        'gid': prior_row.get('gid', target.stat().st_gid),
                        'subsystem': row.get('subsystem', 'photos')}
            if prior.get('status') == 'installed' or matched:
                state = {**prior, 'schema': STATE_SCHEMA, 'status': 'installed',
                         'module': bundle.get('module'), 'compat': compat,
                         'platform': platform, 'skipped': skipped,
                         'files': {**prior.get('files', {}), **matched}, 'updated_at': now()}
                atomic_json(state_file, state)
                return {'status': 'already_installed', 'changed': [], 'skipped': skipped,
                        'compat': compat_status(root, compat), 'platform': platform}
            state = {**prior, 'schema': STATE_SCHEMA, 'status': 'no_compatible_native',
                     'module': bundle.get('module'), 'compat': compat,
                     'platform': platform, 'skipped': skipped, 'updated_at': now()}
            atomic_json(state_file, state)
            return {'status': 'no_compatible_native', 'changed': [], 'skipped': skipped,
                    'compat': compat_status(root, compat), 'platform': platform}
        needed = sum(target.stat().st_size for row, target, source, action, _ in plan
                     if action == 'replace')
        wanted = sum(source.stat().st_size for _, _, source, _, _ in plan)
        free = shutil.disk_usage(root).free if free_bytes is None else int(free_bytes)
        required = wanted + needed + MARGIN
        if free < required:
            raise AccelError('insufficient_space',
                             f'need={required} free={free} margin={MARGIN}')
        txid = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
        backup_root = state_dir / 'backup' / txid
        journal = {'schema': STATE_SCHEMA, 'status': 'applying', 'txid': txid,
                   'module': bundle.get('module'), 'compat': compat,
                   'platform': platform, 'started_at': now(),
                   'files': {}, 'created_dirs': []}
        atomic_json(journal_file, journal)
        applied = []
        try:
            for row, target, source, action, current_sha in plan:
                relative = _rel(root, target)
                prior_row = prior_files.get(str(relative), {})
                prior_created = bool(prior_row.get('created',
                                                   prior_row.get('backup') is None and bool(prior_row)))
                backup = prior_row.get('backup')
                owner_uid, owner_gid = _owner(target)
                rollback_backup = None
                if action == 'replace':
                    # Always snapshot the pre-upgrade file for rollback.  The
                    # uninstall backup stays separate: a file created by the
                    # module has no original to restore, but a failed upgrade
                    # must still bring its old version back.
                    snapshot = backup_root / relative
                    snapshot.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(target, snapshot)
                    shutil.copymode(target, snapshot)
                    rollback_backup = str(snapshot.relative_to(state_dir))
                    if not prior_created and not backup:
                        backup = rollback_backup
                parents = []
                parent = target.parent
                while parent != root and not parent.exists():
                    parents.append(parent)
                    parent = parent.parent
                _write_target(source, target, int(row.get('mode', 0o644)),
                              uid=owner_uid, gid=owner_gid)
                applied.append((target, action, backup, rollback_backup, owner_uid, owner_gid))
                journal['files'][str(relative)] = {'sha256': row['sha256'],
                                                   'mode': int(row.get('mode', 0o644)),
                                                   'action': action, 'backup': backup,
                                                   'rollback_backup': rollback_backup,
                                                   'uid': owner_uid, 'gid': owner_gid,
                                                   'created': action == 'create' or prior_created,
                                                   'restore': ('delete' if action == 'create' or prior_created
                                                               else ('restore' if backup else 'leave')),
                                                   'created_dirs': [str(_rel(root, p)) for p in parents]}
                journal['created_dirs'] = sorted(set(journal['created_dirs']) |
                                                 {str(_rel(root, p)) for p in parents})
                atomic_json(journal_file, journal)
            previous = prior.get('files', {})
            files = dict(previous)
            for row in bundle['files']:
                relative = row['target']
                target = safe_path(root, relative)
                if target.is_file() and not target.is_symlink() and digest(target) == row['sha256']:
                    journal_row = journal['files'].get(relative, {})
                    prior_row = previous.get(relative, {})
                    files[relative] = {'sha256': row['sha256'], 'mode': int(row.get('mode', 0o644)),
                                       'backup': journal_row.get('backup') or prior_row.get('backup'),
                                       'created': bool(journal_row.get('created',
                                                                       prior_row.get('created', False))),
                                       'restore': (journal_row.get('restore') or
                                                   prior_row.get('restore') or 'leave'),
                                       'uid': journal_row.get('uid', prior_row.get('uid')),
                                       'gid': journal_row.get('gid', prior_row.get('gid')),
                                       'subsystem': row.get('subsystem', 'photos')}
            state = {'schema': STATE_SCHEMA, 'status': 'installed', 'module': bundle.get('module'),
                     'compat': compat, 'platform': platform, 'volume': prior.get('volume'),
                     'files': files, 'skipped': skipped, 'installed_at': now(), 'updated_at': now()}
            atomic_json(state_file, state)
            journal_file.unlink(missing_ok=True)
            return {'status': 'installed', 'changed': [row['target'] for row in changed],
                    'skipped': skipped, 'compat': compat_status(root, compat),
                    'platform': platform, 'txid': txid}
        except BaseException as error:
            rollback_errors = []
            for target, action, backup, rollback_backup, owner_uid, owner_gid in reversed(applied):
                try:
                    snapshot = rollback_backup or backup
                    if snapshot:
                        source = state_dir / snapshot
                        _write_target(source, target, source.stat().st_mode & 0o777,
                                  uid=row.get('uid'), gid=row.get('gid'))
                    elif target.exists():
                        target.unlink()
                except OSError as rollback_error:
                    rollback_errors.append(str(rollback_error))
            for relative in reversed(journal.get('created_dirs', [])):
                try:
                    (root / relative).rmdir()
                except OSError:
                    pass
            base = ({**prior, 'status': prior.get('status', 'installed')}
                    if prior.get('status') == 'installed' and not rollback_errors
                    else {'schema': STATE_SCHEMA, 'files': {}})
            failed = {**base, 'status': base.get('status', 'failed'),
                      'module': bundle.get('module'), 'compat': compat, 'platform': platform,
                      'txid': txid, 'error': str(error), 'rollback_errors': rollback_errors,
                      'failed_upgrade': bool(prior.get('status') == 'installed'),
                      'updated_at': now()}
            atomic_json(state_file, failed)
            journal_file.unlink(missing_ok=True)
            raise
    finally:
        lock.close()


def installed_status(root, state_dir=None):
    root = Path(root)
    state_dir = Path(state_dir) if state_dir else safe_path(root, STATE)
    state_file, _ = state_paths(state_dir)
    try:
        state = _load_json(state_file)
    except (OSError, ValueError):
        return {'status': 'not_installed'}
    mismatches = []
    for relative, row in state.get('files', {}).items():
        target = safe_path(root, relative)
        if not target.is_file() or target.is_symlink() or digest(target) != row['sha256']:
            mismatches.append(relative)
    return {'status': state.get('status'), 'module': state.get('module'),
            'mismatches': mismatches, 'skipped': state.get('skipped', []),
            'preserved': state.get('preserved', []),
            'compat': state.get('compat'), 'updated_at': state.get('updated_at')}


def uninstall_bundle(root, *, state_dir=None):
    """Restore only files this module changed; preserve vendor/user changes."""
    root = Path(root)
    state_dir = Path(state_dir) if state_dir else safe_path(root, STATE)
    state_file, _ = state_paths(state_dir)
    if not state_file.exists() and not (state_dir / 'transaction.json').exists():
        return {'status': 'not_installed'}
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (state_dir / LOCK_NAME).open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _recover_transaction(root, state_dir)
        try:
            state = _load_json(state_file)
        except (OSError, ValueError):
            return {'status': 'not_installed'}
        if state.get('status') not in ('installed', 'upgrading', 'failed'):
            return {'status': 'not_installed'}
        restored, preserved = [], []
        for relative, row in reversed(list(state.get('files', {}).items())):
            target = safe_path(root, relative)
            if not target.exists():
                preserved.append({'path': relative, 'reason': 'already_absent'})
                continue
            if target.is_symlink() or not target.is_file() or digest(target) != row['sha256']:
                preserved.append({'path': relative, 'reason': 'changed_after_install'})
                continue
            backup = row.get('backup')
            restore = row.get('restore', 'delete' if not backup else 'restore')
            if restore == 'leave':
                preserved.append({'path': relative, 'reason': 'prestaged_leave'})
                continue
            created = bool(row.get('created', not backup))
            if created:
                target.unlink()
                restored.append(relative)
            elif backup:
                source = state_dir / backup
                if not source.is_file():
                    preserved.append({'path': relative, 'reason': 'backup_missing'})
                    continue
                mode = source.stat().st_mode & 0o777
                _write_target(source, target, mode, uid=row.get('uid'), gid=row.get('gid'))
                restored.append(relative)
            else:
                target.unlink()
                restored.append(relative)
        for relative in reversed(state.get('created_dirs', [])):
            try:
                (root / relative).rmdir()
            except OSError:
                pass
        status = 'uninstalled' if not preserved else 'uninstalled_with_preserved_changes'
        state.update(status=status, uninstalled_at=now(), restored=restored, preserved=preserved)
        atomic_json(state_file, state)
        if not preserved:
            shutil.rmtree(state_dir / 'backup', ignore_errors=True)
        return {'status': status, 'restored': restored, 'preserved': preserved}
    finally:
        lock.close()


def repair_overlay(root, *, platform_probe=None, state_dir=None):
    """Re-apply our own installed files after a known-compatible app update.

    A file the vendor replaced with an unknown hash is left untouched.  This is
    the recovery path called by the systemd runtime timer, not an installer.
    """
    root = Path(root)
    state_dir = Path(state_dir) if state_dir else safe_path(root, STATE)
    state = installed_status(root, state_dir)
    if state.get('status') != 'installed':
        return state
    bundle = safe_path(root, BUNDLE_ROOT)
    if not (bundle / 'bundle.json').is_file():
        return {**state, 'status': 'repair_required', 'reason': 'bundle_missing'}
    try:
        return apply_bundle(root, bundle, platform_probe=platform_probe, state_dir=state_dir)
    except AccelError as error:
        return {**state, 'status': 'repair_required', 'error': str(error)}
