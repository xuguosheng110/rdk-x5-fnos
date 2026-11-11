"""Real-board regression: native fnOS libavcodec + X5 adapter, independent decode.

Creates only owned synthetic test media beneath the supplied output directory.
Use under the project board lock. No service changes, power, or disk deployment.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--library', required=True, type=Path)
    p.add_argument('--output-root', required=True, type=Path)
    p.add_argument('--quick', action='store_true')
    a = p.parse_args()
    work = Path(tempfile.mkdtemp(prefix='codec-regression-', dir=a.output_root))
    env = os.environ.copy()
    env['LD_PRELOAD'] = str(a.library.resolve())
    env['LD_LIBRARY_PATH'] = '/usr/trim/lib/mediasrv/lib'
    env.pop('FNOS_X5_VPU_ENABLE', None)
    env.pop('FNOS_X5_VPU_TRACE', None)
    plain = env.copy(); plain.pop('LD_PRELOAD', None)
    ff = '/usr/trim/lib/mediasrv/ffmpeg'
    fp = '/usr/trim/lib/mediasrv/ffprobe'
    results = []
    def run(command, name, environment):
        r = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=60)
        (work / (name + '.log')).write_text(r.stdout + r.stderr)
        if r.returncode:
            raise RuntimeError(f'{name} failed ({r.returncode}); evidence: {work}')
        return r.stdout
    try:
        for codec in ('h264', 'hevc'):
            for width, height in ([(854, 480)] if a.quick else [(640,480), (854,480), (1920,1080)]):
                for suffix in ('ts', 'mp4'):
                    name = f'{codec}-{width}x{height}-{suffix}'
                    dest = work / (name + '.' + suffix)
                    run([ff, '-v', 'warning', '-f', 'lavfi', '-i', f'testsrc2=size={width}x{height}:rate=25',
                         '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '2',
                         '-c:v', codec + '_hobot', '-b:v', '4M', '-g', '25', '-c:a', 'aac',
                         '-force_key_frames', 'expr:gte(t,n_forced*1)', str(dest)], name, env)
                    data = json.loads(run([fp, '-v', 'error', '-count_frames', '-show_entries',
                                           'stream=codec_name,codec_type,width,height,nb_read_frames,duration',
                                           '-of', 'json', str(dest)], name + '-probe', plain))
                    video = [s for s in data['streams'] if s['codec_type'] == 'video'][0]
                    audio = [s for s in data['streams'] if s['codec_type'] == 'audio'][0]
                    assert (video['width'], video['height'], int(video['nb_read_frames'])) == (width, height, 50), video
                    assert audio['codec_name'] == 'aac'
                    run([ff, '-v', 'error', '-xerror', '-i', str(dest), '-f', 'null', '-'], name + '-decode', plain)
                    results.append({'name': name, 'video': video, 'audio': audio, 'verdict': 'PASS'})
                    print('PASS', name, flush=True)
    finally:
        (work / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
        print('EVIDENCE', work, flush=True)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
