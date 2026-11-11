"""Qualify an opset-11 copy of the pinned native Chinese image encoder.

Build-server tool only. This does not quantize, deploy, or replace the native
model. Preserve the paired text encoder and all learned parameters. Numerical
equivalence here is a prerequisite for later BPU conversion, not an accuracy
or hardware acceptance result.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

SOURCE_SHA256 = '73a177536d297a3e3767ee1cd55f4ab9045bd0c2a4ac9a584fb77eaf3b7e1988'


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def lower_layer_norm(model):
    """Use ONNX's float32 standardization equations for the pinned graph."""
    import onnx
    from onnx import helper, TensorProto

    model = onnx.shape_inference.infer_shapes(model)
    types = {x.name: x.type.tensor_type.elem_type
             for x in [*model.graph.input, *model.graph.value_info]}
    names = {name for node in model.graph.node for name in [*node.input, *node.output]}
    nodes, replaced = [], 0
    for node in model.graph.node:
        if node.op_type != 'LayerNormalization':
            nodes.append(node)
            continue
        attrs = {a.name: helper.get_attribute_value(a) for a in node.attribute}
        if (node.domain not in ('', 'ai.onnx') or attrs.get('axis', -1) != -1
                or attrs.get('stash_type', 1) != 1 or len(node.input) != 3
                or len(node.output) != 1 or types.get(node.input[0]) != TensorProto.FLOAT
                or set(attrs) - {'axis', 'epsilon', 'stash_type'}):
            raise ValueError('unreviewed LayerNormalization contract: ' + node.name)
        prefix = f'fnos_semantic_ln_{replaced}_'
        labels = ['mean', 'centered', 'squared', 'variance', 'epsilon', 'adjusted',
                  'stddev', 'invstddev', 'normalized', 'scaled']
        value = {label: prefix + label for label in labels}
        if names.intersection(value.values()):
            raise ValueError('generated tensor name collision')
        names.update(value.values())
        x, scale, bias = node.input

        def add(op, inputs, output, **attributes):
            nodes.append(helper.make_node(op, inputs, [output], name=output, **attributes))

        add('ReduceMean', [x], value['mean'], axes=[-1], keepdims=1)
        add('Sub', [x, value['mean']], value['centered'])
        add('Mul', [value['centered']] * 2, value['squared'])
        add('ReduceMean', [value['squared']], value['variance'], axes=[-1], keepdims=1)
        add('Constant', [], value['epsilon'], value=helper.make_tensor(
            value['epsilon'], TensorProto.FLOAT, [], [attrs.get('epsilon', 1e-5)]))
        add('Add', [value['variance'], value['epsilon']], value['adjusted'])
        add('Sqrt', [value['adjusted']], value['stddev'])
        add('Reciprocal', [value['stddev']], value['invstddev'])
        add('Mul', [value['centered'], value['invstddev']], value['normalized'])
        add('Mul', [value['normalized'], scale], value['scaled'])
        add('Add', [value['scaled'], bias], node.output[0])
        replaced += 1
    if replaced != 50:
        raise ValueError('expected exactly 50 native LayerNormalization nodes')
    del model.graph.node[:]
    model.graph.node.extend(nodes)
    onnx.checker.check_model(model)
    return model, replaced


