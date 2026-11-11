"""Start isolated CPU/BPU RPCs, verify oracle, time fairly, then clean up.

The board owner runs this explicitly. It never changes ai_manager registration,
the live socket, model selection, storage, indices, power or boot settings.
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

from .semantic768_backend import (MODEL_SHA256, WORKER_SHA256, NATIVE_IMAGE_SHA256,
                                 NATIVE_TEXT_SHA256, NATIVE_TOKENIZER_SHA256, checked_digest)
from .semantic768_benchmark import rpc_response, native_health, NativeProtocolError
from .semantic768_entry import NATIVE_ENTRY_SHA256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('work-dir', 'native-executable', 'native-onnx', 'native-text-model',
                 'native-tokenizer', 'worker', 'model', 'manifest', 'native-rpc-dir'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--cpus', required=True, help='Explicit comma-separated logical CPU IDs')
    parser.add_argument('--threads', type=int, default=1)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--warmup', type=int, default=5)
    parser.add_argument('--oracle-only', action='store_true')
    parser.add_argument('--text-socket', type=Path)
    parser.add_argument('--text-receipt', type=Path)
    parser.add_argument('--verified-oracle', type=Path)
    args = parser.parse_args()
    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    board = Path('/proc/device-tree/model').read_bytes().rstrip(b'\0').decode()
    if platform.machine() != 'aarch64' or 'RDK X5' not in board or os.geteuid() != 0:
        raise ValueError('isolated validation requires root on the actual RDK X5')
    cpus = {int(cpu) for cpu in args.cpus.split(',')}
    if not cpus or not cpus <= os.sched_getaffinity(0) or not 1 <= args.threads <= len(cpus):
        raise ValueError('invalid/common CPU affinity or ONNX threads')
    for path, expected in ((args.native_executable, NATIVE_ENTRY_SHA256), (args.native_onnx, NATIVE_IMAGE_SHA256),
                           (args.native_text_model, NATIVE_TEXT_SHA256), (args.native_tokenizer, NATIVE_TOKENIZER_SHA256),
                           (args.worker, WORKER_SHA256), (args.model, MODEL_SHA256)):
        checked_digest(path, expected)
    if not args.work_dir.is_absolute() or args.work_dir.parent == Path('/'):
        raise ValueError('use an isolated test subdirectory')
    args.work_dir.mkdir(exist_ok=False, parents=True)
    images = json.loads(args.manifest.read_text())
    roots = sorted({str(Path(row['path']).parent) for row in images['images'] + images.get('oracle_images', [])})
    services, logs = [], []
    state = {'status': 'starting', 'board': board, 'affinity': sorted(cpus),
             'native_onnx_threads': args.threads, 'scope': 'isolated native semantic validation only', 'services': []}

    def save():
        (args.work_dir / 'state.json').write_text(json.dumps(state, indent=2) + '\n')

    def launch(name, command, path):
        log = (args.work_dir / (name + '.log')).open('wb')
        logs.append(log)
        started = time.monotonic()
        # Single-threaded parent; both servers and all their children inherit
        # precisely the same CPU budget, not an unverified vendor env knob.
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   preexec_fn=lambda: os.sched_setaffinity(0, cpus), start_new_session=True)
        services.append(process)
        row = {'name': name, 'pid': process.pid, 'socket': str(path), 'command': command}
        state['services'].append(row)
        save()
        deadline = started + 120
        last_error = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(name + ' startup failed; see service log')
            try:
                response = rpc_response(path, 'healthCheck', {}, timeout=2, expected_pid=process.pid)
                row['last_health_response'] = response
                row['health_attempt_elapsed_s'] = time.monotonic() - started
                save()
                if native_health(response):
                    row['startup_to_ready_s'] = time.monotonic() - started
                    save()
                    return process
                last_error = 'native health reports false (initializing)'
            except NativeProtocolError as error:
                row['last_readiness_error'] = repr(error)
                save()
                raise
            except OSError as error:
                # Only absence/refusal/timeout can indicate an initializing
                # local service. Permission and all protocol errors fail now.
                if not isinstance(error, TimeoutError) and error.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                    raise
                last_error = repr(error)
            row['last_readiness_error'] = last_error
            save()
            time.sleep(.25)
        raise TimeoutError(name + ' readiness timeout; last observation: ' + str(last_error))

    def run_phase(name, command, timeout):
        state['status'] = name
        save()
        with (args.work_dir / (name + '.log')).open('wb') as log:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        if result.returncode:
            raise RuntimeError(name + ' failed; inspect JSON report and log before continuing')

    cpu_socket, bpu_socket = args.work_dir / 'cpu.sock', args.work_dir / 'bpu.sock'
    try:
        cpu = launch('cpu', [str(args.native_executable), '--model_format', 'ONNX',
                     '--onnx_model', str(args.native_onnx), '--num_threads_onnx', str(args.threads),
                     '--cpu_usage', '100', '--edition', 'domestic', '--socket_path', str(cpu_socket)], cpu_socket)
        from .semantic768_runtime_identity import capture_receipt
        cpu_receipt = args.work_dir / 'cpu-receipt.json'
        capture_receipt(cpu, list(cpu.args), cpu_socket, args.work_dir / 'cpu.log', 'cpu', cpu_receipt)
        state['cpu_receipt'] = str(cpu_receipt)
        save()
        oracle_command = [sys.executable, '-m', 'fnos.integration.native_photos.semantic768_oracle',
                          '--cpu-socket', str(cpu_socket), '--native-onnx', str(args.native_onnx),
                          '--manifest', str(args.manifest), '--threads', str(args.threads),
                          '--report', str(args.work_dir / 'oracle.json')]
        if args.verified_oracle:
            from .semantic768_evidence import verify_reuse
            reused = verify_reuse(args.verified_oracle, args.manifest, args.native_onnx, expected_threads=args.threads)
            state['reused_oracle'] = {key: value for key, value in reused.items() if key != 'samples'}
            save()
        else:
            run_phase('preprocessing_oracle', oracle_command, 600)
        if not args.oracle_only:
            bpu_command = [sys.executable, '-m', 'fnos.integration.native_photos.semantic768_service',
                           '--worker', str(args.worker), '--model', str(args.model),
                           '--socket-path', str(bpu_socket), '--cpu-usage', '100',
                           '--native-rpc-dir', str(args.native_rpc_dir)]
            for root in roots:
                bpu_command.extend(['--allowed-root', root])
            bpu = launch('bpu', bpu_command, bpu_socket)
            benchmark = [sys.executable, '-m', 'fnos.integration.native_photos.semantic768_benchmark',
                         '--cpu-socket', str(cpu_socket), '--bpu-socket', str(bpu_socket),
                         '--cpu-pid', str(cpu.pid), '--bpu-pid', str(bpu.pid),
                         '--cpu-receipt', str(cpu_receipt),
                         '--native-onnx', str(args.native_onnx), '--manifest', str(args.manifest),
                         '--onnx-threads', str(args.threads), '--rounds', str(args.rounds), '--warmup', str(args.warmup),
                         '--report', str(args.work_dir / 'benchmark.json')]
            if args.text_socket:
                benchmark.extend(['--text-socket', str(args.text_socket)])
            if args.text_receipt:
                benchmark.extend(['--text-receipt', str(args.text_receipt)])
            if args.verified_oracle:
                benchmark.extend(['--verified-oracle', str(args.verified_oracle)])
            run_phase('fair_benchmark', benchmark, 3600)
        state['status'] = ('oracle_evidence_verified' if args.verified_oracle else 'oracle_only_pass') if args.oracle_only else (
            'isolated_validation_pass_product_pending' if images.get('queries') else 'isolated_image_validation_pass_text_pending')
        state['product_acceptance'] = False
        return 0
    except BaseException as error:
        state.update(status='failed', error=repr(error))
        raise
    finally:
        for process in reversed(services):
            # These are dedicated process groups created above. No product
            # PID or shared service receives a signal.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        for log in logs:
            log.close()
        state['cleanup_complete'] = True
        save()


if __name__ == '__main__':
    raise SystemExit(main())
