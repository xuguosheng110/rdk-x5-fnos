"""Stage the pinned in-process native mediasrv VPU adapter into an offline root."""
import hashlib
import json
from pathlib import Path
import shlex
import textwrap

from .platform_runtime import _destination, _prepare_parent
from .media_web import SHIM

NATIVE_CODEC = 'usr/trim/lib/mediasrv/lib/libavcodec.so.62'
NATIVE_SHA256 = 'cd8067ed571b436cb470b7733e953c705f0635a55c02b58c58dc8962b18708bc'
MANIFEST = Path('usr/share/fnos-x5/native-media-manifest.json')
SOURCE_UNIT = Path('etc/systemd/system/mediasrv.service')
SOURCE_UNIT_SHA256 = 'ef43c271aae4cdb45f4c2f39d7e360a26c7b7783ac483c2e7a7df2ee733bd15a'
LAUNCHER = 'usr/lib/fnos-x5/native_media_launcher.py'


def _reviewed_command(root):
    unit = _destination(root, SOURCE_UNIT).resolve(strict=True)
    if not unit.is_relative_to(root) or hashlib.sha256(unit.read_bytes()).hexdigest() != SOURCE_UNIT_SHA256:
        raise ValueError('unreviewed native mediasrv service unit')
    commands = [line.partition('=')[2] for line in unit.read_text().splitlines()
                if line.startswith('ExecStart=')]
    if len(commands) != 1:
        raise ValueError('unreviewed native mediasrv ExecStart')
    dropins = _destination(root, Path('etc/systemd/system/mediasrv.service.d/90-fnos-vpu.conf')).parent
    if dropins.is_dir():
        for path in dropins.glob('*.conf'):
            if path.name != '90-fnos-vpu.conf' and any(
                    line.lstrip().startswith('ExecStart=') for line in path.read_text().splitlines()):
                raise ValueError('unreviewed mediasrv ExecStart override: ' + str(path))
    return shlex.split(commands[0])


def _launcher_dropin():
    return ('[Service]\nExecStart=\nExecStart=/usr/bin/python3 /' + LAUNCHER + '\n'
            'UnsetEnvironment=LD_PRELOAD FNOS_X5_VPU_ENABLE FNOS_X5_VPU_DECODE FNOS_X5_VPU_TRACE\n').encode()


def _launcher_source(*, native_codec, native_sha256, adapter, adapter_sha256,
                     home_environment, command, enable_decode):
    """Build a no-fork launcher; injected paths are also used by exec sandbox tests."""
    config = {'native_codec': native_codec, 'native_sha256': native_sha256,
              'adapter': adapter, 'adapter_sha256': adapter_sha256,
              'home_environment': home_environment, 'command': command,
              'enable_decode': bool(enable_decode)}
    return ('CONFIG = ' + repr(config) + '\n' + textwrap.dedent('''\
        import hashlib
        import os
        from pathlib import Path
        import re
        import sys

        def log(message):
            print('X5 VPU: ' + message, file=sys.stderr, flush=True)

        def digest(path):
            with Path(path).open('rb') as stream:
                return hashlib.file_digest(stream, 'sha256').hexdigest()

        env = os.environ.copy()
        # systemd UnsetEnvironment ensures none of these affected this launcher.
        # Also clear stale settings before deciding the real service's backend.
        for key in ('LD_PRELOAD', 'FNOS_X5_VPU_ENABLE', 'FNOS_X5_VPU_DECODE', 'FNOS_X5_VPU_TRACE'):
            env.pop(key, None)
        preload = []
        home = Path(CONFIG['home_environment'])
        if home.is_file():
            for line in home.read_text().splitlines():
                if line.startswith('LD_PRELOAD='):
                    for path in re.split(r'[:\\s]+', line.partition('=')[2]):
                        if not path:
                            continue
                        # Keep household policy, but never inherit a stale VPU library.
                        if 'libavcodec-hobot-' in Path(path).name:
                            log('discarded stale VPU entry in household environment')
                            continue
                        if not Path(path).is_absolute() or not Path(path).is_file():
                            log('household preload unavailable: ' + path)
                            continue
                        if path not in preload:
                            preload.append(path)
        reason = None
        try:
            if digest(CONFIG['native_codec']) != CONFIG['native_sha256']:
                reason = 'native codec ABI hash changed'
            elif digest(CONFIG['adapter']) != CONFIG['adapter_sha256']:
                reason = 'adapter hash changed'
        except OSError as exc:
            reason = 'runtime file unavailable: ' + str(exc)
        if reason is None:
            preload.append(CONFIG['adapter'])
            env['FNOS_X5_VPU_ENABLE'] = '1'
            if CONFIG['enable_decode']:
                env['FNOS_X5_VPU_DECODE'] = '1'
            log('verified adapter enabled for native mediasrv')
        else:
            log('native CPU fallback; ' + reason + '; service continues without VPU adapter')
        if preload:
            env['LD_PRELOAD'] = ':'.join(preload)
        os.execve(CONFIG['command'][0], CONFIG['command'], env)
        ''')).encode()


