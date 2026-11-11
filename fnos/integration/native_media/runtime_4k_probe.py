"""4K 8-bit smoke: VPU encode/decode against independent native CPU decode.

Caller holds the board lock. Two 24-frame synthetic files; < 30 MiB storage,
usually < 2 minutes. This is a correctness probe, not 4K60 performance evidence.
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
    a = p.parse_args()
    work = Path(tempfile.mkdtemp(prefix='codec-4k-', dir=a.output_root))
    plain = os.environ.copy()
    for key in ('LD_PRELOAD', 'FNOS_X5_VPU_ENABLE', 'FNOS_X5_VPU_DECODE', 'FNOS_X5_VPU_TRACE'):
        plain.pop(key, None)
    plain['LD_LIBRARY_PATH'] = '/usr/trim/lib/mediasrv/lib'
    hw = dict(plain, LD_PRELOAD=str(a.library.resolve()))
    ff = '/usr/trim/lib/mediasrv/ffmpeg'
    results = []
    def run(cmd, name, env):
        started = time.monotonic()
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=90)
        (work / (name + '.log')).write_text(r.stdout + r.stderr)
        if r.returncode:
            raise RuntimeError(f'{name} failed ({r.returncode}); evidence: {work}')
        return r.stdout, time.monotonic() - started
    try:
        for codec in ('h264', 'hevc'):
            dest = work / (codec + '.ts')
            run([ff, '-v', 'info', '-f', 'lavfi', '-i', 'testsrc2=size=3840x2160:rate=25',
                 '-frames:v', '24', '-an', '-c:v', codec + '_hobot', '-b:v', '20M',
                 '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709',
                 str(dest)], codec + '-encode', hw)
            md5 = []
            elapsed = []
            for mode, env, decoder in (('cpu', plain, []), ('vpu', hw, ['-c:v', codec + '_hobot_dec'])):
                stdout, seconds = run([ff, '-v', 'error', '-xerror', *decoder, '-i', str(dest),
                    '-map', '0:v:0', '-an', '-pix_fmt', 'nv12', '-fps_mode', 'passthrough',
                    '-f', 'framemd5', '-'], codec + '-' + mode, env)
                rows = [line.strip() for line in stdout.splitlines() if line and not line.startswith('#')]
                md5.append(rows)
                elapsed.append(seconds)
            assert len(md5[0]) == len(md5[1]) == 24, [len(rows) for rows in md5]
            assert md5[0] == md5[1], f'{codec}: hardware pixels/timestamps differ from CPU'
            results.append(dict(codec=codec, width=3840, height=2160, frames=24,
                exact_pixels_and_timestamps=True, decode_seconds=dict(zip(('cpu', 'vpu'), elapsed)),
                verdict='PASS', performance_claim=False))
            print(json.dumps(results[-1]), flush=True)
    finally:
        (work / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
        print('EVIDENCE', work, flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
