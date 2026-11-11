"""Build a real PCI-core-enabled X5 kernel on the designated Linux server.

This produces isolated artifacts only. It never deploys, changes U-Boot or
mounts a fake PCI filesystem. The unchanged in-tree source is the previously
reviewed recovered-fix19 tree with its recorded compiler compatibility fixes.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess

SERVER_ROOT = Path('/data/disk1/rdk-x5-fnos-guosheng')


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--configuration', required=True, type=Path)
    parser.add_argument('--artifacts', required=True, type=Path)
    args = parser.parse_args()
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise RuntimeError('kernel builds run on the designated x86 Linux server')
    config = json.loads(args.configuration.read_text())
    source, build = Path(config['source']).resolve(), Path(config['build']).resolve()
    artifacts = args.artifacts.resolve()
    if not all(p.is_relative_to(SERVER_ROOT) for p in (source, build, artifacts)):
        raise ValueError('unexpected build location')
    if sha(build / '.config') != config['new_config_sha256']:
        raise ValueError('reviewed kernel config changed')
    artifacts.mkdir(parents=True, exist_ok=False)
    logs = artifacts / 'logs'
    logs.mkdir()
    state = {'pid': os.getpid(), 'source': str(source), 'build': str(build),
             'artifacts': str(artifacts), 'configuration': config,
             'start': datetime.now(timezone.utc).isoformat(), 'steps': []}
    def save():
        (artifacts / 'build-state.json').write_text(json.dumps(state, indent=2) + '\n')
    cmd = ['make', '-C', str(source), 'O=' + str(build), 'ARCH=arm64',
           'LLVM=1', 'CROSS_COMPILE=aarch64-linux-gnu-']
    steps = [('image', ['-j12', 'Image']), ('dtbs', ['-j12', 'dtbs']),
             ('modules', ['-j12', 'modules']),
             ('modules-install', ['-j12', 'INSTALL_MOD_PATH=' + str(artifacts / 'modules-root'), 'modules_install']),
             ('dtbs-install', ['-j1', 'INSTALL_DTBS_PATH=' + str(artifacts / 'dtbs-root'), 'dtbs_install'])]
    try:
        for name, tail in steps:
            state.update(status='running', step=name)
            save()
            print('kernel step: ' + name, flush=True)
            with (logs / (name + '.log')).open('w') as stream:
                result = subprocess.run(cmd + tail, stdout=stream, stderr=subprocess.STDOUT, timeout=2400)
            state['steps'].append({'name': name, 'rc': result.returncode})
            if result.returncode:
                raise RuntimeError('kernel build failed: ' + name)
        for src, dst in [('arch/arm64/boot/Image', 'Image-6.18.0'),
                         ('.config', 'config-6.18.0'), ('System.map', 'System.map-6.18.0')]:
            shutil.copyfile(build / src, artifacts / dst)
        (artifacts / 'dtbs').mkdir()
        dtbs = sorted((build / 'arch/arm64/boot/dts/hobot').glob('*.dtb'))
        if not dtbs:
            raise RuntimeError('X5 DTBs missing')
        for p in dtbs:
            shutil.copyfile(p, artifacts / 'dtbs' / p.name)
        for link in (artifacts / 'modules-root/lib/modules/6.18.0').glob('*'):
            if link.name in ('build', 'source') and link.is_symlink():
                link.unlink()
        state['outputs'] = {name: {'bytes': (artifacts / name).stat().st_size,
                                  'sha256': sha(artifacts / name)}
                            for name in ('Image-6.18.0', 'config-6.18.0', 'System.map-6.18.0')}
        state['dtbs'] = len(dtbs)
        state['modules'] = len(list((artifacts / 'modules-root').rglob('*.ko')))
        state.update(status='built_not_board_validated', end=datetime.now(timezone.utc).isoformat())
        save()
        print(json.dumps({k: state[k] for k in ('status', 'outputs', 'dtbs', 'modules')}), flush=True)
    except BaseException as error:
        state.update(status='failed', error=str(error), end=datetime.now(timezone.utc).isoformat())
        save()
        raise


if __name__ == '__main__':
    main()
