"""Small production-budget screening, never a replacement for release gates.

Fixed six held-out images, three paired rounds. Original CPU has four ONNX
threads and both backends inherit cores 0-3, matching the observed X5 service.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import signal
import statistics
import subprocess
import sys
import time

from .semantic768_backend import MODEL_SHA256, WORKER_SHA256, NATIVE_IMAGE_SHA256, checked_digest
from .semantic768_entry import NATIVE_ENTRY_SHA256
from .semantic768_benchmark import embedding, process_tree, require_continuity, cpu_delta, machine_state, publish_report
from .semantic768_evidence import verify_reuse
from .semantic768_runtime_identity import capture_receipt, validate_receipt
from .semantic768_run_text_validation import wait_ready, stop_owned
from .semantic768_text_compare import load_vectors

INDICES = (0, 5, 10, 15, 20, 25)
CPUS = {0, 1, 2, 3}


def production_evidence(path, native_model):
    raw = path.read_bytes()
    text = raw.decode('utf-8')
    # This specific screening profile is backed by ai_manager's observed
    # 2026-11-10 launch, not by our wrapper's argparse defaults.
    required = ('cpu_usage=50.0', '[0, 1, 2, 3]', 'num_threads_onnx:4', str(native_model))
    if not all(value in text for value in required):
        raise ValueError('production log does not confirm observed 4-thread/cores0-3 Medium profile')
    return {'path': str(path.resolve()), 'sha256': hashlib.sha256(raw).hexdigest(),
            'original_cpu_usage_percent': 50, 'effective_affinity': sorted(CPUS), 'num_threads_onnx': 4}


def summarize(rows):
    import numpy as np
    result = {}
    for name in ('cpu', 'bpu'):
        selected = [row for row in rows if row['backend'] == name]
        if len(selected) != 18:
            raise ValueError('exactly 6 images x 3 rounds required for each backend')
        elapsed = [row['elapsed_s'] for row in selected]
        result[name] = {'median_s': statistics.median(elapsed), 'p95_s': float(np.percentile(elapsed, 95)),
                        'throughput_per_s': len(elapsed) / sum(elapsed),
                        'cpu_s_per_image': statistics.mean(row['cpu_s'] for row in selected),
                        'peak_sampled_rss_bytes': max(row['rss_bytes'] for row in selected),
                        'thread_counts': sorted({row['threads'] for row in selected})}
    return result


def measure(cpu, bpu, sockets, baseline, images, report, report_path):
    import numpy as np
    processes = {'cpu': cpu, 'bpu': bpu}
    trees = {name: process_tree(process.pid) for name, process in processes.items()}
    for tree in trees.values():
        require_continuity(tree, tree, CPUS)
    for index in range(3):
        for name in ('cpu', 'bpu'):
            embedding(sockets[name], 'inferImg2Vec', {'image': images[index]['path']}, expected_pid=processes[name].pid)
            report['progress']['warmup_completed'] += 1
            publish_report(report_path, report)
    vectors = {'cpu': {}, 'bpu': {}}
    for iteration in range(3):
        shuffled = list(images)
        random.Random(90210 + iteration).shuffle(shuffled)
        for index, image in enumerate(shuffled):
            names = ('cpu', 'bpu') if (index + iteration) % 2 == 0 else ('bpu', 'cpu')
            for name in names:
                process = processes[name]
                before = process_tree(process.pid)
                require_continuity(trees[name], before, CPUS)
                started = time.perf_counter()
                vector = embedding(sockets[name], 'inferImg2Vec', {'image': image['path']}, expected_pid=process.pid)
                elapsed = time.perf_counter() - started
                after = process_tree(process.pid)
                require_continuity(trees[name], after, CPUS)
                row = {'backend': name, 'image': image['path'], 'round': iteration,
                       'elapsed_s': elapsed, 'cpu_s': cpu_delta(before, after),
                       'rss_bytes': sum(p['rss_bytes'] for p in after), 'threads': sum(p['threads'] for p in after)}
                # Every CPU result also checks that changing ONNX threads has
                # not changed the already-oracle-verified native coordinates.
                if name == 'cpu':
                    reference = baseline[image['path']]
                    row['cpu_thread_parity_cosine'] = float(np.dot(reference, vector))
                    row['cpu_thread_parity_max_abs'] = float(np.max(np.abs(reference - vector)))
                    if row['cpu_thread_parity_cosine'] < .999999 or row['cpu_thread_parity_max_abs'] > 2e-5:
                        raise ValueError('production CPU threads changed native embedding beyond parity gate')
                vectors[name][image['path']] = vector
                report['samples'].append(row)
                report['progress'].update(phase='timing', completed=len(report['samples']), total=36)
                publish_report(report_path, report)
    native = np.stack([vectors['cpu'][image['path']] for image in images])
    candidate = np.stack([vectors['bpu'][image['path']] for image in images])
    cosine = np.sum(native * candidate, axis=1)
    top1 = float(np.mean(np.argmax(candidate @ native.T, axis=1) == np.arange(6)))
    timing = summarize(report['samples'])
    median_ratio = timing['cpu']['median_s'] / timing['bpu']['median_s']
    throughput_ratio = timing['bpu']['throughput_per_s'] / timing['cpu']['throughput_per_s']
    features = cosine.min() >= .99 and cosine.mean() >= .995 and top1 >= .95
    faster = median_ratio > 1 and throughput_ratio > 1
    report.update(timing=timing, median_speedup=median_ratio, throughput_ratio=throughput_ratio,
                  cosine_min=float(cosine.min()), cosine_mean=float(cosine.mean()), reference_retrieval_top1=top1,
                  gates={'six_image_features': bool(features), 'production_budget_faster': faster,
                         'full_performance_sample_size': False, 'product_acceptance': False},
                  recommendation='retain_native_cpu_default' if not faster or not features else 'candidate_needs_full_production_budget_and_product_acceptance',
                  status='screening_complete', post_state=machine_state())
    report['progress']['phase'] = 'complete'
    return 0 if features else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('work-dir', 'native-executable', 'native-onnx', 'native-rpc-dir', 'worker', 'model',
                 'manifest', 'queries-manifest', 'benchmark-report', 'verified-oracle', 'production-log'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    board = Path('/proc/device-tree/model').read_bytes().rstrip(b'\0').decode()
    if platform.machine() != 'aarch64' or 'RDK X5' not in board or os.geteuid() != 0 or not CPUS <= os.sched_getaffinity(0):
        raise ValueError('requires actual X5 root and cores0-3 available')
    for path, expected in ((args.native_executable, NATIVE_ENTRY_SHA256), (args.native_onnx, NATIVE_IMAGE_SHA256),
                           (args.worker, WORKER_SHA256), (args.model, MODEL_SHA256)):
        checked_digest(path, expected)
    profile = production_evidence(args.production_log, args.native_onnx)
    # The original 34-image preprocessing oracle is reused at its actual
    # recorded one-thread setting. Four-thread numerical parity is measured
    # separately above; it is not falsely called a new 34-image oracle.
    reused = verify_reuse(args.verified_oracle, args.manifest, args.native_onnx, expected_threads=1)
    source, all_images, cpu_vectors, _ = load_vectors(args.benchmark_report, json.loads(args.queries_manifest.read_text()))
    if source['fixture_manifest_sha256'] != hashlib.sha256(args.manifest.read_bytes()).hexdigest():
        raise ValueError('baseline benchmark and original oracle fixture manifests differ')
    images = [all_images[index] for index in INDICES]
    baseline = {all_images[index]['path']: cpu_vectors[index] for index in INDICES}
    if not args.work_dir.is_absolute() or args.work_dir.parent == Path('/'):
        raise ValueError('new isolated work subdirectory required')
    args.work_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    report_path = args.work_dir / 'probe.json'
    report = {'schema': 'fnos-semantic768-production-budget-screen/v1', 'status': 'starting',
              'scope': 'six-image screening; not full performance or product acceptance',
              'production_profile': profile, 'effective_service_cpu_usage': 100,
              'affinity_policy': 'pre-set effective cores0-3; cpu_usage100 prevents a second halving',
              'indices': list(INDICES), 'images': images, 'samples': [], 'services': [],
              'baseline_benchmark_sha256': hashlib.sha256(args.benchmark_report.read_bytes()).hexdigest(),
              'reused_oracle': {key: value for key, value in reused.items() if key != 'samples'},
              'pre_state': machine_state(), 'progress': {'phase': 'warmup', 'warmup_completed': 0, 'warmup_total': 6}}
    services, logs = [], []
    def save(): publish_report(report_path, report)
    def interrupted(signum, _frame): raise SystemExit(128 + signum)
    prior = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    sockets = {name: args.work_dir / (name + '.sock') for name in ('cpu', 'bpu')}
    commands = {'cpu': [str(args.native_executable), '--model_format', 'ONNX', '--onnx_model', str(args.native_onnx),
                       '--num_threads_onnx', '4', '--cpu_usage', '100', '--edition', 'domestic', '--socket_path', str(sockets['cpu'])],
                'bpu': [sys.executable, '-m', 'fnos.integration.native_photos.semantic768_service', '--worker', str(args.worker),
                       '--model', str(args.model), '--socket-path', str(sockets['bpu']), '--cpu-usage', '100',
                       '--native-rpc-dir', str(args.native_rpc_dir)]}
    for root in sorted({str(Path(image['path']).parent) for image in images}):
        commands['bpu'].extend(['--allowed-root', root])
    try:
        for name in ('cpu', 'bpu'):
            log_path = args.work_dir / (name + '.log')
            stream = log_path.open('wb'); logs.append(stream)
            process = subprocess.Popen(commands[name], stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                       start_new_session=True, preexec_fn=lambda: os.sched_setaffinity(0, CPUS))
            services.append(process)
            row = {'name': name, 'pid': process.pid, 'command': commands[name]}
            report['services'].append(row); save()
            wait_ready(process, sockets[name], row, save)
        receipt = args.work_dir / 'cpu-receipt.json'
        capture_receipt(services[0], commands['cpu'], sockets['cpu'], args.work_dir / 'cpu.log', 'cpu', receipt)
        report['native_cpu_identity'] = validate_receipt(receipt, 'cpu', sockets['cpu'], expected_pid=services[0].pid)
        result = measure(*services, sockets, baseline, images, report, report_path)
        validate_receipt(receipt, 'cpu', sockets['cpu'], expected_pid=services[0].pid)
        return result
    except BaseException as error:
        report.update(status='failed', error=repr(error)); raise
    finally:
        for sig in prior: signal.signal(sig, signal.SIG_IGN)
        try:
            failures = []
            for process in reversed(services):
                try: stop_owned(process)
                except Exception as error: failures.append(repr(error))
            report['cleanup_complete'] = not failures
            if failures: report['cleanup_errors'] = failures
            for stream in logs: stream.close()
            save()
            if failures: raise RuntimeError('isolated probe cleanup failed: ' + repr(failures))
        finally:
            for sig, handler in prior.items(): signal.signal(sig, handler)


if __name__ == '__main__':
    raise SystemExit(main())
