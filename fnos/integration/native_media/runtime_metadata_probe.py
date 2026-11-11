"""Real-board VUI acceptance and same-version native CPU control.

Raw H264 ffprobe r_frame_rate can report the field clock (2 ticks/frame), not
the frame cadence. Inspect actual SPS/VPS fields instead. --cpu-baseline runs
the original CLI flags and explicit frame setparams side by side, recording
showinfo before encoding; it never preloads the adapter. The unchanged old
probe is retained as runtime_metadata_probe_legacy.py.

Caller owns the board lock. Synthetic files only: VPU <90s/10MiB; CPU comparison
typically <4min/20MiB. CPU controls diagnose CLI/frame propagation, not hardware.
"""
import argparse
from fractions import Fraction
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def bitstream_rates(codec, trace):
    names = ('num_units_in_tick', 'time_scale') if codec == 'h264' else (
        'vps_num_units_in_tick', 'vps_time_scale', 'vui_num_units_in_tick', 'vui_time_scale')
    fields = {name: [int(x) for x in re.findall(r'(?<!\w)' + name + r'\s+[01]+\s*=\s*(\d+)', trace)]
              for name in names}
    rates = {}
    for label, numerator, denominator, ticks in (
            [('sps', 'time_scale', 'num_units_in_tick', 2)] if codec == 'h264' else
            [('vps', 'vps_time_scale', 'vps_num_units_in_tick', 1),
             ('sps_vui', 'vui_time_scale', 'vui_num_units_in_tick', 1)]):
        nums, dens = fields[numerator], fields[denominator]
        if not nums or len(nums) != len(dens) or any(n <= 0 for n in nums + dens):
            rates[label] = []
        else:
            rates[label] = sorted({str(Fraction(n, ticks * d)) for n, d in zip(nums, dens)})
    return fields, rates


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--library', type=Path)
    p.add_argument('--output-root', type=Path, required=True)
    p.add_argument('--require', action='store_true')
    p.add_argument('--cpu-baseline', action='store_true')
    a = p.parse_args()
    if not a.cpu_baseline and not a.library:
        p.error('--library required unless --cpu-baseline')
    if a.cpu_baseline and a.require:
        p.error('--cpu-baseline is a diagnostic before/after comparison; use --require for candidate acceptance')
    work = Path(tempfile.mkdtemp(prefix='codec-vui-', dir=a.output_root))
    plain = os.environ.copy()
    for key in ('LD_PRELOAD', 'FNOS_X5_VPU_ENABLE', 'FNOS_X5_VPU_DECODE', 'FNOS_X5_VPU_TRACE'):
        plain.pop(key, None)
    plain['LD_LIBRARY_PATH'] = '/usr/trim/lib/mediasrv/lib'
    env = plain.copy()
    if not a.cpu_baseline:
        env['LD_PRELOAD'] = str(a.library.resolve())
    ff = '/usr/trim/lib/mediasrv/ffmpeg'
    fp = '/usr/trim/lib/mediasrv/ffprobe'
    results = []
    def run(cmd, name, environ):
        r = subprocess.run(cmd, env=environ, capture_output=True, text=True, timeout=90)
        (work / (name + '.log')).write_text(r.stdout + r.stderr)
        if r.returncode:
            raise RuntimeError(f'{name} failed ({r.returncode}); evidence: {work}')
        return r.stdout, r.stderr
    try:
        modes = [('cpu-original', False), ('cpu-setparams', True)] if a.cpu_baseline else [('vpu-setparams', True)]
        for codec in ('h264', 'hevc'):
            for rate, value_range in (('25', 'tv'), ('30000/1001', 'pc')):
                for mode, set_frame_metadata in modes:
                    name = f'{codec}-{value_range}-{mode}'
                    dest = work / f'{name}.{codec}'
                    vf = 'setsar=16/15'
                    if set_frame_metadata:
                        vf += ',setparams=range=' + ('limited' if value_range == 'tv' else 'full')
                        vf += ':color_primaries=bt709:color_trc=bt709:colorspace=bt709'
                    vf += ',showinfo'
                    encoder = ('libx264' if codec == 'h264' else 'libx265') if a.cpu_baseline else codec + '_hobot'
                    _, encode_log = run([ff, '-v', 'verbose', '-f', 'lavfi', '-i',
                        f'testsrc2=size=720x576:rate={rate}', '-vf', vf, '-frames:v', '30',
                        '-an', '-c:v', encoder, '-b:v', '2M', '-color_range', value_range,
                        '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709',
                        '-f', codec, str(dest)], name, env)
                    raw, _ = run([fp, '-v', 'error', '-count_frames', '-select_streams', 'v:0',
                        '-show_entries', 'stream=codec_name,width,height,nb_read_frames,sample_aspect_ratio,display_aspect_ratio,color_range,color_space,color_transfer,color_primaries,r_frame_rate,avg_frame_rate',
                        '-of', 'json', str(dest)], name + '-probe', plain)
                    data = json.loads(raw)['streams'][0]
                    _, trace = run([ff, '-v', 'info', '-i', str(dest), '-map', '0:v:0',
                        '-c:v', 'copy', '-bsf:v', 'trace_headers', '-f', 'null', '-'], name + '-headers', plain)
                    fields, rates = bitstream_rates(codec, trace)
                    expected = dict(width=720, height=576, nb_read_frames='30',
                        sample_aspect_ratio='16:15', display_aspect_ratio='4:3', color_range=value_range,
                        color_space='bt709', color_transfer='bt709', color_primaries='bt709')
                    mismatches = {k: {'expected': v, 'actual': data.get(k)}
                                  for k, v in expected.items() if data.get(k) != v}
                    for label, actual in rates.items():
                        if actual != [str(Fraction(rate))]:
                            mismatches[label + '_frame_rate'] = {'expected': str(Fraction(rate)), 'actual': actual}
                    frame_metadata = sorted({line.split('color_range:', 1)[1] for line in encode_log.splitlines()
                                             if 'showinfo' in line and 'color_range:' in line})
                    if not frame_metadata:
                        mismatches['frame_metadata_evidence'] = {'expected': 'showinfo colour fields', 'actual': []}
                    results.append(dict(name=name, mode=mode, frame_metadata=frame_metadata,
                        stream=data, timing_fields=fields, bitstream_frame_rates=rates, mismatches=mismatches,
                        verdict='FAIL' if mismatches else 'PASS'))
                    print(json.dumps(results[-1]), flush=True)
    finally:
        (work / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
        print('EVIDENCE', work, flush=True)
    return int(a.require and any(r['mismatches'] for r in results))


if __name__ == '__main__':
    raise SystemExit(main())
