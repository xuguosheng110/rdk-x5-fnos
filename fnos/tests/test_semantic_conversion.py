"""Run in the pinned X5 conversion container on the build server."""
import copy
import unittest

try:
    import numpy as np
    import onnx
    import onnxruntime as ort
    from onnx import TensorProto as T, helper as h, numpy_helper
except ImportError as error:
    raise unittest.SkipTest('requires the build-server X5 ONNX toolchain') from error

from fnos.integration.native_photos.convert_semantic_onnx import lower_opset11


def model(nodes, inputs, output_shape, initializers=(), output_type=T.FLOAT):
    graph = h.make_graph(nodes, 'version-regression', inputs,
                         [h.make_tensor_value_info('y', output_type, output_shape)],
                         initializer=initializers)
    return h.make_model(graph, opset_imports=[h.make_opsetid('', 17)], ir_version=8)


def run(graph, feeds):
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    return ort.InferenceSession(graph.SerializeToString(), options,
                                providers=['CPUExecutionProvider']).run(None, feeds)[0]


class SemanticVersionTests(unittest.TestCase):
    def softmax(self, **attrs):
        return model([h.make_node('Softmax', ['x'], ['y'], **attrs)],
                     [h.make_tensor_value_info('x', T.FLOAT, [2, 3, 4])], [2, 3, 4])

    def test_naive_version_change_alters_attention(self):
        graph = self.softmax()
        bad = copy.deepcopy(graph)
        bad.opset_import[0].version = 11
        x = np.arange(24, dtype=np.float32).reshape(2, 3, 4) / 8
        self.assertGreater(float(np.max(np.abs(run(graph, {'x': x}) - run(bad, {'x': x})))), .1)
        converted, _ = lower_opset11(graph)
        np.testing.assert_allclose(run(graph, {'x': x}), run(converted, {'x': x}), atol=1e-7)
        self.assertEqual(graph.opset_import[0].version, 17)

    def test_explicit_last_axis(self):
        for axis in (-1, 2):
            graph = self.softmax(axis=axis)
            converted, _ = lower_opset11(graph)
            x = np.random.default_rng(1).normal(size=(2, 3, 4)).astype(np.float32)
            np.testing.assert_allclose(run(graph, {'x': x}), run(converted, {'x': x}), atol=1e-7)

    def test_other_softmax_axis_rejected(self):
        with self.assertRaisesRegex(ValueError, 'last axis'):
            lower_opset11(self.softmax(axis=1))

    def test_constant_unsqueeze_axes_preserve_dimensions(self):
        axes = numpy_helper.from_array(np.array([0, -1], dtype=np.int64), 'axes')
        graph = model([h.make_node('Unsqueeze', ['x', 'axes'], ['y'])],
                      [h.make_tensor_value_info('x', T.FLOAT, [2, 3])], [1, 2, 3, 1], [axes])
        converted, _ = lower_opset11(graph)
        x = np.arange(6, dtype=np.float32).reshape(2, 3)
        np.testing.assert_array_equal(run(graph, {'x': x}), run(converted, {'x': x}))

    def test_dynamic_unsqueeze_rejected(self):
        graph = model([h.make_node('Unsqueeze', ['x', 'axes'], ['y'])],
                      [h.make_tensor_value_info('x', T.FLOAT, [2, 3]),
                       h.make_tensor_value_info('axes', T.INT64, [1])], [1, 2, 3])
        with self.assertRaisesRegex(ValueError, 'constant tensor'):
            lower_opset11(graph)

    def test_reshape_zero_copies_dimension(self):
        shape = numpy_helper.from_array(np.array([0, -1], dtype=np.int64), 'shape')
        graph = model([h.make_node('Reshape', ['x', 'shape'], ['y'], allowzero=0)],
                      [h.make_tensor_value_info('x', T.FLOAT, [2, 3, 4])], [2, 12], [shape])
        converted, _ = lower_opset11(graph)
        x = np.arange(24, dtype=np.float32).reshape(2, 3, 4)
        np.testing.assert_array_equal(run(graph, {'x': x}), run(converted, {'x': x}))

    def test_reshape_allowzero_one_rejected(self):
        shape = numpy_helper.from_array(np.array([0, 3], dtype=np.int64), 'shape')
        graph = model([h.make_node('Reshape', ['x', 'shape'], ['y'], allowzero=1)],
                      [h.make_tensor_value_info('x', T.FLOAT, [0, 3])], [0, 3], [shape])
        with self.assertRaisesRegex(ValueError, 'allowzero'):
            lower_opset11(graph)

    def test_shape_slicing_rejected(self):
        graph = model([h.make_node('Shape', ['x'], ['y'], start=1)],
                      [h.make_tensor_value_info('x', T.FLOAT, [2, 3])], [1], output_type=T.INT64)
        with self.assertRaisesRegex(ValueError, 'start/end'):
            lower_opset11(graph)

    def test_where_broadcast_keeps_values(self):
        for dtype, numpy_type in [(T.FLOAT, np.float32), (T.INT64, np.int64)]:
            graph = model([h.make_node('Where', ['condition', 'x', 'z'], ['y'])],
                          [h.make_tensor_value_info('condition', T.BOOL, [2, 1]),
                           h.make_tensor_value_info('x', dtype, [2, 3]),
                           h.make_tensor_value_info('z', dtype, [])], [2, 3], output_type=dtype)
            feeds = {'condition': np.array([[True], [False]]),
                     'x': np.arange(6, dtype=numpy_type).reshape(2, 3),
                     'z': np.array(42, dtype=numpy_type)}
            converted, _ = lower_opset11(graph)
            np.testing.assert_array_equal(run(graph, feeds), run(converted, feeds))

    def test_bfloat16_rejected(self):
        graph = model([h.make_node('Add', ['x', 'x'], ['y'])],
                      [h.make_tensor_value_info('x', T.BFLOAT16, [2])], [2], output_type=T.BFLOAT16)
        with self.assertRaisesRegex(ValueError, 'does not support'):
            lower_opset11(graph)


if __name__ == '__main__':
    unittest.main()
