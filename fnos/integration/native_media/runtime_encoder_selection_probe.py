"""Real-board automatic encoder selection regression (< 2 min, < 10 MiB).

Run under the project's board lock. Synthetic files only; no service changes.
Requires matching native fnOS libavcodec. Asserts the actual encoder log and
decoded bitstream format, not just successful process exit.
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
    a = p.parse_args()
    work = Path(tempfile.mkdtemp(prefix='codec-selection-', dir=a.output_root))
    env = os.environ.copy()
    env.update(LD_PRELOAD=str(a.library.resolve()), FNOS_X5_VPU_ENABLE='1',
               FNOS_X5_VPU_DIAGNOSTICS='1', LD_LIBRARY_PATH='/usr/trim/lib/mediasrv/lib')
    env.pop('FNOS_X5_VPU_DECODE', None)
    plain = env.copy()
    plain.pop('LD_PRELOAD', None)
    ff = '/usr/trim/lib/mediasrv/ffmpeg'
    fp = '/usr/trim/lib/mediasrv/ffprobe'
    cases = [
        ('h264-8bit-vpu', 'libx264', 'yuv420p', True, []),
        ('hevc-8bit-vpu', 'libx265', 'yuv420p', True, []),
        # Generic codec names exercise FFmpeg's descriptor -> codec-ID lookup,
        # the route also used by mediasrv's generated blank/tail HLS segment.
        ('h264-id-8bit-vpu', 'h264', 'yuv420p', True, []),
        ('hevc-id-8bit-vpu', 'hevc', 'yuv420p', True, []),
        ('hevc-id-main10-pq-native', 'hevc', 'yuv420p10le', False,
         ['-vf', 'setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc',
          '-color_trc', 'smpte2084', '-color_primaries', 'bt2020', '-colorspace', 'bt2020nc']),
        ('hevc-main10-pq-native', 'libx265', 'yuv420p10le', False,
         ['-vf', 'setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc',
          '-color_trc', 'smpte2084', '-color_primaries', 'bt2020', '-colorspace', 'bt2020nc']),
        ('hevc-8bit-pq-native', 'libx265', 'yuv420p', False,
         ['-vf', 'setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc',
          '-color_trc', 'smpte2084', '-color_primaries', 'bt2020', '-colorspace', 'bt2020nc']),
        ('h264-444-native', 'libx264', 'yuv444p', False, []),
        ('h264-baseline-native', 'libx264', 'yuv420p', False, ['-profile:v', 'baseline']),
        ('h264-custom-tune-native', 'libx264', 'yuv420p', False, ['-tune', 'zerolatency']),
    ]
    results = []
    try:
        for name, codec, fmt, hardware, extra in cases:
            dest = work / (name + '.mkv')
            cmd = [ff, '-v', 'info', '-f', 'lavfi', '-i', 'testsrc2=size=640x480:rate=25',
                   '-frames:v', '5', '-an', '-c:v', codec, '-preset', 'ultrafast',
                   '-b:v', '2M', '-bf', '0', '-pix_fmt', fmt, *extra, str(dest)]
            run = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
            (work / (name + '.log')).write_text(run.stdout + run.stderr)
            if run.returncode:
                raise RuntimeError(f'{name}: encoder failed; evidence: {work}')
            actual_hardware = 'X5 VPU encoder active:' in run.stderr
            if actual_hardware != hardware:
                raise RuntimeError(f'{name}: expected hardware={hardware}, actual={actual_hardware}; evidence: {work}')
            probe = subprocess.run([fp, '-v', 'error', '-count_frames', '-select_streams', 'v:0',
                '-show_entries', 'stream=codec_name,profile,pix_fmt,nb_read_frames,color_transfer,color_primaries,color_space',
                '-of', 'json', str(dest)], env=plain, capture_output=True, text=True, timeout=30)
            (work / (name + '-probe.json')).write_text(probe.stdout + probe.stderr)
            if probe.returncode:
                raise RuntimeError(f'{name}: probe failed; evidence: {work}')
            stream = json.loads(probe.stdout)['streams'][0]
            assert stream['pix_fmt'] == fmt and stream['nb_read_frames'] == '5', stream
            if 'main10-pq' in name:
                assert stream['profile'] == 'Main 10' and stream['color_transfer'] == 'smpte2084', stream
            if 'pq-native' in name:
                assert stream['color_transfer'] == 'smpte2084' and stream['color_primaries'] == 'bt2020', stream
            if 'baseline' in name:
                assert stream['profile'] in ('Baseline', 'Constrained Baseline'), stream
            results.append(dict(name=name, hardware=actual_hardware, stream=stream, verdict='PASS'))
            print(json.dumps(results[-1]), flush=True)
    finally:
        (work / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
        print('EVIDENCE', work, flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
