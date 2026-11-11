#!/usr/bin/python3
"""ai_manager-compatible launcher for a separately versioned X5 face backend.

Installation must verify the native executable and an empty/rebuilt face index.
Model namespaces are never silently switched on an existing populated index.
"""
import argparse
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import sys
import tempfile


def ensure_index_namespace(namespace, database):
    """Bind a new empty native index to its encoder; never mix model spaces."""
    database = Path(database)
    marker = database.with_name('.fnos-face-namespace.json')
    if marker.is_symlink() or not database.is_file():
        raise RuntimeError('native face index is unavailable')
    if marker.exists():
        if json.loads(marker.read_text()).get('namespace') != namespace:
            raise RuntimeError('face model changed; native re-recognition required')
        return
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        counts = [db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                  for table in ('face', 'person')]
    if any(counts):
        raise RuntimeError('unversioned populated face index; native re-recognition required')
    # A short write must not publish a partial marker: every later startup
    # reads it before serving, so a truncated JSON file would require manual
    # intervention. Keep the new file on this filesystem and publish only
    # after it is complete and durable. The caller owns the native face lock;
    # link() also refuses to replace any independently created marker.
    fd, temporary = tempfile.mkstemp(prefix='.fnos-face-namespace-', dir=marker.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump({'namespace': namespace, 'initialized_from': 'empty native face/person index'}, out)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.link(temporary, marker)
        os.unlink(temporary)
        temporary = None
        directory = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model_format', choices=['ONNX'], default='ONNX')
    parser.add_argument('--det_model', required=True, type=Path)
    parser.add_argument('--rec_model', required=True, type=Path)
    parser.add_argument('--num_threads_onnx', type=int, default=3)
    parser.add_argument('--cpu_usage', type=float, default=50)
    parser.add_argument('--log_level', default='info')
    parser.add_argument('--confidence', type=float, default=.7)
    parser.add_argument('--resolution', type=int, default=1600)
    parser.add_argument('--socket_path', required=True, type=Path)
    parser.add_argument('--runtime-config', type=Path, default=Path('/etc/fnos-x5/faceid.json'))
    args = parser.parse_args()
    config = json.loads(args.runtime_config.read_text())
    encoder = config.get('encoder', 'insightface')
    namespaces = {'insightface': 'drobotics-faceid-bd9ff56e7b14e26b',
                  'faceid128': 'drobotics-faceid128-aa3ab3230f0957bc'}
    if encoder not in namespaces or config['embedding_namespace'] != namespaces[encoder]:
        raise RuntimeError('face embedding namespace mismatch')
    detector = config.get('detector', 'native-cpu')
    if detector not in ('native-cpu', 'official-x5'):
        raise RuntimeError('unreviewed face detector backend')
    # ai_manager still supplies its legacy model arguments. The official
    # backend verifies both pinned HBM models itself and never opens det.onnx.
    if detector == 'native-cpu':
        with args.det_model.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != config['detector_sha256']:
            raise RuntimeError('unreviewed native detector; preserve existing face namespace')
    if args.resolution != 1600:
        raise RuntimeError('unreviewed detector resolution')
    # The original launcher leaves a Unix socket behind after idle shutdown.
    # Serialize ownership, refuse a live endpoint, and remove only a stale socket.
    path = args.socket_path
    if path != Path('/run/com.trim.face_det_internal.sock'):
        raise ValueError('unexpected native socket path')
    guard = open('/run/lock/fnos-x5-faceid.lock', 'a')
    fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if config.get('require_index_namespace'):
        ensure_index_namespace(config['embedding_namespace'], '/var/apps/trim.photos/var/db/photo.db')
    if path.exists():
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
            raise RuntimeError('unexpected socket owner or type')
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
        except ConnectionRefusedError:
            path.unlink()
        else:
            raise RuntimeError('native face endpoint already active')
        finally:
            probe.close()
    sys.path.insert(0, config['python_root'])
    if config.get('python_deps'):
        sys.path.insert(0, config['python_deps'])
    os.environ['LD_LIBRARY_PATH'] = '/usr/hobot/lib:' + os.environ.get('LD_LIBRARY_PATH', '')
    sys.argv = [sys.argv[0], '--worker', config['worker'], '--model', config['model'],
                '--encoder', encoder,
                '--det-model', str(args.det_model), '--socket-path', str(path),
                '--num-threads', str(args.num_threads_onnx), '--cpu-usage', str(args.cpu_usage),
                '--confidence', str(args.confidence), '--mounted-data-roots']
    sys.argv.extend(['--detector', detector])
    if detector == 'official-x5':
        for flag, key in (('--detection-worker', 'detection_worker'), ('--box-model', 'box_model'),
                          ('--landmark-model', 'landmark_model')):
            sys.argv.extend([flag, config[key]])
    print('X5 face backend: ' + detector + ' detector + faceid BPU, namespace=' + config['embedding_namespace'], flush=True)
    from fnos.integration.native_photos.face_service import main as serve
    return serve()


if __name__ == '__main__':
    raise SystemExit(main())
