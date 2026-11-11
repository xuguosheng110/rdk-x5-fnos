"""systemd ExecCondition: distinguish no SMART devices from real errors."""
import os
import shlex
import subprocess


def main():
    try:
        # Vendor smartd remains Type=notify. An empty device set never sends
        # READY=1, so SuccessExitStatus alone cannot classify that startup.
        # showtests registers devices/configuration without running self-tests.
        command = ['/usr/sbin/smartd', *shlex.split(os.environ.get('smartd_opts', '')),
                   '-q', 'showtests']
        result = subprocess.run(command, timeout=120)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 255
    if result.returncode == 0:
        return 0
    if result.returncode == 17:
        print('No monitorable SMART devices; native service is not applicable.', flush=True)
        return 1
    # ExecCondition 255 is a visible failure, not a skipped service.
    return 255


if __name__ == '__main__':
    raise SystemExit(main())
