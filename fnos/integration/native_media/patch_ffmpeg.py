"""Apply X5 codecs to n8.1.1, matching the pinned fnOS 62.28.101 ABI."""
from pathlib import Path
import argparse
import shutil
import subprocess

PIN = '239f2c733de417201d7ad3b3b8b0d9b63285b2b1'

def main():
    p = argparse.ArgumentParser()
    p.add_argument('source', type=Path)
    a = p.parse_args()
    root = a.source.resolve()
    head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    if head != PIN:
        p.error('unexpected FFmpeg source revision')
    here = Path(__file__).parent
    for name in ('hobotenc.c', 'hobotdec.c'):
        shutil.copyfile(here / name, root / 'libavcodec' / name)
    add = {
        'libavcodec/allcodecs.c': '\nextern const FFCodec ff_h264_hobot_encoder;\nextern const FFCodec ff_hevc_hobot_encoder;\nextern const FFCodec ff_h264_hobot_decoder;\nextern const FFCodec ff_hevc_hobot_decoder;\n',
        'libavcodec/Makefile': '\nOBJS-$(CONFIG_H264_HOBOT_ENCODER) += hobotenc.o\nOBJS-$(CONFIG_HEVC_HOBOT_ENCODER) += hobotenc.o\nOBJS-$(CONFIG_H264_HOBOT_DECODER) += hobotdec.o\nOBJS-$(CONFIG_HEVC_HOBOT_DECODER) += hobotdec.o\n',
    }
    for name, content in add.items():
        target = root / name
        original = target.read_text()
        if name.endswith('allcodecs.c'):
            original = original.replace(content, '')
            anchor = '#include "libavcodec/codec_list.c"'
            if anchor not in original:
                raise ValueError('codec list insertion point not found')
            target.write_text(original.replace(anchor, content + anchor))
        elif content.strip() not in original:
            target.write_text(original + content)

if __name__ == '__main__':
    main()