def lower_opset11(model):
    """Lower only reviewed float32/int64/bool graph patterns to opset 11.

    ONNX 1.12's version converter has no Where-16 downgrade adapter. For this
    pinned graph, version changes other than the four guarded cases below only
    add data types. Validate every actual input against the older schema too.
    In particular, Softmax-11 flattens dimensions after its axis: accepting an
    arbitrary axis here would silently change attention probabilities.
    """
    import copy
    import onnx
    from onnx import helper, numpy_helper, TensorProto

    model = onnx.shape_inference.infer_shapes(copy.deepcopy(model))
    if [(x.domain, x.version) for x in model.opset_import] != [('', 17)]:
        raise ValueError('expected standard opset 17 only')
    allowed = {'Constant', 'Sub', 'Div', 'Conv', 'Shape', 'Slice', 'Concat',
               'Reshape', 'Transpose', 'ConstantOfShape', 'Mul', 'Equal', 'Where',
               'Expand', 'Gather', 'Add', 'MatMul', 'Tile', 'Unsqueeze', 'Cast',
               'Sqrt', 'Softmax', 'Erf', 'ReduceL2', 'ReduceMean', 'Reciprocal'}
    values = {x.name: x.type.tensor_type
              for x in [*model.graph.input, *model.graph.value_info, *model.graph.output]}
    constants = {x.name: x for x in model.graph.initializer}
    for item in model.graph.initializer:
        values[item.name] = helper.make_tensor_type_proto(item.data_type, item.dims).tensor_type
    for node in model.graph.node:
        if node.op_type == 'Constant' and len(node.attribute) == 1 and node.attribute[0].name == 'value':
            constants[node.output[0]] = helper.get_attribute_value(node.attribute[0])
    edits = Counter()
    for node in model.graph.node:
        if node.domain or node.op_type not in allowed:
            raise ValueError('unreviewed operator: ' + node.op_type)
        attrs = {a.name: helper.get_attribute_value(a) for a in node.attribute}
        if node.op_type == 'Unsqueeze':
            if len(node.input) != 2 or attrs or node.input[1] not in constants:
                raise ValueError('Unsqueeze axes must be a constant tensor')
            axes = constants[node.input[1]]
            if axes.data_type != TensorProto.INT64 or len(axes.dims) != 1:
                raise ValueError('Unsqueeze axes must be int64 rank 1')
            axes = numpy_helper.to_array(axes).tolist()
            del node.input[1:]
            node.attribute.extend([helper.make_attribute('axes', axes)])
            edits['constant_unsqueeze_axes'] += 1
        elif node.op_type == 'Reshape':
            if set(attrs) - {'allowzero'} or attrs.get('allowzero', 0) != 0:
                raise ValueError('Reshape allowzero must be 0')
            del node.attribute[:]
            edits['reshape_default_zero_semantics'] += 1
        elif node.op_type == 'Shape':
            if attrs:
                raise ValueError('Shape start/end slicing is not supported')
        elif node.op_type == 'Softmax':
            tensor = values.get(node.input[0])
            rank = len(tensor.shape.dim) if tensor is not None else 0
            axis = attrs.get('axis', -1)
            if set(attrs) - {'axis'} or rank == 0 or axis not in (-1, rank - 1):
                raise ValueError('Softmax must use the last axis with known rank')
            del node.attribute[:]
            node.attribute.extend([helper.make_attribute('axis', rank - 1)])
            edits['softmax_explicit_last_axis'] += 1
        # Old-schema type constraints catch additions such as bfloat16 and
        # narrow integers; never silently retag unsupported inputs.
        schema = onnx.defs.get_schema(node.op_type, 11)
        constraints = {x.type_param_str: set(x.allowed_type_strs) for x in schema.type_constraints}
        for index, name in enumerate(node.input):
            if not name:
                continue
            tensor = values.get(name)
            if tensor is None or not tensor.elem_type:
                raise ValueError('unknown tensor type: ' + name)
            dtype = 'tensor(' + TensorProto.DataType.Name(tensor.elem_type).lower() + ')'
            formal = schema.inputs[min(index, len(schema.inputs) - 1)].typeStr
            if dtype not in constraints.get(formal, {formal}):
                raise ValueError('opset 11 does not support ' + dtype + ' for ' + node.op_type)
    model.opset_import[0].version = 11
    onnx.checker.check_model(model, full_check=True)
    return model, dict(edits)


