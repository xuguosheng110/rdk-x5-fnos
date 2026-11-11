#!/usr/bin/python3
"""Model-ready dispatch for native ai_manager; no polling or index mutation.

Installed independently of the image builder. Each native launch supplies the
real model and socket arguments. Missing evidence, models, or configuration
delegates the exact arguments to the preserved vendor CPU executable.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

NATIVE = Path('/usr/trim/lib/ai_manager/com.trim.img2vec/cp311/img2vec.native.bin')
NATIVE_SHA = '3399764ba791ec678f10b96d66558146001f3dbcafe8e6acf1e281fe2461a534'
POLICY = Path('/etc/fnos-x5/semantic768.json')
RUNTIME = Path('/run/fnos-x5/semantic768')
PYTHON_ROOT = '/usr/lib/fnos-x5'
MODEL_SHA = 'dd605bf507f259df824d22a199b55ca423f7020c51766edb599e2d709e384939'
WORKER_SHA = '2d08cda15f2905251b4006c3608b5fe394a4f946c4f7a7409d9d7653cd49184e'
IMAGE_SHA = '73a177536d297a3e3767ee1cd55f4ab9045bd0c2a4ac9a584fb77eaf3b7e1988'
TEXT_SHA = 'd4712c45f5c5fba9e5603beba03415f06f1d6755d098e781ffcb0a79f4d65a44'
TOKENIZER_SHA = 'b7fe3bcc8d284fcb782691411e8b6fd4f45d7245565b094de6ab795e66bcd32f'
PREPROCESS = 'domestic-medium-pil-resize224-v1'
ACCEPTANCE = 'native-preprocess-search-performance-passed'
RUNTIME_HASHES = {
    'semantic768_entry.py': 'b88712fc30008c0f12c87ca62b015e888988724587ba24908ccfd2e4a74bd788',
    'semantic768_service.py': '2e9be55a0cd3f48d5d7ff51461c4a840dc419409a1ba7e44ddf885ea652f18fd',
    'semantic768_backend.py': '6fe425300787bf66f373f4424721dcf720c42a7401fc9b59ebaed915de9e2de1',
}


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def artifact_identity():
    return {'model_sha256': MODEL_SHA, 'worker_sha256': WORKER_SHA,
            'native_executable_sha256': NATIVE_SHA, 'native_image_sha256': IMAGE_SHA,
            'native_text_sha256': TEXT_SHA, 'native_tokenizer_sha256': TOKENIZER_SHA,
            'preprocess_revision': PREPROCESS, 'runtime_sha256': dict(RUNTIME_HASHES)}


def review_passed(review):
    """A pinned independent review, never a benchmark's self-reported PASS."""
    if not isinstance(review, dict):
        return False
    if (review.get('schema') != 'fnos-semantic768-activation-review/v1'
            or review.get('reviewer') != 'Codex-independent'
            or review.get('status') != 'PASS'
            or review.get('artifacts') != artifact_identity()):
        return False
    gates = review.get('gates', {})
    for name in ('native_preprocess_oracle', 'equal_budget_performance', 'production_default_performance',
                 'native_chinese_search', 'mixed_cpu_bpu_index'):
        row = gates.get(name, {})
        if not isinstance(row, dict) or row.get('status') != 'PASS':
            return False
        if not re.fullmatch('[0-9a-f]{64}', str(row.get('evidence_sha256', ''))):
            return False
    return True


def native_selection(argv):
    parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    parser.add_argument('--model_format', default='ONNX')
    parser.add_argument('--onnx_model', type=Path)
    parser.add_argument('--edition', default='domestic')
    parser.add_argument('--socket_path', type=Path)
    parser.add_argument('--num_threads_onnx', type=int, default=3)
    parser.add_argument('--cpu_usage', type=float, default=50)
    parser.add_argument('--log_level', default='info')
    args, unknown = parser.parse_known_args(argv)
    if (unknown or args.model_format != 'ONNX' or args.edition != 'domestic'
            or args.onnx_model is None or args.socket_path is None
            or not args.socket_path.is_absolute()
            or not 1 <= args.num_threads_onnx <= 64 or not 0 < args.cpu_usage <= 100):
        raise ValueError('outside reviewed native Medium selection')
    return args


def model_pair(image):
    """Accept native model-cache layout, including a moved native volume."""
    parts = image.parts
    if (len(parts) != 7 or parts[0] != '/' or not re.fullmatch('vol[0-9]+', parts[1])
            or parts[2:5] != ('@sysappmeta', 'ai-manager', 'models')
            or parts[5:] != ('img2vec-medium-model-1.0.0', 'img2vec.onnx')):
        raise ValueError('unexpected native Medium image location')
    # Native cache names are only a locator; all three content hashes define
    # the coordinate space. Do not accept a model from another volume.
    models = image.parents[1]
    text_dir = models / 'txt2vec-medium-model-1.0.0'
    text, tokenizer = text_dir / 'txt2vec.onnx', text_dir / 'sp.model'
    if (digest(text) != TEXT_SHA or digest(tokenizer) != TOKENIZER_SHA
            or digest(image) != IMAGE_SHA):
        raise ValueError('native Medium model pairing mismatch')
    return text, tokenizer


def config_for_launch(argv, policy):
    args = native_selection(argv)
    text, tokenizer = model_pair(args.onnx_model)
    enabled = (policy.get('enabled') is True and review_passed(policy.get('activation_review')))
    return {'schema': 'fnos-semantic768-runtime/v1', 'enabled': enabled,
            'native_executable': str(NATIVE), 'python_root': PYTHON_ROOT,
            'python_deps': PYTHON_ROOT + '/photos-python',
            'worker': PYTHON_ROOT + '/semantic768-worker',
            'model': '/usr/share/fnos-x5/models/semantic768/native_cn_image.bin',
            'socket_path': str(args.socket_path),
            'native_text_model': str(text), 'native_tokenizer': str(tokenizer),
            'preprocess_revision': PREPROCESS,
            'activation_acceptance': ACCEPTANCE if enabled else None,
            'activation_review_sha256': policy.get('activation_review_sha256') if enabled else None,
            'model_ready_event': 'native-ai-manager-launch', 'dimensions': 768,
            'index_action': 'preserve-existing-native-vectors'}


def write_runtime(config):
    # /run is RAM state. Root-owned private parents prevent link redirection;
    # the socket-derived filename avoids collisions between model instances.
    for directory in (RUNTIME.parent, RUNTIME):
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o022):
            raise ValueError('unsafe semantic runtime directory')
    name = hashlib.sha256(config['socket_path'].encode()).hexdigest() + '.json'
    target = RUNTIME / name
    fd, temporary = tempfile.mkstemp(prefix='.ready-', dir=RUNTIME)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(config, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        return target
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def cpu_exec(argv):
    if NATIVE.is_symlink() or digest(NATIVE) != NATIVE_SHA:
        raise RuntimeError('preserved native image executable mismatch')
    os.execv(str(NATIVE), [str(NATIVE), *argv])


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        policy = json.loads(POLICY.read_text())
        config = config_for_launch(argv, policy)
        runtime = write_runtime(config)
        sys.path.insert(0, PYTHON_ROOT)
        from fnos.integration.native_photos.semantic768_entry import main as launch
    except (Exception, SystemExit):
        return cpu_exec(argv)
    # Never catch a running service failure and start CPU on its live socket.
    return launch(['--runtime-config', str(runtime), *argv])


if __name__ == '__main__':
    raise SystemExit(main())
