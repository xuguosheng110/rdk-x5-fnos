"""Compare held-out quantized image features with exact native CPU references.

Build-server simulation only. A passing result still requires runtime-version,
board performance and native Photos task validation before activation.
"""
import argparse
import json
from pathlib import Path
import time


def main():
    import numpy as np
    from horizon_tc_ui import HB_ONNXRuntime
    from convert_semantic_onnx import digest

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inputs', type=Path, required=True)
    p.add_argument('--model', type=Path, required=True)
    p.add_argument('--report', type=Path, required=True)
    p.add_argument('--model-kind', choices=['quantized', 'floating'], default='quantized')
    a = p.parse_args()
    source = json.loads(a.inputs.read_text())
    held = [x for x in source['inputs'] if x['split'] == 'held_out']
    if len(held) != 28:
        raise ValueError('expected 28 images excluded from calibration')
    session = HB_ONNXRuntime(model_file=str(a.model), input_batch=1)
    if session.input_names != ['image'] or len(session.output_names) != 1:
        raise ValueError('unexpected quantized model interface')
    rows, native, quantized = [], [], []
    for index, item in enumerate(held):
        if digest(item['input']) != item['input_sha256']:
            raise ValueError('input hash mismatch')
        tensor = np.fromfile(item['input'], dtype='<f4').reshape(1, 3, 224, 224)
        before = time.monotonic()
        # The pinned hb_mapper 1.24.3 runtime uses run() for featuremap too.
        # Older public docs describe run_feature(), which this version lacks.
        feature = np.asarray(session.run(session.output_names, {'image':tensor})[0])
        elapsed = time.monotonic() - before
        expected = np.load(item['reference'], allow_pickle=False)
        if feature.shape != expected.shape or not np.isfinite(feature).all():
            raise ValueError('invalid simulated output: ' + item['name'])
        expected = expected.ravel().astype(np.float64)
        feature = feature.ravel().astype(np.float64)
        norm = float(np.linalg.norm(feature))
        if norm == 0:
            raise ValueError('zero simulated feature')
        native.append(expected / np.linalg.norm(expected))
        quantized.append(feature / norm)
        rows.append({'name':item['name'], 'cosine':float(np.dot(native[-1], quantized[-1])),
                     'feature_norm':norm, 'max_abs_error':float(np.max(np.abs(expected-feature))),
                     'server_simulation_s':elapsed})
        print(json.dumps({'sample':index + 1, **rows[-1]}), flush=True)
    native, quantized = np.asarray(native), np.asarray(quantized)
    top1 = float(np.mean(np.argmax(native @ quantized.T, axis=1) == np.arange(len(rows))))
    cosine = [x['cosine'] for x in rows]
    gates = source['gates']
    passed = (min(cosine) >= gates['held_out_cosine_min']
              and np.mean(cosine) >= gates['held_out_cosine_mean']
              and top1 >= gates['held_out_reference_retrieval_top1_min'])
    result = {'status':'held_out_simulation_passed' if passed else 'held_out_simulation_failed',
              'model_kind':a.model_kind,
              'model_sha256':digest(a.model), 'held_out_count':len(rows),
              'cosine_min':min(cosine), 'cosine_mean':float(np.mean(cosine)),
              'reference_retrieval_top1':top1, 'gates':gates, 'samples':rows,
              'scope':'public-image simulation only; not native Chinese retrieval accuracy or board acceptance'}
    result['evaluator_sha256'] = digest(__file__)
    a.report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k:v for k,v in result.items() if k!='samples'}), flush=True)
    raise SystemExit(0 if passed else 2)


if __name__ == '__main__':
    main()
