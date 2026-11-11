"""Offline tests for unified locks, power/serial/flash backends and runner.

No test opens /dev, calls the cloud, uses sudo or writes a real disk.  All
hardware substitutes are regular files, subprocesses with temporary lock files,
or in-memory fakes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fnos" / "tests" / "fixtures" / "lab-host-valid.json"
CP5_SCRIPTS = Path.home() / ".codex" / "skills" / "mijia-cp5-control" / "scripts"
if not CP5_SCRIPTS.is_dir():
    CP5_SCRIPTS = Path("/home/your-user/Documents/ChatGPT/自动同步助手/skills/device/mijia-cp5-control/scripts")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CP5_SCRIPTS))

from fnos.lab.authorization import (  # noqa: E402
    AuthorizationError,
    issue_authorization,
    verify_authorization,
)
from fnos.lab.errors import PowerUnknown, Refused  # noqa: E402
from fnos.lab.fakes import fake_identity  # noqa: E402
from fnos.lab.flash import (  # noqa: E402
    FileFlashBackend,
    FlashFailure,
    MacDiskutilBackend,
    compare_identity,
    expected_identity,
    parse_identity,
    readback_image,
    require_full_readback,
    write_image,
)
from fnos.lab.inspect import load_fixture  # noqa: E402
from fnos.lab.locks import (  # noqa: E402
    adopt_inherited_lock,
    board_lock,
    board_lock_path,
    lock_root,
)
from fnos.lab.model import STATE_SCHEMA  # noqa: E402
from fnos.lab.plan import build_plan, load_plan, sign_plan  # noqa: E402
from fnos.lab.power import (
    Cp5CliClient,
    Cp5PowerBackend,
    FakeCp5Client,
    PowerManager,
)  # noqa: E402
from fnos.lab.registry import (  # noqa: E402
    access_matrix,
    require_path_enabled,
)
from fnos.lab.runner import BackendSet, execute  # noqa: E402
from fnos.lab.serial import FakeSerialCapture, PosixSerialCapture  # noqa: E402
from fnos.lab.state import RunStore  # noqa: E402
from fnos.lab.status import inspect_run  # noqa: E402
from fnos.lab.util import secure_dir  # noqa: E402

CLI = [sys.executable, "-m", "fnos.lab"]
BASE_ENV = {
    **os.environ,
    "PYTHONPATH": str(ROOT),
    "PYTHONDONTWRITEBYTECODE": "1",
}


def run_cli(*args: str, env: dict[str, str] | None = None, timeout: float = 90):
    merged = dict(BASE_ENV)
    if env:
        merged.update(env)
    return subprocess.run(
        CLI + list(args),
        cwd=ROOT,
        env=merged,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._temp.name)
        self.state = self.tmp / "state"
        self.sleeps: list[float] = []
        self.image = self.tmp / "candidate.img"
        self.image.write_bytes(b"\x5a" * (1024 * 1024))
        self.plan_path = self.tmp / "plan.json"
        self._make_plan()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _make_plan(self) -> None:
        inventory = load_fixture(FIXTURE)
        plan = build_plan(
            inventory,
            board_id="x5-lab-01",
            image_path=self.image,
            target_selector="/dev/disk4",
            expect_serial="X5TEST0001",
            out_path=self.plan_path,
        )
        self.plan = load_plan(self.plan_path)

    def _target(self, *, size: int | None = None) -> Path:
        path = self.tmp / "target.img"
        path.touch()
        os.truncate(path, size or int(self.plan["target"]["size_bytes"]))
        return path

    def _backends(
        self,
        *,
        client: FakeCp5Client | None = None,
        serial: FakeSerialCapture | None = None,
        flash: FileFlashBackend | None = None,
    ) -> BackendSet:
        return BackendSet(
            power=Cp5PowerBackend(
                client or FakeCp5Client(value=True),
                board_id="x5-lab-01",
                sleep=lambda _: None,
            ),
            serial=serial or FakeSerialCapture(
                lines=["Hobot> ", "Linux version OLD", "login:"],
                boot_lines=["Linux version 6.18", "login:"],
                boot_record={"boot_id": "fake-boot-id", "rootfs": True},
                cut_safe=True,
            ),
            flash=flash or FileFlashBackend(self._target(), identity=fake_identity(self.plan)),
        )

    def _run(self, backends: BackendSet, **kwargs):
        engine, state = execute(
            self.plan,
            plan_path=self.plan_path,
            state_root=self.state,
            backend_factory=lambda _held: backends,
            execution_mode="fake",
            run_id=kwargs.pop("run_id", "run-test"),
            sleep=self.sleeps.append,
            **kwargs,
        )
        return engine, state, backends


class LockProtocolTests(Base):
    def test_cp5_boardlock_derives_the_same_path(self) -> None:
        import boardlock

        for board in ("x5-lab-01", "x5-lab-02", "a.b-c_d"):
            self.assertEqual(
                board_lock_path(self.state, board).name,
                boardlock.lock_path(board, self.state).name,
            )
            self.assertEqual(
                board_lock_path(self.state, board).parent,
                boardlock.lock_path(board, self.state).parent,
            )

    def test_cross_tool_cross_process_mutex_and_other_board_parallel(self) -> None:
        ready = self.tmp / "ready"
        holder = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "from fnos.lab.locks import board_lock\n"
            "with board_lock(Path(sys.argv[1]), 'x5-lab-01'):\n"
            "    Path(sys.argv[2]).write_text('ready')\n"
            "    time.sleep(30)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", holder, str(self.state), str(ready)],
            cwd=ROOT,
            env=BASE_ENV,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not ready.exists():
                if proc.poll() is not None:
                    self.fail("holder exited: " + (proc.stderr.read() or ""))
                time.sleep(0.03)
            self.assertTrue(ready.exists())
            code = (
                "import json, sys, boardlock\n"
                "try:\n"
                "    with boardlock.held_for_board('x5-lab-01'):\n"
                "        print('BAD')\n"
                "except boardlock.LockError as exc:\n"
                "    print(json.dumps({'busy': True})); sys.exit(2)\n"
            )
            child = subprocess.run(
                [sys.executable, "-c", code],
                cwd=CP5_SCRIPTS,
                env={
                **BASE_ENV,
                "FNOS_BOARD_LOCK_ROOT": str(self.state),
                "PYTHONPATH": f"{ROOT}:{CP5_SCRIPTS}",
            },
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(child.returncode, 2, child.stdout + child.stderr)
            self.assertNotIn("BAD", child.stdout)
            with board_lock(self.state, "x5-lab-02"):
                pass
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

    def test_inherited_fd_is_accepted_but_plain_fd_is_not(self) -> None:
        with board_lock(self.state, "x5-lab-01") as held:
            code = (
                "import os, sys\n"
                "from fnos.lab.locks import adopt_from_environment, close_inherited\n"
                "h = adopt_from_environment()\n"
                "close_inherited(h)\n"
                "print('ADOPTED')\n"
            )
            env = {**BASE_ENV, **held.child_env()}
            child = subprocess.run(
                [sys.executable, "-c", code],
                cwd=ROOT,
                env=env,
                pass_fds=held.pass_fds,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
            self.assertIn("ADOPTED", child.stdout)
            with self.assertRaises(Refused):
                with board_lock(self.state, "x5-lab-01"):
                    pass

            # A readable fd to the same file, but without the held lock, is
            # not a capability.
            probe = os.open(held.path, os.O_RDONLY)
            try:
                with self.assertRaises(Refused):
                    adopt_inherited_lock(probe, held.path, "x5-lab-01")
            finally:
                os.close(probe)



    def test_cp5_target_board_binding_cannot_be_swapped(self) -> None:
        venv = Path.home() / ".local/share/mijia-cp5/venv/bin/python"
        if not venv.is_file():
            self.skipTest("CP5 runtime is not installed")
        code = (
            "import cp5, sys\n"
            "for binding, board in (({}, 'x5-lab-01'),\n"
            "                       ({'board_id': 'x5-lab-02'}, 'x5-lab-01')):\n"
            "    try:\n"
            "        cp5.require_board_binding(binding, board)\n"
            "    except cp5.Stop:\n"
            "        continue\n"
            "    print('BAD accepted'); sys.exit(1)\n"
            "cp5.require_board_binding({'board_id': 'x5-lab-01'}, 'x5-lab-01')\n"
            "print('OK')\n"
        )
        proc = subprocess.run(
            [str(venv), "-c", code],
            cwd=CP5_SCRIPTS,
            env={**BASE_ENV, "PYTHONPATH": str(CP5_SCRIPTS)},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("OK", proc.stdout)


    def test_cp5_usb_probe_requires_board_and_never_reaches_controller(self) -> None:
        venv = Path.home() / ".local/share/mijia-cp5/venv/bin/python"
        script = CP5_SCRIPTS / "cp5.py"
        if not venv.is_file() or not script.is_file():
            self.skipTest("CP5 runtime is not installed")
        no_board = subprocess.run(
            [
                str(venv),
                str(script),
                "probe",
                "--port",
                "usb",
                "--seconds",
                "1",
                "--load-ready",
            ],
            cwd=CP5_SCRIPTS,
            env={**BASE_ENV, "FNOS_BOARD_LOCK_ROOT": str(self.state)},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(no_board.returncode, 2, no_board.stdout + no_board.stderr)
        self.assertIn("require --board", no_board.stderr)

        with board_lock(self.state, "x5-lab-01"):
            busy = subprocess.run(
                [
                    str(venv),
                    str(script),
                    "probe",
                    "--port",
                    "usb",
                    "--board",
                    "x5-lab-01",
                    "--seconds",
                    "1",
                    "--load-ready",
                ],
                cwd=CP5_SCRIPTS,
                env={**BASE_ENV, "FNOS_BOARD_LOCK_ROOT": str(self.state)},
                capture_output=True,
                text=True,
                timeout=20,
            )
        self.assertEqual(busy.returncode, 2, busy.stdout + busy.stderr)
        self.assertIn("busy", busy.stderr)

        # Even with a free lock and a valid board binding, USB probe must stop
        # before Controller/API code.
        code = (
            "import sys\n"
            "from types import SimpleNamespace\n"
            "import cp5\n"
            "cp5.load_binding = lambda: {'board_id': 'x5-lab-01'}\n"
            "cp5.require_board_binding = lambda binding, board: None\n"
            "class Bomb:\n"
            "    def __init__(self, *a, **k):\n"
            "        print('CONTROLLER-REACHED'); raise SystemExit(1)\n"
            "cp5.Controller = Bomb\n"
            "args = SimpleNamespace(action='probe', board='x5-lab-01', lock_root=sys.argv[1], lock_fd=None, load_ready=True, seconds=1)\n"
            "try:\n"
            "    cp5.usb_entry(args)\n"
            "except cp5.Stop as exc:\n"
            "    print('STOP', exc); sys.exit(0)\n"
            "print('BAD-NO-STOP'); sys.exit(1)\n"
        )
        probe = subprocess.run(
            [str(venv), "-c", code, str(self.state)],
            cwd=CP5_SCRIPTS,
            env={**BASE_ENV, "PYTHONPATH": str(CP5_SCRIPTS)},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)
        self.assertIn("STOP", probe.stdout)
        self.assertNotIn("CONTROLLER-REACHED", probe.stdout)


    def test_ac_probe_still_uses_the_old_ac_path_without_board_lock(self) -> None:
        venv = Path.home() / ".local/share/mijia-cp5/venv/bin/python"
        if not venv.is_file():
            self.skipTest("CP5 runtime is not installed")
        code = (
            "import contextlib, sys\n"
            "import cp5\n"
            "cp5.network_guard = lambda: None\n"
            "cp5.lock = lambda: contextlib.nullcontext()\n"
            "cp5.api_client = lambda: object()\n"
            "cp5.ensure_auth = lambda api: None\n"
            "cp5.load_binding = lambda: {'did': 'd', 'model': 'cuco.plug.cp5d', 'spec_type': cp5.MODELS['cuco.plug.cp5d'], 'ports': {p: {'mapping_verified': True} for p in cp5.PORTS}}\n"
            "cp5.selected_target = lambda binding, port: {**binding, 'port': port, 'mapping_verified': True}\n"
            "class Ctl:\n"
            "    def __init__(self, api, target): self.target = target\n"
            "    def probe_cycle(self, seconds): print('AC-PROBE', self.target['port'], seconds)\n"
            "    def state(self): return True\n"
            "cp5.Controller = Ctl\n"
            "def no_boardlock(*a, **k):\n"
            "    print('BOARDLOCK-REACHED'); raise SystemExit(1)\n"
            "cp5.boardlock.held_for_board = no_boardlock\n"
            "sys.argv = ['cp5', 'probe', '--port', 's1', '--seconds', '1', '--load-ready']\n"
            "cp5.main()\n"
        )
        proc = subprocess.run(
            [str(venv), "-c", code],
            cwd=CP5_SCRIPTS,
            env={**BASE_ENV, "PYTHONPATH": str(CP5_SCRIPTS)},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("AC-PROBE s1 1.0", proc.stdout)
        self.assertNotIn("BOARDLOCK-REACHED", proc.stdout)

    def test_cp5_usb_entry_refuses_busy_lock_and_requires_board(self) -> None:
        venv = Path.home() / ".local/share/mijia-cp5/venv/bin/python"
        script = CP5_SCRIPTS / "cp5.py"
        if not venv.is_file() or not script.is_file():
            self.skipTest("CP5 runtime is not installed")
        with board_lock(self.state, "x5-lab-01"):
            proc = subprocess.run(
                [
                    str(venv),
                    str(script),
                    "usb-status",
                    "--board",
                    "x5-lab-01",
                ],
                cwd=CP5_SCRIPTS,
                env={
                **BASE_ENV,
                "FNOS_BOARD_LOCK_ROOT": str(self.state),
                "PYTHONPATH": f"{ROOT}:{CP5_SCRIPTS}",
            },
                capture_output=True,
                text=True,
                timeout=20,
            )
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("busy", proc.stderr)
        no_board = subprocess.run(
            [str(venv), str(script), "on", "--port", "usb"],
            cwd=CP5_SCRIPTS,
            env={
                **BASE_ENV,
                "FNOS_BOARD_LOCK_ROOT": str(self.state),
                "PYTHONPATH": f"{ROOT}:{CP5_SCRIPTS}",
            },
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(no_board.returncode, 2)
        self.assertIn("require --board", no_board.stderr)


    def test_child_backend_adopts_inherited_fd_without_self_deadlock(self) -> None:
        from unittest import mock

        script = self.tmp / "fake_cp5.py"
        script.write_text(
            "import boardlock, json, os, sys\n"
            "fd = int(os.environ['FNOS_BOARD_LOCK_FD'])\n"
            "with boardlock.held_for_board('x5-lab-01', lock_fd=fd):\n"
            "    value = sys.argv[1] != 'usb-off'\n"
            "    print(json.dumps({'reported_state': value}))\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"FNOS_LAB_ENABLE_REAL_BACKENDS": "1"}):
            with board_lock(self.state, "x5-lab-01") as held:
                client = Cp5CliClient(
                    script,
                    board_id="x5-lab-01",
                    lock=held,
                    python=sys.executable,
                    extra_env={"PYTHONPATH": str(CP5_SCRIPTS)},
                )
                self.assertTrue(client.read_usb())
                client.set_usb(False)
                client.set_usb(True)


    def test_forged_fd_with_copied_metadata_and_offset_is_rejected(self) -> None:
        with board_lock(self.state, "x5-lab-01") as held:
            code = (
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "from fnos.lab.locks import adopt_inherited_lock, board_lock_path\n"
                "path = board_lock_path(Path(sys.argv[1]), 'x5-lab-01')\n"
                "fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)\n"
                "os.lseek(fd, 0, os.SEEK_END)\n"
                "meta = json.loads(os.pread(fd, 8192, 0).decode())\n"
                "assert meta['board_id'] == 'x5-lab-01'\n"
                "try:\n"
                "    adopt_inherited_lock(fd, path, 'x5-lab-01', expected_lock_id=meta['lock_id'])\n"
                "except Exception as exc:\n"
                "    print('REFUSED', type(exc).__name__)\n"
                "else:\n"
                "    print('ADOPTED-BAD'); sys.exit(1)\n"
            )
            child = subprocess.run(
                [sys.executable, "-c", code, str(self.state)],
                cwd=ROOT,
                env=BASE_ENV,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
            self.assertIn("REFUSED", child.stdout)
            self.assertNotIn("ADOPTED-BAD", child.stdout)
            # Parent's lock must still be the same live lock.
            with self.assertRaises(Refused):
                with board_lock(self.state, "x5-lab-01"):
                    pass

    def test_cp5_boardlock_rejects_forged_fd_too(self) -> None:
        import boardlock

        with board_lock(self.state, "x5-lab-01"):
            path = board_lock_path(self.state, "x5-lab-01")
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
            try:
                os.lseek(fd, 0, os.SEEK_END)
                meta = json.loads(os.pread(fd, 8192, 0).decode())
                with self.assertRaises(boardlock.LockError):
                    boardlock._adopt(fd, path, "x5-lab-01", meta["lock_id"])
            finally:
                os.close(fd)

    def test_path_replacement_is_detected(self) -> None:
        with board_lock(self.state, "x5-lab-01") as held:
            held.revalidate()
            moved = held.path.with_suffix(".moved")
            os.replace(held.path, moved)
            try:
                with self.assertRaises(Refused):
                    held.revalidate()
            finally:
                os.replace(moved, held.path)

    def test_crashed_lock_owner_releases_and_does_not_poison_file(self) -> None:
        code = (
            "import sys\n"
            "from pathlib import Path\n"
            "from fnos.lab.locks import board_lock\n"
            "with board_lock(Path(sys.argv[1]), 'x5-lab-01'):\n"
            "    print('locked', flush=True)\n"
            "    raise SystemExit(9)\n"
        )
        child = subprocess.run(
            [sys.executable, "-c", code, str(self.state)],
            cwd=ROOT,
            env=BASE_ENV,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(child.returncode, 9)
        with board_lock(self.state, "x5-lab-01"):
            pass

    def test_lock_root_environment_override_matches_protocol(self) -> None:
        override = self.tmp / "other-root"
        env = {**BASE_ENV, "FNOS_BOARD_LOCK_ROOT": str(override)}
        code = (
            "import json, os\n"
            "from fnos.lab.locks import lock_root\n"
            "print(json.dumps(str(lock_root())))\n"
        )
        child = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout), str(override))


class PowerBackendTests(Base):
    def test_only_usb_switch_is_read_or_written(self) -> None:
        client = FakeCp5Client(value=True)
        backend = Cp5PowerBackend(client, board_id="x5-lab-01", sleep=lambda _: None)
        self.assertEqual(backend.read(), "on")
        backend.set(False)
        self.assertEqual(client.calls, [{"siid": 7, "piid": 1, "value": False}])
        self.assertEqual(backend.verify(False), "off")
        self.assertNotIn("master", json.dumps(client.calls))
        self.assertNotIn("toggle", json.dumps(client.calls))

    def test_read_timeout_is_unknown_not_success(self) -> None:
        client = FakeCp5Client(read_error=TimeoutError("lost"))
        backend = Cp5PowerBackend(client, board_id="x5-lab-01", sleep=lambda _: None)
        with self.assertRaises(PowerUnknown):
            backend.read()

    def test_write_timeout_is_unknown_and_not_retried(self) -> None:
        client = FakeCp5Client(value=True, write_error=TimeoutError("lost"))
        backend = Cp5PowerBackend(client, board_id="x5-lab-01", sleep=lambda _: None)
        with self.assertRaises(PowerUnknown) as ctx:
            backend.set(False)
        self.assertEqual(ctx.exception.action, "off")
        self.assertEqual(len(client.calls), 1)

    def _manager(self, phase: str, *, first_boot: bool = False):
        client = FakeCp5Client(value=True)
        events = []
        backend = Cp5PowerBackend(client, board_id="x5-lab-01", sleep=lambda _: None)
        manager = PowerManager(
            backend,
            phase=lambda: phase,
            emit=lambda _kind, **fields: events.append((_kind, fields)),
            save=lambda: None,
            first_boot_expansion=lambda: first_boot,
            sleep=lambda _: None,
        )
        return manager, client, events

    def test_off_forbidden_in_every_protected_phase(self) -> None:
        for phase in ("writing", "written", "reading", "booting", "validating"):
            with self.subTest(phase=phase):
                manager, client, events = self._manager(phase)
                with self.assertRaises(Refused):
                    manager.turn_off("test")
                self.assertEqual(client.calls, [])
                self.assertTrue(any(kind == "power_off_refused" for kind, _ in events))

    def test_off_forbidden_during_first_boot_expansion(self) -> None:
        manager, client, _ = self._manager("preflight", first_boot=True)
        with self.assertRaises(Refused):
            manager.turn_off("test")
        self.assertEqual(client.calls, [])

    def test_cancel_kind_cannot_cut_power(self) -> None:
        manager, client, _ = self._manager("preflight")
        with self.assertRaises(Refused):
            manager.turn_off("cancel", kind="cancel")
        self.assertEqual(client.calls, [])


    def test_off_requires_safe_to_cut_evidence(self) -> None:
        manager, client, events = self._manager("preflight")
        with self.assertRaises(Refused):
            manager.turn_off("test")
        self.assertEqual(client.calls, [])
        self.assertTrue(any(kind == "power_cut_refused" for kind, _ in events))

    def test_normal_shutdown_is_recorded_separately_from_cancel(self) -> None:
        manager, client, events = self._manager("preflight")
        manager.turn_off("normal-shutdown", kind="normal_shutdown", safe_to_cut=True)
        self.assertEqual(client.calls, [{"siid": 7, "piid": 1, "value": False}])
        self.assertTrue(
            any(
                kind == "power_operation_selected"
                and fields.get("operation") == "normal_shutdown"
                for kind, fields in events
            )
        )
        self.assertFalse(
            any(
                kind == "power_operation_selected"
                and fields.get("operation") == "cancel"
                for kind, fields in events
            )
        )


class FlashBackendTests(Base):
    def test_full_readback_and_hash_match(self) -> None:
        target = self._target()
        backend = FileFlashBackend(target, identity=fake_identity(self.plan))
        fd = backend.open_target()
        try:
            from fnos.lab.flash import open_trusted_image

            image = open_trusted_image(self.plan)
            try:
                result = write_image(backend, fd, image, cancel=lambda: False)
                self.assertTrue(result.complete)
                sha, total, complete = readback_image(
                    backend, fd, image, cancel=lambda: False
                )
                require_full_readback(
                    sha, image, bytes_read=total, complete=complete
                )
            finally:
                image.close()
        finally:
            backend.close_target(fd)

    def test_short_readback_and_hash_mismatch_fail(self) -> None:
        target = self._target()
        backend = FileFlashBackend(target, identity=fake_identity(self.plan))
        fd = backend.open_target()
        try:
            from fnos.lab.flash import open_trusted_image

            image = open_trusted_image(self.plan)
            try:
                write_image(backend, fd, image, cancel=lambda: False)
                with mock.patch("fnos.lab.flash.read_exact", return_value=b"short"):
                    with self.assertRaises(FlashFailure):
                        readback_image(backend, fd, image, cancel=lambda: False)
                with self.assertRaises(FlashFailure):
                    require_full_readback(
                        "0" * 64, image, bytes_read=image.size_bytes, complete=True
                    )
            finally:
                image.close()
        finally:
            backend.close_target(fd)


    def test_real_identity_topology_matches_inspect_convention(self) -> None:
        backend = object.__new__(MacDiskutilBackend)
        backend.device = "/dev/disk4"
        info = {
            "DeviceNode": "/dev/disk4",
            "SerialNumber": "X5TEST0001",
            "MediaName": "UMS disk 0",
            "TotalSize": 31_293_702_144,
            "BusProtocol": "USB",
            "WholeDisk": True,
            "Internal": False,
            "Writable": False,
            "VirtualOrPhysical": "Physical",
        }
        with mock.patch.object(MacDiskutilBackend, "_disk_info", return_value=info), mock.patch.object(
            MacDiskutilBackend, "_mount_points", return_value=()
        ):
            identity = backend.identify()
        self.assertEqual(identity.topology, "USB:UMS disk 0:disk4")
        self.assertTrue(identity.read_only)
        self.assertFalse(identity.internal)

    def test_file_backend_refuses_dev_path(self) -> None:
        with self.assertRaises(Refused):
            FileFlashBackend(Path("/dev/disk4"), identity=fake_identity(self.plan))

    def test_target_identity_change_is_rejected(self) -> None:
        meta = self.tmp / "meta.json"
        raw = fake_identity(self.plan).as_dict()
        meta.write_text(json.dumps(raw), encoding="utf-8")
        backend = FileFlashBackend(self._target(), metadata_path=meta)
        compare_identity(backend.identify(), expected_identity(self.plan))
        for key, value in (
            ("device", "/dev/disk9"),
            ("serial", "OTHER"),
            ("size_bytes", 1),
            ("bus", "PCI-Express"),
            ("mounted", True),
            ("internal", True),
            ("topology", "changed"),
        ):
            with self.subTest(key=key):
                changed = dict(raw)
                changed[key] = value
                meta.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaises(FlashFailure):
                    compare_identity(backend.identify(), expected_identity(self.plan))

    def test_image_replacement_and_truncation_are_refused(self) -> None:
        self.image.write_bytes(b"replaced")
        with self.assertRaises(Refused):
            from fnos.lab.flash import open_trusted_image

            open_trusted_image(self.plan)


class RunnerTests(Base):
    def test_normal_fake_run_passes_and_actions_are_not_real(self) -> None:
        engine, state, _ = self._run(self._backends())
        self.assertEqual(state["status"], "passed")
        self.assertTrue(state["media"]["verified"])
        self.assertEqual(self.sleeps, [5, 5])
        self.assertTrue(state["validation"]["complete"])
        self.assertTrue(state["validation"]["boot_id_verified"])
        self.assertEqual(state["validation"]["source"], "fake")
        self.assertFalse(state["validation"]["hardware_acceptance"])
        self.assertEqual(state["power"]["hold_seconds"], 5)
        self.assertIsNotNone(state["power"]["last_off_at"])
        self.assertIsNotNone(state["power"]["hold_completed_at"])
        self.assertFalse(state["execution"]["write_eligible"])
        self.assertEqual(
            state["hardware_actions"],
            {
                "serial_opened": False,
                "power_api_called": False,
                "disk_written": False,
                "ssh_connected": False,
            },
        )
        events = RunStore(self.state, "run-test").read_events()
        serial_indices = [
            i
            for i, event in enumerate(events)
            if event["kind"] in ("serial_listen_started", "serial_capture_ready")
        ]
        power_indices = [i for i, e in enumerate(events) if e["kind"] == "power_action"]
        self.assertTrue(serial_indices and power_indices)
        self.assertLess(max(serial_indices), min(power_indices))


    def test_cancel_interface_persists_request_without_power_action(self) -> None:
        store = RunStore(self.state, "run-cancel-cli")
        store.create(
            {
                "schema": STATE_SCHEMA,
                "run_id": "run-cancel-cli",
                "simulated": True,
                "board_id": "x5-lab-01",
                "phase": "writing",
                "status": "running",
                "pid": os.getpid(),
                "power": {"state": "on", "last_action": "on", "last_result": "confirmed"},
                "media": {"write_started": True, "verified": False},
            }
        )
        proc = run_cli(
            "cancel", "--state-dir", str(self.state), "--run", "run-cancel-cli"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        state = store.load()
        self.assertTrue(state["cancel_requested"])
        self.assertEqual(state["power"]["state"], "on")
        self.assertTrue((store.run_dir / "cancel.request").is_file())
        events = store.read_events()
        self.assertTrue(
            any(e["kind"] == "cancel_requested" and e.get("power_cut") is False for e in events)
        )


    def test_normal_shutdown_is_an_explicit_separate_operation(self) -> None:
        engine, state, backends = self._run(self._backends(), run_id="run-shutdown")
        self.assertEqual(state["status"], "passed")
        result = engine.normal_shutdown(safe_to_cut=True)
        self.assertEqual(result, "off")
        self.assertEqual(state["interruption"]["kind"], "normal_shutdown")
        self.assertEqual(state["power"]["state"], "off")
        self.assertEqual(
            [c["value"] for c in backends.power.client.calls], [False, True, False, True, False]
        )

    def test_cancel_before_power_does_not_touch_power(self) -> None:
        backend = self._backends()
        with mock.patch("fnos.lab.runner.CancelSignal.is_set", return_value=True):
            engine, state, _ = self._run(backend, run_id="run-cancel-pre")
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["power"]["state"], "unknown")
        self.assertEqual(backend.power.client.calls, [])
        self.assertFalse(state["media"]["write_started"])


    def test_stale_old_serial_lines_cannot_pass_a_new_boot(self) -> None:
        serial = FakeSerialCapture(
            lines=["Hobot> ", "Linux version OLD", "login:"],
            boot_lines=[],
            boot_record=None,
            cut_safe=True,
        )
        engine, state, _ = self._run(
            self._backends(serial=serial), run_id="run-stale-boot"
        )
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["failure"]["code"], "boot_evidence_missing")
        self.assertTrue(state["media"]["verified"])
        self.assertIsNone(state["validation"])
        events = RunStore(self.state, "run-stale-boot").read_events()
        self.assertNotIn("run_passed", [e["kind"] for e in events])
        self.assertNotIn("validation_passed", [e["kind"] for e in events])

    def test_new_banner_only_is_incomplete_and_never_passed(self) -> None:
        serial = FakeSerialCapture(
            lines=["Hobot> "],
            boot_lines=["Linux version 6.18"],
            boot_record=None,
            cut_safe=True,
        )
        engine, state, _ = self._run(
            self._backends(serial=serial), run_id="run-banner-only"
        )
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["failure"]["code"], "boot_evidence_incomplete")
        self.assertFalse(state["validation"]["complete"])
        self.assertFalse(state["validation"]["login_prompt"])
        self.assertNotIn(
            "run_passed",
            [e["kind"] for e in RunStore(self.state, "run-banner-only").read_events()],
        )

    def test_boot_evidence_requires_expected_kernel_match(self) -> None:
        serial = FakeSerialCapture(
            lines=[],
            boot_lines=["Linux version 5.10", "login:"],
            boot_record={"boot_id": "fake", "rootfs": True},
            cut_safe=True,
        )
        serial.start()
        serial.on_power_on()
        evidence = serial.boot_evidence(0, expected_kernel="6.18")
        self.assertFalse(evidence.complete)
        self.assertIn("does not match", evidence.reason)


    def test_expected_kernel_mismatch_blocks_pass(self) -> None:
        inventory = load_fixture(FIXTURE)
        inventory.boards["x5-lab-01"]["expected_kernel"] = "6.18"
        plan_path = self.tmp / "plan-kernel.json"
        plan = build_plan(
            inventory,
            board_id="x5-lab-01",
            image_path=self.image,
            target_selector="/dev/disk4",
            expect_serial="X5TEST0001",
            out_path=plan_path,
        )
        self.plan = load_plan(plan_path)
        serial = FakeSerialCapture(
            lines=["Hobot> "],
            boot_lines=["Linux version 5.10", "login:"],
            boot_record={"boot_id": "fake", "rootfs": True},
            cut_safe=True,
        )
        backend = self._backends(serial=serial)
        engine, state = execute(
            self.plan,
            plan_path=plan_path,
            state_root=self.state,
            backend_factory=lambda _h: backend,
            execution_mode="fake",
            run_id="run-kernel-mismatch",
            sleep=self.sleeps.append,
        )
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["failure"]["code"], "boot_evidence_incomplete")
        self.assertIn("does not match", state["validation"]["reason"])
        self.assertFalse(state["validation"]["complete"])

    def test_real_passive_boot_evidence_never_claims_complete(self) -> None:
        capture = object.__new__(PosixSerialCapture)
        capture._buffer = bytearray(b"Linux version 6.18\nlogin:\n")
        capture._lock = threading.Lock()
        evidence = capture.boot_evidence(0, expected_kernel="6.18")
        self.assertFalse(evidence.complete)
        self.assertIsNone(evidence.boot_id)
        self.assertFalse(evidence.rootfs_mounted)
        self.assertIn("not implemented", evidence.reason)
        self.assertFalse(capture.cut_safe)

    def test_safe_cut_gate_refuses_before_any_power_call(self) -> None:
        serial = FakeSerialCapture(
            lines=["Linux version OLD", "login:"],
            boot_lines=["Linux version 6.18", "login:"],
            boot_record={"boot_id": "fake", "rootfs": True},
            cut_safe=False,
        )
        backend = self._backends(serial=serial)
        engine, state, _ = self._run(backend, run_id="run-unsafe-cut")
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["failure"]["code"], "safe_cut_not_verified")
        self.assertEqual(backend.power.client.calls, [])
        self.assertEqual(self.sleeps, [])
        self.assertEqual(state["power"]["state"], "on")

    def test_cancel_during_off_hold_still_restores_power(self) -> None:
        cancel_path = self.state / "runs" / "run-hold-cancel" / "cancel.request"
        sleeps: list[float] = []

        def sleeper(seconds: float) -> None:
            sleeps.append(seconds)
            cancel_path.parent.mkdir(parents=True, exist_ok=True)
            cancel_path.write_text(
                json.dumps({"schema": "fnos-lab-cancel/v1", "reason": "hold"}),
                encoding="utf-8",
            )

        backend = self._backends()
        engine, state = execute(
            self.plan,
            plan_path=self.plan_path,
            state_root=self.state,
            backend_factory=lambda _h: backend,
            execution_mode="fake",
            run_id="run-hold-cancel",
            sleep=sleeper,
        )
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(sleeps, [5])
        self.assertEqual(
            [c["value"] for c in backend.power.client.calls], [False, True]
        )
        self.assertEqual(state["power"]["state"], "on")
        events = RunStore(self.state, "run-hold-cancel").read_events()
        self.assertIn("power_hold_started", [e["kind"] for e in events])
        self.assertIn("power_hold_completed", [e["kind"] for e in events])

    def test_cut_hold_below_approved_minimum_is_refused(self) -> None:
        with self.assertRaises(Refused):
            execute(
                self.plan,
                plan_path=self.plan_path,
                state_root=self.state,
                backend_factory=lambda _h: self._backends(),
                execution_mode="fake",
                run_id="run-short-hold",
                cut_hold_seconds=1,
            )

    def test_cancel_during_write_waits_for_completion_and_skips_boot(self) -> None:
        target = self._target()
        backend = self._backends()
        backend.flash = FileFlashBackend(target, identity=fake_identity(self.plan))
        cancel_path = self.state / "runs" / "run-cancel-write" / "cancel.request"
        backend.flash.write_hook = lambda _n: cancel_path.parent.mkdir(
            parents=True, exist_ok=True
        ) or cancel_path.write_text(
            json.dumps({"schema": "fnos-lab-cancel/v1", "reason": "test"})
        )
        engine, state, _ = self._run(backend, run_id="run-cancel-write")
        self.assertEqual(state["status"], "cancelled")
        self.assertTrue(state["media"]["write_complete"])
        self.assertFalse(state["media"]["readback_started"])
        self.assertTrue(state["media"]["incomplete"])
        self.assertEqual(state["power"]["state"], "on")
        events = RunStore(self.state, "run-cancel-write").read_events()
        self.assertNotIn("boot_simulated", [e["kind"] for e in events])
        self.assertFalse(
            any(
                e["kind"] == "power_action" and e["action"] == "off" and e["origin"] == "cancel"
                for e in events
            )
        )

    def test_cancel_during_readback_never_claims_verified(self) -> None:
        target = self._target()
        backend = self._backends()
        backend.flash = FileFlashBackend(target, identity=fake_identity(self.plan))
        cancel_path = self.state / "runs" / "run-cancel-read" / "cancel.request"
        backend.flash.read_hook = lambda _n: cancel_path.parent.mkdir(
            parents=True, exist_ok=True
        ) or cancel_path.write_text(
            json.dumps({"schema": "fnos-lab-cancel/v1", "reason": "test"})
        )
        engine, state, _ = self._run(backend, run_id="run-cancel-read")
        self.assertEqual(state["status"], "cancelled")
        self.assertFalse(state["media"]["verified"])
        self.assertFalse(state["media"]["readback_complete"])
        self.assertEqual(state["power"]["state"], "on")

    def test_hash_mismatch_blocks_boot_and_keeps_power(self) -> None:
        target = self._target()
        backend = self._backends()
        backend.flash = FileFlashBackend(target, identity=fake_identity(self.plan))

        def corrupt(_n: int) -> None:
            with target.open("r+b") as stream:
                stream.seek(0)
                stream.write(b"X")

        backend.flash.write_hook = corrupt
        engine, state, _ = self._run(backend, run_id="run-hash")
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["failure"]["code"], "hash_mismatch")
        self.assertFalse(state["media"]["verified"])
        self.assertTrue(state["media"]["incomplete"])
        self.assertEqual(state["power"]["state"], "on")
        events = RunStore(self.state, "run-hash").read_events()
        self.assertNotIn("boot_simulated", [e["kind"] for e in events])

    def test_target_change_after_write_blocks_readback_and_boot(self) -> None:
        meta = self.tmp / "meta.json"
        raw = fake_identity(self.plan).as_dict()
        meta.write_text(json.dumps(raw), encoding="utf-8")
        backend = self._backends()
        backend.flash = FileFlashBackend(self._target(), metadata_path=meta)
        backend.flash.write_hook = lambda _n: meta.write_text(
            json.dumps({**raw, "size_bytes": 1}), encoding="utf-8"
        )
        engine, state, _ = self._run(backend, run_id="run-target-change")
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["failure"]["code"], "target_changed")
        self.assertFalse(state["media"]["verified"])
        self.assertEqual(state["power"]["state"], "on")
        self.assertNotIn(
            "boot_simulated",
            [e["kind"] for e in RunStore(self.state, "run-target-change").read_events()],
        )

    def test_planned_fault_is_distinct_from_cancel_and_leaves_power_off(self) -> None:
        engine, state, _ = self._run(
            self._backends(),
            run_id="run-planned",
            allow_planned_fault=True,
            planned_fault_at="writing",
        )
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["failure"]["code"], "planned_power_fault")
        self.assertEqual(state["power"]["state"], "off")
        self.assertTrue(state["media"]["incomplete"])
        events = RunStore(self.state, "run-planned").read_events()
        self.assertIn("planned_power_fault_injected", [e["kind"] for e in events])
        self.assertNotIn("run_cancelled", [e["kind"] for e in events])

    def test_crash_persists_interrupted_state_and_status_never_resumes(self) -> None:
        proc = run_cli(
            "run",
            "--plan",
            str(self.plan_path),
            "--state-dir",
            str(self.state),
            "--run-id",
            "run-crash",
            "--backends",
            "fake",
            "--crash-at",
            "writing",
        )
        self.assertEqual(proc.returncode, 70, proc.stdout + proc.stderr)
        time.sleep(0.2)
        report = json.loads(
            run_cli("status", "--state-dir", str(self.state), "--run", "run-crash").stdout
        )
        self.assertEqual(report["status"], "interrupted")
        self.assertFalse(report["resume_allowed"])
        self.assertFalse(report["recorded_process_alive"])
        self.assertTrue(report["media"]["incomplete"])

    def test_status_reports_residual_writer(self) -> None:
        self._run(self._backends(), run_id="run-residual")
        store = RunStore(self.state, "run-residual")
        state = store.load()
        sleeper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            state["status"] = "running"
            state["pid"] = os.getpid()
            state["active_children"] = [{"pid": sleeper.pid, "kind": "flash"}]
            store.save(state)
            report = inspect_run(self.state, "run-residual")
            self.assertTrue(report["residual_write_process"])
            self.assertTrue(report["active_children"])
        finally:
            sleeper.terminate()
            sleeper.wait(timeout=5)


class AuthorizationTests(Base):
    def test_token_binds_plan_image_target_and_expiry(self) -> None:
        token = issue_authorization(self.plan, state_root=self.state, ttl_seconds=60)
        verify_authorization(token, self.plan, state_root=self.state)
        changed = json.loads(json.dumps(self.plan))
        changed["target"]["serial"] = "OTHER"
        with self.assertRaises(AuthorizationError):
            verify_authorization(token, changed, state_root=self.state)
        tampered = dict(token)
        tampered["payload"] = {**token["payload"], "nonce": "0" * 32}
        with self.assertRaises(AuthorizationError):
            verify_authorization(tampered, self.plan, state_root=self.state)
        expired = issue_authorization(
            self.plan, state_root=self.state, ttl_seconds=30, now=time.time() - 3600
        )
        with self.assertRaises(AuthorizationError):
            verify_authorization(expired, self.plan, state_root=self.state)

    def test_plan_boolean_and_signature_do_not_authorize_real_run(self) -> None:
        forged = json.loads(json.dumps(self.plan))
        forged["simulated"] = False
        forged["execution"]["write_eligible"] = True
        forged["execution"]["authorized"] = True
        sign_plan(forged)
        with self.assertRaises(AuthorizationError):
            verify_authorization({}, forged, state_root=self.state)
        token = issue_authorization(self.plan, state_root=self.state)
        with self.assertRaises(AuthorizationError):
            verify_authorization(token, forged, state_root=self.state)


    def test_authorize_cli_writes_private_plan_bound_token(self) -> None:
        inventory = load_fixture(FIXTURE)
        inventory.simulated = False
        board = inventory.boards["x5-lab-01"]
        board["reviewed"] = True
        board["allowed_target_serials"] = ["X5TEST0001"]
        board["power"] = {
            "backend": "cp5-usb",
            "channel": "usb",
            "mapping_verified": True,
            "wired": True,
        }
        real_plan_path = self.tmp / "real-auth-plan.json"
        build_plan(
            inventory,
            board_id="x5-lab-01",
            image_path=self.image,
            target_selector="/dev/disk4",
            expect_serial="X5TEST0001",
            out_path=real_plan_path,
        )
        token_path = self.tmp / "authorization.json"
        proc = run_cli(
            "authorize",
            "--plan",
            str(real_plan_path),
            "--out",
            str(token_path),
            "--state-dir",
            str(self.state),
            "--confirm-board",
            "x5-lab-01",
            "--authorize-real-hardware",
            env={"FNOS_LAB_ENABLE_REAL_BACKENDS": "1"},
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
        token = json.loads(token_path.read_text(encoding="utf-8"))
        self.assertEqual(
            verify_authorization(token, load_plan(real_plan_path), state_root=self.state)[
                "board_id"
            ],
            "x5-lab-01",
        )

    def test_real_gate_is_off_by_default(self) -> None:
        from fnos.lab.authorization import real_gate_enabled

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FNOS_LAB_ENABLE_REAL_BACKENDS", None)
            self.assertFalse(real_gate_enabled())
        proc = run_cli(
            "authorize",
            "--plan",
            str(self.plan_path),
            "--out",
            str(self.tmp / "auth.json"),
            "--state-dir",
            str(self.state),
            "--confirm-board",
            "x5-lab-01",
            "--authorize-real-hardware",
            env={"FNOS_LAB_ENABLE_REAL_BACKENDS": ""},
        )
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("disabled by default", proc.stderr)
        self.assertFalse((self.tmp / "auth.json").exists())


class RegistryAndAdapterTests(Base):
    def _binding(self, *, reviewed: bool = True) -> dict:
        return {
            "schema": "fnos-board-bindings/v1",
            "boards": {
                "x5-lab-01": {
                    "board_id": "x5-lab-01",
                    "model": "RDK X5",
                    "reviewed": reviewed,
                    "power": {
                        "backend": "cp5-usb",
                        "channel": "usb",
                        "mapping_verified": True,
                        "wired": True,
                    },
                    "serial": {"port": "/dev/cu.test", "baud": 115200},
                    "allowed_target_serials": ["SERIAL"],
                    "legacy": {
                        "direct_flash": "not_integrated",
                        "direct_command": "blocked_by_guard",
                        "ha_power": "not_integrated",
                        "raw_device": "outside_file_lock",
                        "mijia_app": "outside_file_lock",
                        "manual_unplug": "outside_file_lock",
                    },
                }
            },
        }

    def test_all_access_paths_are_explicit(self) -> None:
        matrix = access_matrix(self._binding()["boards"]["x5-lab-01"])
        ids = {row["id"] for row in matrix}
        self.assertEqual(
            ids,
            {
                "fnos-lab-run",
                "mijia-cp5-usb",
                "legacy-flash-adapter",
                "legacy-flash-direct",
                "legacy-flash-command",
                "ha-power",
                "raw-device-write",
                "mijia-app",
                "manual-unplug",
            },
        )
        for row in matrix:
            if row["unified_lock"]:
                self.assertEqual(row["status"], "enforced_unified")
            elif row["id"] == "legacy-flash-command":
                self.assertEqual(row["status"], "blocked_by_guard")
                self.assertTrue(row["enforced"])
            elif row["id"] in ("legacy-flash-direct", "ha-power"):
                self.assertEqual(row["status"], "not_integrated")
                self.assertFalse(row["enforced"])


    def test_repository_legacy_flash_command_is_actually_blocked(self) -> None:
        script = ROOT / "scripts" / "flash-rdk-x5-sd.command"
        proc = subprocess.run(
            ["bash", str(script)],
            cwd=ROOT,
            env=BASE_ENV,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("REFUSED", proc.stderr)
        self.assertIn("not integrated", proc.stderr)
        self.assertNotIn("sudo", proc.stderr.lower())
        self.assertNotIn("FLASH AND READ-BACK", proc.stdout)

    def test_disabled_legacy_paths_are_refused(self) -> None:
        binding = self._binding()["boards"]["x5-lab-01"]
        for path in (
            "legacy-flash-direct",
            "legacy-flash-command",
            "ha-power",
            "raw-device-write",
            "mijia-app",
        ):
            with self.subTest(path=path), self.assertRaises(Refused):
                require_path_enabled(binding, path)
        require_path_enabled(binding, "fnos-lab-run")
        require_path_enabled(binding, "mijia-cp5-usb")
        require_path_enabled(binding, "legacy-flash-adapter")

    def _adapter(self, binding_path: Path, script: Path, *extra: str):
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "fnos" / "integration" / "hardware" / "legacy_flash_adapter.py"),
                "--board",
                "x5-lab-01",
                "--bindings",
                str(binding_path),
                "--image",
                str(self.image),
                "--sha256",
                __import__("hashlib").sha256(self.image.read_bytes()).hexdigest(),
                "--device",
                "/dev/disk9",
                "--target-size",
                "4096",
                "--report",
                str(self.tmp / "report.json"),
                "--legacy-script",
                str(script),
                "--lock-root",
                str(self.state),
                *extra,
            ],
            cwd=ROOT,
            env={
                **BASE_ENV,
                "FNOS_BOARD_LOCK_ROOT": str(self.state),
                "PYTHONPATH": f"{ROOT}:{CP5_SCRIPTS}",
            },
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_legacy_adapter_passes_the_canonical_lock_without_parent_hold(self) -> None:
        binding_path = self.tmp / "bindings.json"
        binding_path.write_text(json.dumps(self._binding()), encoding="utf-8")
        script = self.tmp / "fake_flash.py"
        script.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
        proc = self._adapter(binding_path, script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        args = json.loads(proc.stdout)
        self.assertIn("--lock", args)
        self.assertEqual(
            Path(args[args.index("--lock") + 1]),
            board_lock_path(self.state, "x5-lab-01"),
        )


    def test_legacy_adapter_child_is_blocked_by_lab_lock(self) -> None:
        binding_path = self.tmp / "bindings.json"
        binding_path.write_text(json.dumps(self._binding()), encoding="utf-8")
        script = self.tmp / "fake_flash_locked.py"
        script.write_text(
            "import boardlock, sys\n"
            "try:\n"
            "    with boardlock.held_for_board('x5-lab-01'):\n"
            "        print('ACQUIRED')\n"
            "except boardlock.LockError:\n"
            "    print('BUSY')\n"
            "    sys.exit(2)\n",
            encoding="utf-8",
        )
        with board_lock(self.state, "x5-lab-01"):
            proc = self._adapter(binding_path, script)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("BUSY", proc.stdout)
        self.assertNotIn("ACQUIRED", proc.stdout)

    def test_legacy_adapter_refuses_direct_and_parent_lock(self) -> None:
        binding_path = self.tmp / "bindings.json"
        binding_path.write_text(json.dumps(self._binding()), encoding="utf-8")
        script = self.tmp / "fake_flash.py"
        script.write_text("print('BAD')\n", encoding="utf-8")
        for extra, needle in (
            (("--direct-legacy",), "direct legacy"),
            (("--lock-fd", "3"), "cannot adopt"),
        ):
            with self.subTest(extra=extra):
                proc = self._adapter(binding_path, script, *extra)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn(needle, proc.stderr)
                self.assertNotIn("BAD", proc.stdout)

    def test_unreviewed_binding_is_refused(self) -> None:
        binding_path = self.tmp / "bindings.json"
        binding_path.write_text(json.dumps(self._binding(reviewed=False)), encoding="utf-8")
        script = self.tmp / "fake_flash.py"
        script.write_text("print('BAD')\n", encoding="utf-8")
        proc = self._adapter(binding_path, script)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("reviewed=true", proc.stderr)
        self.assertNotIn("BAD", proc.stdout)


    def test_real_plan_uses_usb_and_records_canonical_lock(self) -> None:
        inventory = load_fixture(FIXTURE)
        inventory.simulated = False
        board = inventory.boards["x5-lab-01"]
        board["reviewed"] = True
        board["allowed_target_serials"] = ["X5TEST0001"]
        board["power"] = {
            "backend": "cp5-usb",
            "channel": "usb",
            "mapping_verified": True,
            "wired": True,
        }
        out = self.tmp / "real-plan-ok.json"
        plan = build_plan(
            inventory,
            board_id="x5-lab-01",
            image_path=self.image,
            target_selector="/dev/disk4",
            expect_serial="X5TEST0001",
            out_path=out,
        )
        self.assertFalse(plan["simulated"])
        self.assertEqual(plan["execution"]["mode"], "hardware-required")
        self.assertTrue(plan["execution"]["real_backend_wired"])
        self.assertFalse(plan["execution"]["write_eligible"])
        self.assertEqual(plan["lock"]["protocol"], "fnos-board-lock/v1")
        self.assertEqual(
            plan["lock"]["path"], str(board_lock_path(None, "x5-lab-01"))
        )

    def test_real_plan_requires_usb_backend_and_channel(self) -> None:
        inventory = load_fixture(FIXTURE)
        inventory.simulated = False
        board = inventory.boards["x5-lab-01"]
        board["reviewed"] = True
        board["allowed_target_serials"] = ["X5TEST0001"]
        with self.assertRaises(Refused):
            build_plan(
                inventory,
                board_id="x5-lab-01",
                image_path=self.image,
                target_selector="/dev/disk4",
                expect_serial="X5TEST0001",
                out_path=self.tmp / "real-plan.json",
            )


if __name__ == "__main__":
    unittest.main()