def compare(source, converted, fixtures):
    import numpy as np
    import onnxruntime as ort
    from PIL import Image

    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    sessions = [ort.InferenceSession(str(p), options, providers=['CPUExecutionProvider'])
                for p in [source, converted]]
    rng = np.random.default_rng(42)
    cases = [(f'constant-{v}', np.full((1, 3, 224, 224), v, np.float32))
             for v in (0.0, 0.5, 1.0)]
    cases.extend((f'random-{n}', rng.random((1, 3, 224, 224), dtype=np.float32))
                 for n in range(3))
    fixture_hashes = {}
    for path in fixtures:
        # The model embeds (image - .5) / .5. Inputs remain RGB [0,1].
        # Resize here is only a shared numerical fixture, not an app preprocessor.
        with Image.open(path) as picture:
            tensor = np.asarray(picture.convert('RGB').resize((224, 224)), dtype=np.float32)
        cases.append((path.name, tensor.transpose(2, 0, 1)[None] / np.float32(255)))
        fixture_hashes[path.name] = digest(path)
    results = []
    for name, value in cases:
        a, b = [session.run(['image_features'], {'image': value})[0] for session in sessions]
        if a.shape != (1, 768) or b.shape != a.shape or not np.isfinite([a, b]).all():
            raise ValueError('invalid feature tensor: ' + name)
        error = float(np.max(np.abs(a - b)))
        cosine = float(np.dot(a.ravel().astype(np.float64), b.ravel()) /
                       (np.linalg.norm(a.astype(np.float64)) * np.linalg.norm(b.astype(np.float64))))
        results.append({'case': name, 'max_abs_error': error, 'cosine': cosine,
                        'passed': error <= 5e-5 and cosine >= 0.99999})
    return results, fixture_hashes


def main():
    import onnx
    import onnxruntime as ort

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--fixture', action='append', type=Path, default=[])
    args = parser.parse_args()
    if digest(args.source) != SOURCE_SHA256:
        raise ValueError('source model is not the reviewed native encoder')
    args.output_dir.mkdir(exist_ok=False)
    report_path = args.output_dir / 'qualification.json'
    report = {'source_sha256': SOURCE_SHA256, 'onnx': onnx.__version__,
              'onnxruntime': ort.__version__, 'status': 'started',
              'thresholds': {'max_abs_error': 5e-5, 'cosine_min': 0.99999},
              'scope': 'floating-point equivalence only; no BPU or native app acceptance'}
    try:
        source = onnx.load(args.source)
        onnx.checker.check_model(source)
        weights = {x.name: x.SerializeToString() for x in source.graph.initializer}
        lowered, count = lower_layer_norm(source)
        lowered_path = args.output_dir / 'image-opset17-decomposed.onnx'
        onnx.save(lowered, lowered_path)
        report['layer_norm_nodes_replaced'] = count
        report['layer_norm_equivalence'], report['fixture_hashes'] = compare(
            args.source, lowered_path, args.fixture)
        report_path.write_text(json.dumps(report, indent=2) + '\n')
        if not all(x['passed'] for x in report['layer_norm_equivalence']):
            raise ValueError('LayerNormalization decomposition changes native features')
        converted, report['opset_edits'] = lower_opset11(lowered)
        onnx.checker.check_model(converted)
        after = {x.name: x.SerializeToString() for x in converted.graph.initializer}
        if any(after.get(name) != value for name, value in weights.items()):
            raise ValueError('learned initializer changed during conversion')
        out = args.output_dir / 'image-opset11.onnx'
        onnx.save(converted, out)
        report.update(output=str(out), output_sha256=digest(out),
                      learned_initializers_preserved=len(weights),
                      ops=dict(Counter(x.op_type for x in converted.graph.node)))
        report['opset11_equivalence'], _ = compare(args.source, out, args.fixture)
        if not all(x['passed'] for x in report['opset11_equivalence']):
            raise ValueError('opset conversion changes native features')
        report['status'] = 'floating_point_equivalence_passed'
    except Exception as error:
        report.update(status='failed', error=str(error))
        raise
    finally:
        report_path.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
