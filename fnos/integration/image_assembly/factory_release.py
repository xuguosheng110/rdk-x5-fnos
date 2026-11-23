"""Use the updater's transaction when producing a fresh complete image."""
import hashlib
import os
import stat
from pathlib import Path

from fnos.integration.in_place_update import update
from .platform_runtime import _destination

BASELINE_HELPER = Path(__file__).parent.parent / 'in_place_update/baseline-helper-v032.py.txt'
CURRENT_HELPER = Path(__file__).parent / 'quark_log_compat.py'
TARGET_RECORD = update.STATE / 'wifi-target.json'


def digest(path):
    with path.open('rb') as stream:
        value = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
        return value.hexdigest()


def validated_inputs(root, wifi_module, wifi_target):
    if wifi_module is None or wifi_target is None:
        raise ValueError('v0.3.3 factory image requires reviewed Wi-Fi module and target manifest')
    target = update.WifiTarget.from_json(Path(wifi_target).read_bytes())
    if digest(Path(wifi_module)) != target.driver_sha256:
        raise ValueError('Wi-Fi replacement module failed checksum validation')
    if digest(root / 'boot/Image') != target.kernel_image_sha256:
        raise ValueError('Factory kernel does not match the reviewed Wi-Fi target')
    if digest(root / update.DRIVER) not in (target.base_driver_sha256, target.driver_sha256):
        raise ValueError('Factory Wi-Fi module does not match the reviewed target')
    if digest(BASELINE_HELPER) != update.BASE_DIGEST:
        raise ValueError('Factory helper rollback baseline failed checksum validation')
    return target


def prepare_parent(root, relative, mode=0o755):
    """Create only missing system directories, with modes independent of umask."""
    parent = _destination(root, relative).parent
    missing = []
    while parent != root and not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for path in reversed(missing):
        path.mkdir(mode=mode if path == root / relative.parent else 0o755, exist_ok=True)
        os.chown(path, 0, 0)



def normalize_reviewed_driver_metadata(root):
    """Normalize root-owned build artifacts only inside the offline image."""
    driver = _destination(root, update.DRIVER)
    changes = []
    for path in (driver, *driver.parents):
        if path == root:
            break
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        is_driver = path == driver
        expected_type = stat.S_ISREG if is_driver else stat.S_ISDIR
        allowed = (0o644, 0o664) if is_driver else (0o755, 0o775)
        if info.st_uid or info.st_gid or not expected_type(info.st_mode) or mode not in allowed:
            raise ValueError('Unrecognized factory driver metadata: ' + str(path))
        wanted = 0o644 if is_driver else 0o755
        if mode != wanted:
            changes.append((path, wanted))
    # Validate the whole chain before changing any mode. The caller holds the
    # update lock and already matched both the kernel and driver content hashes.
    for path, mode in changes:
        path.chmod(mode)


