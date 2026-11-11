"""Native fnOS Unix HTTP RPC transport around the reviewed X5 CLIP backend.

Opt-in development service. It does not replace native ai_manager model
registration or claim compatibility with the native domestic text model.
"""
import argparse
import os
from pathlib import Path
import signal
import sys
import threading

from .backend import ClipWorker, ImageBackend


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--worker', required=True, type=Path)
    p.add_argument('--model', required=True, type=Path)
    p.add_argument('--allowed-root', required=True, action='append', type=Path)
    p.add_argument('--socket-path', required=True, type=Path)
    p.add_argument('--native-rpc-dir', type=Path,
                   default=Path('/usr/trim/lib/ai_manager/com.trim.img2vec/cp311'))
    args = p.parse_args()
    if not args.socket_path.is_absolute() or args.socket_path.exists():
        p.error('socket path must be absolute and absent')
    if not args.socket_path.parent.is_dir():
        p.error('socket parent directory must already exist')
    # The installed ARM runtime owns wire serialization and SO_PEERCRED checks.
    sys.path.insert(0, str(args.native_rpc_dir))
    from lib.http_service import HttpService

    native_argv = [sys.argv[0], '--socket_path', str(args.socket_path), '--socket_mode', '600']
    sys.argv = native_argv
    worker = ClipWorker(args.worker, args.model)
    backend = ImageBackend(worker, args.allowed_root)
    stop = threading.Event()
    service = None
    thread = None
    try:
        # Prove the model/worker contract before exposing a healthy endpoint.
        worker.infer(bytes(3 * 224 * 224 * 4))
        service = HttpService('com.fnos.x5.img2vec')

        def infer(body):
            try:
                return backend.infer(body), service.CODE_SUCCESS
            except PermissionError:
                return {}, service.CODE_FORBIDDEN
            except (ValueError, FileNotFoundError, IsADirectoryError, NotADirectoryError):
                return {}, service.CODE_BAD_REQUEST
            except OSError:
                return {}, service.CODE_BAD_REQUEST
            except Exception:
                service.logger.exception('X5 BPU inference failed')
                return {}, 50000

        service.append_handler_python('inferImg2Vec', infer)
        service.start_wrapper(native_argv)
        service.set_ready(True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        thread = threading.Thread(target=service.run_in_loop, daemon=True)
        thread.start()
        while not stop.wait(0.5):
            if not thread.is_alive():
                raise RuntimeError('native RPC loop stopped')
    finally:
        if service is not None and thread is not None:
            service.set_ready(False)
            service.shutdown()
            thread.join(timeout=5)
        worker.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
