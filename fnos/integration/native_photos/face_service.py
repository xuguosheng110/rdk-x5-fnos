"""Native Unix RPC face pipeline with versioned detector and FaceID backends.

Development entry; native task-manager registration and index migration are
separate gates. Never register this under an existing embedding model silently.
"""
import argparse
import os
from pathlib import Path
import signal
import sys
import threading

from .face import InsightFaceWorker, FaceId128Worker
from .face_backend import FaceBackend
from .storage_roots import mounted_data_roots


def main():
    parser = argparse.ArgumentParser()
    for name in ('worker', 'model', 'det-model', 'socket-path'):
        parser.add_argument('--' + name, required=True, type=Path)
    roots = parser.add_mutually_exclusive_group(required=True)
    roots.add_argument('--allowed-root', action='append', type=Path)
    roots.add_argument('--mounted-data-roots', action='store_true')
    parser.add_argument('--confidence', type=float, default=0.65)
    parser.add_argument('--num-threads', type=int, default=3)
    parser.add_argument('--cpu-usage', type=float, default=50)
    parser.add_argument('--encoder', choices=['insightface', 'faceid128'], default='insightface')
    parser.add_argument('--detector', choices=['native-cpu', 'official-x5'], default='native-cpu')
    parser.add_argument('--detection-worker', type=Path)
    parser.add_argument('--box-model', type=Path)
    parser.add_argument('--landmark-model', type=Path)
    parser.add_argument('--native-rpc-dir', type=Path,
                        default=Path('/usr/trim/lib/ai_manager/com.trim.face_det/cp311'))
    args = parser.parse_args()
    if not 0 < args.confidence < 1:
        parser.error('confidence must be between zero and one')
    if not 1 <= args.num_threads <= 64 or not 0 < args.cpu_usage <= 100:
        parser.error('invalid CPU limit')
    if not args.socket_path.is_absolute() or args.socket_path.exists() or not args.socket_path.parent.is_dir():
        parser.error('socket path must be absolute, absent, and have an existing parent')
    sys.path.insert(0, str(args.native_rpc_dir))
    cpus = sorted(os.sched_getaffinity(0))
    os.sched_setaffinity(0, cpus[:max(1, int(len(cpus) * args.cpu_usage / 100))])
    from lib.http_service import HttpService

    native_argv = [sys.argv[0], '--socket_path', str(args.socket_path), '--socket_mode', '600']
    sys.argv = native_argv
    if args.detector == 'official-x5':
        if not all((args.detection_worker, args.box_model, args.landmark_model)):
            parser.error('official X5 detector requires worker, box and landmark models')
        from .official_detector import OfficialBpuDetector
        detector = OfficialBpuDetector(args.detection_worker, args.box_model, args.landmark_model,
                                       confidence=args.confidence)
    else:
        from lib.insight import Det
        detector = Det(model_file=str(args.det_model), num_threads_onnx=args.num_threads)
        detector.prepare(ctx_id=-1, det_thresh=args.confidence, input_size=(640, 640))
    worker_type = FaceId128Worker if args.encoder == 'faceid128' else InsightFaceWorker
    worker = worker_type(args.worker, args.model)
    backend = FaceBackend(detector, worker,
                          mounted_data_roots if args.mounted_data_roots else args.allowed_root)
    service, thread = None, None
    stop = threading.Event()
    try:
        if args.detector == 'official-x5':
            import numpy as np
            detector.detect(np.zeros((544, 960, 3), dtype=np.uint8))
        worker.infer(bytes(worker.input_bytes))
        service = HttpService('com.fnos.x5.faceid')

        def infer(body):
            try:
                return backend.infer(body), service.CODE_SUCCESS
            except PermissionError:
                return {}, service.CODE_FORBIDDEN
            except (ValueError, OSError):
                return {}, service.CODE_BAD_REQUEST
            except Exception:
                service.logger.exception('X5 face pipeline failed')
                return {}, 50000

        service.append_handler_python('inferFaceDet', infer)
        service.start_wrapper(native_argv)
        service.set_ready(True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        thread = threading.Thread(target=service.run_in_loop, daemon=True)
        thread.start()
        while not stop.wait(0.5):
            if not thread.is_alive():
                raise RuntimeError('native face RPC loop stopped')
    finally:
        if service is not None and thread is not None:
            service.set_ready(False)
            service.shutdown()
            thread.join(timeout=5)
        worker.close()
        if hasattr(detector, 'close'):
            detector.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
