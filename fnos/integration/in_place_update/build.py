"""Build the standalone Linux update package using Python's standard library."""

import argparse
from pathlib import Path
import stat
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from .update import DRIVER_DIGEST, WifiTarget, sha256


def build(output, driver_path, target=None):
    source = Path(__file__).resolve().parent
    driver = driver_path.read_bytes()
    if sha256(driver) != (target.driver_sha256 if target else DRIVER_DIGEST):
        raise ValueError('Expected the verified v0.3.3 AIC8800 module')
    entries = {
        '__main__.py': b'from update import main\nmain()\n',
        'update.py': (source / 'update.py').read_bytes(),
        'quark_log_compat.py': (source.parent / 'image_assembly/quark_log_compat.py').read_bytes(),
    }
    if target is not None:
        entries['wifi-target.json'] = target.to_json().encode()
    with output.open('xb') as stream, ZipFile(stream, 'w') as package:
        for name, contents in entries.items():
            add_member(package, name, contents.replace(b'\r\n', b'\n'))
        add_member(package, 'aic8800_fdrv.ko', driver)


def add_member(package, name, contents):
    member = ZipInfo(name, (2026, 11, 21, 0, 0, 0))
    member.create_system = 3
    member.external_attr = (stat.S_IFREG | 0o644) << 16
    package.writestr(member, contents, compress_type=ZIP_DEFLATED)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--wifi-module', required=True, type=Path)
    parser.add_argument('--wifi-target', type=Path,
                        help='reviewed kernel/original/patched module digests; omit for the original PR target')
    args = parser.parse_args()
    target = WifiTarget.from_json(args.wifi_target.read_bytes()) if args.wifi_target else None
    build(args.output, args.wifi_module, target)
