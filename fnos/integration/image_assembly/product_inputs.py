"""Embed clean official app installers/model caches and a native bootstrap."""
import hashlib
import json
from pathlib import Path
import shutil

from .platform_runtime import _destination, _prepare_parent
from .product_bootstrap import PAYLOAD, STATE, APPS, digest, verified_inputs
from .image_identity import stage_image_identity, verify_image_identity

MANIFEST = Path('usr/share/fnos-x5/product-bootstrap-manifest.json')
APP_DIRS = ('trim.photos-0.9.14-tpk', 'trim.media-0.9.8-1-tpk')
MODELS = ('face-model-1.0.1', 'img2vec-medium-model-1.0.0', 'txt2vec-medium-model-1.0.0')
SYSTEM_DEFAULTS = {
    Path('etc/systemd/system/nmbd.service.d/80-fnos-product.conf'): (
        '[Service]\n'
        '# Native unit is Type=notify: keep the notifying process as MainPID.\n'
        'ExecStart=\nExecStart=/usr/sbin/nmbd --foreground --no-process-group $NMBDOPTIONS\n'
    ).encode(),
    Path('etc/systemd/system/exim4.service.d/80-fnos-product.conf'): (
        '[Unit]\nRequiresMountsFor=/var/log\n[Service]\n'
        'ExecStartPre=/usr/bin/install -d -o Debian-exim -g Debian-exim -m 0750 /var/log/exim4\n'
    ).encode(),
    Path('etc/systemd/system/nut-server.service.d/80-fnos-product.conf'): (
        '[Service]\n'
        '# No UPS section means not configured. A configured UPS still uses the native driver.\n'
        'ExecCondition=/usr/bin/grep -Eq "^[[:space:]]*[[][^]]+[]]" /etc/nut/ups.conf\n'
    ).encode(),
    Path('etc/systemd/system/smartmontools.service.d/80-fnos-product.conf'): (
        '[Service]\n'
        'ExecCondition=/usr/bin/python3 /usr/lib/fnos-x5/smartd_preflight.py\n'
    ).encode(),
    Path('usr/lib/fnos-x5/smartd_preflight.py'): Path(__file__).with_name('smartd_preflight.py').read_bytes(),
}
RETIRED_GETTY = Path('etc/systemd/system/getty.target.wants/serial-getty@ttyAMA0.service')