def stage_native_media(root: Path, library: Path, *, enable_decode=False):
    root = root.resolve()
    if root == Path('/') or not (root / 'etc/os-release').is_file():
        raise ValueError('offline root filesystem required')
    command = _reviewed_command(root)
    native = _destination(root, Path(NATIVE_CODEC)).resolve(strict=True)
    if not native.is_relative_to(root) or hashlib.sha256(native.read_bytes()).hexdigest() != NATIVE_SHA256:
        raise ValueError('unreviewed native libavcodec ABI')
    data = library.read_bytes()
    if (len(data) < 64 or data[:6] != b'\x7fELF\x02\x01'
            or int.from_bytes(data[16:18], 'little') != 3
            or int.from_bytes(data[18:20], 'little') != 183):
        raise ValueError('adapter must be an AArch64 shared library')
    digest = hashlib.sha256(data).hexdigest()
    if enable_decode:
        build = json.loads(library.with_suffix('.json').read_text())
        if (build.get('native_avcodec_sha256') != NATIVE_SHA256 or
                build.get('ffmpeg_commit') != '239f2c733de417201d7ad3b3b8b0d9b63285b2b1' or
                build.get('files', {}).get(library.name) != digest or
                not build.get('files', {}).get('hobotdec.c')):
            raise ValueError('hardware decode requires the matching reviewed n8.1.1 build manifest')
    target = 'usr/lib/fnos-x5/libavcodec-hobot-' + digest[:16] + '.so'
    preload = []
    home = _destination(root, Path('etc/fnos-x5/home-storage.env'))
    if home.is_file():
        for line in home.read_text().splitlines():
            if line.startswith('LD_PRELOAD='):
                value = line.partition('=')[2]
                for part in value.split(':'):
                    if not part.startswith('/usr/lib/fnos-x5/') or any(c.isspace() for c in part):
                        raise ValueError('unexpected household preload path')
                    if not _destination(root, Path(part.lstrip('/'))).is_file():
                        raise ValueError('household preload library missing')
                    preload.append(part)
    plan = {
        target: data,
        str(SHIM): (Path(__file__).parents[1] / 'native_media' / SHIM.name).read_bytes(),
        # Neutralise the obsolete preload file as well as removing its unit use.
        'etc/fnos-x5/native-media.env': b'# VPU environment is assigned only by native_media_launcher.py after ABI verification.\n',
        LAUNCHER: _launcher_source(native_codec='/' + NATIVE_CODEC,
            native_sha256=NATIVE_SHA256, adapter='/' + target, adapter_sha256=digest,
            home_environment='/etc/fnos-x5/home-storage.env', command=command, enable_decode=enable_decode),
        'etc/systemd/system/mediasrv.service.d/90-fnos-vpu.conf': _launcher_dropin(),
    }
    report = {'schema': 'fnos-native-media/v2', 'native_codec_sha256': NATIVE_SHA256,
              'source_unit': {'path': str(SOURCE_UNIT), 'sha256': SOURCE_UNIT_SHA256, 'argv': command},
              'abi_mismatch': 'native CPU fallback; household environment preserved; same PID via execve',
              'adapter_sha256': digest, 'capability': ('H264/HEVC encode; supported progressive 8-bit 4:2:0 decode on VPU; other formats native CPU'
                                                       if enable_decode else 'H264/HEVC encode; decode remains native CPU'),
              'files': [{'path': name, 'sha256': hashlib.sha256(payload).hexdigest()}
                        for name, payload in plan.items()]}
    plan[str(MANIFEST)] = (json.dumps(report, indent=2) + '\n').encode()
    for name in plan:
        if _destination(root, Path(name)).is_symlink():
            raise ValueError('symlink at native media destination: ' + name)
    for name, payload in plan.items():
        dst = root / name
        _prepare_parent(root, dst.parent)
        dst.write_bytes(payload)
        dst.chmod(0o644)
    verify_native_media(root, report)
    return report


def verify_native_media(root: Path, report: dict, *, require_root_owner=False):
    root = root.resolve()
    manifest = _destination(root, MANIFEST)
    if manifest.is_symlink() or json.loads(manifest.read_text()) != report:
        raise ValueError('native media manifest mismatch')
    if report.get('schema') == 'fnos-native-media/v2':
        if _reviewed_command(root) != report['source_unit']['argv']:
            raise ValueError('native mediasrv launch command mismatch')
    native = _destination(root, Path(NATIVE_CODEC)).resolve(strict=True)
    if not native.is_relative_to(root) or hashlib.sha256(native.read_bytes()).hexdigest() != report['native_codec_sha256']:
        raise ValueError('final native codec ABI mismatch')
    for row in report['files']:
        path = _destination(root, Path(row['path']))
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
            raise ValueError('native media payload mismatch: ' + row['path'])
    if require_root_owner:
        for path in [manifest, *(root / row['path'] for row in report['files'])]:
            while path != root:
                st = path.lstat()
                if st.st_uid or st.st_gid or st.st_mode & 0o022:
                    raise ValueError('unsafe native media runtime path')
                path = path.parent
    return {'verified_files': len(report['files']), 'native_codec': 'pinned hash matched'}
