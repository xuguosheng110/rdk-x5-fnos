"""Hardware-free tests for the RDK X5 / fnOS laboratory executor.

Every test uses temporary files, fixtures or subprocesses.  No test opens a
serial port, calls a power API, connects to SSH or writes a real disk.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import fcntl

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fnos" / "tests" / "fixtures" / "lab-host-valid.json"
sys.path.insert(0, str(ROOT))

from fnos.lab.errors import Refused, SimulationFailure  # noqa: E402
from fnos.lab.inspect import (  # noqa: E402
    analyze_disk,
    load_inventory,
    _collect_mount_points,
)
from fnos.lab.locks import board_lock, board_lock_path  # noqa: E402
from fnos.lab.model import STATE_SCHEMA, validate_transition  # noqa: E402
from fnos.lab.plan import load_plan, sign_plan  # noqa: E402
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


class LabTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._temp.name)
        self.image = self._make_image()
        self.plan_path = self.tmp / "plan.json"
        self.state = self.tmp / "state"

    def tearDown(self) -> None:
        self._temp.cleanup()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _make_image(self, name: str = "candidate.img", size: int = 1024 * 1024) -> Path:
        path = self.tmp / name
        path.write_bytes(b"\x5a" * size)
        return path

    def _fixture(self, mutate=None) -> Path:
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        if mutate:
            mutate(data)
        path = self.tmp / "fixture.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _plan(
        self,
        *,
        fixture: Path | None = None,
        image: Path | None = None,
        board: str = "x5-lab-01",
        target: str = "/dev/disk4",
        serial: str = "X5TEST0001",
        out: Path | None = None,
    ):
        out = out or self.plan_path
        fixture = fixture or FIXTURE
        args = [
            "plan",
            "--board",
            board,
            "--image",
            str(image or self.image),
            "--target",
            target,
            "--expect-serial",
            serial,
            "--out",
            str(out),
            "--fixture",
            str(fixture),
        ]
        return run_cli(*args)

    def _simulate(
        self,
        *extra: str,
        run_id: str,
        plan: Path | None = None,
        env: dict[str, str] | None = None,
    ):
        return run_cli(
            "simulate",
            "--plan",
            str(plan or self.plan_path),
            "--state-dir",
            str(self.state),
            "--run-id",
            run_id,
            *extra,
            env=env,
        )

    def _status(self, run_id: str, *extra: str):
        return run_cli(
            "status",
            "--state-dir",
            str(self.state),
            "--run",
            run_id,
            *extra,
        )

    def _events(self, run_id: str) -> list[dict]:
        path = self.state / "runs" / run_id / "events.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def _state(self, run_id: str) -> dict:
        path = self.state / "runs" / run_id / "state.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _assert_refused(self, proc, needle: str = "") -> None:
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("REFUSED:", proc.stderr)
        if needle:
            self.assertIn(needle, proc.stderr)

    # ------------------------------------------------------------------
    # inspect
    # ------------------------------------------------------------------
    def test_inspect_fixture_is_read_only_and_never_writable(self) -> None:
        proc = run_cli("inspect", "--fixture", str(FIXTURE))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["mode"], "fixture")
        self.assertTrue(data["simulated"])
        self.assertFalse(data["write_eligible"])
        self.assertEqual(data["verified_writable"], [])
        self.assertIn("/dev/disk4", data["verified_identity"])
        self.assertIn("/dev/disk5", data["verified_identity"])
        self.assertNotIn("/dev/disk0", data["verified_identity"])
        self.assertTrue(all(d["write_eligible"] is False for d in data["disks"]))
        self.assertTrue(all(not p["opened"] for p in data["serial_ports"]))
        self.assertEqual(
            data["hardware_actions"],
            {
                "serial_opened": False,
                "power_api_called": False,
                "ssh_connected": False,
                "disk_written": False,
            },
        )
        internal = next(d for d in data["disks"] if d["device"] == "/dev/disk0")
        self.assertEqual(internal["identity_status"], "rejected_internal")

    def test_inspect_text_mode_says_no_write_eligibility(self) -> None:
        proc = run_cli("inspect", "--fixture", str(FIXTURE), "--format", "text")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("verified_writable: none", proc.stdout)
        self.assertIn("write_eligible: false", proc.stdout)

    def test_fixture_claiming_real_hardware_is_refused(self) -> None:
        fixture = self._fixture(lambda d: d.update(simulated=False))
        proc = run_cli("inspect", "--fixture", str(fixture))
        self._assert_refused(proc, "simulated=true")

    def test_fixture_loading_cannot_run_a_host_command(self) -> None:
        from unittest import mock

        with mock.patch("subprocess.run", side_effect=AssertionError("host command")):
            inventory = load_inventory(FIXTURE)
        self.assertTrue(inventory.simulated)
        self.assertEqual(inventory.mode, "fixture")

    def test_simulator_modules_have_no_hardware_backend_imports(self) -> None:
        for name in ("simulate.py", "status.py", "locks.py", "state.py", "plan.py"):
            source = (ROOT / "fnos" / "lab" / name).read_text(encoding="utf-8")
            for forbidden in (
                "import serial",
                "import socket",
                "import paramiko",
                "subprocess.run",
                "subprocess.Popen",
                "sudo ",
            ):
                self.assertNotIn(forbidden, source, f"{forbidden!r} in {name}")

    def test_unknown_whole_or_internal_is_not_verified(self) -> None:
        base = {
            "device": "/dev/disk9",
            "size_bytes": 32 * 1024**3,
            "model": "USB SSD",
            "serial": "SERIAL9",
            "system_disk": False,
            "mount_points": [],
            "read_only": False,
            "virtual": False,
        }
        unknown_whole = analyze_disk({**base, "internal": False, "whole_disk": None})
        self.assertNotEqual(unknown_whole["identity_status"], "verified")
        self.assertEqual(unknown_whole["identity_status"], "unknown_layout")
        unknown_internal = analyze_disk({**base, "internal": None, "whole_disk": True})
        self.assertNotEqual(unknown_internal["identity_status"], "verified")
        self.assertEqual(unknown_internal["identity_status"], "unknown_layout")

    def test_mounted_child_partition_rejects_whole_disk(self) -> None:
        disk = analyze_disk(
            {
                "device": "/dev/disk4",
                "size_bytes": 32 * 1024**3,
                "model": "USB SSD",
                "serial": "SERIAL4",
                "internal": False,
                "whole_disk": True,
                "system_disk": False,
                "mount_points": ["/Volumes/DATA"],
                "read_only": False,
                "virtual": False,
            }
        )
        self.assertEqual(disk["identity_status"], "rejected_mounted")

    def test_mount_point_collection_walks_partitions_and_apfs(self) -> None:
        entry = {
            "DeviceIdentifier": "disk4",
            "Partitions": [{"MountPoint": "/Volumes/BOOT"}],
            "APFSVolumes": [{"MountPoint": "/System/Volumes/Data"}],
        }
        self.assertEqual(
            _collect_mount_points(entry),
            ["/System/Volumes/Data", "/Volumes/BOOT"],
        )

    def test_invalid_phase_transition_is_rejected(self) -> None:
        self.assertEqual(
            validate_transition("preflight", "serial_capture"), "serial_capture"
        )
        with self.assertRaises(SimulationFailure):
            validate_transition("preflight", "booting")
        with self.assertRaises(SimulationFailure):
            validate_transition("passed", "writing")

    # ------------------------------------------------------------------
    # plan
    # ------------------------------------------------------------------
    def test_plan_is_versioned_atomic_and_never_write_eligible(self) -> None:
        proc = self._plan()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["schema"], "fnos-lab-plan/v1")
        self.assertTrue(summary["simulated"])
        self.assertFalse(summary["write_eligible"])
        self.assertEqual(summary["power_channel"], "s1")
        self.assertEqual(summary["target"]["serial"], "X5TEST0001")

        plan = load_plan(self.plan_path)
        self.assertEqual(plan["schema"], "fnos-lab-plan/v1")
        self.assertTrue(plan["simulated"])
        self.assertFalse(plan["execution"]["write_eligible"])
        self.assertFalse(plan["execution"]["real_backend_wired"])
        self.assertEqual(plan["image"]["path"], str(self.image))
        self.assertEqual(
            plan["image"]["sha256"], hashlib.sha256(self.image.read_bytes()).hexdigest()
        )
        self.assertEqual(plan["image"]["size_bytes"], self.image.stat().st_size)
        self.assertEqual(
            plan["fixture"]["sha256"],
            hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        )
        self.assertEqual(plan["fixture"]["path"], str(FIXTURE))
        self.assertEqual(self.plan_path.stat().st_mode & 0o777, 0o600)
        leftovers = [p for p in self.tmp.iterdir() if p.name.startswith(".fnos-lab-")]
        self.assertEqual(leftovers, [])

    def test_plan_rejects_internal_disk(self) -> None:
        fixture = self._fixture(
            lambda d: d["disks"][1].update({"internal": True, "system_disk": True})
        )
        proc = self._plan(fixture=fixture)
        self._assert_refused(proc, "internal/system disk")

    def test_plan_rejects_ambiguous_identity(self) -> None:
        def duplicate(data):
            clone = dict(data["disks"][1])
            clone["device"] = "/dev/disk6"
            data["disks"].append(clone)

        proc = self._plan(fixture=self._fixture(duplicate))
        self._assert_refused(proc, "ambiguous")

    def test_plan_rejects_insufficient_capacity(self) -> None:
        fixture = self._fixture(
            lambda d: d["disks"][1].update({"size_bytes": 1024})
        )
        proc = self._plan(fixture=fixture)
        self._assert_refused(proc, "capacity is insufficient")

    def test_plan_rejects_unbound_power_channel(self) -> None:
        fixture = self._fixture(
            lambda d: d["boards"]["x5-lab-01"]["power"].update(
                {"mapping_verified": False}
            )
        )
        proc = self._plan(fixture=fixture)
        self._assert_refused(proc, "mapping is not verified")

    def test_plan_rejects_missing_serial_identity(self) -> None:
        fixture = self._fixture(lambda d: d["disks"][1].update({"serial": None}))
        proc = self._plan(fixture=fixture)
        self._assert_refused(proc, "serial number is missing")

    def test_plan_rejects_serial_mismatch(self) -> None:
        proc = self._plan(serial="WRONG-SERIAL")
        self._assert_refused(proc, "does not match expected serial")

    def test_plan_rejects_symlink_output_and_keeps_target(self) -> None:
        victim = self.tmp / "victim"
        victim.write_text("do-not-touch", encoding="utf-8")
        out = self.tmp / "plan-link.json"
        out.symlink_to(victim)
        proc = self._plan(out=out)
        self._assert_refused(proc, "symlink")
        self.assertEqual(victim.read_text(encoding="utf-8"), "do-not-touch")

    def test_plan_refuses_output_same_as_image(self) -> None:
        before = self.image.read_bytes()
        proc = self._plan(out=self.image)
        self._assert_refused(proc, "input image")
        self.assertEqual(self.image.read_bytes(), before)

    def test_plan_refuses_output_alias_of_image(self) -> None:
        alias = self.tmp / "alias"
        alias.symlink_to(self.tmp, target_is_directory=True)
        out = alias / self.image.name
        before = self.image.read_bytes()
        proc = self._plan(out=out)
        self._assert_refused(proc, "input image")
        self.assertEqual(self.image.read_bytes(), before)

    def test_plan_refuses_hardlink_alias_of_image(self) -> None:
        out = self.tmp / "image-hardlink.img"
        os.link(self.image, out)
        before = self.image.read_bytes()
        proc = self._plan(out=out)
        self._assert_refused(proc, "input image")
        self.assertEqual(self.image.read_bytes(), before)

    def test_plan_refuses_output_same_as_fixture(self) -> None:
        before = FIXTURE.read_bytes()
        proc = self._plan(out=FIXTURE)
        self._assert_refused(proc, "input fixture")
        self.assertEqual(FIXTURE.read_bytes(), before)

    def test_plan_refuses_existing_unrelated_output(self) -> None:
        out = self.tmp / "existing-plan.json"
        out.write_text("keep-me", encoding="utf-8")
        proc = self._plan(out=out)
        self._assert_refused(proc, "overwrite")
        self.assertEqual(out.read_text(encoding="utf-8"), "keep-me")

    # ------------------------------------------------------------------
    # plan/image integrity before simulation
    # ------------------------------------------------------------------
    def test_image_change_after_plan_refuses_simulation(self) -> None:
        self.assertEqual(self._plan().returncode, 0)
        with self.image.open("ab") as stream:
            stream.write(b"changed")
        proc = self._simulate(run_id="run-image-change")
        self._assert_refused(proc, "image SHA-256 changed")
        self.assertFalse((self.state / "runs" / "run-image-change").exists())

    def test_plan_tamper_refuses_simulation(self) -> None:
        self.assertEqual(self._plan().returncode, 0)
        data = json.loads(self.plan_path.read_text(encoding="utf-8"))
        data["board"]["board_id"] = "tampered"
        self.plan_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        proc = self._simulate(run_id="run-plan-tamper")
        self._assert_refused(proc, "integrity mismatch")

    def test_fixture_plan_cannot_be_flipped_to_real(self) -> None:
        self.assertEqual(self._plan().returncode, 0)
        data = json.loads(self.plan_path.read_text(encoding="utf-8"))
        data["simulated"] = False
        data["execution"]["write_eligible"] = True
        sign_plan(data)
        self.plan_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        proc = self._simulate(run_id="run-fake-real")
        self._assert_refused(proc, "only simulated plans")

    # ------------------------------------------------------------------
    # locks
    # ------------------------------------------------------------------
    def test_cross_process_board_lock_is_mutually_exclusive(self) -> None:
        ready = self.tmp / "ready"
        holder_code = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "from fnos.lab.locks import board_lock\n"
            "with board_lock(Path(sys.argv[1]), sys.argv[2]):\n"
            "    Path(sys.argv[3]).write_text('ready')\n"
            "    time.sleep(30)\n"
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code, str(self.state), "x5-lab-01", str(ready)],
            cwd=ROOT,
            env=BASE_ENV,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not ready.exists():
                if holder.poll() is not None:
                    self.fail("lock holder exited early: " + (holder.stderr.read() or ""))
                time.sleep(0.05)
            self.assertTrue(ready.exists(), "lock holder did not become ready")

            with self.assertRaises(Refused):
                with board_lock(self.state, "x5-lab-01"):
                    pass
            with board_lock(self.state, "x5-lab-02"):
                pass

            # The real simulator must also fail closed behind the same lock.
            self.assertEqual(self._plan().returncode, 0)
            proc = self._simulate(run_id="run-locked")
            self._assert_refused(proc, "busy")
            self.assertFalse((self.state / "runs" / "run-locked").exists())
        finally:
            holder.terminate()
            try:
                holder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait(timeout=5)
            if holder.stdout:
                holder.stdout.close()
            if holder.stderr:
                holder.stderr.close()

    def test_lock_file_symlink_is_refused_and_victim_untouched(self) -> None:
        root = self.tmp / "lockstate"
        secure_dir(root)
        path = board_lock_path(root, "x5-lab-03")
        path.parent.mkdir(parents=True, exist_ok=True)
        victim = root / "victim"
        victim.write_text("do-not-touch", encoding="utf-8")
        path.symlink_to(victim)
        with self.assertRaises(Refused):
            with board_lock(root, "x5-lab-03"):
                pass
        self.assertEqual(victim.read_text(encoding="utf-8"), "do-not-touch")

    def test_lock_directory_is_refused(self) -> None:
        root = self.tmp / "lockstate-dir"
        secure_dir(root)
        path = board_lock_path(root, "x5-lab-dir")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir()
        with self.assertRaises(Refused):
            with board_lock(root, "x5-lab-dir"):
                pass

    def test_lock_fifo_is_refused_without_hanging(self) -> None:
        root = self.tmp / "lockstate-fifo"
        secure_dir(root)
        path = board_lock_path(root, "x5-lab-fifo")
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(path)
        code = (
            "import sys\n"
            "from pathlib import Path\n"
            "from fnos.lab.locks import board_lock\n"
            "from fnos.lab.errors import Refused\n"
            "try:\n"
            "    with board_lock(Path(sys.argv[1]), sys.argv[2]):\n"
            "        print('ACQUIRED')\n"
            "except Refused as exc:\n"
            "    print('refused:', exc)\n"
            "    sys.exit(0)\n"
            "except Exception as exc:\n"
            "    print(type(exc).__name__, exc)\n"
            "    sys.exit(1)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code, str(root), "x5-lab-fifo"],
            cwd=ROOT,
            env=BASE_ENV,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("refused:", proc.stdout)
        self.assertNotIn("ACQUIRED", proc.stdout)

    def test_lock_cleanup_does_not_mask_body_exception(self) -> None:
        root = self.tmp / "lockstate-cleanup"
        secure_dir(root)
        real_flock = fcntl.flock
        calls = {"count": 0}

        def flaky_flock(fd, flags):
            calls["count"] += 1
            if calls["count"] >= 2:
                raise OSError(45, "Operation not supported")
            return real_flock(fd, flags)

        with mock.patch("fnos.lab.locks.fcntl.flock", side_effect=flaky_flock):
            with self.assertRaisesRegex(RuntimeError, "body boom"):
                with board_lock(root, "x5-lab-cleanup"):
                    raise RuntimeError("body boom")
        self.assertGreaterEqual(calls["count"], 2)

    def test_concurrent_processes_same_run_id_have_one_winner(self) -> None:
        root = self.tmp / "race-state"
        barrier = self.tmp / "race-barrier"
        code = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "from fnos.lab.model import STATE_SCHEMA\n"
            "from fnos.lab.state import RunStore\n"
            "root, run_id, barrier, board = sys.argv[1:5]\n"
            "while not Path(barrier).exists():\n"
            "    time.sleep(0.005)\n"
            "state = {'schema': STATE_SCHEMA, 'run_id': run_id, 'board_id': board,\n"
            "         'simulated': True, 'phase': 'created', 'status': 'running'}\n"
            "try:\n"
            "    RunStore(Path(root), run_id).create(state)\n"
            "except Exception as exc:\n"
            "    print(type(exc).__name__, str(exc))\n"
            "    sys.exit(2)\n"
            "print('created')\n"
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(root), "race-run", str(barrier), board],
                cwd=ROOT,
                env=BASE_ENV,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for board in ("x5-lab-01", "x5-lab-02")
        ]
        try:
            time.sleep(0.2)
            barrier.write_text("go", encoding="utf-8")
            results = []
            for process in processes:
                out, err = process.communicate(timeout=15)
                results.append((process.returncode, out, err))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()

        self.assertEqual(sorted(result[0] for result in results), [0, 2], results)
        state = json.loads(
            (root / "runs" / "race-run" / "state.json").read_text(encoding="utf-8")
        )
        self.assertIn(state["board_id"], {"x5-lab-01", "x5-lab-02"})

    # ------------------------------------------------------------------
    # simulation policy
    # ------------------------------------------------------------------
    def test_normal_simulation_passes_without_forbidden_power_off(self) -> None:
        self.assertEqual(self._plan().returncode, 0)
        proc = self._simulate(run_id="run-normal")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["status"], "passed")
        self.assertTrue(summary["media"]["verified"])
        self.assertFalse(summary["write_eligible"])
        self.assertFalse(summary["resume_allowed"])

        events = self._events("run-normal")
        kinds = [event["kind"] for event in events]
        self.assertIn("write_started_simulated", kinds)
        self.assertIn("readback_completed_simulated", kinds)
        self.assertIn("run_passed", kinds)
        for event in events:
            if event["kind"] == "power_action" and event["action"] == "off":
                self.assertNotIn(
                    event["phase"],
                    {"writing", "written", "reading", "booting", "validating"},
                )

        status = self._status("run-normal")
        self.assertEqual(status.returncode, 0, status.stderr)
        report = json.loads(status.stdout)
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["plan_valid"])
        self.assertEqual(report["image_status"], "unchanged")
        self.assertFalse(report["resume_allowed"])
        self.assertEqual(
            report["hardware_actions"],
            {
                "serial_opened": False,
                "power_api_called": False,
                "disk_written": False,
                "ssh_connected": False,
            },
        )

    def test_serial_capture_precedes_any_power_action(self) -> None:
        self.assertEqual(self._plan().returncode, 0)
        proc = self._simulate(run_id="run-serial-order")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        events = self._events("run-serial-order")
        serial_indices = [
            index
            for index, event in enumerate(events)
            if event["kind"]
            in {"serial_listen_started_simulated", "serial_capture_simulated"}
        ]
        power_indices = [
            index
            for index, event in enumerate(events)
            if event["kind"] == "power_action"
        ]
        self.assertEqual(len(serial_indices), 2)
        self.assertTrue(power_indices)
        self.assertLess(max(serial_indices), min(power_indices))

    def test_power_off_is_refused_during_write_and_readback(self) -> None:
        self.assertEqual(self._plan().returncode, 0)
        for fault, phase in (
            ("power_off_at_writing", "writing"),
            ("power_off_at_reading", "reading"),
        ):
            with self.subTest(fault=fault):
                run_id = "run-" + fault
                proc = self._simulate("--fault", fault, run_id=run_id)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                refused = [
                    event
                    for event in self._events(run_id)
                    if event["kind"] == "power_off_refused"
                ]
                self.assertEqual(len(refused), 1)
                self.assertEqual(refused[0]["phase"], phase)
                for event in self._events(run_id):
                    if event["kind"] == "power_action" and event["action"] == "off":
                        self.assertNotEqual(event["phase"], phase)
                self.assertTrue(self._state(run_id)["media"]["verified"])

    def test_hash_mismatch_blocks_boot_and_keeps_power(self) -> None:
        self.assertEqual(self._plan().returncode, 0)
        proc = self._simulate("--fault", "hash_mismatch", run_id="run-hash")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        state = self._state("run-hash")
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["failure"]["code"], "hash_mismatch")
        self.assertFalse(state["media"]["verified"])
        self.assertTrue(state["media"]["incomplete"])
        self.assertEqual(state["power"]["state"], "on")
        kinds = [event["kind"] for event in self._events("run-hash")]
        self.assertNotIn("boot_simulated", kinds)
        self.assertEqual(kinds.count("readback_completed_simulated"), 1)

    def test_cancel_while_writing_waits_boundary_and_no_resume(self) -> None:
        self.assertEqual(self._plan().returncode, 0)
        proc = self._simulate("--cancel-at", "writing", run_id="run-cancel-write")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        state = self._state("run-cancel-write")
        self.assertEqual(state["status"], "cancelled")
        self.assertTrue(state["media"]["write_complete"])
        self.assertFalse(state["media"]["readback_started"])
        self.assertTrue(state["media"]["incomplete"])
        self.assertEqual(state["power"]["state"], "on")
        kinds = [event["kind"] for event in self._events("run-cancel-write")]
        self.assertNotIn("boot_simulated", kinds)
        self.assertNotIn("readback_started_simulated", kinds)

        again = self._simulate(run_id="run-cancel-write")
        self._assert_refused(again, "implicit resume")

    def test_cancel_during_readback_never_claims_verified(self) -> None:
        self.assertEqual(self._plan().returncode, 0)
        proc = self._simulate("--cancel-at", "reading", run_id="run-cancel-read")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        state = self._state("run-cancel-read")
        self.assertEqual(state["status"], "cancelled")
        self.assertFalse(state["media"]["verified"])
        self.assertFalse(state["media"]["readback_complete"])
        self.assertTrue(state["media"]["incomplete"])
        self.assertNotIn(
            "boot_simulated",
            [event["kind"] for event in self._events("run-cancel-read")],
        )

    def test_cancel_before_write_keeps_power_untouched(self) -> None:
        self.assertEqual(self._plan().returncode, 0)
        proc = self._simulate("--cancel-at", "preflight", run_id="run-cancel-pre")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        state = self._state("run-cancel-pre")
        self.assertEqual(state["power"]["state"], "unknown")
        power_events = [
            e
            for e in self._events("run-cancel-pre")
            if e["kind"] == "power_action"
        ]
        self.assertEqual(power_events, [])

    def test_crash_at_writing_is_detected_and_never_resumed(self) -> None:
        self.assertEqual(self._plan().returncode, 0)
        proc = self._simulate("--crash-at", "writing", run_id="run-crash")
        self.assertEqual(proc.returncode, 70, proc.stdout + proc.stderr)
        time.sleep(0.2)

        status = self._status("run-crash")
        self.assertEqual(status.returncode, 0, status.stderr)
        report = json.loads(status.stdout)
        self.assertEqual(report["status"], "interrupted")
        self.assertTrue(report["interrupted"])
        self.assertFalse(report["recorded_process_alive"])
        self.assertFalse(report["resume_allowed"])
        self.assertTrue(report["media"]["incomplete"])
        self.assertNotIn(
            "readback_started_simulated",
            [event["kind"] for event in self._events("run-crash")],
        )

        again = self._simulate(run_id="run-crash")
        self._assert_refused(again, "implicit resume")

    def test_cloud_unknown_attempts_one_recovery_and_no_blind_retry(self) -> None:
        self.assertEqual(self._plan().returncode, 0)
        proc = self._simulate("--fault", "cloud_unknown", run_id="run-cloud")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        state = self._state("run-cloud")
        self.assertEqual(state["failure"]["code"], "power_state_unknown")
        self.assertEqual(state["power"]["state"], "on")
        events = self._events("run-cloud")
        offs = [
            e
            for e in events
            if e["kind"] == "power_action" and e["action"] == "off"
        ]
        ons = [
            e
            for e in events
            if e["kind"] == "power_action" and e["action"] == "on"
        ]
        self.assertEqual(len(offs), 1)
        self.assertEqual(offs[0]["result"], "unknown")
        self.assertEqual(len(ons), 1)
        self.assertEqual(ons[0]["result"], "confirmed")
        self.assertEqual(
            [e for e in events if e["kind"] == "power_recovery_started"][0]["attempts"],
            1,
        )

    def test_recovery_failure_requires_manual_power_and_stays_unknown(self) -> None:
        self.assertEqual(self._plan().returncode, 0)
        proc = self._simulate("--fault", "recovery_failed", run_id="run-recovery")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        state = self._state("run-recovery")
        self.assertEqual(state["failure"]["code"], "power_recovery_failed")
        self.assertTrue(state["failure"]["manual_recovery"])
        self.assertEqual(state["power"]["state"], "unknown")
        status = json.loads(self._status("run-recovery").stdout)
        self.assertEqual(status["power"]["state"], "unknown")
        self.assertIn("manual", status["next_action"])

    def test_status_detects_plan_and_image_changes(self) -> None:
        self.assertEqual(self._plan().returncode, 0)
        self.assertEqual(self._simulate(run_id="run-status").returncode, 0)

        data = json.loads(self.plan_path.read_text(encoding="utf-8"))
        data["created_at"] = "tampered"
        self.plan_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        report = json.loads(self._status("run-status").stdout)
        self.assertFalse(report["plan_valid"])
        self.assertEqual(report["image_status"], "unknown")

        # A fresh valid plan + run, then a content-preserving size change.
        self.plan_path.unlink()
        image2 = self._make_image("status-image.img")
        self.assertEqual(self._plan(image=image2).returncode, 0)
        self.assertEqual(
            self._simulate(run_id="run-status-image", plan=self.plan_path).returncode, 0
        )
        with image2.open("ab") as stream:
            stream.write(b"x")
        report = json.loads(self._status("run-status-image", "--verify-image").stdout)
        self.assertEqual(report["image_status"], "changed")

    def test_status_missing_run_is_refused(self) -> None:
        proc = self._status("run-does-not-exist")
        self._assert_refused(proc, "does not exist")

    # ------------------------------------------------------------------
    # data hygiene
    # ------------------------------------------------------------------
    def test_logs_and_outputs_do_not_contain_secret_markers(self) -> None:
        marker = "SECRET-MARKER-9f1c2a"
        fixture = self._fixture(
            lambda d: (
                d["host"].update({"credential": marker, "token": marker}),
                d["disks"][1].update({"auth_token": marker, "password": marker}),
                d["boards"]["x5-lab-01"].update(
                    {"api_key": marker, "credential": marker}
                ),
            )
        )
        self.assertEqual(self._plan(fixture=fixture).returncode, 0)
        proc = self._simulate(run_id="run-secret", env={"FNOS_LAB_SECRET": marker})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn(marker, proc.stdout)
        self.assertNotIn(marker, proc.stderr)
        self.assertNotIn(marker, self.plan_path.read_text(encoding="utf-8"))
        for path in self.state.rglob("*"):
            if path.is_file():
                self.assertNotIn(
                    marker,
                    path.read_text(encoding="utf-8", errors="replace"),
                    f"secret marker leaked into {path.name}",
                )

    def test_refusal_does_not_print_environment_or_credentials(self) -> None:
        marker = "SECRET-MARKER-bad-plan"
        proc = run_cli(
            "plan",
            "--board",
            "x5-lab-01",
            "--image",
            str(self.image),
            "--target",
            "/dev/disk4",
            "--expect-serial",
            "WRONG",
            "--out",
            str(self.plan_path),
            "--fixture",
            str(FIXTURE),
            env={"FNOS_LAB_SECRET": marker},
        )
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn(marker, proc.stdout + proc.stderr)
        self.assertIn("REFUSED:", proc.stderr)

    # ------------------------------------------------------------------
    # CLI contract
    # ------------------------------------------------------------------
    def test_help_lists_all_entry_points(self) -> None:
        proc = run_cli("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for command in ("inspect", "plan", "simulate", "status"):
            self.assertIn(command, proc.stdout)
        self.assertIn("no real flashing", proc.stdout)


if __name__ == "__main__":
    unittest.main()
