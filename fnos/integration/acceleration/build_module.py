"""Build the standalone ARM64 fnOS .fpk from the same payload as the image.

No board binary is compiled here.  ``--source-root`` is an already assembled
offline root; ``--format-test`` builds a payload-free package only to exercise
the official fnpack container/lifecycle format.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import zlib

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
MODULE = HERE / 'module'
DEFAULT_COMPAT = MODULE / 'compat.json'
DEFAULT_MODEL_INVENTORY = MODULE / 'model-inventory.json'
DEFAULT_RUNTIME_MODEL_INVENTORY = MODULE / 'runtime-model-inventory.json'
APPNAME = 'fnos-x5-accel'
PUBLIC_VERSION = '0.3.0'
BUILD_VERSION = '0.3.4'
VERSION = BUILD_VERSION
OS_VERSION = '1.2.0701'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _png(size, rgb=(8, 145, 178)):
    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data +
                struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))
    raw = b''.join(b'\x00' + bytes(rgb) * size for _ in range(size))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def _manifest(version):
    return '\n'.join((
        f'appname={APPNAME}', f'version={version}',
        'display_name=RDK X5 硬件加速', 'desc=相册 BPU 人脸与影视 VPU 适配；不安装应用、不修改内核和用户媒体。',
        'platform=arm', 'source=thirdparty', 'maintainer=RDK X5 fnOS community',
        f'os_min_version={OS_VERSION}', f'os_max_version={OS_VERSION}',
        'ctl_stop=false', 'checkport=false',
        'changelog=修复影视片尾切换清晰度时的播放中断，保留应用版本检查及安装回滚。',
    )) + '\n'


def _write_text(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def _write_bytes(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def _copy_models(product_inputs, dest, inventory_path=None, profile='full'):
    source = Path(product_inputs)
    manifest = json.loads((source / 'inputs.json').read_text())
    if manifest.get('schema') != 'fnos-product-inputs/v1':
        raise SystemExit('product inputs must be the staged fnos-product-inputs/v1 manifest')
    from fnos.integration.image_assembly import runtime_profile
    path = Path(inventory_path) if inventory_path else None
    if path is None:
        path = (DEFAULT_RUNTIME_MODEL_INVENTORY if profile == 'runtime'
                else DEFAULT_MODEL_INVENTORY)
    try:
        rows = runtime_profile.model_rows(manifest.get('files', []), profile, path)
    except ValueError as exc:
        raise SystemExit(str(exc))
    families = {Path(row['path']).parts[1] for row in rows}
    required_families = {'face-model-1.0.1', 'img2vec-medium-model-1.0.0',
                         'txt2vec-medium-model-1.0.0'}
    if not required_families <= families:
        raise SystemExit('model families incomplete: ' + ','.join(sorted(families)))
    written = []
    for row in rows:
        src = source / row['path']
        if (not src.is_file() or src.is_symlink()
                or src.stat().st_size != row['bytes'] or digest(src) != row['sha256']):
            raise SystemExit('model input differs: ' + row['path'])
        target = dest / row['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        target.chmod(0o644)
        written.append(row)
    _write_text(dest / 'inputs.json', json.dumps({
        'schema': 'fnos-product-inputs/v1', 'provenance': manifest.get('provenance'),
        'apps': [], 'files': written}, indent=2) + '\n')
    return written


def _format_payload(dest):
    accel = dest / 'accel'
    files = accel / 'files' / 'usr/lib/fnos-x5/format-test.txt'
    _write_text(files, 'format-test only; not a board payload\n')
    compat = json.loads(DEFAULT_COMPAT.read_text())
    _write_text(accel / 'compat.json', json.dumps(compat, indent=2) + '\n')
    row = {'path': 'usr/lib/fnos-x5/format-test.txt', 'target': 'usr/lib/fnos-x5/format-test.txt',
           'sha256': digest(files), 'mode': 0o644, 'subsystem': 'photos',
           'kind': 'additive', 'known_native_sha256': []}
    _write_text(accel / 'bundle.json', json.dumps({
        'schema': 'fnos-x5-accel-bundle/v1',
        'module': {'name': APPNAME, 'version': '0.0.0-format'},
        'compat_sha256': digest(accel / 'compat.json'), 'compat': compat,
        'files': [row]}, indent=2) + '\n')
    models = dest / 'product-inputs'
    model = models / 'ai-models/format-test/model.bin'
    _write_bytes(model, b'format-test model\n')
    _write_text(models / 'inputs.json', json.dumps({
        'schema': 'fnos-product-inputs/v1', 'provenance': 'format test only',
        'apps': [], 'files': [{'path': 'ai-models/format-test/model.bin',
                               'bytes': model.stat().st_size, 'sha256': digest(model)}]},
        indent=2) + '\n')


def _tool_files(app):
    sources = {
        'module_install.py': MODULE / 'app' / 'module_install.py',
        'product_accel.py': REPO / 'fnos/integration/image_assembly/product_accel.py',
        'product_ai_defaults.py': REPO / 'fnos/integration/image_assembly/product_ai_defaults.py',
        'product_bootstrap.py': REPO / 'fnos/integration/image_assembly/product_bootstrap.py',
        'media_web.py': REPO / 'fnos/integration/image_assembly/media_web.py',
    }
    for name, source in sources.items():
        if not source.is_file():
            raise SystemExit('missing module tool: ' + name)
        dest = app / name
        shutil.copyfile(source, dest)
        dest.chmod(0o755)


def _prune_bundle(bundle_dir, profile='runtime'):
    from fnos.integration.image_assembly import runtime_profile
    if not (Path(bundle_dir) / 'bundle.json').is_file():
        return {'files': 0, 'removed': 0}
    try:
        return runtime_profile.prune_bundle(bundle_dir, profile)
    except ValueError as exc:
        raise SystemExit(str(exc))


def _make_package(work, source_root, product_inputs, compat, version, format_test,
                  model_inventory=None, profile='runtime'):
    package = work / 'package'
    app = package / 'app'
    app.mkdir(parents=True)
    _tool_files(app)
    payload = app / 'payload'
    payload.mkdir()
    if format_test:
        _format_payload(payload)
        _write_bytes(package / 'ICON.PNG', _png(64))
        _write_bytes(package / 'ICON_256.PNG', _png(256))
    else:
        if source_root is None or product_inputs is None:
            raise SystemExit('real build requires --source-root and --product-inputs')
        from fnos.integration.image_assembly import product_accel
        bundle = product_accel.collect_bundle(source_root, payload / 'accel',
                                             compat_path=compat, module_version=version,
                                             require_complete=True)
        if not any(row.get('subsystem') == 'photos' for row in bundle['files']) and not any(
                row.get('subsystem') == 'media' for row in bundle['files']):
            raise SystemExit('assembled root contains neither Photos nor Media acceleration payload')
        _prune_bundle(payload / 'accel', profile)
        _copy_models(product_inputs, payload / 'product-inputs', model_inventory, profile)
        _write_text(payload / 'profile.json', json.dumps({'schema': 'fnos-x5-accel-profile/v1',
                                                          'profile': profile}) + '\n')
    _write_text(package / 'manifest', _manifest(version))
    _write_text(package / 'config/privilege', json.dumps({
        'defaults': {'run-as': 'root'}, 'username': 'fnosx5accel', 'groupname': 'fnosx5accel'}, indent=2) + '\n')
    _write_text(package / 'config/resource', '{}\n')
    (package / 'wizard').mkdir()
    for source in sorted((MODULE / 'template' / 'cmd').iterdir()):
        if source.name.startswith('._'):
            continue
        dest = package / 'cmd' / source.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        dest.chmod(0o755)
    return package


def _make_package_with_icons(work, source_root, product_inputs, compat, version, icon, icon_256,
                             model_inventory=None, profile='runtime'):
    package = _make_package(work, source_root, product_inputs, compat, version, False, model_inventory, profile)
    for source, name in ((icon, 'ICON.PNG'), (icon_256, 'ICON_256.PNG')):
        if source is None or not Path(source).is_file():
            raise SystemExit('valid package icons required: ' + name)
        shutil.copyfile(source, package / name)
        (package / name).chmod(0o644)
    return package


def _verify_fpk(path, *, format_test):
    members = {}
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive.getmembers():
            members[member.name] = member
            if any(part.startswith('._') for part in Path(member.name).parts):
                raise SystemExit('AppleDouble metadata is not allowed in the package: ' + member.name)
            if member.name.endswith(('.tar.gz', '.tar', '.zip')):
                raise SystemExit('source archive must not be shipped as an installable package')
        required = {'app.tgz', 'manifest', 'config/privilege', 'config/resource',
                    'ICON.PNG', 'ICON_256.PNG', 'cmd/install_init', 'cmd/install_callback',
                    'cmd/uninstall_init', 'cmd/main'}
        missing = required - set(members)
        if missing:
            raise SystemExit('fpk missing members: ' + ','.join(sorted(missing)))
        if members['manifest'].mode & 0o111:
            raise SystemExit('fpk manifest must not be executable')
        for name in ('cmd/install_init', 'cmd/install_callback', 'cmd/uninstall_init', 'cmd/main'):
            if not members[name].mode & 0o111:
                raise SystemExit('fpk lifecycle script is not executable: ' + name)
        manifest = archive.extractfile('manifest').read().decode()
        fields = {}
        for line in manifest.splitlines():
            if '=' in line:
                key, value = line.split('=', 1)
                fields[key.strip()] = value.strip()
        if fields.get('appname') != APPNAME or fields.get('platform') != 'arm':
            raise SystemExit('fpk manifest is not the reviewed ARM app')
        app_bytes = archive.extractfile('app.tgz').read()
    with tempfile.TemporaryDirectory() as tmp:
        app_path = Path(tmp) / 'app.tgz'
        app_path.write_bytes(app_bytes)
        with tarfile.open(app_path, 'r:gz') as app:
            names = {member.name for member in app.getmembers()}
            for member in app.getmembers():
                if any(part.startswith('._') for part in Path(member.name).parts):
                    raise SystemExit('AppleDouble metadata is not allowed in app.tgz: ' + member.name)
            required_app = {'module_install.py', 'payload/accel/bundle.json',
                            'payload/product-inputs/inputs.json'}
            if not format_test:
                required_app.add('payload/profile.json')
            missing = required_app - names
            if missing:
                raise SystemExit('app.tgz missing payload: ' + ','.join(sorted(missing)))
            bundle = json.loads(app.extractfile('payload/accel/bundle.json').read())
            if bundle.get('schema') != 'fnos-x5-accel-bundle/v1':
                raise SystemExit('unreviewed accel bundle in app.tgz')
            if format_test:
                for row in bundle['files']:
                    if not row.get('sha256'):
                        raise SystemExit('bundle row without hash: ' + row.get('path', '?'))
            else:
                app_root = Path(tmp) / 'app'
                app.extractall(app_root)
                from fnos.integration.image_assembly import product_accel
                product_accel.verify_bundle(app_root / 'payload/accel',
                                            require_complete=True)
    return {'bytes': path.stat().st_size, 'sha256': digest(path),
            'members': sorted(members), 'format_test': bool(format_test)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--product-inputs', type=Path)
    parser.add_argument('--compat', type=Path, default=DEFAULT_COMPAT)
    parser.add_argument('--model-inventory', type=Path, default=None)
    parser.add_argument('--profile', choices=('full', 'runtime'), default='runtime')
    parser.add_argument('--version', default=VERSION)
    parser.add_argument('--icon', type=Path)
    parser.add_argument('--icon-256', type=Path)
    parser.add_argument('--fnpack', default=shutil.which('fnpack'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--format-test', action='store_true')
    parser.add_argument('--keep-work', action='store_true')
    args = parser.parse_args(argv)
    if args.model_inventory is None:
        args.model_inventory = (DEFAULT_RUNTIME_MODEL_INVENTORY if args.profile == 'runtime'
                                else DEFAULT_MODEL_INVENTORY)
    if not args.fnpack or not Path(args.fnpack).is_file():
        raise SystemExit('official fnpack 1.2.3 is required (https://developer.fnnas.com/docs/cli/fnpack/)')
    args.output.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='fnos-accel-fpk-'))
    try:
        if args.format_test:
            package = _make_package(work, None, None, args.compat, '0.0.0-format', True,
                                    args.model_inventory, args.profile)
        else:
            package = _make_package_with_icons(work, args.source_root, args.product_inputs,
                                               args.compat, args.version, args.icon, args.icon_256,
                                               args.model_inventory, args.profile)
        result = subprocess.run([str(args.fnpack), 'build', '--directory', str(package)],
                                cwd=args.output, capture_output=True, text=True)
        if result.returncode:
            raise SystemExit('fnpack build failed: ' + (result.stdout + result.stderr))
        fpk = args.output / f'{APPNAME}.fpk'
        if not fpk.is_file():
            raise SystemExit('fnpack did not produce ' + str(fpk))
        report = _verify_fpk(fpk, format_test=args.format_test)
        report.update({'appname': APPNAME,
                       'version': '0.0.0-format' if args.format_test else args.version,
                       'public_version': PUBLIC_VERSION,
                       'payload_complete': not args.format_test,
                       'profile': args.profile,
                       'format': 'fnpack 1.2.3 gzip/tar .fpk; app.tgz payload',
                       'source_root': str(args.source_root) if args.source_root else None,
                       'product_inputs': str(args.product_inputs) if args.product_inputs else None})
        (args.output / 'module-package.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, ensure_ascii=False))
        return 0
    finally:
        if not args.keep_work:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
