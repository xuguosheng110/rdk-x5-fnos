"""Short real-board VUI probe; no service, power, or user-library changes.

The caller owns the board lock. Raw elementary streams deliberately exclude
container metadata that could hide missing encoder VUI. Expected < 90 seconds
and < 10 MiB for both codecs on X5. --require enables candidate acceptance.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--library', type=Path, required=True)
    p.add_argument('--output-root', type=Path, required=True)
    p.add_argument('--require', action='store_true')
    a = p.parse_args()
    work = Path(tempfile.mkdtemp(prefix='codec-vui-', dir=a.output_root))
    env = os.environ.copy()
    env.update(LD_PRELOAD=str(a.library.resolve()),
               LD_LIBRARY_PATH='/usr/trim/lib/mediasrv/lib')
    for key in ('FNOS_X5_VPU_ENABLE', 'FNOS_X5_VPU_DECODE', 'FNOS_X5_VPU_TRACE'):
        env.pop(key, None)
    plain = env.copy()
    plain.pop('LD_PRELOAD', None)
    ff = '/usr/trim/lib/mediasrv/ffmpeg'
    fp = '/usr/trim/lib/mediasrv/ffprobe'
    results = []
    def run(cmd, name, environ):
        r = subprocess.run(cmd, env=environ, capture_output=True, text=True, timeout=45)
        (work / (name + '.log')).write_text(r.stdout + r.stderr)
        if r.returncode:
            raise RuntimeError(f'{name} failed ({r.returncode}); evidence: {work}')
        return r.stdout
    try:
        for codec, suffix in (('h264', 'h264'), ('hevc', 'hevc')):
            for rate, value_range, probe_range in (('25', 'tv', 'tv'), ('30000/1001', 'pc', 'pc')):
                name = f'{codec}-{value_range}'
                dest = work / f'{name}.{suffix}'
                run([ff, '-v', 'info', '-f', 'lavfi', '-i',
                     f'testsrc2=size=720x576:rate={rate}', '-vf', 'setsar=16/15',
                     '-frames:v', '30', '-an', '-c:v', codec + '_hobot',
                     '-b:v', '2M', '-color_range', value_range,
                     '-colorspace', 'bt709', '-color_primaries', 'bt709',
                     '-color_trc', 'bt709', '-f', suffix, str(dest)], name, env)
                data = json.loads(run([fp, '-v', 'error', '-count_frames', '-select_streams', 'v:0',
                    '-show_entries', 'stream=codec_name,width,height,nb_read_frames,sample_aspect_ratio,display_aspect_ratio,color_range,color_space,color_transfer,color_primaries,r_frame_rate,avg_frame_rate',
                    '-of', 'json', str(dest)], name + '-probe', plain))['streams'][0]
                expected = dict(width=720, height=576, nb_read_frames='30',
                    sample_aspect_ratio='16:15', display_aspect_ratio='4:3',
                    color_range=probe_range, color_space='bt709', color_transfer='bt709',
                    color_primaries='bt709', r_frame_rate=rate if '/' in rate else rate + '/1')
                mismatches = {k: {'expected': v, 'actual': data.get(k)}
                              for k, v in expected.items() if data.get(k) != v}
                results.append(dict(name=name, stream=data, mismatches=mismatches,
                                    verdict='FAIL' if mismatches else 'PASS'))
                print(json.dumps(results[-1]), flush=True)
    finally:
        (work / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
        print('EVIDENCE', work, flush=True)
    return int(a.require and any(r['mismatches'] for r in results))


if __name__ == '__main__':
    raise SystemExit(main())
