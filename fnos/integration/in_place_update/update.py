"""Install and roll back the v0.3.3 update on the supported X5 image."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import zipfile


BASE = b'v0.3.2\n'
TARGET = b'v0.3.3\n'
BASE_DIGEST = '6c91f145426dfc55ad9b03b448949f02d36394a30255cea021084b672d1dd5c6'
BASE_DRIVER_DIGEST = '52a3d2eb9d2dec4663b40da3d8e10aa3b3eb4ff5c906959126f4a1b5bda41fd4'
DRIVER_DIGEST = '83e88d11da3b0c0d38d3a4bb53623c928ab660e86d908dcb0a4bc5de19c03bfe'
KERNEL_IMAGE_DIGEST = '608dbb9fe29e49ac1f10ca009f9004ff069c2c28260695806a2a1fc67a3312f0'
RELEASE = Path('etc/fnos-x5/release')
HELPER = Path('usr/lib/fnos-x5/quark_log_compat.py')
DRIVER = Path('usr/lib/modules/6.18.0/kernel/drivers/net/wireless/aic8800/aic8800_fdrv/aic8800_fdrv.ko')
STATE = Path('var/lib/fnos-x5/update-v0.3.3')
LEGACY_DRIVER_BACKUP = Path('var/lib/fnos-x5/wifi-driver-backup/aic8800_fdrv.ko')

WIFI_POLICY = Path('etc/modprobe.d/fnos-x5-aic8800-performance.conf')
CPU_UNIT = Path('etc/systemd/system/fnos-x5-performance.service')
CPU_ENABLE = CPU_UNIT.parent / 'multi-user.target.wants' / CPU_UNIT.name
CPU_UNIT_TARGET = Path('/') / CPU_UNIT
POLICY_FILES = (
    (WIFI_POLICY, b'# Keep X5 Wi-Fi enabled, without AIC8800 power saving.\n'
                  b'options aic8800_fdrv ps_on=N\n'),
    (CPU_UNIT, b"""[Unit]
Description=Select the RDK X5 CPU performance governor
ConditionPathIsDirectory=/sys/devices/system/cpu/cpufreq

[Service]
Type=oneshot
ExecStart=/bin/sh -ec 'for policy in /sys/devices/system/cpu/cpufreq/policy*/scaling_governor; do test -w "$policy" && echo performance > "$policy"; done'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""),
)


def sha256(contents):
    return hashlib.sha256(contents).hexdigest()


@dataclass(frozen=True)
class WifiTarget:
    """Bind the original and replacement module to one exact kernel build."""
    kernel_image_sha256: str
    base_driver_sha256: str
    driver_sha256: str

    def __post_init__(self):
        for value in (self.kernel_image_sha256, self.base_driver_sha256, self.driver_sha256):
            if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
                raise ValueError('Invalid Wi-Fi target digest')

    @classmethod
    def from_json(cls, contents):
        data = json.loads(contents)
        if not isinstance(data, dict) or data.pop('schema', None) != 'fnos-x5-wifi-target/v1':
            raise ValueError('Unrecognized Wi-Fi target manifest')
        return cls(**data)

    def to_json(self):
        return json.dumps({'schema': 'fnos-x5-wifi-target/v1', **self.__dict__}, sort_keys=True) + '\n'


def trusted_path(path, mode):
    info = path.lstat()
    if info.st_uid or info.st_gid or info.st_mode != mode:
        raise ValueError(f'Unexpected ownership or mode: {path}')


def trust_parents(path, root):
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid or info.st_mode & 0o022:
            raise ValueError(f'Unsafe parent directory: {parent}')
        if parent == root:
            return


def read_file(path, mode=0o644):
    trusted_path(path, stat.S_IFREG | mode)
    return path.read_bytes()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, contents, mode):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            os.fchmod(output.fileno(), mode)
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


@dataclass(frozen=True)
class ManagedFile:
    path: Path
    backup: Path
    baseline: str
    payload: bytes
    legacy_backup: Path | None = None


