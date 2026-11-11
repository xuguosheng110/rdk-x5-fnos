"""Identity for native services that replace argv using setproctitle.

Only a live controlled Popen child can get a receipt. Consumers must be the
controller or another child of that same still-live controller. This is not an
argv guess or an acceptance path for unrelated external native endpoints.
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess

from .semantic768_backend import NATIVE_IMAGE_SHA256, NATIVE_TEXT_SHA256, NATIVE_TOKENIZER_SHA256, checked_digest
from .semantic768_entry import NATIVE_ENTRY_SHA256

TEXT_ENTRY_SHA256 = '4b45c00d32322a0c7023786055b4ceb47b196d3273f30afe2b3b11862d18c615'


def process_identity(pid):
    raw = Path('/proc/' + str(pid) + '/stat').read_text()
    fields = raw[raw.rfind(')') + 2:].split()
    return {'pid': pid, 'parent_pid': int(fields[1]), 'start_ticks': int(fields[19])}


def peer_pid(path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(str(path))
        pid, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid != 0:
        raise ValueError('native endpoint is not root-owned')
    return pid


def configuration(command, role):
    if role not in ('cpu', 'text') or not isinstance(command, list) or not all(isinstance(value, str) for value in command):
        raise ValueError('invalid native controlled command')
    def arg(name):
        if command.count(name) != 1:
            raise ValueError('explicit single native option required: ' + name)
        return command[command.index(name) + 1]
    if arg('--model_format') != 'ONNX' or arg('--edition') != 'domestic':
        raise ValueError('native domestic ONNX profile required')
    model = arg('--onnx_model')
    threads = int(arg('--num_threads_onnx'))
    if not Path(model).is_absolute() or not 1 <= threads <= 64:
        raise ValueError('invalid native model path/thread budget')
    result = {'model': model, 'model_sha256': checked_digest(model, NATIVE_IMAGE_SHA256 if role == 'cpu' else NATIVE_TEXT_SHA256),
              'num_threads_onnx': threads, 'socket': arg('--socket_path')}
    if role == 'text':
        tokenizer = arg('--sp')
        if not Path(tokenizer).is_absolute():
            raise ValueError('absolute native tokenizer path required')
        result.update(tokenizer=tokenizer, tokenizer_sha256=checked_digest(tokenizer, NATIVE_TOKENIZER_SHA256))
    return result


def verify_log(prefix, config):
    text = prefix.decode('utf-8', errors='strict').splitlines()
    required = ['edition:domestic', 'onnx_model:' + config['model'],
                'num_threads_onnx:' + str(config['num_threads_onnx'])]
    if config.get('tokenizer'):
        # Real txt2vec r7 startup does not print model_format. Its exact
        # controlled command still requires ONNX; pinned ELF/model/tokenizer
        # and these observed native log fields confirm the text profile.
        required.extend(['sp:' + config['tokenizer'], 'device:CPU'])
    else:
        required.append('model_format:ONNX')
    if any(not any(line.endswith(marker) for line in text) for marker in required):
        raise ValueError('native startup log does not confirm the controlled model/thread profile')


def capture_receipt(process, command, endpoint, startup_log, role, receipt_path):
    if not isinstance(process, subprocess.Popen) or process.poll() is not None or list(process.args) != command:
        raise ValueError('receipt requires the exact live Popen object and its launch argv')
    identity = process_identity(process.pid)
    controller = process_identity(os.getpid())
    if identity['parent_pid'] != controller['pid'] or peer_pid(endpoint) != process.pid:
        raise ValueError('native service is not this controller child/socket')
    config = configuration(command, role)
    if str(endpoint) != config['socket']:
        raise ValueError('controlled command socket mismatch')
    expected = NATIVE_ENTRY_SHA256 if role == 'cpu' else TEXT_ENTRY_SHA256
    checked_digest(command[0], expected)
    checked_digest('/proc/' + str(process.pid) + '/exe', expected)
    prefix = Path(startup_log).read_bytes()
    if not 0 < len(prefix) <= 1024 * 1024:
        raise ValueError('invalid native startup log length')
    verify_log(prefix, config)
    receipt = {'schema': 'fnos-semantic768-controlled-native-process/v1', 'role': role,
               'controller': controller, 'process': identity, 'command': command,
               'executable_sha256': expected, 'configuration': config,
               'startup_log': str(Path(startup_log).resolve()), 'startup_prefix_bytes': len(prefix),
               'startup_prefix_sha256': hashlib.sha256(prefix).hexdigest(),
               'controller_module_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    with Path(receipt_path).open('x') as stream:
        stream.write(json.dumps(receipt, indent=2) + '\n')
    return receipt


def validate_receipt(receipt_path, role, endpoint, *, expected_pid=None):
    receipt = json.loads(Path(receipt_path).read_text())
    if receipt.get('schema') != 'fnos-semantic768-controlled-native-process/v1' or receipt.get('role') != role:
        raise ValueError('unreviewed native process receipt')
    if receipt['controller_module_sha256'] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise ValueError('native process controller implementation changed')
    controller, recorded = receipt['controller'], receipt['process']
    if os.getpid() != controller['pid'] and os.getppid() != controller['pid']:
        raise ValueError('receipt is not from this benchmark controller')
    if process_identity(controller['pid']) != controller or process_identity(recorded['pid']) != recorded:
        raise ValueError('controlled process/controller identity changed')
    if recorded['parent_pid'] != controller['pid'] or (expected_pid is not None and expected_pid != recorded['pid']):
        raise ValueError('native process is not the controlled child')
    if peer_pid(endpoint) != recorded['pid']:
        raise ValueError('native socket peer does not match controlled process')
    expected = NATIVE_ENTRY_SHA256 if role == 'cpu' else TEXT_ENTRY_SHA256
    if receipt['executable_sha256'] != expected:
        raise ValueError('unreviewed native executable family')
    checked_digest('/proc/' + str(recorded['pid']) + '/exe', expected)
    config = configuration(receipt['command'], role)
    if config != receipt['configuration'] or config['socket'] != str(endpoint):
        raise ValueError('controlled native model configuration changed')
    count = receipt['startup_prefix_bytes']
    if type(count) is not int or not 0 < count <= 1024 * 1024:
        raise ValueError('invalid startup log evidence length')
    with Path(receipt['startup_log']).open('rb') as stream:
        prefix = stream.read(count)
    if len(prefix) != count or hashlib.sha256(prefix).hexdigest() != receipt['startup_prefix_sha256']:
        raise ValueError('native startup evidence changed')
    verify_log(prefix, config)
    return {'pid': recorded['pid'], 'process': recorded, 'controller': controller,
            'executable_sha256': expected, 'receipt_sha256': hashlib.sha256(Path(receipt_path).read_bytes()).hexdigest(), **config}
