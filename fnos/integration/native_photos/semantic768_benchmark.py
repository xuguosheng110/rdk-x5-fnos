"""Compare two isolated native image RPCs; never starts/stops product services.

Inputs: JSON {"images":[{"path":"/absolute/photo.jpg","sha256":"..."}]}.
Optional oracle_images adds image-mode edge cases to CPU-preprocessing parity.
Optional queries are {"text":"...","expected_images":[image paths]}; the
original domestic txt2vec socket is used unchanged. No index/database writes.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import random
import socket
import statistics
import struct
import time

from .semantic768_backend import MODEL_SHA256, WORKER_SHA256, NATIVE_IMAGE_SHA256, NATIVE_TEXT_SHA256, NATIVE_TOKENIZER_SHA256, checked_digest

NATIVE_TEXT_ENTRY_SHA256 = '4b45c00d32322a0c7023786055b4ceb47b196d3273f30afe2b3b11862d18c615'


class NativeProtocolError(RuntimeError):
    """An actual response contradicted the observed native RPC contract."""


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=60):
        super().__init__('localhost', timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def rpc_response(path, method, body, *, timeout=60, expected_pid=None):
    connection = UnixConnection(path, timeout=timeout)
    try:
        connection.connect()
        if expected_pid is not None:
            peer_pid = struct.unpack('3i', connection.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
            if peer_pid != expected_pid:
                raise NativeProtocolError('RPC socket peer PID does not match measured service')
        payload = json.dumps({'method': method, 'body': body}).encode()
        connection.request('POST', '/', body=payload, headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        raw = response.read(131073)
        if response.status != 200 or len(raw) > 131072:
            raise NativeProtocolError('native RPC HTTP ' + str(response.status) + ': ' + repr(raw[:300]))
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise NativeProtocolError('non-JSON native RPC response: ' + repr(raw[:300])) from error
        if not isinstance(result, dict) or type(result.get('code')) is not int or result['code'] != 0 or 'data' not in result:
            raise NativeProtocolError('native RPC returned an error: ' + repr(result)[:300])
        return result
    finally:
        connection.close()


def rpc(path, method, body, *, expected_pid=None):
    result = rpc_response(path, method, body, expected_pid=expected_pid)
    if not isinstance(result['data'], dict):
        raise NativeProtocolError('native inference RPC data is not an object: ' + repr(result)[:300])
    return result['data']


def native_health(response):
    # Real X5 evidence: semantic-native-health-protocol-r2.json, HTTP200,
    # {"code":0,"data":true}. Do not infer readiness from an arbitrary object.
    if (not isinstance(response, dict) or type(response.get('code')) is not int
            or response['code'] != 0 or type(response.get('data')) is not bool):
        raise NativeProtocolError('unrecognized native health response: ' + repr(response)[:300])
    return response['data']


def embedding(path, method, body, *, expected_pid=None):
    # Checking norm before normalizing catches accidental non-native output.
    import numpy as np
    vector = np.asarray(rpc(path, method, body, expected_pid=expected_pid)['embedding'], dtype=np.float32)
    if vector.shape != (768,) or not np.isfinite(vector).all():
        raise ValueError('native RPC must return 768 finite coordinates')
    if abs(float(np.linalg.norm(vector)) - 1) > 1e-5:
        raise ValueError('native RPC vector is not L2 normalized')
    return vector.astype(np.float64)


def process_tree(pid):
    """Read process and all live descendants; no ptrace/profiler in timed run."""
    records = {}
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            raw = (path / 'stat').read_text()
            fields = raw[raw.rfind(')') + 2:].split()
            records[int(path.name)] = (int(fields[1]), fields, path)
        except (OSError, ValueError):
            continue
    selected = {pid}
    while True:
        expanded = selected | {child for child, (parent, _, _) in records.items() if parent in selected}
        if expanded == selected:
            break
        selected = expanded
    if pid not in records:
        raise RuntimeError('benchmark service process exited')
    clock = os.sysconf('SC_CLK_TCK')
    items = []
    for child in sorted(selected):
        _, fields, path = records[child]
        status = dict(line.split(':', 1) for line in (path / 'status').read_text().splitlines())
        if int(status.get('TracerPid', '0')):
            raise RuntimeError('timed service is traced; remove strace/profiling')
        items.append({'pid': child, 'start_ticks': int(fields[19]),
                      'cpu_s': (int(fields[11]) + int(fields[12])) / clock,
                      'rss_bytes': int(fields[21]) * os.sysconf('SC_PAGE_SIZE'),
                      'threads': int(fields[17]), 'affinity': sorted(os.sched_getaffinity(child)),
                      'thread_affinities': {thread.name: sorted(os.sched_getaffinity(int(thread.name)))
                                            for thread in (path / 'task').iterdir()}})
    return items


def require_continuity(initial, current, budget):
    identities = lambda tree: {(row['pid'], row['start_ticks']) for row in tree}
    if identities(initial) != identities(current):
        raise RuntimeError('service process identity changed between timed requests')
    budget = set(budget)
    for row in current:
        if set(row['affinity']) != budget:
            raise RuntimeError('service leader CPU affinity changed during benchmark')
        if not row['thread_affinities'] or any(not values or not set(values) <= budget
                                              for values in row['thread_affinities'].values()):
            raise RuntimeError('a service thread escaped the common CPU budget')


def native_text_identity(path, receipt_path=None):
    from .semantic768_runtime_identity import validate_receipt
    if receipt_path is None:
        raise NativeProtocolError('native text requires a controlled launch receipt; rewritten argv is not evidence')
    return validate_receipt(receipt_path, 'text', path)


def cpu_delta(before, after):
    old = {(row['pid'], row['start_ticks']): row for row in before}
    new = {(row['pid'], row['start_ticks']): row for row in after}
    if set(old) != set(new):
        raise RuntimeError('process tree restarted during timing; repeat benchmark')
    return sum(new[key]['cpu_s'] - old[key]['cpu_s'] for key in old)


def machine_state():
    values = {}
    for pattern in ('/sys/class/thermal/thermal_zone*/temp',
                    '/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq'):
        import glob
        for name in glob.glob(pattern):
            try:
                values[name] = Path(name).read_text().strip()
            except OSError:
                pass
    return values


def publish_report(path, report):
    """A complete JSON snapshot after every request, never a partially read file."""
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    temporary.replace(path)


def main():
    import numpy as np
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('cpu-socket', 'bpu-socket', 'native-onnx', 'manifest', 'report'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--cpu-pid', type=int, required=True)
    parser.add_argument('--cpu-receipt', type=Path, required=True)
    parser.add_argument('--bpu-pid', type=int, required=True)
    parser.add_argument('--onnx-threads', type=int, default=1)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--warmup', type=int, default=5)
    parser.add_argument('--text-socket', type=Path)
    parser.add_argument('--text-receipt', type=Path)
    parser.add_argument('--verified-oracle', type=Path)
    args = parser.parse_args()
    if not 1 <= args.onnx_threads <= 64 or args.rounds < 1 or args.warmup < 1:
        parser.error('invalid thread/round/warmup count')
    if args.cpu_pid == args.bpu_pid or args.cpu_socket == args.bpu_socket:
        parser.error('CPU and BPU must be separate isolated service instances')
    manifest = json.loads(args.manifest.read_text())
    images = manifest['images']
    if not images or len({row['path'] for row in images}) != len(images):
        raise ValueError('image manifest empty or duplicated')
    for row in images + manifest.get('oracle_images', []):
        if not Path(row['path']).is_absolute():
            raise ValueError('absolute image paths required')
        checked_digest(row['path'], row['sha256'])
    checked_digest(args.native_onnx, NATIVE_IMAGE_SHA256)
    oracle, reused_oracle = None, None
    if args.verified_oracle:
        from .semantic768_evidence import verify_reuse
        reused_oracle = verify_reuse(args.verified_oracle, args.manifest, args.native_onnx, expected_threads=args.onnx_threads)
    else:
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.intra_op_num_threads = args.onnx_threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        oracle = ort.InferenceSession(str(args.native_onnx), sess_options=options, providers=['CPUExecutionProvider'])
        if oracle.get_inputs()[0].name != 'image' or len(oracle.get_outputs()) != 1:
            raise ValueError('unreviewed native CPU model interface')
    endpoints = {'cpu': args.cpu_socket, 'bpu': args.bpu_socket}
    pids = {'cpu': args.cpu_pid, 'bpu': args.bpu_pid}
    from .semantic768_runtime_identity import validate_receipt
    cpu_identity = validate_receipt(args.cpu_receipt, 'cpu', args.cpu_socket, expected_pid=args.cpu_pid)
    native_threads = cpu_identity['num_threads_onnx']
    if native_threads != args.onnx_threads:
        raise ValueError('native CPU and oracle ONNX thread counts differ')
    records, native_vectors, bpu_vectors, parity = [], {}, {}, []
    report = {'schema': 'fnos-native-semantic768-benchmark/v1', 'status': 'running',
              'scope': 'isolated native RPC; no Photos task/index changes; not full product acceptance',
              'images': len(images), 'rounds': args.rounds, 'warmup': args.warmup,
              'fixture_manifest_path': str(args.manifest.resolve()),
              'fixture_manifest_sha256': hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
              'thread_policy': 'explicit native ONNX threads; equal CPU affinity budget; SDK internal threads reported, not claimed equal',
              'native_onnx_threads': native_threads, 'pre_state': machine_state(), 'samples': records,
              'native_cpu_identity': cpu_identity,
              'progress': {'phase': 'warmup', 'completed_requests': 0, 'total_requests': 2 * args.rounds * len(images)}}
    if reused_oracle:
        report['reused_oracle'] = {key: value for key, value in reused_oracle.items() if key != 'samples'}
    publish_report(args.report, report)
    try:
        for index in range(args.warmup):
            row = images[index % len(images)]
            for name, endpoint in endpoints.items():
                embedding(endpoint, 'inferImg2Vec', {'image': row['path']}, expected_pid=pids[name])
                report['progress'].update(warmup_completed=2 * index + (1 if name == 'cpu' else 2), warmup_total=2 * args.warmup)
                publish_report(args.report, report)
        trees = {name: process_tree(pid) for name, pid in pids.items()}
        affinity = trees['cpu'][0]['affinity']
        if any(item['affinity'] != affinity for tree in trees.values() for item in tree):
            raise ValueError('all CPU/BPU service workers must have identical CPU affinity')
        for tree in trees.values():
            require_continuity(tree, tree, affinity)
        report['initial_process_trees'] = trees
        for iteration in range(args.rounds):
            shuffled = list(images)
            random.Random(90210 + iteration).shuffle(shuffled)
            for index, row in enumerate(shuffled):
                names = ['cpu', 'bpu'] if (index + iteration) % 2 == 0 else ['bpu', 'cpu']
                for name in names:
                    before = process_tree(pids[name])
                    require_continuity(trees[name], before, affinity)
                    start = time.perf_counter()
                    vector = embedding(endpoints[name], 'inferImg2Vec', {'image': row['path']}, expected_pid=pids[name])
                    elapsed = time.perf_counter() - start
                    after = process_tree(pids[name])
                    require_continuity(trees[name], after, affinity)
                    used = cpu_delta(before, after)
                    records.append({'backend': name, 'image': row['path'], 'round': iteration,
                                    'elapsed_s': elapsed, 'cpu_s': used, 'cpu_percent_one_core': 100 * used / elapsed,
                                    'rss_bytes': sum(p['rss_bytes'] for p in after),
                                    'threads': sum(p['threads'] for p in after)})
                    (native_vectors if name == 'cpu' else bpu_vectors)[row['path']] = vector
                    report['progress'].update(phase='timing', completed_requests=len(records),
                                              current_round=iteration + 1, current_backend=name, current_image=row['path'],
                                              latest_elapsed_s=elapsed, latest_cpu_s=used)
                    publish_report(args.report, report)
                print(json.dumps({'round': iteration, 'completed': index + 1, 'images': len(images)}), flush=True)
        # Oracle happens after timing, so its CPU work cannot pollute RPC CPU deltas.
        report['progress']['phase'] = 'checking_oracle_evidence' if reused_oracle else 'preprocessing_oracle'
        publish_report(args.report, report)
        if reused_oracle:
            parity = verify_reuse(args.verified_oracle, args.manifest, args.native_onnx, expected_threads=args.onnx_threads)['samples']
        else:
            from .semantic768_oracle import evaluate
            parity = evaluate(oracle, args.cpu_socket, images + manifest.get('oracle_images', []))
        native = np.stack([native_vectors[row['path']] for row in images])
        bpu = np.stack([bpu_vectors[row['path']] for row in images])
        cosine = np.sum(native * bpu, axis=1)
        retrieval = float(np.mean(np.argmax(bpu @ native.T, axis=1) == np.arange(len(images))))
        timing = {}
        for name in endpoints:
            rows = [row for row in records if row['backend'] == name]
            latencies = [row['elapsed_s'] for row in rows]
            timing[name] = {'median_s': statistics.median(latencies), 'p95_s': float(np.percentile(latencies, 95)),
                            'throughput_per_s': len(rows) / sum(latencies), 'cpu_s_per_image': statistics.mean(row['cpu_s'] for row in rows),
                            'peak_sampled_rss_bytes': max(row['rss_bytes'] for row in rows),
                            'observed_thread_counts': sorted({row['threads'] for row in rows})}
        report.update(timing=timing, median_speedup=timing['cpu']['median_s'] / timing['bpu']['median_s'],
                      throughput_ratio=timing['bpu']['throughput_per_s'] / timing['cpu']['throughput_per_s'],
                      cosine_min=float(cosine.min()), cosine_mean=float(cosine.mean()), reference_retrieval_top1=retrieval,
                      preprocessing_oracle=parity, post_state=machine_state())
        report['gates'] = {'preprocessing': all(row['passed'] for row in parity),
                           'features': float(cosine.min()) >= .99 and float(cosine.mean()) >= .995 and retrieval >= .95,
                           'performance_sample_size': len(images) >= 28 and args.rounds >= 3 and args.warmup >= 3,
                           'faster_end_to_end': report['median_speedup'] > 1 and report['throughput_ratio'] > 1}
        # Preserve the exact measured RPC vectors for subsequent native text
        # comparison, without running the 28 image encoders again. This is
        # outside all timed intervals and never writes a Photos index.
        vector_path = args.report.with_suffix('.vectors.npz')
        temporary = vector_path.with_name(vector_path.name + '.tmp')
        with temporary.open('wb') as stream:
            np.savez_compressed(stream, cpu=native.astype('<f4'), bpu=bpu.astype('<f4'))
        temporary.replace(vector_path)
        report['vectors'] = {'file': vector_path.name, 'sha256': hashlib.sha256(vector_path.read_bytes()).hexdigest(),
                             'images': images, 'dimensions': 768, 'source': 'last completed timed round original RPC coordinates',
                             'native_image_sha256': NATIVE_IMAGE_SHA256, 'bpu_model_sha256': MODEL_SHA256,
                             'worker_sha256': WORKER_SHA256}
        publish_report(args.report, report)
        queries = manifest.get('queries', [])
        report['native_text_status'] = 'not_run'
        if queries:
            if not args.text_socket:
                raise ValueError('query comparison requires preserved native text socket')
            queries_report = []
            identity = native_text_identity(args.text_socket, args.text_receipt)
            report['native_text_identity'] = identity
            for query in queries:
                if not isinstance(query.get('text'), str) or not query['text'].strip():
                    raise ValueError('query text must be nonempty')
                expected = query.get('expected_images', [])
                if expected and (not isinstance(expected, list) or not set(expected) <= {row['path'] for row in images}):
                    raise ValueError('query labels must refer to images in this corpus')
                if process_tree(identity['pid'])[0]['start_ticks'] != identity['process']['start_ticks']:
                    raise NativeProtocolError('native text process restarted')
                text = embedding(args.text_socket, 'inferTxt2Vec', {'txt': query['text']}, expected_pid=identity['pid'])
                old_rank = np.argsort(-(native @ text))
                new_rank = np.argsort(-(bpu @ text))
                old_top, new_top = images[int(old_rank[0])]['path'], images[int(new_rank[0])]['path']
                queries_report.append({'text': query['text'], 'cpu_top1': old_top, 'bpu_top1': new_top,
                                       'top1_agrees': old_top == new_top,
                                       'cpu_relevant': old_top in expected if expected else None,
                                       'bpu_relevant': new_top in expected if expected else None})
            current = native_text_identity(args.text_socket, args.text_receipt)
            for key in ('pid', 'executable_sha256', 'model_sha256', 'tokenizer_sha256'):
                if current[key] != identity[key]:
                    raise NativeProtocolError('native text identity changed during query comparison')
            if current['process']['start_ticks'] != identity['process']['start_ticks']:
                raise NativeProtocolError('native text process restarted')
            report['native_text_queries'] = queries_report
            report['gates']['text_top1_agreement'] = statistics.mean(row['top1_agrees'] for row in queries_report) >= .95
            labelled = all(row['cpu_relevant'] is not None for row in queries_report)
            report['native_text_status'] = 'labelled_engineering_queries' if labelled else 'unlabelled_consistency_only'
            if labelled:
                report['text_relevance_cpu'] = statistics.mean(row['cpu_relevant'] for row in queries_report)
                report['text_relevance_bpu'] = statistics.mean(row['bpu_relevant'] for row in queries_report)
                report['gates']['labelled_relevance_not_worse'] = report['text_relevance_bpu'] >= report['text_relevance_cpu']
        passed = all(report['gates'].values())
        report['product_acceptance'] = 'pending_native_photos_tasks_mixed_index_web_and_cold_boot'
        report['status'] = ('isolated_image_rpc_pass_text_pending' if not queries else 'isolated_rpc_pass_product_pending') if passed else 'isolated_rpc_gate_failed'
        report['progress']['phase'] = 'complete'
    except BaseException as error:
        report.update(status='failed', error=repr(error))
        raise
    finally:
        publish_report(args.report, report)
    print(json.dumps({key: value for key, value in report.items() if key not in ('samples', 'preprocessing_oracle')}, ensure_ascii=False))
    return int(not all(report['gates'].values()))


if __name__ == '__main__':
    raise SystemExit(main())
