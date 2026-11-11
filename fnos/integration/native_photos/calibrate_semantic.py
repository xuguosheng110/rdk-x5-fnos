"""Prepare public-image calibration and held-out inputs on the build server.

This numerical comparison pipeline does not claim native preprocessing or a
representative household accuracy benchmark. No calibration images are shipped
in the product image. Run with the pinned X5 toolchain after ONNX qualification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main():
    import numpy as np
    import onnxruntime as ort
    from PIL import Image
    import yaml

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--converted', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from convert_semantic_onnx import digest, SOURCE_SHA256
    if digest(args.source) != SOURCE_SHA256:
        raise ValueError('unexpected native source')
    if digest(args.converted) != '0b92873012c6270977a1e771f373c91e34850baad3e5ac9efe2d1688de8fdd61':
        raise ValueError('converted ONNX has not passed qualification')
    data = json.loads((args.dataset / 'manifest.json').read_text())
    if (data['archive_sha256'] != '61e5e3028863d8ffc3b81d6a514603954889f0edd5e4b44c4ce60b2da99aeb8e'
            or data['calibration_count'] != 100 or data['held_out_count'] != 28):
        raise ValueError('unreviewed calibration dataset')
    args.output.mkdir(exist_ok=False)
    for name in ('calibration_f32', 'held_out_f32', 'reference'):
        (args.output / name).mkdir()
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 4
    opts.inter_op_num_threads = 1
    session = ort.InferenceSession(str(args.source), opts, providers=['CPUExecutionProvider'])
    rows, hashes = [], set()
    for index, item in enumerate(data['images']):
        path = args.dataset / 'images' / item['name']
        if path.name != item['name'] or digest(path) != item['sha256']:
            raise ValueError('image path/hash mismatch')
        if item['sha256'] in hashes:
            raise ValueError('duplicate calibration/held-out image')
        hashes.add(item['sha256'])
        with Image.open(path) as image:
            tensor = np.asarray(image.convert('RGB').resize((224, 224), Image.Resampling.BICUBIC),
                                dtype=np.float32).transpose(2, 0, 1)[None] / np.float32(255)
        reference = session.run(['image_features'], {'image': tensor})[0]
        if reference.shape != (1, 768) or not np.isfinite(reference).all():
            raise ValueError('invalid native reference output')
        split = item['split']
        if split not in ('calibration', 'held_out'):
            raise ValueError('invalid split')
        input_path = args.output / (split + '_f32') / (path.stem + '.bin')
        tensor.astype('<f4').tofile(input_path)
        ref = args.output / 'reference' / (path.stem + '.npy')
        np.save(ref, reference, allow_pickle=False)
        rows.append({'name':item['name'], 'split':split, 'image_sha256':item['sha256'],
                     'input':str(input_path), 'input_sha256':digest(input_path), 'reference':str(ref)})
        if (index + 1) % 16 == 0:
            print(json.dumps({'prepared':index + 1, 'total':len(data['images'])}), flush=True)
    manifest = {'source_sha256':SOURCE_SHA256, 'converted_sha256':digest(args.converted),
                'dataset_manifest_sha256':digest(args.dataset / 'manifest.json'),
                'calibration_count':100, 'held_out_count':28, 'inputs':rows,
                'preprocessing':'PIL RGB/BICUBIC stretch224, float32 NCHW /255; model embeds -.5/.5',
                'scope':'public-image engineering calibration and numerical evaluation only',
                'gates':{'held_out_cosine_min':.99, 'held_out_cosine_mean':.995,
                         'held_out_reference_retrieval_top1_min':.95}}
    (args.output / 'inputs.json').write_text(json.dumps(manifest, indent=2) + '\n')
    config = {
        'model_parameters': {'onnx_model':str(args.converted), 'march':'bayes-e',
                             'output_model_file_prefix':'native_cn_image',
                             'working_dir':'/work/model', 'layer_out_dump':False},
        'input_parameters': {'input_name':'image', 'input_type_train':'featuremap',
                             'input_layout_train':'NCHW', 'input_shape':'1x3x224x224',
                             'input_type_rt':'featuremap', 'input_layout_rt':'NCHW',
                             'norm_type':'no_preprocess'},
        'calibration_parameters': {'cal_data_dir':str(args.output / 'calibration_f32'),
                                  'cal_data_type':'float32', 'preprocess_on':False,
                                  'calibration_type':'default'},
        'compiler_parameters': {'compile_mode':'latency', 'debug':False,
                                'optimize_level':'O3', 'core_num':1, 'jobs':4},
    }
    (args.output / 'quantize.yaml').write_text(yaml.safe_dump(config, sort_keys=False))
    print(json.dumps({'prepared':len(rows), 'calibration':100, 'held_out':28,
                      'config':str(args.output / 'quantize.yaml')}), flush=True)


if __name__ == '__main__':
    main()
