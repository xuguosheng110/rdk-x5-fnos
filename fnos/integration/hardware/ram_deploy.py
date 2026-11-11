"""Mac-owned RAM rescue deployment. Default is an entirely local read-only plan.

The root controller must approve the exact image/helper/backup manifest digest.
No reboot, power control, U-Boot, registration or normal-root write exists here.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time

from fnos.lab.locks import board_lock, adopt_inherited_lock, board_lock_path

SERVER = 'guosheng@192.0.2.10'
SERVER_ROOT = PurePosixPath('/data/disk1/rdk-x5-fnos-guosheng')
BOARD = 'x5-lab-01'
CID = '035344534b333247854bf6868b01a200'
CARD_BYTES = 31914983424
DEFAULT_LOCK = Path('/home/your-user/.local/state/fnos-lab')
HASH = re.compile(r'[0-9a-f]{64}\Z')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def regular(path, *, private=False):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('expected a non-symlink single-link regular file: ' + str(path))
    if private and (info.st_mode & 0o077 or path.parent.stat().st_mode & 0o077):
        raise ValueError('backup must remain private (file0600, directory0700)')
    return path


def hashed_json(path, expected):
    if not isinstance(expected, str) or not HASH.fullmatch(expected):
        raise ValueError('exact external evidence SHA required')
    path = regular(path)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('approval/evidence SHA mismatch')
    return json.loads(raw)


def validate_approval(approval_path, approval_sha, helper_path, backup_review_path):
    approval = hashed_json(approval_path, approval_sha)
    if (approval.get('schema') != 'fnos-ram-deploy-approval/v1' or approval.get('accepted') is not True
            or approval.get('review_status') != 'independently_approved_for_board_deploy'
            or not approval.get('reviewer') or approval.get('server') != SERVER):
        raise ValueError('final independent board-deployment approval required')
    image = approval['image']
    path = PurePosixPath(image['path'])
    if (not path.is_absolute() or '..' in path.parts or str(path) != image['path']
            or not path.is_relative_to(SERVER_ROOT) or not path.name.endswith('.img')):
        raise ValueError('image must be a canonical file under the designated server build root')
    size = image['bytes']
    if type(size) is not int or size <= 0 or size > CARD_BYTES or size % 512 or not HASH.fullmatch(image['sha256']):
        raise ValueError('invalid final image length/hash')
    target = approval['target']
    if (target.get('board_id') != BOARD or target.get('device') != '/dev/mmcblk1'
            or target.get('cid') != CID or target.get('bytes') != CARD_BYTES
            or target.get('major') != 179 or type(target.get('minor')) is not int
            or not 0 <= target['minor'] <= 1048575):
        raise ValueError('only the exact reviewed SD is accepted; sda/other targets are forbidden')
    boot = approval['rescue_boot_id']
    if not isinstance(boot, str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', boot):
        raise ValueError('fresh rescue boot ID required, not the prior NAS boot')
    if (ipaddress.ip_address(approval['rescue_host']) not in ipaddress.ip_network('192.0.2.10/24')
            or not HASH.fullmatch(approval['known_hosts_sha256'])):
        raise ValueError('serial-verified rescue host and known_hosts SHA required')
    helper = regular(helper_path)
    if digest(helper) != approval['helper_sha256']:
        raise ValueError('helper differs from the independently reviewed static artifact')
    review = hashed_json(backup_review_path, approval['selective_backup_review_sha256'])
    if (review.get('status') != 'PASS' or review.get('backup_mode') != 'selective_backup'
            or review.get('whole_disk_backup_required') is not False):
        raise ValueError('selective key-file backup independent PASS required')
    required_checks = ('archive_integrity_read_complete', 'exact_member_set', 'all_regular_file_sizes_and_sha256',
                       'all_uid_gid_modes', 'all_symlink_targets', 'local_and_server_archive_identical')
    if any(review.get('checks', {}).get(name) is not True for name in required_checks):
        raise ValueError('selective backup independent integrity checks incomplete')
    archive = regular(Path(review['private_paths']['local_directory']) / 'key-files.tar.gz', private=True)
    if archive.stat().st_size != review['archive']['bytes'] or digest(archive) != review['archive']['sha256']:
        raise ValueError('verified selective backup archive changed')
    return approval


# The server opens the approved image once, hashes that FD, then streams that
# same FD. Header precedes data, so the controller never writes an unverified
# source. No disk/device/network mutation occurs on the build server.
SOURCE = r'''
import os,sys,stat,hashlib,json
p,n,want=sys.argv[1],int(sys.argv[2]),sys.argv[3]
if os.path.realpath(p)!=p:raise SystemExit('source path includes symlink')
fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
s=os.fstat(fd)
if not stat.S_ISREG(s.st_mode) or s.st_size!=n:raise SystemExit('source identity/length mismatch')
h=hashlib.sha256();count=0
while True:
 b=os.read(fd,1048576)
 if not b:break
 h.update(b);count+=len(b)
if count!=n or h.hexdigest()!=want:raise SystemExit('full source SHA mismatch')
before=(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
s=os.fstat(fd)
if before!=(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns):raise SystemExit('source changed during verification')
os.lseek(fd,0,0)
print(json.dumps({'status':'SOURCE_VERIFIED','bytes':n,'sha256':want}),flush=True)
out=sys.stdout.buffer
while True:
 b=os.read(fd,1048576)
 if not b:break
 out.write(b)
out.flush();s=os.fstat(fd)
if before!=(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns):raise SystemExit('source changed while streaming')
os.close(fd)
'''


def ssh_board(host, known_hosts):
    address = ipaddress.ip_address(host)
    if address not in ipaddress.ip_network('192.0.2.10/24'):
        raise ValueError('use the physically verified LAN rescue address')
    regular(known_hosts)
    return ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UserKnownHostsFile=' + str(Path(known_hosts).resolve()),
            '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3',
            '-p', '2222', 'root@' + host]


def remote(argv, script, **kwargs):
    return subprocess.run([*argv, script], check=True, capture_output=True, **kwargs)


def quiet_emit(emit, event, **values):
    """Diagnostics can fail, but can never unwind a writer-ownership gate."""
    try:
        return emit(event, **values) is not False
    except BaseException:
        return False


def wait_terminal(ssh, helper, token, emit, *, boot_id):
    """Keep the physical lock until the remote writer is proven terminal.

    Transport loss is not proof of remote completion. No timeout releases this
    lock while a possibly-live writer remains. The operator may inspect serial
    concurrently under the parent's inherited lock, never start power actions.
    """
    while True:
        try:
            result = remote(ssh, shlex.join([helper, '--status', token]), timeout=20, text=True)
            row = json.loads(result.stdout)
            if row.get('status') == 'TERMINAL' and row.get('run', {}).get('token') == token:
                quiet_emit(emit, 'remote_terminal', remote_status=row['run'].get('status'))
                return row['run']
        except (subprocess.SubprocessError, OSError, ValueError):
            # A different boot proves the former kernel/writer has terminated,
            # but is never treated as successful deployment.
            try:
                result = remote(ssh, 'cat /proc/sys/kernel/random/boot_id', timeout=20, text=True)
                now = result.stdout.strip()
                if re.fullmatch(r'[0-9a-f-]{36}', now) and now != boot_id:
                    quiet_emit(emit, 'rescue_boot_changed_completion_failed')
                    return {'status': 'BOOT_CHANGED'}
            except (subprocess.SubprocessError, OSError):
                pass
        quiet_emit(emit, 'completion_unknown_lock_retained_no_power')
        time.sleep(10)


def ensure_writer_terminal(writer, ssh, helper, token, emit, *, boot_id):
    """Non-optional ownership cleanup, invoked from execute's first finally.

    A failing diagnostic, pipe close or wait cannot skip remote termination
    proof. Unknown completion keeps retrying under the caller's physical lock.
    """
    try:
        if writer.stdin is not None and not writer.stdin.closed:
            writer.stdin.close()
    except BaseException:
        pass
    try:
        code = writer.wait(timeout=150)
    except BaseException:
        code = None
    if type(code) is int and 0 <= code < 255:
        quiet_emit(emit, 'remote_command_terminal', exit_code=code)
        return {'status': 'COMMAND_TERMINAL', 'exit_code': code}
    while True:
        try:
            return wait_terminal(ssh, helper, token, emit, boot_id=boot_id)
        except BaseException:
            # This path deliberately cannot unwind the enclosing physical lock.
            # Even an unexpected decoder/diagnostic exception leaves ownership
            # held until a subsequent authenticated probe proves termination.
            quiet_emit(emit, 'terminal_probe_failed_lock_retained_no_power')
            with contextlib.suppress(BaseException):
                time.sleep(10)


def execute(approval, helper_path, ssh, server_socket, evidence, held):
    held.revalidate()
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {'schema': 'fnos-ram-controller/v1', 'status': 'starting', 'automatic_reboot': False,
              'image': approval['image'], 'target': approval['target'], 'events': []}
    mutex = threading.Lock()

    def write_diagnostic(event, **values):
        with mutex:
            report['events'].append({'at': time.time(), 'event': event, **values})
            (evidence / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps({'event': event, **values}), flush=True)

    def emit(event, **values):
        ok = quiet_emit(write_diagnostic, event, **values)
        if not ok:
            with contextlib.suppress(BaseException):
                report['diagnostics_degraded'] = True
        return ok

    interrupted = []
    old = {}
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        old[sig] = signal.signal(sig, lambda signum, _frame: interrupted.append(signum))
    source = writer = source_log = writer_log = None
    terminal_proven = False
    token = secrets.token_hex(16)
    helper = '/run/fnos-ram-deploy-' + token + '/helper'
    report['token'] = token
    try:
        # Upload only to an empty private directory in the proven rescue RAM.
        boot = approval['rescue_boot_id']
        stage = ('set -eu; test "$(cat /proc/sys/kernel/random/boot_id)" = ' + shlex.quote(boot)
                 + '; case "$(/bin/busybox stat -f -c %T /)" in ramfs|tmpfs) ;; *) exit 73;; esac'
                 + '; umask 077; mkdir ' + shlex.quote(str(PurePosixPath(helper).parent))
                 + '; cat > ' + shlex.quote(helper) + '; chmod 700 ' + shlex.quote(helper)
                 + '; /bin/busybox sha256sum ' + shlex.quote(helper))
        staged = remote(ssh, stage, input=Path(helper_path).read_bytes(), timeout=60)
        if staged.stdout.decode().split()[0] != approval['helper_sha256']:
            raise ValueError('staged helper SHA mismatch')
        emit('helper_staged_and_hashed')
        image = approval['image']
        server = ['ssh', '-S', str(server_socket), '-T', '-o', 'BatchMode=yes',
                  '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3', SERVER]
        command = shlex.join(['python3', '-c', SOURCE, image['path'], str(image['bytes']), image['sha256']])
        source_log = (evidence / 'source.stderr').open('wb')
        source = subprocess.Popen([*server, command], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=source_log)
        header = json.loads(source.stdout.readline(4096))
        if header != {'status': 'SOURCE_VERIFIED', 'bytes': image['bytes'], 'sha256': image['sha256']}:
            raise ValueError('source verification receipt mismatch')
        emit('server_source_full_sha_verified')
        if interrupted:
            raise InterruptedError('interrupted before write')
        held.revalidate()
        if report.get('diagnostics_degraded'):
            raise OSError('diagnostic sink failed before write; no board writer started')
        target = approval['target']
        command = shlex.join([helper, '--execute', token, boot, target['cid'], str(target['bytes']),
                              str(target['major']), str(target['minor']), str(image['bytes']), image['sha256']])
        writer_log = (evidence / 'writer.stderr').open('wb')
        writer = subprocess.Popen([*ssh, command], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=writer_log, bufsize=0)
        responses = queue.Queue()

        def consume():
            try:
                for line in writer.stdout:
                    row = json.loads(line)
                    responses.put(row)
                    emit('writer', result=row)
            except Exception as error:
                responses.put({'status': 'PROTOCOL_ERROR', 'error': type(error).__name__})
            finally:
                responses.put({'status': 'EOF'})

        reader = threading.Thread(target=consume, daemon=True)
        reader.start()
        ready = responses.get(timeout=60)
        if ready.get('status') != 'READY':
            raise ValueError('remote inactive-SD preflight failed')
        count = 0
        while count < image['bytes']:
            held.revalidate()
            if interrupted:
                raise InterruptedError('interrupted during write; incomplete image must not boot')
            chunk = source.stdout.read(min(1024 * 1024, image['bytes'] - count))
            if not chunk:
                raise ValueError('source image truncated')
            pending = memoryview(chunk)
            while pending:
                n = writer.stdin.write(pending)
                if not n:
                    raise OSError('board stream closed')
                pending = pending[n:]
            count += len(chunk)
        if source.stdout.read(1):
            raise ValueError('source contained bytes after approved length')
        if source.wait(timeout=60):
            raise ValueError('source process failed after streaming')
        writer.stdin.close()
        # Writer stderr/stdout are continuously drained; wait owns the lock.
        while writer.poll() is None:
            held.revalidate()
            time.sleep(.5)
        reader.join(timeout=10)
        # A normal authenticated SSH command exit proves that writer terminal,
        # including exit2 after a failed/partial remote evidence-file write.
        # Never endlessly probe a corrupt status file for a known failure.
        if type(writer.returncode) is int and 0 <= writer.returncode < 255:
            terminal_proven = True
        if writer.returncode:
            raise RuntimeError('remote writer exited unsuccessfully; no boot')
        response = remote(ssh, shlex.join([helper, '--status', token]), timeout=20, text=True)
        terminal = json.loads(response.stdout)
        if terminal.get('status') != 'TERMINAL' or terminal.get('run', {}).get('token') != token:
            raise RuntimeError('remote terminal receipt is unavailable; no boot')
        last = [row['result'] for row in report['events'] if row['event'] == 'writer' and row['result'].get('status') == 'PASS']
        if (len(last) != 1 or terminal['run'].get('status') != 'DONE'
                or last[0].get('token') != token or last[0].get('target') != '/dev/mmcblk1'
                or last[0].get('written') != image['bytes'] or last[0].get('readback') != image['bytes']
                or last[0].get('sha256') != image['sha256']):
            raise RuntimeError('write/full readback did not produce exact PASS evidence')
        if report.get('diagnostics_degraded'):
            raise RuntimeError('write verified but diagnostics degraded; preserve remote evidence, no automatic boot')
        report['status'] = 'PASS_write_and_full_readback_only_not_booted'
        if not emit('complete_no_reboot'):
            raise OSError('final acceptance evidence could not be saved/emitted; no automatic boot')
        return report
    except BaseException as error:
        report['status'] = 'FAILED_no_boot'
        emit('failed', error=type(error).__name__ + ': ' + str(error))
        raise
    finally:
        # This must precede every potentially-failing log/resource cleanup.
        # It also runs if even the except block itself raises unexpectedly.
        if writer is not None and not terminal_proven:
            ensure_writer_terminal(writer, ssh, helper, token, emit, boot_id=approval['rescue_boot_id'])
            terminal_proven = True
        if source is not None and source.poll() is None:
            source.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                source.wait(timeout=10)
            if source.poll() is None:
                source.kill(); source.wait()
        for stream in (source_log, writer_log):
            if stream is not None:
                stream.close()
        for process in (source, writer):
            if process is not None:
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        with contextlib.suppress(OSError):
                            stream.close()
        for sig, handler in old.items():
            signal.signal(sig, handler)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('approval', 'helper', 'backup-review', 'known-hosts', 'evidence-dir'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--approval-sha256', required=True)
    p.add_argument('--rescue-host', required=True)
    p.add_argument('--server-socket', type=Path, default=Path('/home/your-user/.cache/rdk-fnos-ssh/build-server-r3.sock'))
    p.add_argument('--lock-root', type=Path, default=DEFAULT_LOCK)
    p.add_argument('--lock-fd', type=int)
    p.add_argument('--execute', action='store_true')
    args = p.parse_args()
    approval = validate_approval(args.approval, args.approval_sha256, args.helper, args.backup_review)
    if args.rescue_host != approval['rescue_host'] or digest(regular(args.known_hosts)) != approval['known_hosts_sha256']:
        raise ValueError('rescue address/serial-verified host key changed since approval')
    ssh = ssh_board(args.rescue_host, args.known_hosts)
    if not args.execute:
        print(json.dumps({'status': 'local_plan_only', 'network_actions': 0, 'board_actions': 0,
                          'image': approval['image'], 'target': approval['target'], 'backup': 'selective_PASS',
                          'automatic_reboot': False}, indent=2))
        return 0
    manager = (contextlib.nullcontext(adopt_inherited_lock(args.lock_fd, board_lock_path(args.lock_root, BOARD), BOARD))
               if args.lock_fd is not None else board_lock(args.lock_root, BOARD))
    with manager as held:
        execute(approval, args.helper, ssh, args.server_socket, args.evidence_dir, held)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
