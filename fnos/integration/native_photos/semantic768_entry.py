#!/usr/bin/python3
"""Candidate ai_manager launcher. Packaging/activation remain separate gates.

The reviewed domestic Medium ONNX selection uses the native 768D BPU encoder.
All other model choices delegate unchanged to the preserved native executable.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import sys

NATIVE_ENTRY_SHA256 = '3399764ba791ec678f10b96d66558146001f3dbcafe8e6acf1e281fe2461a534'
NATIVE_FALLBACK_PATHS = (
    Path('/usr/trim/lib/ai_manager/com.trim.img2vec/cp311/img2vec.native.bin'),
    Path('/usr/trim/lib/ai_manager/com.trim.img2vec/cp311/img2vec.bin'),
)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def claim_socket(path):
    """Keep a lock for process lifetime and remove only a stale owned socket."""
    if not path.is_absolute() or not path.parent.is_dir():
        raise ValueError('invalid native socket path')
    lock = path.with_name('.' + path.name + '.semantic768.lock')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    guard = os.fdopen(fd, 'a')
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise RuntimeError('unexpected native socket lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                raise RuntimeError('unexpected native socket owner/type')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(2)
                try:
                    probe.connect(str(path))
                except ConnectionRefusedError:
                    path.unlink()
                else:
                    raise RuntimeError('native image endpoint already active')
        return guard
    except BaseException:
        guard.close()
        raise


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument('--runtime-config', type=Path, default=Path('/etc/fnos-x5/semantic768.json'))
    config_args, native_argv = config_parser.parse_known_args(argv)
    try:
        config = json.loads(config_args.runtime_config.read_text())
        original = Path(config['native_executable'])
    except (OSError, ValueError, TypeError, KeyError) as error:
        # The installer must preserve the pinned original at this fixed
        # sibling before replacing img2vec.bin. It remains discoverable even
        # if runtime JSON is missing or corrupt. The live path is useful only
        # before replacement and is accepted solely if still the original ELF.
        for candidate in NATIVE_FALLBACK_PATHS:
            try:
                valid = candidate.is_file() and digest(candidate) == NATIVE_ENTRY_SHA256
            except OSError:
                valid = False
            if valid:
                print('native semantic remains on CPU: runtime config unavailable: ' + str(error),
                      file=sys.stderr, flush=True)
                os.execv(str(candidate), [str(candidate), *native_argv])
        raise RuntimeError('runtime config unavailable and no verified preserved native executable') from error
    if original.resolve() == Path(__file__).resolve() or digest(original) != NATIVE_ENTRY_SHA256:
        raise RuntimeError('preserved native img2vec executable mismatch')
    original_affinity = os.sched_getaffinity(0)
    original_library_path = os.environ.get('LD_LIBRARY_PATH')

    def fallback(reason):
        print('native semantic remains on CPU: ' + reason, file=sys.stderr, flush=True)
        os.sched_setaffinity(0, original_affinity)
        if original_library_path is None:
            os.environ.pop('LD_LIBRARY_PATH', None)
        else:
            os.environ['LD_LIBRARY_PATH'] = original_library_path
        os.execv(str(original), [str(original), *native_argv])

    if not config.get('enabled', False):
        fallback('candidate disabled')
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--model_format', default='ONNX')
    parser.add_argument('--onnx_model', type=Path)
    parser.add_argument('--edition', default='domestic')
    parser.add_argument('--socket_path', type=Path)
    parser.add_argument('--num_threads_onnx', type=int, default=3)
    parser.add_argument('--cpu_usage', type=float, default=50)
    parser.add_argument('--log_level', default='info')
    args, unknown = parser.parse_known_args(native_argv)
    try:
        sys.path.insert(0, config['python_root'])
        if config.get('python_deps'):
            sys.path.insert(0, config['python_deps'])
        from fnos.integration.native_photos.semantic768_backend import (
            NATIVE_IMAGE_SHA256, NATIVE_TEXT_SHA256, NATIVE_TOKENIZER_SHA256,
            PREPROCESS_REVISION, checked_digest, Semantic768Worker, SemanticStartupError)
        selected = (not unknown and args.model_format == 'ONNX' and args.edition == 'domestic'
                    and args.onnx_model is not None and args.onnx_model.is_file()
                    and digest(args.onnx_model) == NATIVE_IMAGE_SHA256)
    except Exception as error:
        fallback('candidate configuration unavailable: ' + str(error))
    if not selected:
        # Preserve native Large/international/accelerator options and errors.
        fallback('native model selection is outside this candidate')
    try:
        if config.get('preprocess_revision') != PREPROCESS_REVISION:
            raise RuntimeError('semantic preprocessing configuration mismatch')
        # The rollout owner fills this only after native oracle, performance
        # and mixed-index/text retrieval acceptance. Never activate by default.
        if config.get('activation_acceptance') != 'native-preprocess-search-performance-passed':
            raise RuntimeError('native semantic activation acceptance is incomplete')
        checked_digest(config['native_text_model'], NATIVE_TEXT_SHA256)
        checked_digest(config['native_tokenizer'], NATIVE_TOKENIZER_SHA256)
        # Artifact validation is complete before any native socket is claimed.
        Semantic768Worker(config['worker'], config['model']).close()
    except Exception as error:
        fallback('candidate setup failed: ' + str(error))
    if args.socket_path is None or str(args.socket_path) != config['socket_path']:
        raise ValueError('unexpected native image socket')
    if not 1 <= args.num_threads_onnx <= 64 or not 0 < args.cpu_usage <= 100:
        raise ValueError('invalid native CPU budget')
    # num_threads_onnx controls the preserved CPU implementation. This SDK has
    # no verified internal CPU-thread setting; compare with equal affinity.
    guard = claim_socket(args.socket_path)
    try:
        os.environ['LD_LIBRARY_PATH'] = '/usr/hobot/lib:' + os.environ.get('LD_LIBRARY_PATH', '')
        sys.argv = [sys.argv[0], '--worker', config['worker'], '--model', config['model'],
                    '--socket-path', str(args.socket_path), '--cpu-usage', str(args.cpu_usage),
                    '--log-level', args.log_level, '--mounted-data-roots']
        if config.get('native_rpc_dir'):
            sys.argv.extend(['--native-rpc-dir', config['native_rpc_dir']])
        from fnos.integration.native_photos.semantic768_service import main as serve
        try:
            return serve()
        except SemanticStartupError as error:
            # No candidate socket has been bound. Release ownership before
            # delegating; restoring affinity preserves native cpu_usage.
            guard.close()
            fallback(str(error))
    finally:
        guard.close()


if __name__ == '__main__':
    raise SystemExit(main())