def stage_product_inputs(root, inputs):
    root, inputs = Path(root).resolve(), Path(inputs).resolve()
    if root == Path('/') or not (root / 'etc/os-release').is_file():
        raise ValueError('offline root filesystem required')
    original = json.loads((inputs / 'inputs.json').read_text())
    if original.get('schema') != 'fnos-clean-product-inputs/v1':
        raise ValueError('clean product input manifest required')
    rows, plan, seen = [], [], set()
    for row in original['files']:
        name = row['path']
        relative = Path(name)
        if name in seen or relative.is_absolute() or '..' in relative.parts or str(relative) != name:
            raise ValueError('invalid product input path')
        seen.add(name)
        if len(relative.parts) < 3 or not (
            relative.parts[:2] in [('apps', app) for app in APP_DIRS] or
            relative.parts[:2] in [('ai-models', model) for model in MODELS]
        ):
            raise ValueError('unreviewed product input family')
        source = _destination(inputs, relative)
        if source.is_symlink() or not source.is_file() or source.stat().st_size != row['bytes'] or digest(source) != row['sha256']:
            raise ValueError('product input differs: ' + name)
        # Drop transport metadata and installer-generated empty DB copies.
        # The vendor app.tgz already contains its clean original databases.
        if relative.name.startswith('._') or (relative.parent.name == 'cmd' and relative.suffix == '.db'):
            continue
        mode = 0o755 if relative.parent.name == 'cmd' else 0o644
        plan.append((source, PAYLOAD / relative, mode))
        rows.append(row)
    data = {'schema': 'fnos-product-inputs/v1', 'provenance': original['provenance'],
            'apps': [{'name': name, 'directory': 'apps/' + directory} for name, directory in zip(APPS, APP_DIRS)],
            'files': rows}
    configs = {
        **SYSTEM_DEFAULTS,
        PAYLOAD / 'inputs.json': (json.dumps(data, indent=2) + '\n').encode(),
        Path('usr/lib/fnos-x5/product_bootstrap.py'): Path(__file__).with_name('product_bootstrap.py').read_bytes(),
        Path('usr/lib/fnos-x5/product_ai_defaults.py'): Path(__file__).with_name('product_ai_defaults.py').read_bytes(),
        Path('etc/systemd/system/fnos-product-bootstrap.service'): (
            '[Unit]\nDescription=Prepare bundled fnOS applications on the selected data volume\n'
            'After=trim_app_center.service filestor_service.service fnos-x5-runtime.service\n'
            f'ConditionPathExists=!/{STATE}/complete.json\n'
            '[Service]\nType=oneshot\nUMask=0077\n'
            'ExecStart=/usr/bin/python3 /usr/lib/fnos-x5/product_bootstrap.py --execute\n'
            'TimeoutStartSec=35min\n'
        ).encode(),
        Path('etc/systemd/system/fnos-product-bootstrap.timer'): (
            '[Unit]\nDescription=Wait for fnOS storage initialization\n'
            '[Timer]\nOnBootSec=60\nOnUnitInactiveSec=60\nAccuracySec=10\n'
            '[Install]\nWantedBy=timers.target\n'
        ).encode(),
    }
    enabled = Path('etc/systemd/system/timers.target.wants/fnos-product-bootstrap.timer')
    retired = _destination(root, RETIRED_GETTY)
    if retired.is_symlink():
        if str(retired.readlink()) not in ('/lib/systemd/system/serial-getty@.service',
                                          '/usr/lib/systemd/system/serial-getty@.service'):
            raise ValueError('unreviewed source-image serial getty link')
    elif retired.exists():
        raise ValueError('source-image serial getty entry is not a link')
    for relative in [*(target for _, target, _ in plan), *configs, MANIFEST, enabled]:
        dest = _destination(root, relative)
        if dest.is_symlink() or dest.exists():
            raise ValueError('product staging destination already exists: ' + str(relative))
    report = {'schema': 'fnos-product-bootstrap/v1', 'files': [], 'apps': list(APPS),
              'removed_paths': [str(RETIRED_GETTY)],
              'scope': 'native app installer, model cache and AI configuration defaults; clean first activation requires real acceptance'}
    report['factory_identity'] = stage_image_identity(root)
    for source, relative, mode in plan:
        dest = root / relative
        _prepare_parent(root, dest.parent)
        shutil.copyfile(source, dest)
        dest.chmod(mode)
        report['files'].append({'path': str(relative), 'sha256': digest(dest), 'mode': mode})
    for relative, content in configs.items():
        dest = root / relative
        _prepare_parent(root, dest.parent)
        dest.write_bytes(content)
        dest.chmod(0o644)
        report['files'].append({'path': str(relative), 'sha256': hashlib.sha256(content).hexdigest(), 'mode': 0o644})
    _prepare_parent(root, (root / enabled).parent)
    (root / enabled).symlink_to('../fnos-product-bootstrap.timer')
    # X5's real console is ttyS0, activated by systemd-getty-generator from
    # the kernel command line. The foreign ttyAMA0 link otherwise waits for
    # a device that never appears and prevents boot from reaching its target.
    if retired.is_symlink():
        retired.unlink()
    report['files'].append({'path': str(enabled), 'symlink': '../fnos-product-bootstrap.timer'})
    (root / MANIFEST).write_text(json.dumps(report, indent=2) + '\n')
    (root / MANIFEST).chmod(0o644)
    verify_product_inputs(root, report)
    return report


def verify_product_inputs(root, report, *, require_root_owner=False):
    root = Path(root).resolve()
    manifest = _destination(root, MANIFEST)
    if manifest.is_symlink() or json.loads(manifest.read_text()) != report:
        raise ValueError('product bootstrap manifest mismatch')
    for name in report.get('removed_paths', []):
        path = _destination(root, Path(name))
        if path.exists() or path.is_symlink():
            raise ValueError('retired source-image configuration present: ' + name)
    if 'factory_identity' in report:
        verify_image_identity(root, report['factory_identity'])
    for row in report['files']:
        p = _destination(root, Path(row['path']))
        if 'symlink' in row:
            if not p.is_symlink() or str(p.readlink()) != row['symlink']:
                raise ValueError('product timer enablement mismatch')
        elif p.is_symlink() or digest(p) != row['sha256'] or p.stat().st_mode & 0o777 != row['mode']:
            raise ValueError('product bootstrap file mismatch: ' + row['path'])
    if require_root_owner:
        identity_files = report.get('factory_identity', {}).get('files', [])
        for p in [manifest, *(root / row['path'] for row in [*report['files'], *identity_files])]:
            while p != root:
                info = p.lstat()
                if info.st_uid or info.st_gid or (not p.is_symlink() and info.st_mode & 0o022):
                    raise ValueError('unsafe product bootstrap ownership or mode')
                p = p.parent
    inputs = verified_inputs(root)
    return {'verified_files': len(report['files']), 'input_files': len(inputs['files']), 'apps': list(APPS)}
