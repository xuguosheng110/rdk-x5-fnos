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
from datetime import datetime, timezone

if __package__:
    from . import product_ai_defaults
else:
    import product_ai_defaults

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
    def __init__(self, root=Path('/'), runner=subprocess.run):
        self.root = Path(root)
        self.runner = runner
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
        if not re.fullmatch(r'[1-9][0-9]*', value):
            return None
        volume = safe_path(self.root, 'vol' + value)
        # A plain directory is not a data volume. Never fall back to / or pick
        # another disk when the fnOS-selected volume is absent.
        # /proc/self is the kernel-provided symlink to this process.
        mounts = (self.root / 'proc/self/mountinfo').read_text().splitlines()
        target = '/vol' + value
        if not any(len(line.split()) > 5 and line.split()[4] == target and
                   'rw' in line.split()[5].split(',') for line in mounts):
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
                with os.fdopen(fd, 'wb') as out, safe_path(self.root / PAYLOAD, row['path']).open('rb') as source:
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
        self.verify_python(destination / 'bin/python3')
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
            self.cli('restart', 'trim.photos', timeout=120)
            if self.cli('status', 'trim.photos') != 'running':
                raise RuntimeError('native Photos configuration reload failed')

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if args.execute and os.geteuid() != 0:
        raise SystemExit('native first-boot installation requires root')
    print(json.dumps(Bootstrap().run(execute=args.execute), ensure_ascii=False))


if __name__ == '__main__':
    main()
