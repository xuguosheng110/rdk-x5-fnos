"""Offline tests for the one-shot post-flash r3 boot entry.

No test opens /dev, calls CP5, reads a disk, uses sudo, touches the network or
creates a real acceptance.  Serial, power and USB identity are in-memory fakes;
evidence files are ordinary temporary JSON files.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fnos.integration.hardware.boot_r3 import (  # noqa: E402
    ACCEPTANCE_SCHEMA,
    BOARD_ID,
    CANDIDATE_SHA256,
    CANDIDATE_SIZE,
    DEFAULT_REVIEWED_TARGET,
    TARGET_BYTES,
    VerifiedUmsSession,
    parse_pipeline_processes,
    run,
    sha256_file,
    verify_acceptance,
)
from fnos.lab.errors import Refused  # noqa: E402
from fnos.lab.locks import board_lock  # noqa: E402
from fnos.lab.power import Cp5PowerBackend, FakeCp5Client, PowerManager  # noqa: E402
from fnos.lab.uart_session import FakeSerialTransport  # noqa: E402

DEVICE = "/dev/fake-ums"
BEGIN_RE = re.compile(rb"__FNOS_BEGIN_([0-9a-f]{32})__")
CTRL_C_REPLY = b"\r\nHobot> \r\n"
BOOT_BYTES = (
    b"NOTICE:  BL31: v2.6\r\n"
    b"U-Boot SPL 2024.01\r\n"
    b"Hit <SPACE> key to stop autoboot:  0 \r\n"
    b"Linux version 6.18.0 (build@x5) (aarch64-linux-gnu-gcc) #1 SMP\r\n"
    b"[    0.000000] Booting Linux on physical CPU 0x0\r\n"
    b"Debian GNU/Linux 12 x5-lab-01 ttyS0\r\n"
    b"x5-lab-01 login: "
)
STALE_BYTES = b"STALE-UMS-OLD-BANNER\r\n"


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += max(0.0, seconds)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


class ClockedFakeSerial(FakeSerialTransport):
    """Fake transport that advances the injected monotonic clock per read."""

    def __init__(self, clock: FakeClock, *, step: float = 0.2, **kwargs):
        super().__init__(**kwargs)
        self.clock = clock
        self.step = step

    def read(self, timeout: float, max_bytes: int = 65536) -> bytes:
        self.clock.advance(min(self.step, max(0.0, timeout)))
        return super().read(timeout, max_bytes)


def ums_responder(ctrl_c_reply: bytes = CTRL_C_REPLY):
    def responder(written: bytes) -> bytes:
        if written == b"\x03":
            return ctrl_c_reply
        if b"mmc list" in written:
            match = BEGIN_RE.search(written)
            if match:
                token = match.group(1)
                return (
                    b"\r\n__FNOS_BEGIN_"
                    + token
                    + b"__\r\nmmc@fe2c0000: 1 (SD), 0 (eMMC)\r\n__FNOS_END_"
                    + token
                    + b"__\r\nHobot> \r\n"
                )
        return b""

    return responder


class RecordingClient(FakeCp5Client):
    def __init__(self, clock: FakeClock, transport, *, on_bytes: bytes = b"", off_bytes: bytes = b""):
        super().__init__(value=True)
        self.clock = clock
        self.transport = transport
        self.on_bytes = on_bytes
        self.off_bytes = off_bytes
        self.on_times: list[float] = []
        self.off_times: list[float] = []

    def set_usb(self, value: bool) -> None:
        super().set_usb(value)
        if value:
            self.on_times.append(self.clock())
            if self.on_bytes:
                self.transport.feed(self.on_bytes)
        else:
            self.off_times.append(self.clock())
            if self.off_bytes:
                self.transport.feed(self.off_bytes)


class OffFailsOnceClient:
    def __init__(self):
        self.value = True
        self.calls: list[bool] = []
        self.failed = False

    def read_usb(self) -> bool:
        return self.value

    def set_usb(self, value: bool) -> None:
        self.calls.append(value)
        if value is False and not self.failed:
            self.failed = True
            raise RuntimeError("simulated lost CP5 response")
        self.value = value


def _reviewed() -> dict:
    return json.loads(DEFAULT_REVIEWED_TARGET.read_text())


def _fake_live() -> dict:
    live = _reviewed()
    live.update(
        {
            "device": DEVICE,
            "mounts": [],
            "whole": True,
            "internal": False,
            "bus": "USB",
            "virtual": "Physical",
            "read_only": False,
        }
    )
    return live


def _write_json(path: Path, value: dict) -> str:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return sha256_file(path)


def make_acceptance(tmp: Path) -> Path:
    reviewed = _reviewed()
    backup = tmp / "backup-evidence.json"
    readback = tmp / "readback-evidence.json"
    _write_json(
        backup,
        {
            "ok": True,
            "bytes": TARGET_BYTES,
            "sha256": "a" * 64,
            "remote_path": "/remote/backups/sd-current.img",
            "remote_sha256": "a" * 64,
        },
    )
    _write_json(
        readback,
        {
            "ok": True,
            "image_sha256": CANDIDATE_SHA256,
            "readback_sha256": CANDIDATE_SHA256,
            "bytes": CANDIDATE_SIZE,
            "target": DEVICE,
        },
    )
    target = {
        key: reviewed[key]
        for key in (
            "model",
            "size_bytes",
            "usb_gadget_serial",
            "usb_vid",
            "usb_pid",
            "usb_location_id",
            "usb_path",
            "whole",
            "internal",
            "bus",
        )
    }
    target["device"] = DEVICE
    acceptance = tmp / "acceptance.json"
    _write_json(
        acceptance,
        {
            "schema": ACCEPTANCE_SCHEMA,
            "accepted": True,
            "reviewer": "Codex",
            "candidate": {
                "sha256": CANDIDATE_SHA256,
                "size": CANDIDATE_SIZE,
                "kernel": "6.18.0",
                "root_uuid": "c2d3e4f5-a6b7-4c8d-9e0f-1a2b3c4d5e6f",
                "fnos_version": "1.2.0701",
            },
            "target": target,
            "evidence": {
                "backup": {
                    "path": str(backup),
                    "sha256": sha256_file(backup),
                },
                "readback": {
                    "path": str(readback),
                    "sha256": sha256_file(readback),
                },
            },
        },
    )
    return acceptance


def harness(
    tmp: Path,
    *,
    transport,
    client,
    clock: FakeClock,
    acceptance: Path | None = None,
    max_capture_seconds: int = 15,
    process_lister=None,
):
    acceptance = acceptance or make_acceptance(tmp)
    state_root = tmp / "state"

    def session_factory(held):
        return VerifiedUmsSession(
            board_id=BOARD_ID,
            held=held,
            port="/dev/fake-boot-serial",
            state_root=state_root,
            transport=transport,
            clock=clock,
            sleep=clock.sleep,
        )

    def power_factory(held, emit):
        backend = Cp5PowerBackend(client, board_id=BOARD_ID, sleep=clock.sleep)
        return PowerManager(
            backend,
            phase=lambda: "postflash-boot",
            emit=lambda *args, **kwargs: None,
            save=lambda: None,
            sleep=clock.sleep,
        )

    return run(
        acceptance_path=acceptance,
        reviewed_target=DEFAULT_REVIEWED_TARGET,
        device=DEVICE,
        out_dir=tmp / "out",
        lock_root=state_root,
        execute=True,
        max_capture_seconds=max_capture_seconds,
        process_lister=process_lister or (lambda: []),
        usb_live=lambda device: _fake_live(),
        session_factory=session_factory,
        power_factory=power_factory,
        clock=clock,
        sleep=clock.sleep,
        emit=lambda value: None,
    )


class AcceptanceTests(unittest.TestCase):
    def test_missing_acceptance_refused_before_any_hardware(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            called = []
            with self.assertRaises(Refused):
                run(
                    acceptance_path=tmp / "missing.json",
                    reviewed_target=DEFAULT_REVIEWED_TARGET,
                    device=DEVICE,
                    out_dir=tmp / "out",
                    lock_root=tmp / "state",
                    execute=True,
                    process_lister=lambda: [],
                    usb_live=lambda device: called.append(device) or _fake_live(),
                )
            self.assertEqual(called, [])

    def test_wrong_candidate_sha_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            acceptance = make_acceptance(tmp)
            value = json.loads(acceptance.read_text())
            value["candidate"]["sha256"] = "b" * 64
            acceptance.write_text(json.dumps(value))
            with self.assertRaises(Refused):
                verify_acceptance(
                    acceptance, reviewed_target=DEFAULT_REVIEWED_TARGET, device=DEVICE
                )

    def test_evidence_file_changed_after_acceptance_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            acceptance = make_acceptance(tmp)
            backup = tmp / "backup-evidence.json"
            backup.write_text(backup.read_text() + "\n")
            with self.assertRaises(Refused):
                verify_acceptance(
                    acceptance, reviewed_target=DEFAULT_REVIEWED_TARGET, device=DEVICE
                )

    def test_acceptance_accepts_self_verification_and_rejects_blank_reviewer(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            acceptance = make_acceptance(tmp)
            value = json.loads(acceptance.read_text())
            value["reviewer"] = "DeepSeek Harness (self-verified)"
            value["reviewer_independence"] = "self"
            acceptance.write_text(json.dumps(value))
            accepted = verify_acceptance(
                acceptance, reviewed_target=DEFAULT_REVIEWED_TARGET, device=DEVICE
            )
            self.assertEqual(accepted.reviewer_independence, "self")

            value["reviewer"] = ""
            acceptance.write_text(json.dumps(value))
            with self.assertRaises(Refused):
                verify_acceptance(
                    acceptance, reviewed_target=DEFAULT_REVIEWED_TARGET, device=DEVICE
                )


class PreflightTests(unittest.TestCase):
    def test_running_deployment_process_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            called = []
            with self.assertRaises(Refused) as caught:
                run(
                    acceptance_path=make_acceptance(tmp),
                    reviewed_target=DEFAULT_REVIEWED_TARGET,
                    device=DEVICE,
                    out_dir=tmp / "out",
                    lock_root=tmp / "state",
                    execute=True,
                    process_lister=lambda: [{"pid": 55623, "command": "deploy_r3.py --execute"}],
                    usb_live=lambda device: called.append(device) or _fake_live(),
                )
            self.assertIn("still running", str(caught.exception))
            self.assertEqual(called, [])

    def test_parse_pipeline_processes_ignores_self(self):
        text = (
            "  101 python deploy_r3.py --execute\n"
            "  102 sudo dd if=/dev/rdisk12 bs=16m\n"
            "  103 tail -f run-terminal.log\n"
            "  104 python -m unittest fnos.tests.test_boot_r3\n"
        )
        found = parse_pipeline_processes(text, own_pid=104)
        self.assertEqual([item["pid"] for item in found], [101, 102])

    def test_busy_board_lock_refused_before_serial(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            acceptance = make_acceptance(tmp)
            state = tmp / "state"
            with board_lock(state, BOARD_ID):
                with self.assertRaises(Refused):
                    run(
                        acceptance_path=acceptance,
                        reviewed_target=DEFAULT_REVIEWED_TARGET,
                        device=DEVICE,
                        out_dir=tmp / "out",
                        lock_root=state,
                        execute=True,
                        process_lister=lambda: [],
                        usb_live=lambda device: _fake_live(),
                    )


class TakeoverTests(unittest.TestCase):
    def test_old_hobot_is_not_a_fresh_ums_stop(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            clock = FakeClock()
            transport = ClockedFakeSerial(clock, initial=b"Hobot> \r\n")
            client = RecordingClient(clock, transport)
            result = harness(tmp, transport=transport, client=client, clock=clock)
            self.assertEqual(result["status"], "refused")
            self.assertFalse(result["boot_ok"])
            self.assertEqual(client.calls, [])
            self.assertNotIn("ums_takeover", result["phases"])

    def test_buffered_old_prompt_does_not_satisfy_ctrl_c(self):
        class FeedPromptAfterOpen(VerifiedUmsSession):
            def open(self, *, passive_timeout: float = 1.0) -> str:
                state = super().open(passive_timeout=passive_timeout)
                self.transport.feed(b"Hobot> \r\n")
                return state

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            clock = FakeClock()
            transport = ClockedFakeSerial(clock)
            client = RecordingClient(clock, transport)
            acceptance = make_acceptance(tmp)
            state_root = tmp / "state"

            def session_factory(held):
                return FeedPromptAfterOpen(
                    board_id=BOARD_ID,
                    held=held,
                    port="/dev/fake-old-prompt",
                    state_root=state_root,
                    transport=transport,
                    clock=clock,
                    sleep=clock.sleep,
                )

            def power_factory(held, emit):
                return PowerManager(
                    Cp5PowerBackend(client, board_id=BOARD_ID, sleep=clock.sleep),
                    phase=lambda: "postflash-boot",
                    emit=lambda *args, **kwargs: None,
                    save=lambda: None,
                    sleep=clock.sleep,
                )

            result = run(
                acceptance_path=acceptance,
                reviewed_target=DEFAULT_REVIEWED_TARGET,
                device=DEVICE,
                out_dir=tmp / "out",
                lock_root=state_root,
                execute=True,
                process_lister=lambda: [],
                usb_live=lambda device: _fake_live(),
                session_factory=session_factory,
                power_factory=power_factory,
                clock=clock,
                sleep=clock.sleep,
                emit=lambda value: None,
            )
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["boot_ok"])
            self.assertEqual(client.calls, [])
            self.assertEqual(transport.writes.count(b"\x03"), 1)

    def test_no_new_prompt_never_cuts_power(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            clock = FakeClock()
            transport = ClockedFakeSerial(clock, responder=ums_responder(ctrl_c_reply=b""))
            client = RecordingClient(clock, transport)
            result = harness(tmp, transport=transport, client=client, clock=clock)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["code"], "serial_session_timeout")
            self.assertEqual(client.calls, [])
            self.assertEqual(transport.writes.count(b"\x03"), 1)

    def test_one_ctrl_c_and_fresh_prompt_then_safe_cut(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            clock = FakeClock()
            transport = ClockedFakeSerial(clock, responder=ums_responder())
            client = RecordingClient(clock, transport, on_bytes=BOOT_BYTES)
            result = harness(tmp, transport=transport, client=client, clock=clock)
            self.assertTrue(result["boot"]["kernel_login_ok"], result.get("error"))
            self.assertFalse(result["boot_ok"])
            self.assertEqual(result["status"], "login")
            self.assertEqual(transport.writes.count(b"\x03"), 1)
            self.assertEqual(result["phases"]["ums_takeover"]["ctrl_c_writes"], 1)
            self.assertEqual(result["phases"]["safe_cut"]["state"], "ums-storage-stopped")
            self.assertEqual(result["phases"]["power_off"]["state"], "off")
            self.assertEqual(result["phases"]["power_on"]["state"], "on")


class PowerPolicyTests(unittest.TestCase):
    def test_off_unknown_recovers_on_once_and_never_retries_off(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            clock = FakeClock()
            transport = ClockedFakeSerial(clock, responder=ums_responder())
            client = OffFailsOnceClient()
            result = harness(tmp, transport=transport, client=client, clock=clock)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["code"], "power_state_unknown")
            self.assertEqual(client.calls, [False, True])
            self.assertEqual(result["phases"]["power_off"]["recovery_on_attempts"], 1)
            self.assertTrue(result["phases"]["power_off"]["recovered"])
            self.assertNotIn("power_on", result["phases"])

    def test_off_hold_is_at_least_five_seconds(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            clock = FakeClock()
            transport = ClockedFakeSerial(clock, responder=ums_responder())
            client = RecordingClient(clock, transport, on_bytes=BOOT_BYTES)
            result = harness(tmp, transport=transport, client=client, clock=clock)
            self.assertTrue(result["boot"]["kernel_login_ok"], result.get("error"))
            self.assertFalse(result["boot_ok"])
            self.assertGreaterEqual(result["phases"]["hold"]["actual_seconds"], 5.0)
            self.assertEqual(len(client.off_times), 1)
            self.assertEqual(len(client.on_times), 1)
            self.assertGreaterEqual(client.on_times[0] - client.off_times[0], 5.0)

    def test_old_bytes_are_discarded_and_not_in_boot_capture(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            clock = FakeClock()
            transport = ClockedFakeSerial(clock, responder=ums_responder())
            client = RecordingClient(clock, transport, on_bytes=BOOT_BYTES, off_bytes=STALE_BYTES)
            result = harness(tmp, transport=transport, client=client, clock=clock)
            self.assertTrue(result["boot"]["kernel_login_ok"], result.get("error"))
            self.assertFalse(result["boot_ok"])
            self.assertGreater(result["serial"]["discarded_pre_on_bytes"], 0)
            self.assertEqual(result["phases"]["power_on"]["capture_cursor"], 0)
            raw_bytes = Path(result["serial"]["capture"]["raw_path"]).read_bytes()
            self.assertNotIn(b"STALE", raw_bytes)
            self.assertIn(b"Linux version 6.18.0", raw_bytes)
            self.assertEqual(result["boot"]["kernel_release"], "6.18.0")
            self.assertTrue(result["boot"]["login_seen"])
            self.assertEqual(result["status"], "login")
            self.assertFalse(result["complete"])
            self.assertEqual(result["runtime_identity"], None)
            self.assertFalse(result["product_identity_ok"])

    def test_linux_bytes_buffered_before_on_are_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            clock = FakeClock()
            transport = ClockedFakeSerial(clock, responder=ums_responder())
            client = RecordingClient(
                clock, transport, on_bytes=BOOT_BYTES, off_bytes=STALE_BYTES
            )
            client.off_bytes = b"Linux version 6.18.0 fake\r\nlogin:\r\n"
            result = harness(tmp, transport=transport, client=client, clock=clock)
            self.assertEqual(result["status"], "refused")
            self.assertIn("Linux bytes", result["error"]["message"])
            self.assertNotIn("power_on", result["phases"])
            self.assertEqual([item["value"] for item in client.calls], [False, True])
            self.assertEqual(result["phases"]["power_restore"]["state"], "on")

    def test_raw_capture_hash_and_segments_are_consistent(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            clock = FakeClock()
            transport = ClockedFakeSerial(clock, responder=ums_responder())
            client = RecordingClient(clock, transport, on_bytes=BOOT_BYTES)
            result = harness(tmp, transport=transport, client=client, clock=clock)
            capture = result["serial"]["capture"]
            data = Path(capture["raw_path"]).read_bytes()
            self.assertEqual(capture["raw_bytes"], len(data))
            self.assertEqual(capture["raw_sha256"], sha256_file(Path(capture["raw_path"])))
            self.assertGreaterEqual(len(capture["segments"]), 4)
            labels = [segment["label"] for segment in capture["segments"]]
            self.assertIn("takeover", labels)
            self.assertIn("safe-cut", labels)
            self.assertIn("power-off-hold", labels)
            self.assertIn("new-boot", labels)
            # gate/runtime replies must be in the raw log, not only parsed text
            self.assertIn(b"mmc@fe2c0000", data)
            for segment in capture["segments"]:
                self.assertIn("started_at", segment)
                self.assertIn("ended_at", segment)
                self.assertIn("sha256", segment)
                self.assertIn("bytes", segment)


class RestoreTests(unittest.TestCase):
    def test_exception_during_hold_restores_power_once(self):
        class BoomSleep:
            def __call__(self, seconds):
                raise RuntimeError("simulated hold interruption")

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            clock = FakeClock()
            transport = ClockedFakeSerial(clock, responder=ums_responder())
            client = RecordingClient(clock, transport, on_bytes=BOOT_BYTES)
            acceptance = make_acceptance(tmp)
            state_root = tmp / "state"

            def session_factory(held):
                return VerifiedUmsSession(
                    board_id=BOARD_ID,
                    held=held,
                    port="/dev/fake-hold-boom",
                    state_root=state_root,
                    transport=transport,
                    clock=clock,
                    sleep=clock.sleep,
                )

            def power_factory(held, emit):
                return PowerManager(
                    Cp5PowerBackend(client, board_id=BOARD_ID, sleep=clock.sleep),
                    phase=lambda: "postflash-boot",
                    emit=lambda *args, **kwargs: None,
                    save=lambda: None,
                    sleep=clock.sleep,
                )

            with self.assertRaises(RuntimeError):
                run(
                    acceptance_path=acceptance,
                    reviewed_target=DEFAULT_REVIEWED_TARGET,
                    device=DEVICE,
                    out_dir=tmp / "out",
                    lock_root=state_root,
                    execute=True,
                    process_lister=lambda: [],
                    usb_live=lambda device: _fake_live(),
                    session_factory=session_factory,
                    power_factory=power_factory,
                    clock=clock,
                    sleep=BoomSleep(),
                    emit=lambda value: None,
                )
            result = json.loads((tmp / "out" / "result.json").read_text())
            self.assertEqual(result["status"], "failed")
            self.assertEqual([item["value"] for item in client.calls], [False, True])
            self.assertEqual(result["phases"]["power_restore"]["state"], "on")
            self.assertNotIn("power_on", result["phases"])


class OneShotTests(unittest.TestCase):
    def test_existing_result_refuses_second_attempt(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            out = tmp / "out"
            out.mkdir()
            (out / "result.json").write_text("{}\n")
            clock = FakeClock()
            transport = ClockedFakeSerial(clock, responder=ums_responder())
            client = RecordingClient(clock, transport)
            acceptance = make_acceptance(tmp)
            state_root = tmp / "state"

            def session_factory(held):
                return VerifiedUmsSession(
                    board_id=BOARD_ID,
                    held=held,
                    port="/dev/fake-second",
                    state_root=state_root,
                    transport=transport,
                    clock=clock,
                    sleep=clock.sleep,
                )

            def power_factory(held, emit):
                return PowerManager(
                    Cp5PowerBackend(client, board_id=BOARD_ID, sleep=clock.sleep),
                    phase=lambda: "postflash-boot",
                    emit=lambda *args, **kwargs: None,
                    save=lambda: None,
                    sleep=clock.sleep,
                )

            with self.assertRaises(Refused):
                run(
                    acceptance_path=acceptance,
                    reviewed_target=DEFAULT_REVIEWED_TARGET,
                    device=DEVICE,
                    out_dir=out,
                    lock_root=state_root,
                    execute=True,
                    process_lister=lambda: [],
                    usb_live=lambda device: _fake_live(),
                    session_factory=session_factory,
                    power_factory=power_factory,
                    clock=clock,
                    sleep=clock.sleep,
                    emit=lambda value: None,
                )
            self.assertEqual(client.calls, [])
            self.assertEqual(transport.writes, [])


if __name__ == "__main__":
    unittest.main()
