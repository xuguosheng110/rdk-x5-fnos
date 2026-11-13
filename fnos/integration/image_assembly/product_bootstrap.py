"""Install bundled native apps after fnOS selects a mounted data volume.

This is also the standalone first-boot entry in the image. It uses the native
app installer, never formats storage and never creates a user or login secret.
Completion is durable: subsequently uninstalling an app is a user choice.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone

if __package__:
    from . import media_web, product_ai_defaults, product_accel
else:
    import media_web
    import product_ai_defaults
    import product_accel

PAYLOAD = Path('usr/share/fnos-x5/product-inputs')
STATE = Path('var/lib/fnos-x5/product-bootstrap')
CLI = Path('usr/local/bin/appcenter-cli')
CLI_SHA = '73c75a31dcea71b6b7a5055815783a95dc294f4292fd1a533c8f834ed0281c73'
APPS = ('trim.photos', 'trim.media')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_path(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise ValueError('invalid relative path')
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError('symlink in bootstrap path: ' + str(relative))
    return path


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def verified_inputs(root):
    payload = safe_path(root, PAYLOAD)
    manifest = safe_path(payload, 'inputs.json')
    data = json.loads(manifest.read_text())
    if data.get('schema') != 'fnos-product-inputs/v1':
        raise ValueError('unreviewed product input schema')
    if [app['name'] for app in data['apps']] != list(APPS):
        raise ValueError('unreviewed native app set')
    seen = set()
    for row in data['files']:
        name = row['path']
        if name in seen or Path(name).parts[0] not in ('apps', 'ai-models'):
            raise ValueError('unexpected product input path')
        seen.add(name)
        source = safe_path(payload, name)
        if not source.is_file() or source.stat().st_size != row['bytes'] or digest(source) != row['sha256']:
            raise ValueError('product input checksum mismatch: ' + name)
    for app in data['apps']:
        directory = Path(app['directory'])
        if directory.parts[0] != 'apps' or len(directory.parts) != 2:
            raise ValueError('unexpected native app directory')
        for required in ('manifest', 'app.tgz', 'cmd/main'):
            if str(directory / required) not in seen:
                raise ValueError('incomplete native app payload')
        text = safe_path(payload, directory / 'manifest').read_text()
        if not re.search(r'^appname\s*=\s*"?' + re.escape(app['name']) + r'"?\s*$', text, re.M):
            raise ValueError('native application identity mismatch')
    return data


class Bootstrap:
    def __init__(self, root=Path('/'), runner=subprocess.run, payload=None):
        self.root = Path(root)
        self.runner = runner
        if payload is not None:
            self.payload = Path(payload)
        else:
            # The native app target is intentionally a symlink to /volN.
            module_payload = self.root / 'var/apps/fnos-x5-accel/target/payload/product-inputs'
            self.payload = (module_payload if (module_payload / 'inputs.json').is_file()
                            else safe_path(self.root, PAYLOAD))
        self.state_dir = safe_path(self.root, STATE)
        self.state_file = self.state_dir / 'state.json'
        self.complete = self.state_dir / 'complete.json'
        self.state = {'schema': 'fnos-product-bootstrap/v1', 'apps': {}, 'models': {}}

    def save(self, status, **details):
        self.state.update(status=status, updated_at=datetime.now(timezone.utc).isoformat(), **details)
        atomic_json(self.state_file, self.state)
        return self.state

    def cli(self, *args, timeout=30):
        result = self.runner([str(self.root / CLI), *args], capture_output=True,
                             text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        if result.returncode:
            # Installer logs stay in the native app center; no environment or
            # arbitrary command output is copied to product status files.
            raise RuntimeError('native app command failed: ' + args[0] + ' rc=' + str(result.returncode))
        return result.stdout.strip()

    def volume(self):
        value = self.cli('default-volume')
        if value == '0':
            # Web installation records its chosen volume in native app metadata,
            # independently of the CLI default. Never guess from attached disks.
            choices = set()
            for name in APPS:
                parent = safe_path(self.root, Path('var/apps') / name)
                meta = parent / 'meta'
                if not meta.is_symlink():
                    continue
                match = re.fullmatch(r'/vol([1-9][0-9]*)/@appmeta/' + re.escape(name),
                                     os.readlink(meta))
                if not match:
                    return None
                selected = match.group(1)
                if not safe_path(self.root, Path('vol' + selected) / '@appmeta' / name).is_dir():
                    return None
                choices.add(selected)
            if len(choices) != 1:
                return None
            value = choices.pop()
        if not re.fullmatch(r'[1-9][0-9]*', value):
            return None
        volume = safe_path(self.root, 'vol' + value)
        # A plain directory is not a data volume. Never fall back to / or pick
        # another disk when the fnOS-selected volume is absent.
        # /proc/self is the kernel-provided symlink to this process.
        mounts = (self.root / 'proc/self/mountinfo').read_text().splitlines()
        target = '/vol' + value
        usable = False
        for line in mounts:
            fields = line.split()
            if len(fields) < 10 or fields[4] != target or '-' not in fields:
                continue
            separator = fields.index('-')
            if len(fields) <= separator + 3:
                continue
            mount_options = set(fields[5].split(','))
            super_options = set(fields[separator + 3].split(','))
            # ext4 can retain VFS rw while the superblock is emergency_ro.
            # Both layers must allow writes before touching model/app state.
            usable = ('rw' in mount_options and 'rw' in super_options and
                      not {'ro', 'emergency_ro'} & (mount_options | super_options))
            break
        if not usable:
            return None
        if not volume.is_dir():
            return None
        return value, volume

    def seed_models(self, volume, manifest):
        rows = [r for r in manifest['files'] if r['path'].startswith('ai-models/')]
        missing = []
        for row in rows:
            relative = Path('@sysappmeta/ai-manager/models') / Path(row['path']).relative_to('ai-models')
            dest = safe_path(volume, relative)
            if dest.exists():
                if not dest.is_file() or digest(dest) != row['sha256']:
                    raise RuntimeError('existing native model differs: ' + str(relative))
            else:
                missing.append((row, relative))
        required = sum(row['bytes'] for row, _ in missing) + 128 * 1024 * 1024
        if shutil.disk_usage(volume).free < required:
            raise RuntimeError('insufficient free space for bundled native models')
        for row, relative in missing:
            dest = safe_path(volume, relative)
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            fd, temporary = tempfile.mkstemp(prefix='.fnos-seed-', dir=dest.parent)
            try:
                with os.fdopen(fd, 'wb') as out, safe_path(self.payload, row['path']).open('rb') as source:
                    shutil.copyfileobj(source, out, 8 * 1024 * 1024)
                    out.flush()
                    os.fsync(out.fileno())
                if digest(Path(temporary)) != row['sha256']:
                    raise RuntimeError('model copy checksum mismatch')
                os.chmod(temporary, 0o644)
                # No replace: an independently installed model always wins.
                try:
                    os.link(temporary, dest)
                except FileExistsError:
                    if digest(dest) != row['sha256']:
                        raise RuntimeError('native model changed during seed')
            finally:
                os.unlink(temporary)
        self.state['models'] = {'verified_files': len(rows), 'new_files': len(missing),
                                'registration': 'verified native model cache'}

    def seed_python(self, volume, *, dependencies=None):
        """Use image-bundled ARM packages; no network pip or personal cache."""
        dependencies = (safe_path(self.root, 'usr/lib/fnos-x5/photos-python')
                        if dependencies is None else Path(dependencies))
        if not dependencies.is_dir():
            raise RuntimeError('bundled native AI Python dependencies missing')
        relative = Path('@sysappmeta/ai-manager/python/3_11_arm')
        destination = safe_path(volume, relative)
        created = False
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            temporary = Path(tempfile.mkdtemp(prefix='.fnos-python-', dir=destination.parent))
            try:
                result = self.runner([str(self.root / 'usr/bin/python3'), '-m', 'venv', '--without-pip', str(temporary)],
                                     capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
                if result.returncode:
                    raise RuntimeError('native Python environment creation failed')
                site = temporary / 'lib/python3.11/site-packages'
                if not site.is_dir():
                    raise RuntimeError('native AI runtime requires CPython 3.11')
                (site / 'fnos-x5-bundled.pth').write_text(str(dependencies) + '\n')
                # Native package detection checks the final directory. Publish
                # it only after the environment is complete and importable.
                self.verify_python(temporary / 'bin/python3')
                os.rename(temporary, destination)
                created = True
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        # Native embedded executables use PYTHONPATH without processing .pth.
        # Expose bundled packages directly in the directory supplied by ai_manager.
        site = destination / 'lib/python3.11/site-packages'
        for package in dependencies.iterdir():
            if package.name.startswith('.') or package.name == '__pycache__':
                continue
            link = site / package.name
            if link.is_symlink():
                if os.readlink(link) != str(package):
                    raise RuntimeError('existing native Python package link differs: ' + package.name)
            elif not link.exists():
                link.symlink_to(package)
        self.verify_python(destination / 'bin/python3')
        # ai_manager's embedded executables do not load site or .pth files.
        # Validate that exact import mode, not just the ordinary venv mode.
        probe = ('import numpy,onnxruntime,cv2,sklearn,faiss,sentencepiece; '
                 'print("FNOS_AI_EMBEDDED_IMPORTS_OK")')
        result = self.runner([str(self.root / 'usr/bin/python3'), '-S', '-c', probe],
                             env={**os.environ, 'PYTHONPATH': str(site), 'PYTHONNOUSERSITE': '1'},
                             capture_output=True, text=True, timeout=120,
                             stdin=subprocess.DEVNULL, cwd=str(self.root))
        if result.returncode or result.stdout.strip() != 'FNOS_AI_EMBEDDED_IMPORTS_OK':
            raise RuntimeError('native embedded AI Python import check failed')
        self.state['python'] = {'created': created, 'source': str(dependencies),
                                'verified': 'CPython3.11; numpy, onnxruntime, cv2, sklearn, faiss, sentencepiece'}

    def verify_python(self, executable):
        # The venv interpreter is intentionally a link to /usr/bin/python3.
        probe = ('import sys; assert sys.version_info[:2] == (3,11); '
                 'import numpy,onnxruntime,cv2,sklearn,faiss,sentencepiece; print("FNOS_AI_IMPORTS_OK")')
        result = self.runner([str(executable), '-I', '-c', probe], capture_output=True,
                             text=True, timeout=120, stdin=subprocess.DEVNULL)
        if result.returncode or result.stdout.strip() != 'FNOS_AI_IMPORTS_OK':
            raise RuntimeError('native AI Python import check failed')

    def configure_ai(self, index):
        self.state['ai_defaults'] = product_ai_defaults.seed_ai(index, root=self.root, runner=self.runner)

    def configure_photos(self):
        self.state['photos_defaults'] = product_ai_defaults.seed_photos(root=self.root)
        # A retry also reloads an active manager: a prior transaction may have
        # committed just before power loss, while its process still cached the
        # previous configuration. A deliberately stopped manager stays stopped.
        result = self.runner([str(self.root / 'usr/bin/systemctl'), 'try-restart', 'ai_manager.service'],
                             capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        if result.returncode:
            raise RuntimeError('native AI manager configuration reload failed')
        if self.cli('status', 'trim.photos') == 'running':
            # appcenter-cli 1.0.2 has no restart command (even errors exit 0).
            self.cli('stop', 'trim.photos', timeout=120)
            self.cli('start', 'trim.photos', timeout=120)
            if self.cli('status', 'trim.photos') != 'running':
                raise RuntimeError('native Photos configuration reload failed')

    def accel_bundle(self):
        path = self.root / product_accel.BUNDLE_ROOT
        return path if (path / 'bundle.json').is_file() else None

    def reload_policy(self, report, app):
        return report.get('restart_policy', {}).get(app, {})

    def reload_once(self, app, report, *, reason):
        """Reload a running app once.  Persist a backoff before returning.

        A failed start must not turn the five-minute timer into a crash loop.
        The user-visible recovery entry is ``--reset-reload``; a later successful
        check also clears the budget.
        """
        policy = self.reload_policy(report, app)
        if policy.get('next_retry', 0) > time.time():
            return False, 'deferred'
        if policy.get('attempts', 0) >= 3:
            return False, 'exhausted'
        # Persist before restart so a crash retries the reload, not a half state.
        atomic_json(self.state_dir / 'runtime-health.json',
                    {**report, 'status': 'reloading', 'reload_pending': True,
                     'reload_reason': reason})
        self.cli('stop', app, timeout=120)
        self.cli('start', app, timeout=120)
        if self.cli('status', app) != 'running':
            attempts = policy.get('attempts', 0) + 1
            delay = min(300 * (2 ** (attempts - 1)), 3600)
            report.setdefault('restart_policy', {})[app] = {
                'attempts': attempts, 'last_error': reason,
                'next_retry': time.time() + delay}
            return False, 'failed'
        report.setdefault('restart_policy', {})[app] = {'attempts': 0}
        return True, 'reloaded'

    def repair_accel(self, report, *, require_complete=True):
        """Verify/reapply the reviewed overlay; unknown vendor files stay native."""
        bundle = self.accel_bundle()
        if bundle is None:
            return {'status': 'not_staged'}
        state = product_accel.installed_status(self.root)
        if state.get('status') in ('uninstalled', 'uninstalled_with_preserved_changes'):
            return {'status': 'not_installed', 'preserved': state.get('preserved', [])}
        try:
            if state.get('status') not in ('installed', 'already_installed') or state.get('mismatches'):
                # First runtime check after the image (or module) is staged
                # verifies the in-place payload and records it.  A mismatch
                # reapplies only reviewed overlays; unknown vendor files stay.
                applied = product_accel.apply_bundle(self.root, bundle, require_complete=require_complete)
            else:
                applied = {'status': 'already_installed', 'changed': []}
        except product_accel.AccelError as error:
            return {'status': 'repair_required', 'error': str(error),
                    'mismatches': state.get('mismatches', [])}
        return {'status': applied.get('status'),
                'changed': applied.get('changed', []),
                'skipped': applied.get('skipped', state.get('skipped', []))}

    def repair_runtime(self):
        """Repair installed dependencies without reinstalling or starting apps.

        This is separate from initial provisioning: complete.json must not hide
        missing runtime files after an app update. Never resets AI preferences,
        never writes into user indexes/media and never starts a stopped app.
        Without the standalone module bundle this keeps the original Photos-only
        contract; with it, Media and the reviewed overlay are covered too.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        report = self.state_dir / 'runtime-health.json'
        lock_path = self.state_dir / 'lock'
        lock = lock_path.open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            return {'status': 'skipped_busy',
                    'reason': 'another product runtime repair holds the lock',
                    'health': str(report)}
        with lock:
            try:
                prior = json.loads(report.read_text()) if report.exists() else {}
                has_bundle = self.accel_bundle() is not None
                apps = list(APPS) if has_bundle else [product_accel.PHOTOS]
                installed = {}
                for app in apps:
                    value = self.cli('check', app)
                    if value not in ('Installed', 'Not Installed'):
                        raise RuntimeError('unrecognized native installation state')
                    installed[app] = value
                if all(value == 'Not Installed' for value in installed.values()):
                    result = {'status': 'not_installed'}
                else:
                    selected = self.volume()
                    if selected is None:
                        result = {'status': 'waiting_for_native_data_volume',
                                  'apps': installed}
                    else:
                        index, volume = selected
                        result = {'status': 'runtime_ready', 'volume': '/vol' + index,
                                  'apps': installed, 'compat': {},
                                  'reload_pending': False}
                        if has_bundle:
                            result['accel'] = self.repair_accel(report)
                        elif installed.get(product_accel.PHOTOS) == 'Installed':
                            result['accel'] = {'status': 'legacy_photos_only'}
                        photos = installed.get(product_accel.PHOTOS) == 'Installed'
                        media = installed.get(product_accel.MEDIA) == 'Installed'
                        if photos:
                            compat = product_accel.compat_status(self.root, product_accel.load_compat(
                                self.accel_bundle() / 'compat.json')) if has_bundle else None
                            result['compat']['photos'] = compat['photos'] if compat else 'legacy_pinned'
                            if compat and compat['photos']['status'] != 'supported':
                                result['status'] = 'compat_unknown'
                                result['photos'] = {'status': 'preserved_native_no_patch',
                                                    'reason': compat['photos']['status'],
                                                    'recovery': compat['photos'].get('recovery')}
                            else:
                                product_ai_defaults.check_binary(
                                    self.root / 'var/apps/trim.photos/target/trim-photos',
                                    product_ai_defaults.PHOTOS_SHA)
                                self.seed_python(volume)
                                manifest_path = self.payload / 'inputs.json'
                                if manifest_path.is_file():
                                    model_manifest = json.loads(manifest_path.read_text())
                                    model_manifest['files'] = [
                                        row for row in model_manifest.get('files', [])
                                        if row['path'].startswith('ai-models/')]
                                    if model_manifest['files']:
                                        self.seed_models(volume, model_manifest)
                                acl = product_ai_defaults.ensure_postgres_access(self.root, self.runner)
                                defaults = product_ai_defaults.seed_photos(root=self.root)
                                accel_changed = result.get('accel', {}).get('changed', [])
                                reload_needed = (defaults['inserted'] > 0 or acl.get('changed', False)
                                                 or prior.get('reload_pending', False)
                                                 or any('face_det.bin' in name or 'face128_worker' in name
                                                        or 'face_detection_worker' in name
                                                        for name in accel_changed))
                                result['photos'] = {'defaults': defaults,
                                                    'postgres_access': acl.get('changed', False)}
                                result['reload_pending'] = bool(reload_needed)
                                if reload_needed and self.cli('status', product_accel.PHOTOS) == 'running':
                                    ok, why = self.reload_once(product_accel.PHOTOS, prior,
                                                               reason='photos dependencies/defaults changed')
                                    result['reload'] = {'app': product_accel.PHOTOS, 'result': why}
                                    result['reload_pending'] = not ok
                                    if not ok:
                                        result['status'] = 'runtime_reload_pending'
                                elif reload_needed:
                                    result['status'] = 'runtime_reload_pending'
                        if media and has_bundle:
                            compat = product_accel.compat_status(self.root, product_accel.load_compat(
                                self.accel_bundle() / 'compat.json'))
                            result['compat']['media'] = compat['media']
                            if compat['media']['status'] != 'supported':
                                result['status'] = ('compat_unknown' if result['status'] == 'runtime_ready'
                                                    else result['status'])
                                result['media'] = {'status': 'native_fallback',
                                                   'reason': compat['media']['status'],
                                                   'recovery': compat['media'].get('recovery')}
                            else:
                                changed = result.get('accel', {}).get('changed', [])
                                result['media'] = {'status': 'hardware_overlay_verified'}
                                if (any('mediasrv' in name or 'libavcodec-hobot-' in name for name in changed)
                                        and self.cli('status', product_accel.MEDIA) == 'running'):
                                    ok, why = self.reload_once(product_accel.MEDIA, prior,
                                                               reason='media overlay changed')
                                    result['reload_media'] = {'app': product_accel.MEDIA, 'result': why}
                                    if not ok:
                                        result['status'] = 'runtime_reload_pending'
                            shim_ready = ((self.root / media_web.SHIM).is_file()
                                          and result['accel']['status'] in ('installed', 'already_installed'))
                            result['media']['web_quality_switch'] = media_web.reconcile(
                                self.root, enabled=shim_ready and compat['media']['status'] == 'supported')
                        unknown_overlay = [row for row in result.get('accel', {}).get('skipped', [])
                                           if row.get('reason') in ('unknown_native', 'unknown_native_unit')]
                        if unknown_overlay:
                            result['overlay'] = {'status': 'preserved_native_no_patch',
                                                 'skipped': unknown_overlay,
                                                 'recovery': '更新模块兼容清单后再安装；当前保留原生可用路径'}
                            if result['status'] == 'runtime_ready':
                                result['status'] = 'compat_unknown'
                        if result.get('accel', {}).get('status') in ('repair_required',
                                                                     'no_compatible_native'):
                            result['status'] = 'repair_required'
                        if not photos and not media:
                            result['status'] = 'not_installed'
                        if photos or media:
                            result.setdefault('scope', 'dependencies, reviewed overlay, postgres directory access; '
                                                       'never user config/index/media')
                result['checked_at'] = datetime.now(timezone.utc).isoformat()
                result['restart_policy'] = prior.get('restart_policy', {})
                atomic_json(report, result)
                return result
            except Exception as error:
                pending = json.loads(report.read_text()).get('reload_pending', False) if report.exists() else False
                atomic_json(report, {'status': 'repair_required', 'reload_pending': pending, 'error': str(error),
                                     'checked_at': datetime.now(timezone.utc).isoformat()})
                raise

    def run(self, *, execute=False):
        if self.complete.exists():
            return {'status': 'already_complete', 'scope': 'native app installation and AI defaults'}
        if not execute:
            manifest = verified_inputs(self.root)
            return {'status': 'verified_only', 'files': len(manifest['files']), 'apps': list(APPS)}
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.state_dir / 'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.complete.exists():
                return {'status': 'already_complete'}
            if self.state_file.exists():
                self.state = json.loads(self.state_file.read_text())
            try:
                if digest(safe_path(self.root, CLI)) != CLI_SHA:
                    raise RuntimeError('unreviewed appcenter-cli version')
                selected = self.volume()
                if selected is None:
                    return self.save('waiting_for_native_data_volume')
                index, volume = selected
                self.save('verifying_inputs', volume='/vol' + index)
                manifest = verified_inputs(self.root)
                self.save('preparing_ai_runtime')
                self.seed_python(volume)
                self.save('seeding_model_cache')
                self.seed_models(volume, manifest)
                self.save('configuring_native_ai')
                self.configure_ai(index)
                for app in manifest['apps']:
                    name = app['name']
                    installed = self.cli('check', name)
                    if installed == 'Installed':
                        if self.state['apps'].get(name) == 'installing':
                            if self.cli('status', name) != 'running':
                                self.cli('start', name, timeout=120)
                            if self.cli('status', name) != 'running':
                                raise RuntimeError('partially installed native app cannot start: ' + name)
                            self.state['apps'][name] = 'installed and running'
                        else:
                            self.state['apps'][name] = 'present; existing state retained'
                        self.save('installing_native_apps')
                        continue
                    if installed != 'Not Installed':
                        raise RuntimeError('unrecognized native installation state')
                    self.state['apps'][name] = 'installing'
                    self.save('installing_native_apps')
                    # Native installers may modify their source directory.
                    # Give them a private working copy, not the immutable image payload.
                    with tempfile.TemporaryDirectory(prefix='install-', dir=self.state_dir) as temp:
                        source = safe_path(self.root / PAYLOAD, app['directory'])
                        work = Path(temp) / 'app'
                        shutil.copytree(source, work)
                        self.cli('install-local', '-d', str(work), '-v', index, '-a', 'yes', timeout=900)
                    if self.cli('check', name) != 'Installed' or self.cli('status', name) != 'running':
                        raise RuntimeError('native app did not reach running state: ' + name)
                    self.state['apps'][name] = 'installed and running'
                    self.save('installing_native_apps')
                self.save('configuring_native_photos')
                self.configure_photos()
                self.save('complete', scope='native app installation, model cache and per-user AI defaults')
                atomic_json(self.complete, self.state)
                return self.state
            except Exception as error:
                self.save('retry_required', error=str(error))
                raise


