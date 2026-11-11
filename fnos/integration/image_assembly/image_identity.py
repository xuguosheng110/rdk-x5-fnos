"""Prepare unique machine identities in an offline factory image.

No live-board identity is changed. PID1 initializes the empty machine-id;
OpenSSH creates missing host keys before its native configuration check.
"""
import hashlib
from pathlib import Path

from .platform_runtime import _destination, _prepare_parent

MACHINE_ID = Path('etc/machine-id')
DBUS_ID = Path('var/lib/dbus/machine-id')
REMOVED = [Path('var/lib/systemd/random-seed'), Path('var/lib/NetworkManager/secret_key')]
REMOVED += [Path('etc/ssh') / ('ssh_host_' + kind + '_key' + suffix)
            for kind in ('rsa', 'ecdsa', 'ed25519', 'dsa') for suffix in ('', '.pub')]
CONFIGS = {
    Path('etc/systemd/system/fnos-host-keys.service'): (
        '[Unit]\nDescription=Initialize missing SSH host keys for this installation\n'
        'Before=ssh.service\n[Service]\nType=oneshot\nUMask=0077\n'
        'ExecStart=/usr/bin/ssh-keygen -A\nRemainAfterExit=yes\n'
    ).encode(),
    Path('etc/systemd/system/ssh.service.d/80-fnos-host-keys.conf'): (
        '[Unit]\nRequires=fnos-host-keys.service\nAfter=fnos-host-keys.service\n'
    ).encode(),
    Path('etc/NetworkManager/conf.d/80-fnos-x5-ethernet.conf'): (
        '# The X5 driver supplies a random fallback hardware MAC on every boot.\n'
        '# Derive a stable default from this machine and interface, not the\n'
        '# temporary default profile UUID. Explicit user profile values win.\n'
        '[connection-fnos-x5-ethernet]\nmatch-device=driver:hobot_gmac\n'
        'connection.stable-id=fnos-x5/${DEVICE}\n'
        'ethernet.cloned-mac-address=stable\n'
    ).encode(),
}


def stage_image_identity(root):
    root = Path(root).resolve()
    if root == Path('/') or not (root / 'etc/os-release').is_file():
        raise ValueError('offline root filesystem required for factory identity')
    # Validate all replacements/removals before changing anything. These are
    # only generic source-image identities, never accounts or application DBs.
    for name in [MACHINE_ID, *REMOVED]:
        path = _destination(root, name)
        if path.is_symlink():
            raise ValueError('symlink at factory identity file: ' + str(name))
    dbus = _destination(root, DBUS_ID)
    if dbus.is_symlink() and str(dbus.readlink()) not in ('/etc/machine-id', '../../../etc/machine-id'):
        raise ValueError('unexpected D-Bus machine identity link')
    for name in CONFIGS:
        path = _destination(root, name)
        if path.exists() or path.is_symlink():
            raise ValueError('factory identity config already exists: ' + str(name))
    report = {'schema': 'fnos-factory-identity/v1', 'files': [],
              'removed_paths': [str(p) for p in REMOVED],
              'scope': 'offline generic identities only; native firstboot credentials are not preconfigured'}
    for name in REMOVED:
        path = root / name
        if path.exists():
            path.unlink()
    machine = root / MACHINE_ID
    _prepare_parent(root, machine.parent)
    # Empty (rather than "uninitialized") generates a unique ID without
    # rerunning systemd preset-all over the image's reviewed service choices.
    if machine.exists():
        machine.unlink()
    machine.write_bytes(b'')
    machine.chmod(0o444)
    report['files'].append({'path': str(MACHINE_ID), 'sha256': hashlib.sha256(b'').hexdigest(), 'mode': 0o444})
    _prepare_parent(root, dbus.parent)
    if dbus.exists() or dbus.is_symlink():
        dbus.unlink()
    dbus.symlink_to('/etc/machine-id')
    report['files'].append({'path': str(DBUS_ID), 'symlink': '/etc/machine-id'})
    for name, data in CONFIGS.items():
        path = root / name
        _prepare_parent(root, path.parent)
        path.write_bytes(data)
        path.chmod(0o644)
        report['files'].append({'path': str(name), 'sha256': hashlib.sha256(data).hexdigest(), 'mode': 0o644})
    verify_image_identity(root, report)
    return report


def verify_image_identity(root, report):
    root = Path(root).resolve()
    if report.get('schema') != 'fnos-factory-identity/v1':
        raise ValueError('unreviewed factory identity report')
    for name in report['removed_paths']:
        path = _destination(root, Path(name))
        if path.exists() or path.is_symlink():
            raise ValueError('source machine identity retained: ' + name)
    for row in report['files']:
        path = _destination(root, Path(row['path']))
        if 'symlink' in row:
            if not path.is_symlink() or str(path.readlink()) != row['symlink']:
                raise ValueError('factory identity link mismatch')
        elif path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256'] or path.stat().st_mode & 0o777 != row['mode']:
            raise ValueError('factory identity file mismatch: ' + row['path'])
    return {'verified_files': len(report['files']), 'source_identity_files_absent': len(report['removed_paths'])}
