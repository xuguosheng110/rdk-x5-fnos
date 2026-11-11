import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from fnos.integration.native_photos import semantic768_production_probe as probe


class ProductionProbeTests(unittest.TestCase):
    def test_production_evidence_requires_actual_four_thread_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stdout.log'
            path.write_text('cpu_usage=50.0 [0, 1, 2, 3]\nnum_threads_onnx:4\nonnx_model:/models/image.onnx\n')
            result = probe.production_evidence(path, Path('/models/image.onnx'))
            self.assertEqual(result['effective_affinity'], [0, 1, 2, 3])
            path.write_text(path.read_text().replace('num_threads_onnx:4', 'num_threads_onnx:3'))
            with self.assertRaises(ValueError): probe.production_evidence(path, Path('/models/image.onnx'))

    def test_fixed_six_images_three_rounds_and_slower_bpu_retains_cpu(self):
        images = [{'path': '/image' + str(i)} for i in range(6)]
        baseline = {row['path']: np.eye(6, 768)[i] for i, row in enumerate(images)}
        tree = [{'pid': 10, 'start_ticks': 1, 'affinity': [0, 1, 2, 3],
                 'thread_affinities': {'10': [0, 1, 2, 3]}, 'cpu_s': 1, 'rss_bytes': 1024, 'threads': 4}]
        report = {'samples': [], 'progress': {'warmup_completed': 0}}
        clock = [0.0]
        current = ['cpu']
        def embed(endpoint, method, body, **kwargs):
            current[0] = endpoint
            return baseline[body['image']]
        def timer():
            # Every paired call has a start then end; CPU latency1, BPU2.
            timer.count += 1
            if timer.count % 2 == 0: clock[0] += 1 if current[0] == 'cpu' else 2
            return clock[0]
        timer.count = 0
        with patch.object(probe, 'process_tree', return_value=tree), patch.object(probe, 'embedding', side_effect=embed), \
             patch.object(probe, 'publish_report'), patch.object(probe, 'machine_state', return_value={}), \
             patch.object(probe.time, 'perf_counter', side_effect=timer):
            result = probe.measure(Mock(pid=10), Mock(pid=11), {'cpu': 'cpu', 'bpu': 'bpu'}, baseline, images, report, Path('/unused'))
        self.assertEqual(result, 0)
        self.assertEqual(len(report['samples']), 36)
        self.assertEqual(report['progress']['warmup_completed'], 6)
        self.assertEqual(report['recommendation'], 'retain_native_cpu_default')
        self.assertFalse(report['gates']['full_performance_sample_size'])
        self.assertFalse(report['gates']['product_acceptance'])
        self.assertEqual(report['timing']['cpu']['median_s'], 1)
        self.assertEqual(report['timing']['bpu']['median_s'], 2)

    def test_incomplete_screen_cannot_produce_summary(self):
        with self.assertRaises(ValueError): probe.summarize([])


if __name__ == '__main__': unittest.main()
