"""Real-board MKV/MP4 global-header regression; caller owns the board lock.

Each of five input frames has a unique grey level. Independent native CPU
decode must return exactly those five frames, in order, beginning with an IDR.
This detects a leaked black bootstrap packet or a consumed first user frame.
Four synthetic clips, normally <60s / 5MiB; no services or user files modified.
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
    a = p.parse_args()
    work = Path(tempfile.mkdtemp(prefix='codec-global-header-', dir=a.output_root))
    plain = os.environ.copy()
    for key in ('LD_PRELOAD', 'FNOS_X5_VPU_ENABLE', 'FNOS_X5_VPU_DECODE', 'FNOS_X5_VPU_TRACE'):
        plain.pop(key, None)
    plain['LD_LIBRARY_PATH'] = '/usr/trim/lib/mediasrv/lib'
    hardware = dict(plain, LD_PRELOAD=str(a.library.resolve()), FNOS_X5_VPU_ENABLE='1')
    ff = '/usr/trim/lib/mediasrv/ffmpeg'
    fp = '/usr/trim/lib/mediasrv/ffprobe'
    results = []
    try:
        for codec in ('h264', 'hevc'):
            for suffix in ('mkv', 'mp4'):
                name = codec + '-' + suffix
                dest = work / (name + '.' + suffix)
                encoder = 'libx264' if codec == 'h264' else 'libx265'
                cmd = [ff, '-v', 'verbose', '-f', 'lavfi', '-i',
                    'color=c=black:s=640x480:r=25,geq=lum=32+32*N:cb=128:cr=128',
                    '-frames:v', '5', '-an', '-pix_fmt', 'yuv420p', '-c:v', encoder,
                    '-preset', 'ultrafast', '-bf', '0', '-b:v', '2M', str(dest)]
                enc = subprocess.run(cmd, env=hardware, capture_output=True, timeout=60)
                (work / (name + '-encode.log')).write_bytes(enc.stdout + enc.stderr)
                if enc.returncode or b'X5 VPU encoder active:' not in enc.stderr:
                    raise RuntimeError(f'{name}: hardware encoding failed; evidence: {work}')
                dec = subprocess.run([ff, '-v', 'error', '-xerror', '-i', str(dest),
                    '-map', '0:v:0', '-an', '-pix_fmt', 'yuv420p', '-fps_mode', 'passthrough',
                    '-f', 'rawvideo', '-'], env=plain, capture_output=True, timeout=30)
                (work / (name + '-decode.log')).write_bytes(dec.stderr)
                frame_size = 640 * 480 * 3 // 2
                if dec.returncode or len(dec.stdout) != 5 * frame_size:
                    raise RuntimeError(f'{name}: expected exactly 5 decoded frames; bytes={len(dec.stdout)}; evidence: {work}')
                means = []
                for frame in range(5):
                    luma = dec.stdout[frame * frame_size:frame * frame_size + 640 * 480:64]
                    means.append(sum(luma) / len(luma))
                expected = [32 + 32 * i for i in range(5)]
                if any(abs(actual - target) > 3 for actual, target in zip(means, expected)):
                    raise RuntimeError(f'{name}: frame content/order changed: {means}; expected {expected}; evidence: {work}')
                probe = subprocess.run([fp, '-v', 'error', '-select_streams', 'v:0', '-show_frames',
                    '-show_entries', 'frame=key_frame,pict_type', '-of', 'json', str(dest)],
                    env=plain, capture_output=True, text=True, timeout=30)
                (work / (name + '-frames.json')).write_text(probe.stdout + probe.stderr)
                frames = json.loads(probe.stdout)['frames'] if not probe.returncode else []
                if len(frames) != 5 or frames[0]['key_frame'] != 1 or frames[0]['pict_type'] != 'I':
                    raise RuntimeError(f'{name}: first user frame is not an independent I picture; evidence: {work}')
                results.append(dict(name=name, verdict='PASS', hardware=True, decoded_frames=5,
                    expected_luma=expected, decoded_luma_mean=means, first_frame_key=True,
                    bootstrap_frame_leaked=False, user_frames_consumed=False))
                print(json.dumps(results[-1]), flush=True)
    finally:
        (work / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
        print('EVIDENCE', work, flush=True)


if __name__ == '__main__':
    main()
