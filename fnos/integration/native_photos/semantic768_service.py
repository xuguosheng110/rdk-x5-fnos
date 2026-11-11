"""Use the native img2vec RPC transport; no registration/default activation."""
import argparse
import os
from pathlib import Path
import signal
import sys
import threading

from .semantic768_backend import Semantic768Backend, Semantic768Worker, SemanticStartupError
from .storage_roots import mounted_data_roots


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('worker', 'model', 'socket-path'):
        parser.add_argument('--' + name, required=True, type=Path)
    roots = parser.add_mutually_exclusive_group(required=True)
    roots.add_argument('--allowed-root', action='append', type=Path)
    roots.add_argument('--mounted-data-roots', action='store_true')
    parser.add_argument('--cpu-usage', type=float, default=100)
    parser.add_argument('--log-level', default='info')
    parser.add_argument('--native-rpc-dir', type=Path,
                        default=Path('/usr/trim/lib/ai_manager/com.trim.img2vec/cp311'))
    args = parser.parse_args()
    path = args.socket_path
    if not path.is_absolute() or path.exists() or path.is_symlink() or not path.parent.is_dir():
        parser.error('socket must be absolute, absent, and have an existing parent')
    if not 0 < args.cpu_usage <= 100:
        parser.error('invalid CPU budget')
    cpus = sorted(os.sched_getaffinity(0))
    # Apply the budget once before the warmup child exists. Transport receives
    # 100% of this existing affinity, so neither it nor fallback halves it twice.
    os.sched_setaffinity(0, cpus[:max(1, int(len(cpus) * args.cpu_usage / 100))])
    sys.path.insert(0, str(args.native_rpc_dir))
    try:
        from lib.http_service import HttpService
    except Exception as error:
        raise SemanticStartupError('native RPC runtime unavailable before binding socket') from error
    # HttpService owns SO_PEERCRED, wire protocol and readiness.
    native_argv = [sys.argv[0], '--socket_path', str(path), '--socket_mode', '600',
                   '--cpu_usage', '100', '--log_level', args.log_level]
    sys.argv = native_argv
    try:
        worker = Semantic768Worker(args.worker, args.model)
        backend = Semantic768Backend(worker, mounted_data_roots if args.mounted_data_roots else args.allowed_root)
    except Exception as error:
        raise SemanticStartupError('semantic artifact validation failed before binding socket') from error
    service, thread = None, None
    stop = threading.Event()
    try:
        try:
            worker.infer(bytes(worker.input_bytes))
            service = HttpService('trim.img2vec')
        except Exception as error:
            raise SemanticStartupError('768D model warmup failed before binding socket') from error

        def infer(body):
            try:
                return backend.infer(body), service.CODE_SUCCESS
            except PermissionError:
                return {}, service.CODE_FORBIDDEN
            except (ValueError, OSError):
                return {}, service.CODE_BAD_REQUEST
            except Exception:
                service.logger.exception('native 768D image inference failed')
                return {}, 50000

        service.append_handler_python('inferImg2Vec', infer)
        service.start_wrapper(native_argv)
        service.set_ready(True)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        thread = threading.Thread(target=service.run_in_loop, daemon=True)
        thread.start()
        while not stop.wait(.5):
            if not thread.is_alive():
                raise RuntimeError('native RPC loop stopped')
    finally:
        if service is not None:
            service.set_ready(False)
            # shutdown() waits for serve_forever; never call it before the loop.
            if thread is not None:
                service.shutdown()
                thread.join(timeout=5)
        worker.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
