"""Prepare immutable held-out JPEGs and mode probes; no board or product I/O."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def main():
    import numpy as np
    from PIL import Image
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--calibration-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--target-root', type=Path, required=True)
    args = parser.parse_args()
    if not args.target_root.is_absolute():
        parser.error('target root must be absolute')
    args.output.mkdir(exist_ok=False, parents=True)
    source = json.loads((args.calibration_dir / 'manifest.json').read_text())
    held = [row for row in source['images'] if row['split'] == 'held_out']
    if len(held) != 28:
        raise ValueError('expected original 28 held-out images')
    images, probes = [], []

    def record(name):
        data = (args.output / name).read_bytes()
        return {'path': str(args.target_root / name), 'sha256': hashlib.sha256(data).hexdigest()}

    for row in held:
        file = args.calibration_dir / 'images' / row['name']
        if hashlib.sha256(file.read_bytes()).hexdigest() != row['sha256']:
            raise ValueError('source fixture mismatch')
        shutil.copyfile(file, args.output / row['name'])
        images.append(record(row['name']))
    # Distinct colors and alpha reveal stretch/crop, palette, channel and
    # premultiplied-alpha differences that a solid RGB image cannot reveal.
    y, x = np.mgrid[0:113, 0:317]
    rgb = np.stack(((x * 7) % 256, (y * 5) % 256, (x + y * 3) % 256), axis=-1).astype('uint8')
    base = Image.fromarray(rgb, 'RGB')
    modes = [('rgb.png', base), ('gray.png', base.convert('L')),
             ('palette.png', base.convert('P', palette=Image.Palette.ADAPTIVE)),
             ('cmyk.jpg', base.convert('CMYK'))]
    rgba = np.concatenate((rgb, ((x * 3 + y * 5) % 256).astype('uint8')[:, :, None]), axis=-1)
    modes.append(('rgba.png', Image.fromarray(rgba, 'RGBA')))
    for name, image in modes:
        name = 'oracle-' + name
        image.save(args.output / name)
        probes.append(record(name))
    exif = base.getexif()
    exif[274] = 6
    base.save(args.output / 'oracle-exif6.jpg', exif=exif)
    probes.append(record('oracle-exif6.jpg'))
    manifest = {'schema': 'fnos-semantic768-native-fixtures/v1', 'images': images,
                'oracle_images': probes,
                'scope': 'engineering held-out performance plus synthetic preprocessing parity; no household retrieval accuracy claim'}
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'images': len(images), 'oracle_images': len(probes), 'output': str(args.output)}))


if __name__ == '__main__':
    main()
