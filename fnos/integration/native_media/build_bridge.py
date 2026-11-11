"""Cross-build the X5 libavcodec adapter on the designated Linux build server."""
import argparse
import hashlib
import json
import platform
from pathlib import Path
import subprocess

from patch_ffmpeg import PIN

def main():
    parser = argparse.ArgumentParser()
    for option in ('ffmpeg-source', 'sysroot', 'sdk-include', 'sdk-lib', 'output'):
        parser.add_argument('--' + option, required=True, type=Path)
    args = parser.parse_args()
    if platform.system() != 'Linux':
        parser.error('Compilation is restricted to the Linux build server')
    root, sysroot = args.ffmpeg_source.resolve(), args.sysroot.resolve()
    if subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip() != PIN:
        parser.error('FFmpeg source revision mismatch')
    if not (root / 'config.h').is_file():
        parser.error('Configure FFmpeg against the target sysroot first')
    here = Path(__file__).parent
    sources = [here / name for name in ('hobotenc.c', 'hobotdec.c', 'native_bridge.c')]
    output = args.output.resolve()
    command = ['aarch64-linux-gnu-gcc', '-O2', '-Wall', '-Wextra', '-Werror',
               '-Wno-missing-field-initializers', '-fPIC', '-shared',
               '--sysroot=' + str(sysroot), '-isystem', str(sysroot / 'usr/include/aarch64-linux-gnu'),
               '-isystem', str(sysroot / 'usr/include'), '-I' + str(root),
               '-I' + str(root / 'libavcodec'), '-I' + str(args.sdk_include.resolve()),
               *(str(p) for p in sources),
               '-L' + str(sysroot / 'usr/lib/aarch64-linux-gnu'), '-L' + str(args.sdk_lib.resolve()),
               '-Wl,-rpath-link,' + str(args.sdk_lib.resolve()), '-Wl,-rpath,/usr/hobot/lib',
               '-lmultimedia', '-ldl', '-o', str(output)]
    subprocess.run(command, check=True)
    hashes = {str(p.name): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources + [output]}
    manifest = {'schema': 1, 'ffmpeg_commit': PIN, 'native_avcodec_version': '62.28.101',
                'native_avcodec_sha256': 'cd8067ed571b436cb470b7733e953c705f0635a55c02b58c58dc8962b18708bc',
                'files': hashes, 'command': command,
                'compiler': subprocess.check_output(['aarch64-linux-gnu-gcc', '--version'], text=True).splitlines()[0]}
    output.with_suffix('.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'artifact': str(output), 'sha256': hashes[output.name]}))

if __name__ == '__main__':
    main()
