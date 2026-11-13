"""Linux regression for native-created data paths used by unmodified writers.

Run under the project's board lock, as root, with the reviewed ARM library.
Only unique fixture directories are created; containers are covered separately
by the recorded Docker workflow. No compilation is performed on the board.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('library', type=Path)
    ap.add_argument('--volume', type=Path, default=Path('/vol1'))
    args = ap.parse_args()
    assert os.geteuid() == 0
    assert re.fullmatch(r'/vol[1-9][0-9]*', str(args.volume))
    assert args.volume.is_dir() and not args.volume.is_symlink()
    assert args.volume.stat().st_dev != Path('/').stat().st_dev
    library = args.library.resolve(strict=True)
    token = str(1000000000 + secrets.randbelow(900000000))
    shared = args.volume / token
    private = args.volume / ('@fnos-acl-probe-' + token)
    assert not shared.exists() and not private.exists()
    native_env = dict(os.environ, LD_PRELOAD=str(library), FNOS_HOME_DATA_ROOT='auto')
    plain_env = dict(os.environ)
    for key in ('LD_PRELOAD', 'FNOS_HOME_DATA_ROOT', 'FNOS_HOME_FTP_VIEW'):
        plain_env.pop(key, None)

    def child(uid, code, env):
        result = subprocess.run(['setpriv', '--reuid=' + str(uid), '--regid=' + str(uid),
                               '--clear-groups', 'python3', '-c', code], env=env,
                              capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        return result

    def default_acl(path):
        try:
            return os.getxattr(path, 'system.posix_acl_default')
        except OSError:
            return None

    os.environ['FNOS_HOME_DATA_ROOT'] = 'auto'
    lib = ctypes.CDLL(str(library), use_errno=True)
    lib.trimacl_get_file.argtypes = [ctypes.c_char_p]
    lib.trimacl_get_file.restype = ctypes.c_void_p
    libc = ctypes.CDLL(None)
    libc.free.argtypes = [ctypes.c_void_p]

    def native_access(path):
        pointer = lib.trimacl_get_file(os.fsencode(path))
        assert pointer, ctypes.get_errno()
        libc.free(pointer)

    try:
        child(1000, 'import os;os.umask(0o077);os.mkdir(' + repr(str(shared)) + ')', native_env)
        assert shared.stat().st_mode & 0o777 == 0o777
        assert default_acl(shared), 'native-created fresh user root has no inherited policy'
        unmanaged = shared / 'unmanaged'
        child(12345, 'import os;from pathlib import Path;os.umask(0o077);p=Path(' + repr(str(unmanaged)) + ');p.mkdir();(p/"data").write_text(' + repr('first\n') + ')', plain_env)
        assert unmanaged.stat().st_mode & 0o777 == 0o777
        assert (unmanaged / 'data').stat().st_mode & 0o777 == 0o666
        child(12346, 'from pathlib import Path;p=Path(' + repr(str(unmanaged / 'data')) + ');assert p.read_text()==' + repr('first\n') + ';p.open("a").write(' + repr('second\n') + ')', plain_env)
        assert (unmanaged / 'data').read_text() == 'first\nsecond\n'

        # Regression for already-shared directories: the fast permission path
        # must also establish inheritance when a native user selects the path.
        existing = shared / 'existing'
        existing.mkdir()
        os.removexattr(existing, 'system.posix_acl_default')
        existing.chmod(0o777)
        native_access(existing)
        assert default_acl(existing)

        # Team directories are created by a privileged service. The later
        # native ACL job runs as the requesting household user.
        assert existing.stat().st_uid == 0
        update = (
            'import ctypes,os;'
            f'lib=ctypes.CDLL({str(library)!r},use_errno=True);'
            'lib.trimacl_set_file.argtypes=[ctypes.c_char_p,ctypes.c_void_p];'
            f'assert lib.trimacl_set_file(os.fsencode({str(existing)!r}),None)==0,ctypes.get_errno()'
        )
        child(1000, update, native_env)
        assert existing.stat().st_uid == 0 and existing.stat().st_mode & 0o777 == 0o777
        assert default_acl(existing)

        child(1000, 'import os;from pathlib import Path;os.umask(0o077);p=Path(' + repr(str(private)) + ');p.mkdir();(p/"private").write_text("private")', native_env)
        assert private.stat().st_mode & 0o777 == 0o700
        assert (private / 'private').stat().st_mode & 0o777 == 0o600
        assert default_acl(private) is None
        with tempfile.TemporaryDirectory(prefix='fnos-acl-outside-') as outside:
            link = shared / 'outside'
            link.symlink_to(outside, target_is_directory=True)
            pointer = lib.trimacl_get_file(os.fsencode(link))
            if pointer:
                libc.free(pointer)
            assert default_acl(outside) is None

        # Explicit private modes remain kernel semantics for unmodified
        # writers. Native data access reconciles them with the household policy.
        restricted = shared / 'explicit-private-mode'
        child(12345, 'import os;p=' + repr(str(restricted)) + ';f=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(f)', plain_env)
        assert restricted.stat().st_mode & 0o777 == 0o600
        native_access(restricted)
        child(12346, 'open(' + repr(str(restricted)) + ',"a").write("shared")', plain_env)
        print(json.dumps({'status': 'PASS', 'fresh_root_inheritance': True,
                          'unmanaged_umask_0077_cross_uid': True,
                          'existing_shared_directory': True, 'non_owner_acl_reapply': True,
                          'private_metadata_preserved': True,
                          'symlink_outside_preserved': True, 'native_explicit_mode_reconciliation': True}))
    finally:
        for path in (shared, private):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)


if __name__ == '__main__':
    main()
