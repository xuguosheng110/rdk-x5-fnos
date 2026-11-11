"""True-board decoder checks against independent native CPU decode, including B frames.

Run under the board lock. Each run owns a fresh fixture directory; no service changes.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--library', required=True, type=Path)
    p.add_argument('--output-root', required=True, type=Path)
    p.add_argument('--fixtures', type=Path, help='reuse previously generated MP4 fixtures read-only')
    a = p.parse_args()
    work = Path(tempfile.mkdtemp(prefix='decoder-regression-', dir=a.output_root))
    plain = os.environ.copy()
    for name in ('LD_PRELOAD', 'FNOS_X5_VPU_ENABLE', 'FNOS_X5_VPU_DECODE'):
        plain.pop(name, None)
    plain['LD_LIBRARY_PATH'] = '/usr/trim/lib/mediasrv/lib'
    hw = dict(plain, LD_PRELOAD=str(a.library.resolve()))
    ff = '/usr/trim/lib/mediasrv/ffmpeg'
    results = []

    def run(args, name, env):
        start = time.monotonic()
        r = subprocess.run([ff, '-hide_banner', '-y', '-v', 'info', '-xerror', *args],
                           env=env, capture_output=True, text=True, timeout=120)
        (work / (name + '.log')).write_text(r.stdout + r.stderr)
        if r.returncode:
            raise RuntimeError(f'{name}: exit={r.returncode}; {work}')
        return round(time.monotonic() - start, 4)

    def frames(path):
        return [tuple(x.strip() for x in row.split(','))
                for row in path.read_text().splitlines() if row and not row.startswith('#')]

    try:
        for codec in ('h264', 'hevc'):
            for width, height in ((854, 480), (1920, 1080)):
                name = f'{codec}-{width}x{height}-bframes'
                src = (a.fixtures if a.fixtures else work) / (name + '.mp4')
                options = ['-c:v', 'libx264', '-preset', 'fast', '-bf', '3', '-g', '25']
                if codec == 'hevc':
                    options = ['-c:v', 'libx265', '-preset', 'fast', '-x265-params',
                               'bframes=3:keyint=25:log-level=error:pools=2:frame-threads=2']
                if a.fixtures:
                    if not src.is_file():
                        raise FileNotFoundError(src)
                else:
                    run(['-f', 'lavfi', '-i', f'testsrc2=size={width}x{height}:rate=25',
                         '-frames:v', '50', *options, str(src)], name + '-create', plain)
                for suffix in ('mp4', 'ts'):
                    source = src
                    if suffix == 'ts':
                        source = work / (name + '.ts')
                        run(['-i', str(src), '-c', 'copy', str(source)], name + '-remux', plain)
                    for mode, before in (('full', []), ('seek', ['-ss', '0.64']),
                                         ('loop', ['-stream_loop', '1'])):
                        tag = name + '-' + suffix + '-' + mode
                        cpu, vpu = work / (tag + '-cpu.md5'), work / (tag + '-vpu.md5')
                        after = ['-map', '0:v', '-an', '-pix_fmt', 'nv12', '-fps_mode', 'passthrough',
                                 '-f', 'framemd5']
                        run([*before, '-i', str(source), *after, str(cpu)], tag + '-cpu', plain)
                        elapsed = run([*before, '-c:v', codec + '_hobot_dec', '-i', str(source),
                                       *after, str(vpu)], tag + '-vpu', hw)
                        expected, actual = frames(cpu), frames(vpu)
                        assert len(actual) == len(expected) and len(actual) > 0, (tag, len(expected), len(actual))
                        # PTS, DTS, duration, size and MD5 must agree for every frame.
                        assert actual == expected, (tag, 'frame/timestamp mismatch',
                            next((i for i, (x, y) in enumerate(zip(expected, actual)) if x != y), None))
                        row = {'name': tag, 'frames': len(actual), 'pixel_and_timestamps': 'exact match',
                               'vpu_seconds': elapsed, 'verdict': 'PASS'}
                        results.append(row)
                        print(json.dumps(row), flush=True)
    finally:
        (work / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
        print('EVIDENCE', work, flush=True)


if __name__ == '__main__':
    main()
