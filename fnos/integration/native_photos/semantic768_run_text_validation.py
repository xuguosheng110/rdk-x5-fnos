"""Own one isolated native text service and compare measured image vectors.

No image inference is repeated and no product registration or index is changed.
The board owner invokes this only after the separate image benchmark finishes.
"""
import argparse
import errno
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time

from .semantic768_backend import NATIVE_TEXT_SHA256, NATIVE_TOKENIZER_SHA256, checked_digest
from .semantic768_benchmark import NativeProtocolError, native_health, publish_report, rpc_response
from .semantic768_runtime_identity import TEXT_ENTRY_SHA256, capture_receipt
from .semantic768_text_compare import load_vectors


def native_command(executable, model, tokenizer, endpoint, threads):
    if type(threads) is not int or not 1 <= threads <= 64:
        raise ValueError('invalid text ONNX thread budget')
    for path in (executable, model, tokenizer, endpoint):
        if not Path(path).is_absolute():
            raise ValueError('all native text paths must be absolute')
    return [str(executable), '--model_format', 'ONNX', '--onnx_model', str(model),
            '--sp', str(tokenizer), '--num_threads_onnx', str(threads),
            '--cpu_usage', '100', '--edition', 'domestic', '--socket_path', str(endpoint)]


def wait_ready(process, endpoint, row, save, *, timeout=120):
    started = time.monotonic()
    last_error = None
    while time.monotonic() - started < timeout:
        if process.poll() is not None:
            raise RuntimeError('controlled text service exited; inspect text.log')
        try:
            response = rpc_response(endpoint, 'healthCheck', {}, timeout=2, expected_pid=process.pid)
            row['last_health_response'] = response
            row['health_attempt_elapsed_s'] = time.monotonic() - started
            save()
            if native_health(response):
                row['startup_to_ready_s'] = time.monotonic() - started
                save()
                return
            last_error = 'native health reports false (initializing)'
        except NativeProtocolError as error:
            row['last_readiness_error'] = repr(error)
            save()
            raise
        except OSError as error:
            if not isinstance(error, TimeoutError) and error.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                raise
            last_error = repr(error)
        row['last_readiness_error'] = last_error
        save()
        time.sleep(.25)
    raise TimeoutError('text readiness timeout; last observation: ' + str(last_error))


def stop_owned(process):
    if process is None:
        return
    # Each child has its own session. The main product and unrelated native
    # endpoints are never selected by name, pid file or socket discovery.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('work-dir', 'native-executable', 'native-text-model', 'native-tokenizer',
                 'benchmark-report', 'queries-manifest'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--cpus', required=True)
    parser.add_argument('--threads', type=int, default=1)
    args = parser.parse_args()
    board = Path('/proc/device-tree/model').read_bytes().rstrip(b'\0').decode()
    if platform.machine() != 'aarch64' or 'RDK X5' not in board or os.geteuid() != 0:
        raise ValueError('isolated validation requires root on the actual RDK X5')
    cpus = {int(cpu) for cpu in args.cpus.split(',')}
    if not cpus or not cpus <= os.sched_getaffinity(0) or not 1 <= args.threads <= len(cpus):
        raise ValueError('invalid text CPU affinity or ONNX threads')
    for path, expected in ((args.native_executable, TEXT_ENTRY_SHA256),
                           (args.native_text_model, NATIVE_TEXT_SHA256),
                           (args.native_tokenizer, NATIVE_TOKENIZER_SHA256)):
        checked_digest(path, expected)
    queries = json.loads(args.queries_manifest.read_text())
    load_vectors(args.benchmark_report, queries)
    if not args.work_dir.is_absolute() or args.work_dir.parent == Path('/'):
        raise ValueError('use a new isolated validation subdirectory')
    endpoint = args.work_dir / 'text.sock'
    command = native_command(args.native_executable, args.native_text_model,
                             args.native_tokenizer, endpoint, args.threads)
    args.work_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    state = {'schema': 'fnos-semantic768-native-text-controller/v1', 'status': 'starting',
             'scope': 'isolated unlabelled Chinese CPU/BPU ranking consistency',
             'product_acceptance': False, 'image_inference_repeated': False,
             'board': board, 'affinity': sorted(cpus), 'num_threads_onnx': args.threads,
             'service': {'command': command, 'socket': str(endpoint)}}

    def save():
        publish_report(args.work_dir / 'state.json', state)

    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)

    prior = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    text_process = comparison = text_log = compare_log = None
    try:
        save()
        text_log = (args.work_dir / 'text.log').open('wb')
        text_process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=text_log,
                                        stderr=subprocess.STDOUT, start_new_session=True,
                                        preexec_fn=lambda: os.sched_setaffinity(0, cpus))
        state['service']['pid'] = text_process.pid
        save()
        wait_ready(text_process, endpoint, state['service'], save)
        receipt_path = args.work_dir / 'text-receipt.json'
        capture_receipt(text_process, list(text_process.args), endpoint, args.work_dir / 'text.log',
                        'text', receipt_path)
        state.update(status='comparing', receipt=str(receipt_path))
        save()
        compare_log = (args.work_dir / 'compare.log').open('wb')
        comparison = subprocess.Popen([sys.executable, '-m', 'fnos.integration.native_photos.semantic768_text_compare',
            '--benchmark-report', str(args.benchmark_report), '--queries-manifest', str(args.queries_manifest),
            '--text-socket', str(endpoint), '--text-receipt', str(receipt_path),
            '--report', str(args.work_dir / 'text-consistency.json')], stdin=subprocess.DEVNULL,
            stdout=compare_log, stderr=subprocess.STDOUT, start_new_session=True)
        state['comparison_pid'] = comparison.pid
        save()
        if comparison.wait(timeout=900):
            raise RuntimeError('native text comparison failed; inspect report and compare.log')
        result = json.loads((args.work_dir / 'text-consistency.json').read_text())
        if result.get('status') != 'native_chinese_result_consistency_pass':
            raise ValueError('native text comparison did not produce exact PASS evidence')
        state.update(status='unlabelled_native_chinese_consistency_pass',
                     top1_agreement=result['top1_agreement'], mean_top5_overlap=result['mean_top5_overlap'])
        return 0
    except BaseException as error:
        state.update(status='failed', error=repr(error))
        raise
    finally:
        # A second user interrupt must not strand the controlled native child.
        for sig in prior:
            signal.signal(sig, signal.SIG_IGN)
        try:
            try:
                stop_owned(comparison)
            finally:
                stop_owned(text_process)
            state['cleanup_complete'] = True
        finally:
            for stream in (text_log, compare_log):
                if stream is not None:
                    stream.close()
            try:
                save()
            finally:
                for sig, handler in prior.items():
                    signal.signal(sig, handler)


if __name__ == '__main__':
    raise SystemExit(main())