def stage(root, release, wifi_module, wifi_target, write_text):
    root = root.resolve()
    if root == Path('/'):
        raise ValueError('Offline factory root required')
    if release not in ('v0.3.2', 'v0.3.3'):
        raise ValueError('Unsupported factory release')
    for relative in (update.RELEASE, update.HELPER, update.DRIVER, Path('boot/Image'),
                     TARGET_RECORD, update.STATE / 'lock'):
        if _destination(root, relative).is_symlink():
            raise ValueError('Symlink in factory managed path: ' + str(relative))
    # Complete all artifact checks before changing a version or helper.
    target = validated_inputs(root, wifi_module, wifi_target) if release == 'v0.3.3' else None
    baseline = BASELINE_HELPER.read_bytes()
    if update.sha256(baseline) != update.BASE_DIGEST:
        raise ValueError('Factory helper rollback baseline failed checksum validation')
    driver = Path(wifi_module).read_bytes() if target else (root / update.DRIVER).read_bytes()
    if target and update.sha256(driver) != target.driver_sha256:
        raise ValueError('Wi-Fi replacement module changed during factory preflight')
    install = update.Installation(root, CURRENT_HELPER.read_bytes(), driver,
                                  driver_baseline=target.base_driver_sha256 if target else update.sha256(driver))
    prepare_parent(root, update.STATE / 'lock', mode=0o700)
    with install.locked():
        if target:
            if validated_inputs(root, wifi_module, wifi_target) != target:
                raise ValueError('Factory Wi-Fi target changed while acquiring the update lock')
            normalize_reviewed_driver_metadata(root)
        # Validate recovery state while holding the same lock used by apply and
        # rollback. A competing factory invocation must never rewrite a marker.
        for entry in install.files:
            if entry.backup.exists():
                install.original_backup(entry)
        helper = root / update.HELPER
        current_helper = update.read_file(helper) if helper.exists() else None
        if current_helper not in (None, baseline, CURRENT_HELPER.read_bytes()):
            raise ValueError('Unrecognized helper in the factory staging tree')
        if not target and (any((root / p).exists() for p, _ in update.POLICY_FILES)
                           or (root / update.CPU_ENABLE).is_symlink()):
            raise ValueError('v0.3.2 factory staging requires a clean baseline without v0.3.3 policies')
        if target and digest(root / update.DRIVER) == target.driver_sha256:
            install.original_backup(install.files[1])
        if current_helper == CURRENT_HELPER.read_bytes() and not install.backup.exists():
            update.atomic_write(install.backup, baseline, 0o600)
        for relative in (update.RELEASE, update.HELPER):
            prepare_parent(root, relative)
        if current_helper is None or not target:
            write_text(helper, baseline.decode())
        if not (root / update.RELEASE).exists() or not target:
            write_text(root / update.RELEASE, update.BASE.decode())
        if not target:
            return {'version': release, 'wifi_hotfix': False}
        install.apply()
        if install.status() != 'v0.3.3':
            raise RuntimeError('Factory upgrade transaction is incomplete')
        write_text(root / TARGET_RECORD, target.to_json(), 0o600)
    return {'version': release, 'wifi_hotfix': True, 'target': target.__dict__,
            'rollback_baselines': True}


def verify(root, release, wifi_target=None):
    if (root / update.RELEASE).read_bytes() != (release + '\n').encode():
        raise ValueError('Factory release marker differs from the requested version')
    if release == 'v0.3.2':
        if digest(root / update.HELPER) != update.BASE_DIGEST:
            raise ValueError('v0.3.2 helper differs from its reviewed baseline')
        return {'version': release, 'status': 'verified'}
    if release != 'v0.3.3' or wifi_target is None:
        raise ValueError('Missing factory Wi-Fi target for verification')
    target = update.WifiTarget.from_json(Path(wifi_target).read_bytes())
    update.trust_parents(root / update.STATE, root)
    update.trusted_path(root / update.STATE, stat.S_IFDIR | 0o700)
    update.trust_parents(root / TARGET_RECORD, root)
    record = update.WifiTarget.from_json(update.read_file(root / TARGET_RECORD, 0o600))
    if target != record or digest(root / 'boot/Image') != target.kernel_image_sha256:
        raise ValueError('Factory Wi-Fi target or kernel differs')
    if digest(root / update.DRIVER) != target.driver_sha256:
        raise ValueError('Factory image contains the wrong Wi-Fi module')
    if (root / update.HELPER).read_bytes() != CURRENT_HELPER.read_bytes():
        raise ValueError('Factory application compatibility helper differs')
    install = update.Installation(root, CURRENT_HELPER.read_bytes(), (root / update.DRIVER).read_bytes(),
                                  driver_baseline=target.base_driver_sha256)
    if install.status() != release:
        raise ValueError('Factory policies or rollback baselines are incomplete')
    return {'version': release, 'status': 'verified', 'wifi_hotfix': True,
            'performance_policy': True, 'rollback_baselines': True}
