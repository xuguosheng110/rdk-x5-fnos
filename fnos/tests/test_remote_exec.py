"""Tests for the build-server remote_exec helper.

Nothing here opens a connection, reads the real keychain, or uses a real
password.  subprocess.run is replaced with a recorder so the tests can assert
the exact ssh argv order, the bytes handed to sudo stdin, and the failure
behaviour when the ControlMaster channel is gone or a call times out.

One class additionally runs the generated remote command against a fake sudo
in a temp dir, to show that the password line cannot reach a script that reads
stdin, whether or not sudo actually consumes it.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = (
    ROOT / "fnos" / "logs" / "build-server-20261105" / "prepare" / "remote_exec.py"
)

# Obvious fakes: never a real credential.
FAKE_PASSWORD = "FAKE-keychain-value-0123456789-do-not-use"
LEAK_MARKER = "FAKE-raw-keychain-stderr-marker"

# Local stand-in for sudo.  FAKE_SUDO_CONSUME=1 mirrors a password prompt
# (read exactly one line, then exec); 0 mirrors a cached/NOPASSWD credential
# that never touches stdin.  It never needs root and never talks to the host.
FAKE_SUDO = r'''#!/usr/bin/env python3
import json
import os
import sys

argv = sys.argv[1:]
log = os.environ.get("FAKE_SUDO_ARGV_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(argv) + "\n")

if os.environ.get("FAKE_SUDO_CONSUME") == "1":
    fd = sys.stdin.fileno()
    while True:
        char = os.read(fd, 1)
        if not char or char == b"\n":
            break

index = 0
while index < len(argv) and argv[index].startswith("-"):
    index += 2 if argv[index] in ("-p", "-u", "-g") else 1
os.execvp(argv[index], argv[index:])
'''


def load_helper():
    spec = importlib.util.spec_from_file_location("remote_exec_helper", HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = load_helper()


class Recorder:
    """Stand-in for subprocess.run that records argv/kwargs per call."""

    def __init__(
        self,
        *,
        ssh_rc: int = 0,
        keychain_rc: int = 0,
        keychain_stdout: bytes | None = None,
        keychain_stderr: bytes = b"",
        explode_on: str | None = None,
    ) -> None:
        self.ssh_rc = ssh_rc
        self.keychain_rc = keychain_rc
        self.keychain_stdout = (
            (FAKE_PASSWORD + "\n").encode() if keychain_stdout is None else keychain_stdout
        )
        self.keychain_stderr = keychain_stderr
        self.explode_on = explode_on
        self.calls: list[tuple[list[str], dict]] = []

    @property
    def ssh_calls(self) -> list[tuple[list[str], dict]]:
        return [call for call in self.calls if call[0][0] == "ssh"]

    @property
    def keychain_calls(self) -> list[tuple[list[str], dict]]:
        return [call for call in self.calls if call[0][0] == "security"]

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))

        if argv[0] == "security":
            if self.explode_on == "keychain":
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))
            return SimpleNamespace(
                returncode=self.keychain_rc,
                stdout=self.keychain_stdout,
                stderr=self.keychain_stderr,
            )

        if self.explode_on == "ssh":
            raise subprocess.TimeoutExpired(
                argv,
                kwargs.get("timeout", 0),
                output=("timed out while holding " + FAKE_PASSWORD).encode(),
            )
        if self.explode_on == "missing-ssh":
            raise FileNotFoundError(2, "No such file or directory", "ssh")
        return SimpleNamespace(returncode=self.ssh_rc, stdout=b"", stderr=b"")


class RemoteExecTest(unittest.TestCase):
    def invoke(
        self,
        argv: list[str],
        recorder: Recorder | None = None,
        *,
        patch_channel: bool = True,
        socket_path: str | None = None,
        chdir: Path | None = None,
    ):
        recorder = recorder or Recorder()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(helper.subprocess, "run", side_effect=recorder)
            )
            if patch_channel:
                stack.enter_context(mock.patch.object(helper, "check_channel"))
            if socket_path is not None:
                stack.enter_context(mock.patch.object(helper, "SOCKET", socket_path))
            if chdir is not None:
                stack.enter_context(contextlib.chdir(chdir))
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = helper.main(argv)
        return rc, out.getvalue(), err.getvalue(), recorder

    # -- ssh argv order -------------------------------------------------

    def test_plain_argv_puts_options_before_host_and_command_last(self):
        rc, out, err, rec = self.invoke(
            ["--mode", "plain", "--", "echo hello"]
        )

        self.assertEqual(rc, 0)
        self.assertEqual((out, err), ("", ""))
        self.assertEqual(len(rec.calls), 1)
        argv, kwargs = rec.calls[0]

        self.assertEqual(argv[0], "ssh")
        host_at = argv.index(helper.HOST)
        self.assertEqual(host_at, len(argv) - 2)
        self.assertEqual(argv[-1], "echo hello")
        for index, token in enumerate(argv[:host_at]):
            if token == "-o":
                self.assertLess(index + 1, host_at, "an -o value landed after HOST")
        self.assertIn(f"ControlPath={helper.SOCKET}", argv)
        self.assertNotIn("input", kwargs)
        self.assertNotIn("env", kwargs)
        self.assertEqual(kwargs["timeout"], helper.DEFAULT_TIMEOUT)
        self.assertEqual(rec.keychain_calls, [])

    def test_ssh_options_refuse_interactive_authentication(self):
        self.assertIn("BatchMode=yes", helper.SSH_OPTIONS)
        self.assertIn("PasswordAuthentication=no", helper.SSH_OPTIONS)
        self.assertIn("KbdInteractiveAuthentication=no", helper.SSH_OPTIONS)

    def test_explicit_timeout_is_forwarded(self):
        _, _, _, rec = self.invoke(
            ["--mode", "plain", "--timeout", "7.5", "--", "true"]
        )
        self.assertEqual(rec.ssh_calls[0][1]["timeout"], 7.5)

    # -- sudo command mode ----------------------------------------------

    def test_sudo_wraps_command_and_sends_one_password_line(self):
        command = "id; echo SUDO_RC=$?"
        rc, out, err, rec = self.invoke(
            ["--mode", "sudo", "--", command], Recorder(ssh_rc=7)
        )

        self.assertEqual(rc, 7)
        self.assertEqual((out, err), ("", ""))
        argv, kwargs = rec.ssh_calls[0]
        self.assertEqual(argv[-1], helper.sudo_remote_command(command))
        self.assertIn(helper.STDIN_ISOLATION, argv[-1])
        self.assertEqual(kwargs["input"], (FAKE_PASSWORD + "\n").encode())
        self.assertNotIn("env", kwargs)

        security_argv, security_kwargs = rec.keychain_calls[0]
        self.assertEqual(security_argv[0], "security")
        self.assertIn(helper.SERVICE, security_argv)
        self.assertNotIn(FAKE_PASSWORD, security_argv)
        self.assertEqual(security_kwargs["timeout"], helper.KEYCHAIN_TIMEOUT)
        self.assertIs(security_kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(security_kwargs["capture_output"])

    # -- --file payload -------------------------------------------------

    def test_file_mode_executes_script_as_one_bash_c_argument(self):
        script = "set -eu\nprintf '%s\\n' \"it's fine\"\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "step.sh"
            path.write_text(script, encoding="utf-8")
            rc, out, err, rec = self.invoke(["--mode", "sudo", "--file", str(path)])

        self.assertEqual(rc, 0)
        self.assertEqual((out, err), ("", ""))
        argv, kwargs = rec.ssh_calls[0]
        self.assertEqual(argv[-1], helper.sudo_remote_command(script))
        # The remote shell parses the guarded script back as exactly one argv entry.
        tokens = shlex.split(argv[-1])
        self.assertEqual(tokens[:6], ["sudo", "-S", "-p", "", "bash", "-c"])
        self.assertEqual(tokens[6], helper.STDIN_ISOLATION + "\n" + script)
        self.assertEqual(tokens[6].splitlines()[0], helper.STDIN_ISOLATION)
        # sudo stdin carries the password line only; the script is not mixed in.
        self.assertEqual(kwargs["input"], (FAKE_PASSWORD + "\n").encode())
        self.assertNotIn(b"printf", kwargs["input"])

    def test_file_mode_rejects_empty_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.sh"
            path.write_text("  \n\n", encoding="utf-8")
            rc, _, err, rec = self.invoke(["--mode", "sudo", "--file", str(path)])

        self.assertEqual(rc, helper.EXIT_USAGE)
        self.assertIn("empty command", err)
        self.assertEqual(rec.calls, [])

    def test_file_mode_requires_sudo(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "step.sh"
            path.write_text("true\n", encoding="utf-8")
            rc, _, err, rec = self.invoke(["--mode", "plain", "--file", str(path)])

        self.assertEqual(rc, helper.EXIT_USAGE)
        self.assertIn("only supported with --mode sudo", err)
        self.assertEqual(rec.calls, [])

    def test_file_and_command_are_mutually_exclusive(self):
        rc, _, err, rec = self.invoke(
            ["--mode", "sudo", "--file", "/tmp/x.sh", "--", "true"]
        )
        self.assertEqual(rc, helper.EXIT_USAGE)
        self.assertIn("mutually exclusive", err)
        self.assertEqual(rec.calls, [])

    # -- channel gone / timeouts ---------------------------------------

    def test_missing_socket_fails_fast_without_ssh_or_keychain(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, err, rec = self.invoke(
                ["--mode", "sudo", "--", "true"],
                patch_channel=False,
                socket_path=str(Path(tmp) / "absent.sock"),
            )

        self.assertEqual(rc, helper.EXIT_USAGE)
        self.assertEqual(rec.calls, [])
        self.assertIn("socket", err)
        self.assertIn("no password fallback", err)
        self.assertNotIn(FAKE_PASSWORD, out + err)

    def test_non_socket_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "not-a-socket"
            path.write_text("stale\n", encoding="utf-8")
            rc, _, err, rec = self.invoke(
                ["--mode", "plain", "--", "true"],
                patch_channel=False,
                socket_path=str(path),
            )

        self.assertEqual(rc, helper.EXIT_USAGE)
        self.assertEqual(rec.calls, [])
        self.assertIn("not a socket", err)

    def test_ssh_timeout_returns_124_without_leaking_inputs(self):
        rc, out, err, rec = self.invoke(
            ["--mode", "sudo", "--timeout", "2", "--", "sleep 999"],
            Recorder(explode_on="ssh"),
        )

        self.assertEqual(rc, helper.EXIT_TIMEOUT)
        self.assertIn("timed out after 2s", err)
        self.assertNotIn(FAKE_PASSWORD, out + err)
        self.assertEqual(rec.ssh_calls[0][1]["timeout"], 2.0)
        self.assertEqual(rec.keychain_calls[0][1]["timeout"], helper.KEYCHAIN_TIMEOUT)

    def test_missing_ssh_binary_is_a_generic_failure(self):
        rc, _, err, _ = self.invoke(
            ["--mode", "plain", "--", "true"], Recorder(explode_on="missing-ssh")
        )
        self.assertEqual(rc, helper.EXIT_USAGE)
        self.assertIn("ssh could not be started", err)

    def test_keychain_failure_is_generic_and_never_echoes_stderr(self):
        rc, out, err, rec = self.invoke(
            ["--mode", "sudo", "--", "true"],
            Recorder(keychain_rc=44, keychain_stderr=b"denied " + LEAK_MARKER.encode()),
        )

        self.assertEqual(rc, helper.EXIT_USAGE)
        self.assertIn("rc=44", err)
        self.assertNotIn(LEAK_MARKER, out + err)
        self.assertNotIn(FAKE_PASSWORD, out + err)
        self.assertEqual(rec.ssh_calls, [])

    def test_keychain_timeout_is_bounded(self):
        rc, _, err, rec = self.invoke(
            ["--mode", "sudo", "--", "true"], Recorder(explode_on="keychain")
        )
        self.assertEqual(rc, helper.EXIT_USAGE)
        self.assertIn("keychain lookup timed out", err)
        self.assertEqual(rec.ssh_calls, [])

    def test_empty_keychain_value_is_rejected(self):
        rc, _, err, rec = self.invoke(
            ["--mode", "sudo", "--", "true"], Recorder(keychain_stdout=b"\n")
        )
        self.assertEqual(rc, helper.EXIT_USAGE)
        self.assertIn("empty password", err)
        self.assertEqual(rec.ssh_calls, [])

    # -- credential containment ----------------------------------------

    def test_credential_stays_out_of_argv_stdout_files_and_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "step.sh"
            script.write_text("true\n", encoding="utf-8")
            workdir = Path(tmp) / "cwd"
            workdir.mkdir()

            rc, out, err, rec = self.invoke(
                ["--mode", "sudo", "--file", str(script)],
                chdir=workdir,
            )
            created = sorted(item.name for item in workdir.iterdir())

        self.assertEqual(rc, 0)
        rendered = "\n".join(" ".join(argv) for argv, _ in rec.calls)
        self.assertNotIn(FAKE_PASSWORD, rendered)
        self.assertNotIn(FAKE_PASSWORD, out + err)
        self.assertEqual(created, [])
        for _, kwargs in rec.calls:
            self.assertNotIn("env", kwargs)
        self.assertEqual(
            rec.ssh_calls[0][1]["input"], (FAKE_PASSWORD + "\n").encode()
        )

    # -- argument hygiene ----------------------------------------------

    def test_empty_command_is_rejected_without_spawning(self):
        for mode in ("plain", "sudo"):
            rc, _, err, rec = self.invoke(["--mode", mode, "--"])
            self.assertEqual(rc, helper.EXIT_USAGE)
            self.assertIn("empty command", err)
            self.assertEqual(rec.calls, [])

    def test_invalid_timeout_is_a_usage_error_without_traceback(self):
        cases = {
            "0": "greater than zero",
            "-5": "greater than zero",
            "nan": "finite",
            "NaN": "finite",
            "inf": "finite",
            "-inf": "finite",
            "1e400": "finite",
            "soon": "not a number",
        }
        for value, expected in cases.items():
            stream = io.StringIO()
            with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(stream):
                helper.main(["--mode", "plain", f"--timeout={value}", "--", "true"])
            self.assertEqual(caught.exception.code, helper.EXIT_USAGE, value)
            message = stream.getvalue()
            self.assertIn(expected, message, value)
            self.assertNotIn("Traceback", message, value)

    def test_cli_rejects_non_finite_timeout_with_status_2(self):
        # A real subprocess, but parsing fails before the channel/keychain path,
        # so nothing can connect or prompt.
        for value in ("nan", "inf", "1e400", "0"):
            proc = subprocess.run(
                [
                    sys.executable,
                    str(HELPER_PATH),
                    "--mode",
                    "plain",
                    "--timeout",
                    value,
                    "--",
                    "true",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=tempfile.gettempdir(),
            )
            self.assertEqual(proc.returncode, helper.EXIT_USAGE, value)
            self.assertNotIn("Traceback", proc.stderr, value)
            self.assertNotIn(FAKE_PASSWORD, proc.stdout + proc.stderr)

    def test_sh_quote_round_trips_through_shell_tokenisation(self):
        for text in ("plain", "it's $HOME `id`", "a\nb", "a'b'c", "--flag"):
            self.assertEqual(shlex.split(helper.sh_quote(text)), [text])


class SudoStdinIsolationTest(unittest.TestCase):
    """Runs the generated remote command locally, against a fake sudo.

    No ssh, no keychain, no real password.  The fake sudo either consumes one
    line from stdin (password prompt) or consumes nothing (cached credential /
    NOPASSWD), then execs the requested command exactly like sudo would.
    """

    LEAKY_SCRIPT = (
        "echo SCRIPT_RAN\n"
        "cat\n"
        'if read -r line; then echo "READ:$line"; else echo "READ:<eof>"; fi\n'
        "cat > stolen.txt\n"
        "echo DONE\n"
    )

    def run_locally(self, remote_command: str, *, consume: bool):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        bindir = root / "bin"
        bindir.mkdir()
        fake_sudo = bindir / "sudo"
        fake_sudo.write_text(FAKE_SUDO, encoding="utf-8")
        fake_sudo.chmod(0o755)
        workdir = root / "work"
        workdir.mkdir()
        argv_log = root / "argv.log"

        env = dict(os.environ)
        env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
        env["FAKE_SUDO_CONSUME"] = "1" if consume else "0"
        env["FAKE_SUDO_ARGV_LOG"] = str(argv_log)

        proc = subprocess.run(
            ["/bin/sh", "-c", remote_command],
            input=(FAKE_PASSWORD + "\n").encode(),
            cwd=workdir,
            env=env,
            capture_output=True,
            timeout=30,
        )
        files = {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }
        return proc, files

    def test_password_never_reaches_script_for_either_sudo_behaviour(self):
        remote = helper.sudo_remote_command(self.LEAKY_SCRIPT)
        self.assertTrue(remote.startswith("sudo -S -p '' bash -c "))
        self.assertIn(helper.STDIN_ISOLATION, remote)

        for consume in (True, False):
            with self.subTest(consume=consume):
                proc, files = self.run_locally(remote, consume=consume)
                stdout = proc.stdout.decode()
                stderr = proc.stderr.decode()

                self.assertEqual(proc.returncode, 0, stderr)
                self.assertIn("SCRIPT_RAN", stdout)
                self.assertIn("READ:<eof>", stdout)
                self.assertNotIn(FAKE_PASSWORD, stdout + stderr)
                # `cat` and the redirect both saw EOF, not the password line.
                self.assertEqual(files.get("work/stolen.txt"), b"")
                for name, data in files.items():
                    self.assertNotIn(FAKE_PASSWORD.encode(), data, name)
                self.assertIn("argv.log", files)
                self.assertNotIn(FAKE_PASSWORD, files["argv.log"].decode())

    def test_negative_control_unisolated_command_does_leak(self):
        # Control for the test above: without the guard the harness does see
        # the fake password, so a green run is not vacuous.  The value is fake
        # and only compared in memory; it is never printed or written.
        remote = "sudo -S -p '' bash -c " + helper.sh_quote("cat")
        proc, files = self.run_locally(remote, consume=False)
        self.assertIn(FAKE_PASSWORD, proc.stdout.decode())
        for name, data in files.items():
            self.assertNotIn(FAKE_PASSWORD.encode(), data, name)


if __name__ == "__main__":
    unittest.main()