def _module_owned(root=Path('/')):
    """True when the standalone accel module owns the runtime repair timer."""
    try:
        health = json.loads((Path(root) / 'var/lib/fnos-x5/accel/module-health.json').read_text())
    except (OSError, ValueError):
        return False
    return health.get('status') in (
        'installed', 'installed_repair_required', 'waiting_for_native_apps')


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--execute', action='store_true')
    mode.add_argument('--repair-runtime', action='store_true')
    mode.add_argument('--reset-reload', action='store_true',
                      help='clear the persisted restart backoff before the next repair')
    mode.add_argument('--module-owned-check', action='store_true',
                      help='systemd ExecCondition: skip the image timer while the module owns repair')
    args = parser.parse_args()
    if (args.execute or args.repair_runtime or args.reset_reload) and os.geteuid() != 0:
        raise SystemExit('native first-boot installation requires root')
    if args.module_owned_check:
        raise SystemExit(1 if _module_owned(Path('/')) else 0)
    bootstrap = Bootstrap()
    if args.reset_reload:
        report = bootstrap.state_dir / 'runtime-health.json'
        data = json.loads(report.read_text()) if report.exists() else {}
        data['restart_policy'] = {}
        data['reload_pending'] = False
        data['status'] = 'reload_budget_reset'
        data['checked_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(report, data)
        result = data
    elif args.repair_runtime:
        result = bootstrap.repair_runtime()
    else:
        result = bootstrap.run(execute=args.execute)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
