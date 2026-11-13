"""fnOS .fpk lifecycle entry for the standalone X5 acceleration module.

This runs from the package target during install/upgrade and from the persistent
copy under /var/lib/fnos-x5/accel/tool during uninstall.  It reuses the image's
product_accel overlay, model cache and AI-default code; it does not install the
native Photos/Media apps, change a kernel, or touch user photos/indexes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
# Repository development import; the packaged app keeps the tools at HERE.
_SOURCE_LIB = HERE.parents[2] / 'image_assembly'
if _SOURCE_LIB.is_dir() and str(_SOURCE_LIB) not in sys.path:
    sys.path.insert(0, str(_SOURCE_LIB))

import media_web
import product_accel
import product_ai_defaults
import product_bootstrap

PERSIST = Path('var/lib/fnos-x5/accel')
TOOL = PERSIST / 'tool'
HEALTH = 'module-health.json'
RUNTIME = Path('usr/lib/fnos-x5')
RUNTIME_NAMES = ('product_bootstrap.py', 'product_ai_defaults.py', 'product_accel.py', 'media_web.py')
RUNTIME_SERVICE = Path('etc/systemd/system/fnos-x5-accel-runtime.service')
RUNTIME_TIMER = Path('etc/systemd/system/fnos-x5-accel-runtime.timer')
RUNTIME_WANTS = Path('etc/systemd/system/timers.target.wants/fnos-x5-accel-runtime.timer')
RUNTIME_STATE = 'runtime-files.json'
SERVICE_TEXT = ('[Unit]\nDescription=Recheck RDK X5 acceleration after native app changes\n'
                'After=trim_app_center.service filestor_service.service\n'
                'ConditionPathExists=/usr/share/fnos-x5/accel/bundle.json\n'
                '[Service]\nType=oneshot\nUMask=0077\n'
                'ExecStart=/usr/bin/python3 /usr/lib/fnos-x5/product_bootstrap.py --repair-runtime\n'
                'TimeoutStartSec=5min\n')
TIMER_TEXT = ('[Unit]\nDescription=Recheck RDK X5 acceleration after application changes\n'
              '[Timer]\nOnBootSec=90\nOnUnitInactiveSec=5min\nAccuracySec=10\n'
              '[Install]\nWantedBy=timers.target\n')


def _write(path, value):
    product_accel.atomic_json(path, value)
    return value


def _payload(package_root):
    package_root = Path(package_root)
    return package_root / 'payload'


def _compat(package_root):
    return product_accel.load_compat(_payload(package_root) / 'accel' / 'compat.json')


def _profile(package_root):
    try:
        data = json.loads((_payload(package_root) / 'profile.json').read_text())
        return data.get('profile', 'full')
    except (OSError, ValueError):
        return 'full'


def _package_apps(root):
    installed = {}
    for name in product_accel.PHOTOS, product_accel.MEDIA:
        try:
            value = product_bootstrap.Bootstrap(root).cli('check', name)
        except Exception:
            value = 'unknown'
        installed[name] = value
    return installed


def _copy_tool(root, package_root=None):
    tool = product_accel.safe_path(Path(root), TOOL)
    tool.mkdir(parents=True, exist_ok=True, mode=0o700)
    package_root = Path(package_root) if package_root else HERE
    for name in ('module_install.py', *RUNTIME_NAMES):
        source = package_root / name
        if not source.is_file():
            source = HERE / name
        if not source.is_file():
            continue
        dest = tool / name
        shutil.copyfile(source, dest)
        dest.chmod(0o755)
    return tool


def _models_manifest(package_root):
    payload = _payload(package_root) / 'product-inputs'
    manifest = payload / 'inputs.json'
    if not manifest.is_file():
        return None, payload
    data = json.loads(manifest.read_text())
    files = [row for row in data.get('files', []) if row['path'].startswith('ai-models/')]
    if not files:
        return None, payload
    return {'schema': 'fnos-product-inputs/v1', 'files': files}, payload


def _write_bytes(path, data, mode, uid=None, gid=None):
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.accel-runtime-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        if uid is not None and gid is not None:
            os.chown(name, uid, gid)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _owner(path):
    try:
        info = Path(path).stat()
        return info.st_uid, info.st_gid
    except OSError:
        return os.geteuid(), os.getegid()


def _load_runtime_records(state_dir):
    try:
        data = json.loads((Path(state_dir) / RUNTIME_STATE).read_text())
        return {row['path']: row for row in data.get('files', [])}
    except (OSError, ValueError):
        return {}


def _snapshot_target(target, tx_dir, relative):
    target = Path(target)
    snapshot = {'existed': target.exists() or target.is_symlink()}
    if target.is_symlink():
        snapshot['link'] = os.readlink(target)
    elif target.is_file():
        path = Path(tx_dir) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target, path)
        shutil.copymode(target, path)
        snapshot['snapshot'] = str(path.relative_to(Path(tx_dir).parent.parent))
        snapshot['uid'], snapshot['gid'] = _owner(target)
    return snapshot


def _restore_target(target, snapshot, state_dir):
    target = Path(target)
    if not snapshot.get('existed'):
        if target.is_symlink() or target.is_file():
            target.unlink()
        return
    if snapshot.get('link') is not None:
        if target.is_symlink() or target.exists():
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(snapshot['link'])
        return
    source = Path(state_dir) / snapshot['snapshot']
    if source.is_file():
        product_accel._write_target(source, target, source.stat().st_mode & 0o777,
                                    uid=snapshot.get('uid'), gid=snapshot.get('gid'))


def _recover_runtime_tx(root, state_dir):
    path = Path(state_dir) / 'runtime-transaction.json'
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    canonical = _load_runtime_records(state_dir)

    def current_ok(row):
        target = Path(root) / row['path']
        try:
            if canonical[row['path']].get('symlink') is not None:
                return target.is_symlink() and os.readlink(target) == canonical[row['path']]['symlink']
            return (target.is_file() and not target.is_symlink()
                    and _digest(target) == canonical[row['path']].get('sha256'))
        except OSError:
            return False

    complete = bool(canonical) and all(
        current_ok(row) for row in data.get('files', []) if row['path'] in canonical)
    if complete and len(canonical) >= len(data.get('files', [])):
        path.unlink(missing_ok=True)
        return True
    for row in reversed(data.get('files', [])):
        try:
            _restore_target(Path(root) / row['path'], row.get('snapshot', row), state_dir)
        except OSError:
            pass
    path.unlink(missing_ok=True)
    return True


def _activate_timer(root):
    root = Path(root)
    systemctl = root / 'usr/bin/systemctl'
    if root != Path('/') or not systemctl.is_file():
        return None
    try:
        if subprocess.run([str(systemctl), 'daemon-reload'], capture_output=True, timeout=60).returncode:
            return False
        if subprocess.run([str(systemctl), 'enable', '--now', RUNTIME_TIMER.name],
                          capture_output=True, timeout=60).returncode:
            return False
        return subprocess.run([str(systemctl), 'is-active', '--quiet', RUNTIME_TIMER.name],
                              capture_output=True, timeout=60).returncode == 0
    except Exception:
        return False


def _install_runtime_files(root, package_root, state_dir):
    """Persist the repair tools/timer with per-attempt rollback.

    Canonical ownership (``runtime-files.json``) is only replaced after every
    mutation succeeds.  A reinstall preserves a previous ``create``/``replace``
    record instead of downgrading it to ``leave``.  A separate transaction
    journal snapshots each target before writing so a process death restores the
    previous module version, not the image originals.
    """
    root, package_root, state_dir = Path(root), Path(package_root), Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _recover_runtime_tx(root, state_dir)
    prior = _load_runtime_records(state_dir)
    txid = str(int(__import__('time').time())) + '-' + os.urandom(4).hex()
    tx_dir = state_dir / 'runtime-tx' / txid
    tx = {'schema': 'fnos-x5-accel-runtime-tx/v1', 'status': 'applying',
          'txid': txid, 'files': []}
    tx_path = state_dir / 'runtime-transaction.json'
    planned = []
    conflicts = []

    def save_tx():
        product_accel.atomic_json(tx_path, tx)

    def plan(relative, incoming, *, data=None, source=None, symlink=None):
        relative = Path(relative)
        target = root / relative
        prior_row = prior.get(str(relative))
        exists = target.is_symlink() or target.exists()
        if symlink is not None:
            current = os.readlink(target) if target.is_symlink() else None
        else:
            current = _digest(target) if target.is_file() and not target.is_symlink() else None
        if current == incoming:
            if prior_row:
                planned.append(prior_row)
            elif exists:
                uid, gid = _owner(target)
                planned.append({'path': str(relative), 'action': 'leave', 'backup': None,
                                'uid': uid, 'gid': gid,
                                'sha256': None if symlink is not None else incoming,
                                'symlink': symlink})
            else:
                planned.append({'path': str(relative), 'action': 'create', 'backup': None,
                                'uid': os.geteuid(), 'gid': os.getegid(),
                                'sha256': None if symlink is not None else incoming,
                                'symlink': symlink})
            return
        if exists and prior_row and prior_row.get('sha256') and current != prior_row.get('sha256'):
            conflicts.append(str(relative))
            planned.append({**prior_row, 'conflict': current})
            return
        if prior_row and prior_row.get('action') in ('create', 'replace'):
            action = prior_row['action']
            backup = prior_row.get('backup')
            backup_kind = prior_row.get('backup_kind')
            uid, gid = prior_row.get('uid'), prior_row.get('gid')
        else:
            action = 'replace' if exists else 'create'
            backup = None
            backup_kind = None
            uid, gid = _owner(target)
        snapshot = _snapshot_target(target, tx_dir, relative) if exists else {'existed': False}
        if action == 'replace' and not backup:
            backup = snapshot.get('snapshot')
            backup_kind = 'link' if snapshot.get('link') is not None else 'file'
            if backup is None:
                backup = snapshot.get('link')
        tx['files'].append({'path': str(relative), 'snapshot': snapshot})
        save_tx()
        if symlink is not None:
            if target.is_symlink() or target.exists():
                target.unlink()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(symlink)
        elif data is not None:
            _write_bytes(target, data, 0o644, uid, gid)
        else:
            product_accel._write_target(source, target, 0o644, uid=uid, gid=gid)
        planned.append({'path': str(relative), 'action': action, 'backup': backup,
                        'backup_kind': backup_kind, 'uid': uid, 'gid': gid,
                        'sha256': None if symlink is not None else incoming,
                        'symlink': symlink})
        if exists and prior_row and prior_row.get('action') == 'leave':
            planned[-1]['uid'], planned[-1]['gid'] = prior_row.get('uid'), prior_row.get('gid')

    try:
        for name in RUNTIME_NAMES:
            source = package_root / name
            if not source.is_file():
                raise RuntimeError('packaged runtime file missing: ' + name)
            plan(Path(RUNTIME) / name, _digest(source), source=source)
        plan(RUNTIME_SERVICE, hashlib.sha256(SERVICE_TEXT.encode()).hexdigest(),
             data=SERVICE_TEXT.encode())
        plan(RUNTIME_TIMER, hashlib.sha256(TIMER_TEXT.encode()).hexdigest(),
             data=TIMER_TEXT.encode())
        wants = root / RUNTIME_WANTS
        plan(RUNTIME_WANTS, '../' + RUNTIME_TIMER.name, symlink='../' + RUNTIME_TIMER.name)
        product_accel.atomic_json(state_dir / RUNTIME_STATE,
                                  {'schema': 'fnos-x5-accel-runtime/v1', 'files': planned})
        tx_path.unlink(missing_ok=True)
    except Exception:
        _recover_runtime_tx(root, state_dir)
        raise
    activation = _activate_timer(root)
    status = 'installed' if activation is not False else 'activation_failed'
    return {'status': 'conflict' if conflicts else status, 'files': len(planned),
            'timer_active': activation, 'conflicts': conflicts}


def _uninstall_runtime_files(root, state_dir):
    root, state_dir = Path(root), Path(state_dir)
    _recover_runtime_tx(root, state_dir)
    try:
        data = json.loads((state_dir / RUNTIME_STATE).read_text())
    except (OSError, ValueError):
        return {'status': 'not_installed'}
    preserved = []
    for row in reversed(data.get('files', [])):
        target = root / row['path']
        action = row.get('action')
        if action == 'leave':
            continue
        if row.get('symlink') is not None:
            current = os.readlink(target) if target.is_symlink() else None
            expected = row['symlink']
        else:
            current = _digest(target) if target.is_file() and not target.is_symlink() else None
            expected = row.get('sha256')
        if current != expected:
            preserved.append({'path': row['path'], 'reason': 'changed_after_install'})
            continue
        try:
            if action == 'create':
                if target.is_symlink() or target.is_file():
                    target.unlink()
                continue
            if action == 'replace' and row.get('backup'):
                if row.get('backup_kind') == 'link':
                    if target.is_symlink() or target.exists():
                        target.unlink()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(row['backup'])
                else:
                    backup = state_dir / row['backup']
                    if not backup.is_file():
                        preserved.append({'path': row['path'], 'reason': 'backup_missing'})
                        continue
                    product_accel._write_target(backup, target,
                                                backup.stat().st_mode & 0o777,
                                                uid=row.get('uid'), gid=row.get('gid'))
        except OSError as error:
            preserved.append({'path': row['path'], 'reason': str(error)})
    if not preserved:
        (state_dir / RUNTIME_STATE).unlink(missing_ok=True)
    return {'status': 'uninstalled' if not preserved else 'uninstalled_with_preserved_changes',
            'preserved': preserved}


def _pointer_tx_path(state_dir):
    return Path(state_dir) / 'pointer-transaction.json'


def _recover_pointer_tx(root, state_dir):
    path = _pointer_tx_path(state_dir)
    if not path.is_file():
        return False
    try:
        tx = json.loads(path.read_text())
    except (OSError, ValueError):
        tx = {}
    dest = Path(root) / product_accel.BUNDLE_ROOT
    backup_value = tx.get('backup')
    backup = state_dir / backup_value if backup_value else None
    if backup is not None and backup.is_dir():
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(backup, dest)
    elif tx.get('created'):
        shutil.rmtree(dest, ignore_errors=True)
    path.unlink(missing_ok=True)
    return True


def _stage_bundle_pointer(root, package_root, *, require_complete=True):
    """Expose the package's reviewed bundle to the systemd repair timer.

    The large payload is referenced at its installed package path instead of
    being copied twice.  A full-image in-place bundle (no module marker) is
    never modified.  A pointer previously created by this module is updated
    atomically on upgrade so the timer reads the new compat.json.
    """
    dest = Path(root) / product_accel.BUNDLE_ROOT
    source = _payload(package_root) / 'accel'
    product_accel.verify_bundle(source, require_complete=require_complete)
    state_dir = Path(root) / PERSIST
    _recover_pointer_tx(root, state_dir)
    if (dest / 'bundle.json').exists() and not (dest / 'module-pointer.json').is_file():
        return {'status': 'image_owned'}
    bundle = json.loads((source / 'bundle.json').read_text())
    files = (source / 'files').absolute()
    bundle['files_root'] = str(files)
    payload = json.dumps(bundle, indent=2, sort_keys=True) + '\n'
    compat = (source / 'compat.json').read_bytes()
    pointer = (json.dumps({'schema': 'fnos-x5-accel-pointer/v1',
                           'appname': 'fnos-x5-accel'}) + '\n').encode()
    if (dest / 'bundle.json').is_file():
        old_bundle = (dest / 'bundle.json').read_bytes()
        old_compat = (dest / 'compat.json').read_bytes() if (dest / 'compat.json').is_file() else b''
        if old_bundle == payload.encode() and old_compat == compat:
            return {'status': 'existing'}
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = state_dir / 'pointer-original' / (str(int(__import__('time').time())) + '-' + os.urandom(3).hex())
    created = not dest.exists()
    if not created:
        shutil.copytree(dest, backup)
    product_accel.atomic_json(_pointer_tx_path(state_dir), {
        'schema': 'fnos-x5-accel-pointer-tx/v1', 'status': 'applying',
        'backup': str(backup.relative_to(state_dir)) if not created else None,
        'created': created})
    try:
        dest.mkdir(parents=True, exist_ok=True, mode=0o755)
        (dest / 'files').mkdir(parents=True, exist_ok=True, mode=0o755)
        for row in bundle.get('files', []):
            if row.get('files_root') == 'files':
                src = source / 'files' / row['path']
                target = dest / 'files' / row['path']
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                shutil.copyfile(src, target)
                target.chmod(int(row.get('mode', 0o644)))
        _write_bytes(dest / 'compat.json', compat, 0o644)
        _write_bytes(dest / 'bundle.json', payload.encode(), 0o644)
        _write_bytes(dest / 'module-pointer.json', pointer, 0o644)
    except Exception:
        _recover_pointer_tx(root, state_dir)
        raise
    _pointer_tx_path(state_dir).unlink(missing_ok=True)
    if backup.is_dir():
        shutil.rmtree(backup, ignore_errors=True)
    return {'status': 'created', 'files_root': str(files)}


def _remove_bundle_pointer(root):
    dest = Path(root) / product_accel.BUNDLE_ROOT
    _recover_pointer_tx(Path(root), Path(root) / PERSIST)
    if not (dest / 'module-pointer.json').is_file():
        return False
    for name in ('bundle.json', 'compat.json', 'module-pointer.json'):
        (dest / name).unlink(missing_ok=True)
    shutil.rmtree(dest / 'files', ignore_errors=True)
    return True


def _snapshot_install(root, state_dir):
    """Snapshot the current repair chain before an upgrade/install attempt."""
    root, state_dir = Path(root), Path(state_dir)
    records = _load_runtime_records(state_dir)
    pointer = Path(root) / product_accel.BUNDLE_ROOT
    pointer_owned = (pointer / 'module-pointer.json').is_file()
    if not records and not pointer_owned:
        return None
    backup = state_dir / 'apply-rollback' / (str(int(__import__('time').time())) + '-' + os.urandom(3).hex())
    backup.mkdir(parents=True, exist_ok=True)
    data = {'schema': 'fnos-x5-accel-apply-rollback/v1', 'backup': str(backup.relative_to(state_dir)),
            'files': [], 'pointer': None, 'pointer_owned': pointer_owned}
    for relative, row in records.items():
        target = Path(root) / relative
        snapshot = {'path': relative, 'existed': target.exists() or target.is_symlink()}
        if target.is_symlink():
            snapshot['link'] = os.readlink(target)
        elif target.is_file():
            path = backup / 'files' / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, path)
            shutil.copymode(target, path)
            snapshot['snapshot'] = str(path.relative_to(state_dir))
            snapshot['uid'], snapshot['gid'] = _owner(target)
        data['files'].append(snapshot)
    if pointer_owned:
        dest = backup / 'pointer'
        shutil.copytree(pointer, dest)
        data['pointer'] = str(dest.relative_to(state_dir))
    product_accel.atomic_json(state_dir / 'apply-rollback.json', data)
    return data


def _restore_install(root, state_dir, data):
    root, state_dir = Path(root), Path(state_dir)
    for row in reversed(data.get('files', [])):
        target = Path(root) / row['path']
        if not row.get('existed'):
            if target.is_symlink() or target.is_file():
                target.unlink()
        elif row.get('link') is not None:
            if target.is_symlink() or target.exists():
                target.unlink()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(row['link'])
        else:
            source = state_dir / row['snapshot']
            if source.is_file():
                product_accel._write_target(source, target, source.stat().st_mode & 0o777,
                                            uid=row.get('uid'), gid=row.get('gid'))
    pointer = Path(root) / product_accel.BUNDLE_ROOT
    if data.get('pointer'):
        backup = state_dir / data['pointer']
        if backup.is_dir():
            shutil.rmtree(pointer, ignore_errors=True)
            shutil.copytree(backup, pointer)
    elif not data.get('pointer_owned') and (pointer / 'module-pointer.json').is_file():
        shutil.rmtree(pointer, ignore_errors=True)


def _recover_apply_rollback(root, state_dir):
    path = Path(state_dir) / 'apply-rollback.json'
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    _restore_install(root, state_dir, data)
    path.unlink(missing_ok=True)
    value = data.get('backup')
    backup = Path(state_dir) / value if value else None
    if backup is not None and backup.is_dir():
        shutil.rmtree(backup, ignore_errors=True)
    return True


def _clear_apply_rollback(state_dir):
    path = Path(state_dir) / 'apply-rollback.json'
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    path.unlink(missing_ok=True)
    value = data.get('backup')
    backup = Path(state_dir) / value if value else None
    if backup is not None and backup.is_dir():
        shutil.rmtree(backup, ignore_errors=True)


def _reload_if_running(root, apps, state):
    """At most one reload per app; failures are recorded for the timer."""
    bootstrap = product_bootstrap.Bootstrap(root)
    reasons = []
    for name in apps:
        if bootstrap.cli('status', name) != 'running':
            state.setdefault('stopped', []).append(name)
            continue
        try:
            bootstrap.cli('stop', name, timeout=120)
            bootstrap.cli('start', name, timeout=120)
            if bootstrap.cli('status', name) != 'running':
                state.setdefault('reload_failed', []).append(name)
                reasons.append(name + ': start failed; timer backoff will retry')
            else:
                state.setdefault('reloaded', []).append(name)
        except Exception as error:
            state.setdefault('reload_failed', []).append(name)
            reasons.append(name + ': ' + str(error))
    return reasons


def install(root, package_root, *, upgrade=False, require_complete=True):
    root = Path(root)
    package_root = Path(package_root)
    state_dir = product_accel.safe_path(root, PERSIST)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _recover_apply_rollback(root, state_dir)
    compat = _compat(package_root)
    profile = _profile(package_root)
    platform = product_accel.platform_status(root, compat)
    if platform['blocked']:
        result = {'status': 'blocked_platform', 'platform': platform,
                  'recovery': '仅支持已验收的 RDK X5 aarch64 / 6.18.0；未替换内核或驱动'}
        _write(state_dir / HEALTH, result)
        return result, 2
    try:
        product_accel.verify_bundle(_payload(package_root) / 'accel',
                                    require_complete=require_complete)
    except product_accel.AccelError as error:
        result = {'status': 'blocked', 'code': error.code, 'detail': error.detail,
                  'platform': platform, 'apps': _package_apps(root),
                  'recovery': '载荷不完整或未通过校验；未写入任何运行时文件'}
        _write(state_dir / HEALTH, result)
        return result, 2
    app_state = _package_apps(root)
    tool = _copy_tool(root, package_root)
    _write(tool / 'compat.json', compat)
    runtime = None
    pointer = None
    snapshot = _snapshot_install(root, state_dir)
    try:
        runtime = _install_runtime_files(root, package_root, state_dir)
        pointer = _stage_bundle_pointer(root, package_root, require_complete=require_complete)
    except Exception as error:
        if snapshot:
            _restore_install(root, state_dir, snapshot)
        else:
            _uninstall_runtime_files(root, state_dir)
            _remove_bundle_pointer(root)
            shutil.rmtree(tool, ignore_errors=True)
        _clear_apply_rollback(state_dir)
        result = {'status': 'blocked', 'code': 'runtime_setup_failed', 'detail': str(error),
                  'platform': platform, 'apps': app_state, 'runtime': runtime,
                  'recovery': '持久化修复资源失败；已恢复本次操作前状态，请检查系统分区空间/权限后重试'}
        _write(state_dir / HEALTH, result)
        return result, 2
    if runtime.get('status') == 'conflict':
        if snapshot:
            _restore_install(root, state_dir, snapshot)
        else:
            _uninstall_runtime_files(root, state_dir)
        _clear_apply_rollback(state_dir)
        result = {'status': 'blocked', 'code': 'runtime_conflict',
                  'detail': ','.join(runtime.get('conflicts', [])),
                  'profile': profile, 'platform': platform, 'apps': app_state, 'runtime': runtime,
                  'recovery': '已有运行时文件被未知版本修改，未覆盖；已恢复本次操作前状态'}
        _write(state_dir / HEALTH, result)
        return result, 2
    if all(value == 'Not Installed' for value in app_state.values()):
        result = {'status': ('waiting_for_native_apps'
                             if runtime.get('status') == 'installed'
                             else 'installed_repair_required'),
                  'apps': app_state, 'platform': platform, 'runtime': runtime, 'bundle': pointer,
                  'recovery': ('持久化修复链路已安装；安装相册或影视后定时器会自动继续，无需重装本模块'
                               if runtime.get('status') == 'installed' else
                               '运行时文件已安装但定时器未激活；执行 systemctl enable --now '
                               'fnos-x5-accel-runtime.timer 或重启后复查')}
        _clear_apply_rollback(state_dir)
        _write(state_dir / HEALTH, result)
        return result, 0
    try:
        applied = product_accel.apply_bundle(root, _payload(package_root) / 'accel',
                                           require_complete=require_complete)
    except product_accel.AccelError as error:
        if snapshot:
            _restore_install(root, state_dir, snapshot)
        else:
            _uninstall_runtime_files(root, state_dir)
            _remove_bundle_pointer(root)
            shutil.rmtree(tool, ignore_errors=True)
        _clear_apply_rollback(state_dir)
        result = {'status': 'blocked', 'code': error.code, 'detail': error.detail,
                  'platform': platform, 'apps': app_state,
                  'recovery': '安装事务已回滚到操作前状态；查看 /var/lib/fnos-x5/accel/state.json'}
        _write(state_dir / HEALTH, result)
        return result, 2
    result = {'status': 'installed', 'upgrade': bool(upgrade), 'profile': profile,
              'platform': platform, 'apps': app_state,
              'accel': {k: applied.get(k) for k in
                        ('status', 'changed', 'skipped', 'compat')},
              'state': str(state_dir / 'state.json')}
    unknown = [row for row in applied.get('skipped', [])
               if row.get('reason') in ('unknown_native', 'unknown_native_unit')]
    if unknown:
        result['overlay'] = {'status': 'preserved_native_no_patch', 'skipped': unknown,
                             'recovery': '更新模块兼容清单后再安装；当前保留原生可用路径'}
    result['runtime'] = runtime
    result['bundle'] = pointer
    if runtime.get('status') == 'activation_failed':
        result['status'] = 'installed_repair_required'
        result['recovery'] = '运行时文件已安装，但系统定时器未激活；执行 systemctl enable --now fnos-x5-accel-runtime.timer 或重启后复查'
    # A module is useful even before the data volume exists; the runtime timer
    # seeds the volume-dependent pieces later.  Never guess a volume.
    selected = product_bootstrap.Bootstrap(root).volume()
    if selected is None:
        result['status'] = 'installed_waiting_for_native_data_volume'
        result['recovery'] = '等待飞牛挂载数据卷；运行时定时器会自动继续，不会改用系统盘'
        _clear_apply_rollback(state_dir)
        _write(state_dir / HEALTH, result)
        return result, 0
    index, volume = selected
    result['volume'] = '/vol' + index
    model_manifest, model_payload = _models_manifest(package_root)
    bootstrap = product_bootstrap.Bootstrap(root, payload=model_payload)
    try:
        if model_manifest is not None and app_state[product_accel.PHOTOS] == 'Installed':
            bootstrap.seed_models(volume, model_manifest)
        compat_state = product_accel.compat_status(root, compat)
        result['compat'] = compat_state
        if app_state[product_accel.PHOTOS] == 'Installed':
            if compat_state['photos']['status'] == 'supported':
                product_ai_defaults.check_binary(
                    root / 'var/apps/trim.photos/target/trim-photos',
                    product_ai_defaults.PHOTOS_SHA)
                bootstrap.seed_python(volume)
                ai_manager = compat.get('native', {}).get('ai_manager', {})
                if ai_manager and (root / ai_manager['path']).is_file():
                    product_ai_defaults.check_binary(root / ai_manager['path'], ai_manager['sha256'])
                    result['ai_defaults'] = product_ai_defaults.seed_ai(
                        index, root=root, runner=bootstrap.runner)
                result['photos'] = {
                    'acl': product_ai_defaults.ensure_postgres_access(root, bootstrap.runner)}
                result['photos']['defaults'] = product_ai_defaults.seed_photos(root=root)
            else:
                result['photos'] = {'status': 'preserved_native_no_patch',
                                    'reason': compat_state['photos']['status']}
        if app_state[product_accel.MEDIA] == 'Installed':
            result['media'] = {'status': ('hardware_overlay_installed'
                                          if compat_state['media']['status'] == 'supported'
                                          else 'native_fallback'),
                               'reason': compat_state['media']['status']}
            result['media']['web_quality_switch'] = media_web.reconcile(
                root, enabled=(root / media_web.SHIM).is_file()
                and compat_state['media']['status'] == 'supported')
        running = [name for name in (product_accel.PHOTOS, product_accel.MEDIA)
                   if app_state[name] == 'Installed']
        failures = _reload_if_running(root, running, result)
        if failures:
            result['status'] = 'installed_reload_pending'
            result['recovery'] = '; '.join(failures)
    except Exception as error:
        result['status'] = 'installed_repair_required'
        result['error'] = str(error)
        result['recovery'] = '文件已安装；运行时定时器将重试依赖/配置，不会循环重启应用'
    _clear_apply_rollback(state_dir)
    _write(state_dir / HEALTH, result)
    return result, 0


def uninstall(root):
    root = Path(root)
    state_dir = product_accel.safe_path(root, PERSIST)
    runtime = _uninstall_runtime_files(root, state_dir)
    web = media_web.reconcile(root, enabled=False)
    result = product_accel.uninstall_bundle(root)
    if result['status'] == 'uninstalled':
        _remove_bundle_pointer(root)
    result['runtime'] = runtime
    result['media_web'] = web
    _write(state_dir / HEALTH, {'status': result['status'], **result,
                                'recovery': '仅恢复本模块修改的系统文件；用户照片、索引、配置与应用数据未改动'})
    tool = state_dir / 'tool'
    if result['status'] == 'uninstalled':
        shutil.rmtree(tool, ignore_errors=True)
    return result, 0


def _timer_active(root):
    root = Path(root)
    systemctl = root / 'usr/bin/systemctl'
    if root != Path('/') or not systemctl.is_file():
        return None
    try:
        return subprocess.run([str(systemctl), 'is-active', '--quiet', RUNTIME_TIMER.name],
                              capture_output=True, timeout=30).returncode == 0
    except Exception:
        return False


def status(root):
    root = Path(root)
    state_dir = product_accel.safe_path(root, PERSIST)
    accel = product_accel.installed_status(root)
    health = {}
    try:
        health = json.loads((state_dir / HEALTH).read_text())
    except (OSError, ValueError):
        pass
    checks = {
        'bundle': (root / product_accel.BUNDLE_ROOT / 'bundle.json').is_file(),
        'runtime_bootstrap': (root / RUNTIME / 'product_bootstrap.py').is_file(),
        'runtime_accel': (root / RUNTIME / 'product_accel.py').is_file(),
        'timer_unit': (root / RUNTIME_TIMER).is_file(),
        'timer_enabled': (root / RUNTIME_WANTS).is_symlink(),
    }
    active = _timer_active(root)
    checks['timer_active'] = active
    result = {**accel, 'health': health, 'checks': checks,
              'recovery': '重新安装模块或运行 product_bootstrap --repair-runtime'}
    if accel.get('status') == 'not_installed' and health.get('status') in (
            'waiting_for_native_apps', 'installed_waiting_for_native_data_volume'):
        result['status'] = health['status']
    if accel.get('status') == 'installed' and (
            not all(value for key, value in checks.items() if key != 'timer_active')
            or active is False):
        result['status'] = 'installed_repair_required'
        result['recovery'] = '持久化修复链路不完整：重新安装模块；' + result['recovery']
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--install', action='store_true')
    action.add_argument('--upgrade', action='store_true')
    action.add_argument('--uninstall', action='store_true')
    action.add_argument('--status', action='store_true')
    action.add_argument('--preflight', action='store_true')
    parser.add_argument('--root', type=Path, default=Path('/'))
    parser.add_argument('--package-root', type=Path,
                        default=Path(os.environ.get('TRIM_APPDEST', HERE)))
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.status:
        print(json.dumps(status(root), ensure_ascii=False))
        return 0
    if root == Path('/') and os.geteuid() != 0:
        print('加速模块安装需要 root；请通过飞牛应用中心安装。', file=sys.stderr)
        return 1
    if args.preflight:
        compat = _compat(args.package_root)
        print(json.dumps(product_accel.platform_status(root, compat), ensure_ascii=False))
        return 0
    if args.uninstall:
        result, code = uninstall(root)
    else:
        result, code = install(root, args.package_root, upgrade=args.upgrade)
    print(json.dumps(result, ensure_ascii=False))
    if result['status'] in ('blocked', 'blocked_platform'):
        message = result.get('detail') or result.get('recovery') or result['status']
        print('加速模块未安装：' + message, file=sys.stderr)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