class Installation:
    def __init__(self, root, helper_payload, driver_payload, *, driver_baseline=None):
        self.root = root
        self.release = root / RELEASE
        self.state = root / STATE
        self.backup = self.state / 'original-helper.py'
        self.driver_backup = self.state / 'original-aic8800_fdrv.ko'
        self.files = (
            ManagedFile(root / HELPER, self.backup, BASE_DIGEST, helper_payload),
            ManagedFile(root / DRIVER, self.driver_backup, driver_baseline or BASE_DRIVER_DIGEST, driver_payload,
                        root / LEGACY_DRIVER_BACKUP),
        )

    @contextmanager
    def locked(self):
        trust_parents(self.state, self.root)
        self.state.mkdir(mode=0o700, exist_ok=True)
        trusted_path(self.state, stat.S_IFDIR | 0o700)
        sync_dir(self.state.parent)
        fd = os.open(self.state / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def inspect(self):
        trust_parents(self.release, self.root)
        version = read_file(self.release)
        if version not in (BASE, TARGET):
            raise ValueError('This update requires the v0.3.2 or v0.3.3 RDK X5 image')
        files = []
        for entry in self.files:
            trust_parents(entry.path, self.root)
            contents = read_file(entry.path)
            original = sha256(contents) == entry.baseline
            if not original and contents != entry.payload:
                raise ValueError(f'Unrecognized local file: {entry.path}')
            files.append((entry, contents, original))
        return version, files

    def inspect_policy(self):
        missing = []
        for relative, expected in POLICY_FILES:
            path = self.root / relative
            trust_parents(path, self.root)
            try:
                contents = read_file(path)
            except FileNotFoundError:
                missing.append((path, expected))
            else:
                if contents != expected:
                    raise ValueError(f'Unrecognized local file: {path}')

        link = self.root / CPU_ENABLE
        trust_parents(link, self.root)
        try:
            info = link.lstat()
        except FileNotFoundError:
            return missing, False
        if (not stat.S_ISLNK(info.st_mode) or info.st_uid or info.st_gid
                or link.readlink() != CPU_UNIT_TARGET):
            raise ValueError(f'Unrecognized enable link: {link}')
        return missing, True

    def original_backup(self, entry):
        path = entry.backup
        try:
            original = read_file(path, 0o600)
        except FileNotFoundError:
            if entry.legacy_backup is None:
                raise
            path = entry.legacy_backup
            trust_parents(path, self.root)
            original = read_file(path, 0o600)
        if sha256(original) != entry.baseline:
            raise ValueError(f'Rollback backup failed checksum validation: {path}')
        return original

    def status(self):
        version, files = self.inspect()
        for entry, _, original in files:
            if not original:
                self.original_backup(entry)
        missing, enabled = self.inspect_policy()
        expected = version == TARGET
        if (any(original == expected for _, _, original in files)
                or enabled != expected
                or len(missing) != (0 if expected else len(POLICY_FILES))):
            return 'interrupted'
        return version.decode().strip()

    def set_release(self, version):
        atomic_write(self.release, version, 0o644)

    def apply(self):
        version, files = self.inspect()
        missing, enabled = self.inspect_policy()
        for entry, contents, original in files:
            try:
                backup = self.original_backup(entry)
            except FileNotFoundError:
                if not original:
                    raise ValueError(f'Rollback backup is missing: {entry.path}') from None
                backup = contents
            if not entry.backup.exists():
                atomic_write(entry.backup, backup, 0o600)
        for entry, _, original in files:
            if original:
                atomic_write(entry.path, entry.payload, 0o644)
        for path, contents in missing:
            atomic_write(path, contents, 0o644)
        if not enabled:
            link = self.root / CPU_ENABLE
            link.symlink_to(CPU_UNIT_TARGET)
            sync_dir(link.parent)
        if version != TARGET:
            self.set_release(TARGET)

    def rollback(self):
        version, files = self.inspect()
        missing, enabled = self.inspect_policy()
        backups = [(entry, self.original_backup(entry)) for entry, _, original in files if not original]
        for entry, backup in backups:
            atomic_write(entry.path, backup, 0o644)
        if enabled:
            link = self.root / CPU_ENABLE
            link.unlink()
            sync_dir(link.parent)
        missing_paths = {path for path, _ in missing}
        for relative, _ in POLICY_FILES:
            path = self.root / relative
            if path not in missing_paths:
                path.unlink()
                sync_dir(path.parent)
        if version != BASE:
            self.set_release(BASE)


def check_board(target=None):
    model = Path('/proc/device-tree/model').read_bytes().rstrip(b'\0')
    root_volume = subprocess.check_output(['findmnt', '-nro', 'SOURCE,FSTYPE', '/'], text=True).strip()
    trim = subprocess.check_output(['dpkg-query', '-W', '-f=${Version}', 'trim'], text=True)
    if (not model.startswith(b'D-Robotics RDK X5') or os.uname().release != '6.18.0'
            or not re.fullmatch(r'/dev/mmcblk[0-9]+p2 ext4', root_volume) or trim != '1.2.0701'):
        raise ValueError('Unsupported board, system partition, kernel or fnOS version')
    expected_kernel = target.kernel_image_sha256 if target else KERNEL_IMAGE_DIGEST
    if sha256(Path('/boot/Image').read_bytes()) != expected_kernel:
        raise ValueError('The kernel Image does not match the supported Wi-Fi module')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'apply', 'rollback'))
    args = parser.parse_args()
    if os.geteuid():
        parser.exit(1, 'Run the package with sudo.\n')
    try:
        with zipfile.ZipFile(sys.argv[0]) as archive:
            payload = archive.read('quark_log_compat.py')
            driver = archive.read('aic8800_fdrv.ko')
            target = WifiTarget.from_json(archive.read('wifi-target.json')) if 'wifi-target.json' in archive.namelist() else None
        check_board(target)
        if sha256(driver) != (target.driver_sha256 if target else DRIVER_DIGEST):
            raise ValueError('Wi-Fi module in the update package failed checksum validation')
        install = Installation(Path('/'), payload, driver,
                               driver_baseline=target.base_driver_sha256 if target else None)
        with install.locked():
            if args.action == 'apply':
                install.apply()
            elif args.action == 'rollback':
                install.rollback()
            print(install.status())
            if args.action != 'status':
                print('Reboot to load the Wi-Fi driver and CPU policy from disk.', file=sys.stderr)
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        parser.exit(1, f'Update stopped: {error}\n')
