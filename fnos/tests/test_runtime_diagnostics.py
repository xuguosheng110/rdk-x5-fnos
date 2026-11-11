"""Offline tests for serial ownership/session bounds, safe-cut evidence,
runtime identity, candidate manifests, target mapping and backup planning.

No test opens /dev, sends a real serial byte, switches power or writes a disk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import time
import unittest

import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fnos.lab.backup import build_backup_plan, load_backup_plan, write_backup_plan  # noqa: E402
from fnos.lab.candidate import (  # noqa: E402
    load_candidate,
    require_candidate_identity,
    verify_candidate_image,
)
from fnos.lab.authorization import issue_authorization  # noqa: E402
from fnos.lab.fakes import build_fake_backends, fake_identity  # noqa: E402
from fnos.lab.flash import FileFlashBackend  # noqa: E402
from fnos.lab.inspect import load_fixture  # noqa: E402
from fnos.lab.plan import build_plan, load_plan  # noqa: E402
from fnos.lab.power import Cp5PowerBackend, FakeCp5Client  # noqa: E402
from fnos.lab.runner import BackendSet, execute  # noqa: E402
from fnos.lab.serial import FakeSerialCapture  # noqa: E402
from fnos.lab.safe_cut import CutEvidence  # noqa: E402
from fnos.lab.errors import HardwareFailure, Refused  # noqa: E402
from fnos.lab.flash import TargetIdentity  # noqa: E402
from fnos.lab.locks import board_lock  # noqa: E402
from fnos.lab.power import Cp5PowerBackend, FakeCp5Client, PowerManager  # noqa: E402
from fnos.lab.runtime_identity import (  # noqa: E402
    IdentityExpectation,
    parse_runtime_outputs,
    verify_identity,
)
from fnos.lab.safe_cut import (  # noqa: E402
    CUT_LINUX_POWEROFF,
    CUT_UBOOT_IDLE,
    CutEvidenceGate,
)
from fnos.lab.target_mapping import (  # noqa: E402
    BoardMedia,
    MappingRecord,
    TargetMap,
    map_unique,
    parse_mmc_info,
    parse_mmc_list,
    reidentify,
)
from fnos.lab.uart_session import (  # noqa: E402
    FakeSerialTransport,
    SerialSession,
    SessionRefused,
    SessionTimeout,
    STATE_LINUX_SHELL,
    STATE_UBOOT,
    classify_state,
)

UID = "11111111-2222-4333-8444-555555555555"


def uboot_responder(written: bytes) -> bytes:
    match = re.search(rb"__FNOS_BEGIN_([0-9a-f]+)__", written)
    if not match:
        return b""
    nonce = match.group(1)
    return (
        b"__FNOS_BEGIN_" + nonce + b"__\n"
        b"Device: sdio-host@35020000\n"
        b"Manufacturer ID: 3\nOEM: 5344\nName: SC64G\n"
        b"Capacity: 64 GiB\n"
        b"CID: 00112233445566778899aabbccddeeff\n"
        b"__FNOS_END_" + nonce + b"__\n"
        b"Hobot> "
    )



class ProductionRecoveryTests(unittest.TestCase):
    """Same LiveProviderContext/SessionSerialCapture orchestration as the CLI.

    The transport, flash and CP5 are substitutes; no real device, serial byte,
    power API or disk is touched.
    """

    def test_production_recovery_end_to_end_with_bound_receipts(self):
        import hashlib
        import json
        from unittest import mock
        from fnos.lab.live import LiveProviderContext
        from fnos.lab.serial import SessionSerialCapture
        from fnos.lab.uart_session import FakeSerialTransport, SerialSession

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "shared"
            image = root / "candidate.img"
            image.write_bytes(b"x" * 4096)
            fixture = Path(ROOT / "fnos/tests/fixtures/lab-host-valid.json")
            inventory = load_fixture(fixture)
            inventory.simulated = False
            board = inventory.boards["x5-lab-01"]
            board["reviewed"] = True
            board["allowed_target_serials"] = ["X5TEST0001"]
            board["power"] = {
                "backend": "cp5-usb", "channel": "usb",
                "mapping_verified": True, "wired": True,
            }
            plan_path = root / "plan.json"
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({
                "schema": "fnos-lab-candidate/v1",
                "candidate_id": "test-candidate",
                "board_model": "RDK X5",
                "base_os_id": "debian",
                "base_os_version": "12",
                "kernel_release": "6.18.0",
                "fnos_id": "fnos",
                "fnos_version": "1.2.0701",
                "root_uuid": UID,
                "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                "image_size_bytes": image.stat().st_size,
                "source": "offline-test",
                "created_at": "now",
                "accepted": True,
            }))
            manifest = load_candidate(manifest_path)
            with mock.patch.dict(
                os.environ,
                {
                    "FNOS_BOARD_LOCK_ROOT": str(shared),
                    "FNOS_LAB_ENABLE_REAL_BACKENDS": "1",
                },
            ), mock.patch("os.geteuid", return_value=0):
                build_plan(
                    inventory, board_id="x5-lab-01", image_path=image,
                    target_selector="/dev/disk4", expect_serial="X5TEST0001",
                    out_path=plan_path,
                )
                plan = load_plan(plan_path)
                token = issue_authorization(plan, state_root=shared)
                target_size = int(plan["target"]["size_bytes"])
                target = root / "target.img"
                target.touch()
                os.truncate(target, target_size)
                target_serial = "X5TEST0001"
                cid = "a" * 32
                boot_id = UID
                boot_lines = [
                    "Linux version 6.18.0",
                    "login:",
                    "root@x5:~# ",
                ]
                linux_values = {
                    "cat /proc/device-tree/model": "D-Robotics RDK X5 V0.1",
                    "cat /proc/device-tree/compatible": "D-Robotics, x5",
                    "cat /proc/device-tree/serial-number": target_serial,
                    "uname -r": "6.18.0",
                    "cat /proc/sys/kernel/random/boot_id": boot_id,
                    "findmnt -n -o SOURCE,UUID /": f"/dev/mmcblk1p2 {UID}",
                    "cat /etc/os-release": 'ID=debian\nNAME="Debian GNU/Linux"\nVERSION_ID="12"\n',
                    "cat /usr/trim/etc/version": "1.2.0701\n",
                    "cat /etc/fnos-x5/source-identity.txt": "source_image=fnOS ARM 1.2.0701 友善 NanoPi R3S\n",
                    "dpkg-query -W -f='${Version}' trim": "1.2.0701\n",
                    "dpkg-query -W -f='${Status}' trim": "install ok installed",
                }

                def responder(written: bytes) -> bytes:
                    text = written.decode("utf-8", "replace")
                    if written == b" ":
                        return b"\r\nHobot> "
                    if written == b"\x03":
                        return b"\r\nHobot> "
                    if text.startswith("reset"):
                        return (
                            b"resetting ...\r\nU-Boot 2022.10\r\n"
                            b"Hit <SPACE> key to stop autoboot in  1s\r\n"
                        )
                    if text.startswith("ums 0 mmc"):
                        ums_state["started"] = True
                        return (
                            b"UMS: LUN 0, dev mmc 1, hwpart 0, sector 0x0, "
                            b"count 0x3b72400\r\n"
                        )
                    if text.startswith("echo "):
                        import re as _re
                        match = _re.search(r"echo (__FNOS_BEGIN_[0-9a-f]+__); (.+); echo (__FNOS_END_[0-9a-f]+__)", text)
                        if not match:
                            return b""
                        begin, command, end = match.group(1), match.group(2), match.group(3)
                        if command == "mmc list":
                            body = "sdio-host@1: 1"
                        elif command == "mmc info":
                            body = (
                                "Name: SC64G\n"
                                f"Sector Count: {target_size // 512}\n"
                                "Block Length: 512\n"
                            )
                        elif command == "mmc cid":
                            body = ""
                        else:
                            body = ""
                        return (
                            f"{begin}\r\n{body}\r\n{end}\r\nHobot> ".encode()
                        )
                    if text.startswith("printf "):
                        import re as _re
                        match = _re.search(r"printf '\\n(__FNOS_BEGIN_[0-9a-f]+__)\\n'; (.+); printf '\\n(__FNOS_END_[0-9a-f]+__)\\n'", text)
                        if not match:
                            return b""
                        begin, command, end = match.group(1), match.group(2), match.group(3)
                        value = linux_values.get(command, "")
                        return (
                            f"{begin}\r\n{value}\n{end}\r\nroot@x5:~# ".encode()
                        )
                    return b""

                ums_state = {"started": False}
                transport = FakeSerialTransport(initial=b"Hobot> ", responder=responder)

                class ScriptedCapture(SessionSerialCapture):
                    def __init__(self, session):
                        super().__init__(session)
                        self.power_count = 0

                    def on_power_on(self):
                        super().on_power_on()
                        self.power_count += 1
                        if self.power_count == 1:
                            transport.feed(
                                b"resetting ...\r\nU-Boot 2022.10\r\n"
                                b"Hit <SPACE> key to stop autoboot in  1s\r\n"
                            )
                        elif self.power_count == 2:
                            transport.feed(("\r\n".join(boot_lines)).encode())

                context = LiveProviderContext(
                    plan,
                    disk_provider=lambda: (
                        [
                            TargetIdentity(
                                device="/dev/disk4", serial=target_serial,
                                model="USB 3.0 SSD", size_bytes=target_size, bus="USB",
                                whole_disk=True, internal=False, mounted=False,
                                read_only=False, virtual=False,
                                topology="USB:UMS disk 0:disk4",
                            )
                        ]
                        if ums_state["started"] else []
                    ),
                    reviewed_record=None,
                    authorize_ums=True,
                )
                sessions = []
                def factory(held):
                    session = SerialSession(
                        board_id="x5-lab-01", held=held,
                        port="/dev/fake-production", state_root=shared,
                        transport=transport,
                    )
                    sessions.append(session)
                    context.bind_session(session)
                    return BackendSet(
                        power=Cp5PowerBackend(
                            FakeCp5Client(value=True), board_id="x5-lab-01",
                            sleep=lambda _: None,
                        ),
                        serial=ScriptedCapture(session),
                        flash=FileFlashBackend(target, identity=fake_identity(plan)),
                    )

                # reviewed record is required because the fake U-Boot exposes no CID
                from fnos.lab.target_mapping import MappingRecord
                context.reviewed_record = MappingRecord(
                    schema="fnos-lab-target-map/v1", board_id="x5-lab-01",
                    board_media=BoardMedia(
                        mmc_index=1, name="SC64G", size_bytes=target_size,
                        size_exact=True,
                    ),
                    host_device="/dev/disk4", host_size_bytes=target_size,
                    host_model="USB 3.0 SSD", host_serial=target_serial,
                    evidence=("exact-sector-capacity", "single-usb-whole-disk"),
                    reviewed_by="offline-production-test", created_at="now",
                )
                engine, state = execute(
                    plan, plan_path=plan_path, state_root=shared,
                    backend_factory=factory,
                    execution_mode="real",
                    authorization=token, confirm_board="x5-lab-01",
                    cut_evidence_provider=context.cut_provider,
                    cut_evidence_validator=context.cut_validator,
                    candidate_manifest=manifest,
                    runtime_identity_provider=context.runtime_identity,
                    target_map_provider=context.target_map,
                    ums_prepare_provider=context.prepare_ums,
                    cold_boot_prepare_provider=context.prepare_cold_boot,
                    sleep=lambda _s: None,
                )
                self.assertEqual(state["status"], "passed", state.get("failure"))
                self.assertEqual(state["validation"]["candidate_id"], "test-candidate")
                self.assertTrue(state["validation"]["runtime_identity"]["boot_id"])


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / "state"

    def tearDown(self):
        self._tmp.cleanup()

    def test_expired_countdown_does_not_send_space(self):
        now = [0.0]
        with board_lock(self.state, "x5-lab-01") as held:
            session = SerialSession(
                board_id="x5-lab-01", held=held, port="/dev/fake-expired",
                state_root=self.state,
                transport=FakeSerialTransport(
                    initial=b"Hit <SPACE> key to stop autoboot in  1s\r\n"
                ),
                clock=lambda: now[0],
            )
            session.open()
            now[0] = 60.0
            with self.assertRaises(SessionRefused):
                session.interrupt_autoboot()
            self.assertEqual(session.transport.writes, [])
            session.close()

    def test_terminal_state_change_invalidates_cut_receipt(self):
        with board_lock(self.state, "x5-lab-01") as held:
            transport = FakeSerialTransport(initial=b"Hobot> \r\n", responder=uboot_responder)
            session = SerialSession(
                board_id="x5-lab-01", held=held, port="/dev/fake-state",
                state_root=self.state, transport=transport,
            )
            session.open()
            gate = CutEvidenceGate(board_id="x5-lab-01", session=session, ttl_seconds=10)
            evidence = gate.uboot_idle()
            gate.assert_valid(evidence)
            transport.feed(b"root@x5:~# ")
            session.refresh(0.1)
            with self.assertRaises(HardwareFailure):
                gate.assert_valid(evidence)
            session.close()


    def test_close_invalidates_receipt_evidence(self):
        with board_lock(self.state, "x5-lab-01") as held:
            session = SerialSession(
                board_id="x5-lab-01", held=held, port="/dev/fake-close",
                state_root=self.state,
                transport=FakeSerialTransport(initial=b"Hobot> \r\n", responder=uboot_responder),
            )
            session.open()
            gate = CutEvidenceGate(board_id="x5-lab-01", session=session, ttl_seconds=10)
            evidence = gate.uboot_idle()
            session.close()
            with self.assertRaises(HardwareFailure):
                gate.assert_valid(evidence)

    def test_same_physical_port_different_board_alias_is_exclusive(self):
        with board_lock(self.state, "x5-lab-01") as h1, board_lock(self.state, "x5-lab-02") as h2:
            first = SerialSession(
                board_id="x5-lab-01", held=h1, port="/dev/cu.usbserial-alias",
                state_root=self.state,
                transport=FakeSerialTransport(initial=b"Hobot> \r\n"),
            )
            first.open()
            second = SerialSession(
                board_id="x5-lab-02", held=h2, port="/dev/tty.usbserial-alias",
                state_root=self.state,
                transport=FakeSerialTransport(initial=b"Hobot> \r\n"),
            )
            with self.assertRaises(SessionRefused):
                second.open()
            first.close()
            second.close()

    def test_terminal_parser_handles_echo_nul_crlf_and_chunked_payload(self):
        from fnos.lab.uart_session import clean_terminal_output
        payload = "root@trim:~# uname -r\n6.18.0\nroot@trim:~# printf end"
        self.assertEqual(clean_terminal_output(payload, command="uname -r"), "6.18.0")
        self.assertEqual(
            clean_terminal_output("D-Robotics RDK X5\x00root@trim:~# ", command="cat /proc/device-tree/model"),
            "D-Robotics RDK X5",
        )
        self.assertEqual(
            clean_terminal_output("ID=debian\r\nNAME=\"Debian\"\r\nroot@trim:~# ", command="cat /etc/os-release"),
            "ID=debian\nNAME=\"Debian\"",
        )


    def test_actual_r3_manifest_loads_with_verified_module_release(self):
        from fnos.lab.candidate import load_candidate
        root=Path(__file__).resolve().parents[2]
        m=load_candidate(root/"fnos/logs/fnos-x5-candidate-r3-20261106/00-candidate.manifest.json")
        self.assertEqual(m.schema,"fnos-x5-assembly/v2")
        self.assertEqual(m.image_sha256,"653ffe7a5e20b019f0a5192799c5ddbda3752ffe5d8f6d67ba87e1afc498f599")
        self.assertEqual(m.image_size_bytes,13157531648)
        self.assertEqual(m.kernel_release,"6.18.0")
        self.assertEqual(m.fnos_version,"1.2.0701")
        self.assertFalse(m.accepted)

    def test_v2_product_identity_requires_version_file_not_source_marker(self):
        outputs = {
            "cat /proc/device-tree/model": "D-Robotics RDK X5 V0.1",
            "cat /proc/device-tree/compatible": "D-Robotics, x5",
            "cat /proc/device-tree/serial-number": "X5",
            "uname -r": "6.18.0",
            "cat /proc/sys/kernel/random/boot_id": UID,
            "findmnt -n -o SOURCE,UUID /": f"/dev/mmcblk1p2 {UID}",
            "cat /etc/os-release": 'ID=debian\nVERSION_ID="12"',
            "cat /usr/trim/etc/version": "1.2.0701",
            "cat /etc/fnos-x5/source-identity.txt": "",
            "dpkg-query -W -f='${Version}' trim": "1.2.0701",
            "dpkg-query -W -f='${Status}' trim": "install ok installed",
        }
        identity = parse_runtime_outputs(
            outputs, session_id="s1", source="serial", collected_at="now"
        )
        self.assertEqual(identity.fnos_version, "1.2.0701")
        self.assertEqual(identity.product_source_kind, "trim-version-file")
        ok, failures = verify_identity(
            identity, IdentityExpectation(fnos_version="1.2.0701", require_version_file=True)
        )
        self.assertTrue(ok, failures)

    def test_boot_identity_binding_rejects_previous_boot(self):
        from fnos.lab.runtime_identity import BootIdentityBinding
        identity = parse_runtime_outputs(
            {
                "cat /proc/device-tree/model": "D-Robotics RDK X5 V0.1",
                "cat /proc/device-tree/compatible": "D-Robotics, x5",
                "cat /proc/device-tree/serial-number": "X5",
                "uname -r": "6.18.0",
                "cat /proc/sys/kernel/random/boot_id": UID,
                "findmnt -n -o SOURCE,UUID /": f"/dev/mmcblk1p2 {UID}",
                "cat /etc/os-release": 'ID=debian\nVERSION_ID="12"',
                "cat /usr/trim/etc/version": "1.2.0701",
                "cat /etc/fnos-x5/source-identity.txt": "source_image=fnOS ARM 1.2.0701",
                "dpkg-query -W -f='${Version}' trim": "1.2.0701",
                "dpkg-query -W -f='${Status}' trim": "install ok installed",
            },
            session_id="s1", source="serial", collected_at="now",
        )
        with self.assertRaises(HardwareFailure):
            BootIdentityBinding(UID).require_new(identity)
        BootIdentityBinding("22222222-2222-4222-8222-222222222222").require_new(identity)


    def test_actual_assembly_v2_manifest_uses_os_and_trim_fields(self):
        from fnos.lab.candidate import ASSEMBLY_V2_SCHEMA, load_candidate
        path = Path(ROOT / "fnos/logs/fnos-x5-candidate-r1-20261106/artifacts/fnos-x5-candidate-r1.img.manifest.json")
        manifest = load_candidate(path)
        self.assertEqual(manifest.schema, ASSEMBLY_V2_SCHEMA)
        self.assertEqual(manifest.base_os_id, "debian")
        self.assertEqual(manifest.base_os_version, "12")
        self.assertEqual(manifest.fnos_id, "fnos")
        self.assertEqual(manifest.fnos_version, "1.2.0701")
        self.assertEqual(manifest.kernel_release, "6.18.0")
        self.assertEqual(manifest.root_uuid, "b7e6d5c4-3a21-4f8e-9d7c-6b5a49382716")
        self.assertEqual(manifest.image_sha256, "3bfc60c78845b35bba03527f4e3f6d1b6aefe6ea5d249afd3f65c4ba69cf33f4")
        self.assertEqual(manifest.image_size_bytes, 13157531648)
        self.assertFalse(manifest.accepted)
        with self.assertRaises(Refused):
            load_candidate(path, product_version="1.2.0000")

    def test_actual_assembly_manifest_requires_product_adapter(self):
        from fnos.lab.candidate import ASSEMBLY_SCHEMA, load_candidate
        path = Path(ROOT / "fnos/logs/fnos-x5-candidate-20261106/00-candidate.manifest.json")
        with self.assertRaises(Refused):
            load_candidate(path)
        manifest = load_candidate(path, product_version="1.2.0701")
        self.assertEqual(manifest.schema, ASSEMBLY_SCHEMA)
        self.assertEqual(manifest.base_os_id, "debian")
        self.assertEqual(manifest.base_os_version, "12")
        self.assertEqual(manifest.fnos_id, "fnos")
        self.assertEqual(manifest.fnos_version, "1.2.0701")
        self.assertEqual(manifest.kernel_release, "6.18.0")
        self.assertEqual(manifest.root_uuid, "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")
        self.assertFalse(manifest.accepted)


    def test_raw_echo_transcript_is_parsed_without_prompt_as_value(self):
        values = {
            "cat /proc/device-tree/model": "D-Robotics RDK X5 V0.1",
            "cat /proc/device-tree/compatible": "D-Robotics, x5",
            "cat /proc/device-tree/serial-number": "X5",
            "uname -r": "6.18.0",
            "cat /proc/sys/kernel/random/boot_id": UID,
            "findmnt -n -o SOURCE,UUID /": f"/dev/mmcblk1p2 {UID}",
            "cat /etc/os-release": 'ID=debian\nNAME="Debian GNU/Linux"\nVERSION_ID="12"',
            "cat /usr/trim/etc/version": "1.2.0701",
            "cat /etc/fnos-x5/source-identity.txt": "source_image=fnOS ARM 1.2.0701",
            "dpkg-query -W -f='${Version}' trim": "1.2.0701",
            "dpkg-query -W -f='${Status}' trim": "install ok installed",
        }
        raw = {
            command: f"root@trim:~# {command}\n{value}\nroot@trim:~# printf end"
            for command, value in values.items()
        }
        identity = parse_runtime_outputs(
            raw, session_id="raw", source="serial", collected_at="now"
        )
        self.assertEqual(identity.kernel_release, "6.18.0")
        self.assertEqual(identity.base_os_id, "debian")
        self.assertEqual(identity.base_os_version, "12")
        self.assertEqual(identity.fnos_version, "1.2.0701")
        self.assertTrue(identity.boot_id)

    def test_live_normal_shutdown_binds_previous_boot_id(self):
        from fnos.lab.live import LiveProviderContext
        from fnos.lab.uart_session import SerialSession

        def responder(written: bytes) -> bytes:
            text = written.decode("utf-8", "replace")
            if text.startswith("sync; poweroff"):
                return b"Power down\r\n"
            import re as _re
            match = _re.search(r"printf '\\n(__FNOS_BEGIN_[0-9a-f]+__)\\n'; (.+); printf '\\n(__FNOS_END_[0-9a-f]+__)\\n'", text)
            if not match:
                return b""
            begin, command, end = match.group(1), match.group(2), match.group(3)
            values = {
                "cat /proc/device-tree/model": "D-Robotics RDK X5 V0.1",
                "cat /proc/device-tree/compatible": "D-Robotics, x5",
                "cat /proc/device-tree/serial-number": "X5",
                "uname -r": "6.18.0",
                "cat /proc/sys/kernel/random/boot_id": UID,
                "findmnt -n -o SOURCE,UUID /": f"/dev/mmcblk1p2 {UID}",
                "cat /etc/os-release": 'ID=debian\nVERSION_ID="12"',
                "cat /usr/trim/etc/version": "1.2.0701",
                "cat /etc/fnos-x5/source-identity.txt": "source_image=fnOS ARM 1.2.0701",
                "dpkg-query -W -f='${Version}' trim": "1.2.0701",
                "dpkg-query -W -f='${Status}' trim": "install ok installed",
            }
            return f"{begin}\r\n{values.get(command, '')}\n{end}\r\nroot@trim:~# ".encode()

        with board_lock(self.state, "x5-lab-01") as held:
            transport = FakeSerialTransport(initial=b"root@trim:~# ", responder=responder)
            session = SerialSession(
                board_id="x5-lab-01", held=held, port="/dev/fake-normal",
                state_root=self.state, transport=transport,
            )
            session.open()
            context = LiveProviderContext(
                {"board": {"board_id": "x5-lab-01"}}, authorize_ums=False
            )
            context.bind_session(session)
            evidence = context.normal_shutdown()
            self.assertEqual(evidence.state, "linux-poweroff-complete")
            self.assertEqual(context.previous_boot_id, UID)
            session.close()

            # A second session reporting the same boot_id is stale even if all
            # strings look valid.
            transport2 = FakeSerialTransport(initial=b"root@trim:~# ", responder=responder)
            session2 = SerialSession(
                board_id="x5-lab-01", held=held, port="/dev/fake-normal2",
                state_root=self.state, transport=transport2,
            )
            session2.open()
            context.bind_session(session2)
            with self.assertRaises(HardwareFailure):
                context.runtime_identity()
            session2.close()

class SerialSessionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"

    def tearDown(self):
        self._tmp.cleanup()

    def test_state_classification(self):
        self.assertEqual(classify_state("U-Boot 2022\nHobot> "), STATE_UBOOT)
        self.assertEqual(classify_state("login: "), "linux-login")
        self.assertEqual(classify_state("root@board:~# "), STATE_LINUX_SHELL)
        self.assertEqual(classify_state("random text"), "unknown")

    def test_session_ownership_is_exclusive(self):
        transport = FakeSerialTransport(initial=b"Hobot> \r\n", responder=uboot_responder)
        with board_lock(self.state, "x5-lab-01") as held:
            first = SerialSession(
                board_id="x5-lab-01", held=held, port="/dev/fake0",
                state_root=self.state, transport=transport,
            )
            self.assertEqual(first.open(), STATE_UBOOT)
            try:
                second = SerialSession(
                    board_id="x5-lab-01", held=held, port="/dev/fake0",
                    state_root=self.state,
                    transport=FakeSerialTransport(initial=b"Hobot> \r\n"),
                )
                with self.assertRaises(SessionRefused):
                    second.open()
            finally:
                first.close()
            third = SerialSession(
                board_id="x5-lab-01", held=held, port="/dev/fake0",
                state_root=self.state,
                transport=FakeSerialTransport(initial=b"Hobot> \r\n"),
            )
            self.assertEqual(third.open(), STATE_UBOOT)
            third.close()


    def test_autoboot_interrupt_requires_fresh_countdown_and_prompt(self):
        initial = b"Hit <SPACE> key to stop autoboot in  1s\r\n"

        def responder(written: bytes) -> bytes:
            if written == b" ":
                return b"\r\nHobot> "
            return b""

        with board_lock(self.state, "x5-lab-01") as held:
            session = SerialSession(
                board_id="x5-lab-01", held=held, port="/dev/fake5",
                state_root=self.state,
                transport=FakeSerialTransport(initial=initial, responder=responder),
            )
            session.open()
            self.assertEqual(session.interrupt_autoboot(timeout=1.0), STATE_UBOOT)
            self.assertEqual(session.transport.writes, [b" "])
            session.close()


    def test_stale_countdown_after_switch_is_refused(self):
        initial = (
            b"Hit <SPACE> key to stop autoboot in  1s\r\n"
            b"switch to partitions #0, OK\r\n"
        )
        with board_lock(self.state, "x5-lab-01") as held:
            session = SerialSession(
                board_id="x5-lab-01", held=held, port="/dev/fake7",
                state_root=self.state,
                transport=FakeSerialTransport(initial=initial),
            )
            session.open()
            with self.assertRaises(SessionRefused):
                session.interrupt_autoboot()
            self.assertEqual(session.transport.writes, [])
            session.close()

    def test_autoboot_interrupt_never_sent_without_countdown(self):
        with board_lock(self.state, "x5-lab-01") as held:
            session = SerialSession(
                board_id="x5-lab-01", held=held, port="/dev/fake6",
                state_root=self.state,
                transport=FakeSerialTransport(initial=b"root@x5:~# \r\n"),
            )
            session.open()
            with self.assertRaises(SessionRefused):
                session.interrupt_autoboot()
            self.assertEqual(session.transport.writes, [])
            session.close()



    def test_fixed_numeric_probe_is_allowlisted(self):
        with board_lock(self.state, "x5-lab-01") as held:
            session = SerialSession(
                board_id="x5-lab-01", held=held, port="/dev/fake-probe",
                state_root=self.state,
                transport=FakeSerialTransport(initial=b"Hobot> \r\n", responder=uboot_responder),
            )
            session.open()
            result = session.uboot_safe_probe("guard_hex_gt")
            self.assertIn("0x120b838a", result.command)
            self.assertTrue(result.receipt_id)
            with self.assertRaises(SessionRefused):
                session.uboot_safe_probe("rm")
            session.close()


    def test_ums_start_requires_gadget_lun_output(self):
        def responder(written: bytes) -> bytes:
            if written.startswith(b"ums 0 mmc"):
                return b"not a ums response\r\n"
            return b"Hobot> "
        with board_lock(self.state, "x5-lab-01") as held:
            session = SerialSession(
                board_id="x5-lab-01", held=held, port="/dev/fake-ums-bad",
                state_root=self.state,
                transport=FakeSerialTransport(initial=b"Hobot> \r\n", responder=responder),
            )
            session.open()
            with self.assertRaises(Exception):
                session.uboot_ums(1)
            self.assertNotEqual(session.state, "u-boot-ums")
            session.close()

    def test_ums_stop_requires_post_ctrl_c_prompt_not_old_text(self):
        def responder(written: bytes) -> bytes:
            if written.startswith(b"ums 0 mmc"):
                return b"UMS: LUN 0, dev mmc 1, hwpart 0, sector 0x0, count 0x3b72400\r\n"
            if written == b"\x03":
                return b"\r\n"  # no fresh prompt
            return b"Hobot> "
        with board_lock(self.state, "x5-lab-01") as held:
            session = SerialSession(
                board_id="x5-lab-01", held=held, port="/dev/fake-ums-stop",
                state_root=self.state,
                transport=FakeSerialTransport(initial=b"Hobot> \r\n", responder=responder),
            )
            session.open()
            session.uboot_ums(1)
            with self.assertRaises(SessionTimeout):
                session.uboot_stop_ums(timeout=0.5)
            session.close()

    def test_mmc_selector_is_separate_and_receipted(self):
        with board_lock(self.state, "x5-lab-01") as held:
            session = SerialSession(
                board_id="x5-lab-01", held=held, port="/dev/fake-mmcdev",
                state_root=self.state,
                transport=FakeSerialTransport(initial=b"Hobot> \r\n", responder=uboot_responder),
            )
            session.open()
            result = session.uboot_select_mmc(1)
            self.assertEqual(result.command, "mmc dev 1")
            self.assertEqual(result.state_after, STATE_UBOOT)
            self.assertTrue(result.receipt_id)
            self.assertEqual(session.receipt(result.receipt_id).command, "mmc dev 1")
            with self.assertRaises(SessionRefused):
                session.uboot_select_mmc(-1)
            session.close()

    def test_uboot_command_uses_fresh_nonce_and_excludes_old_marker(self):
        old = b"old __FNOS_END_deadbeef__\nHobot> \r\n"
        transport = FakeSerialTransport(initial=old, responder=uboot_responder)
        with board_lock(self.state, "x5-lab-01") as held:
            session = SerialSession(
                board_id="x5-lab-01", held=held, port="/dev/fake0",
                state_root=self.state, transport=transport,
            )
            session.open()
            result = session.uboot_readonly("mmc info")
            self.assertIn("Capacity: 64 GiB", result.output)
            self.assertNotIn("deadbeef", result.output)
            self.assertNotEqual(result.nonce, "deadbeef")
            self.assertEqual(session.state, STATE_UBOOT)
            session.close()

    def test_uboot_command_refused_in_linux_and_vice_versa(self):
        with board_lock(self.state, "x5-lab-01") as held:
            u = SerialSession(
                board_id="x5-lab-01", held=held, port="/dev/fake1",
                state_root=self.state,
                transport=FakeSerialTransport(initial=b"Hobot> \r\n"),
            )
            u.open()
            with self.assertRaises(SessionRefused):
                u.linux_identity("uname -r")
            u.close()
            linux = SerialSession(
                board_id="x5-lab-01", held=held, port="/dev/fake2",
                state_root=self.state,
                transport=FakeSerialTransport(initial=b"root@x5:~# \r\n"),
            )
            self.assertEqual(linux.open(), STATE_LINUX_SHELL)
            with self.assertRaises(SessionRefused):
                linux.uboot_readonly("mmc info")
            linux.close()

    def test_marker_embedded_in_echo_is_not_a_response_boundary(self):
        def echo_only(written: bytes) -> bytes:
            return written + b"\r\n"  # no standalone end-marker output line

        with board_lock(self.state, "x5-lab-01") as held:
            session = SerialSession(
                board_id="x5-lab-01", held=held, port="/dev/fake3",
                state_root=self.state,
                transport=FakeSerialTransport(initial=b"Hobot> \r\n", responder=echo_only),
            )
            session.open()
            with self.assertRaises(SessionTimeout):
                session.uboot_readonly("mmc info", timeout=0.1)
            session.close()


    def test_linux_normal_shutdown_captures_fresh_power_down(self):
        def responder(written: bytes) -> bytes:
            if b"poweroff" in written:
                return b"systemd-shutdown[1]: Powering off.\r\nPower down\r\n"
            return b""

        with board_lock(self.state, "x5-lab-01") as held:
            session = SerialSession(
                board_id="x5-lab-01", held=held, port="/dev/fake8",
                state_root=self.state,
                transport=FakeSerialTransport(
                    initial=b"root@x5:~# \r\n", responder=responder
                ),
            )
            session.open()
            result = session.linux_normal_shutdown(timeout=1.0)
            self.assertEqual(result.command, "sync; poweroff")
            self.assertIn("Power down", result.output)
            self.assertGreaterEqual(len(result.nonce), 16)
            session.close()

    def test_linux_identity_allowlist(self):
        def linux_responder(written: bytes) -> bytes:
            match = re.search(rb"__FNOS_BEGIN_([0-9a-f]+)__", written)
            nonce = match.group(1) if match else b"deadbeef"
            return (
                b"__FNOS_BEGIN_" + nonce + b"__\n"
                b"6.18.0-x5\n"
                b"__FNOS_END_" + nonce + b"__\n"
                b"root@x5:~# "
            )
        with board_lock(self.state, "x5-lab-01") as held:
            session = SerialSession(
                board_id="x5-lab-01", held=held, port="/dev/fake4",
                state_root=self.state,
                transport=FakeSerialTransport(
                    initial=b"root@x5:~# \r\n", responder=linux_responder
                ),
            )
            session.open()
            result = session.linux_identity("uname -r")
            self.assertIn("6.18.0-x5", result.output)
            with self.assertRaises(SessionRefused):
                session.linux_identity("rm -rf /")
            session.close()


class SafeCutTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"

    def tearDown(self):
        self._tmp.cleanup()

    def _session(self, held, port="/dev/fake-cut", initial=b"Hobot> \r\n"):
        s = SerialSession(
            board_id="x5-lab-01", held=held, port=port,
            state_root=self.state,
            transport=FakeSerialTransport(initial=initial, responder=uboot_responder),
        )
        s.open()
        return s

    def test_uboot_idle_evidence_bound_and_expiry(self):
        with board_lock(self.state, "x5-lab-01") as held:
            session = self._session(held)
            now = [1000.0]
            gate = CutEvidenceGate(
                board_id="x5-lab-01", session=session, ttl_seconds=5, now=lambda: now[0]
            )
            ev = gate.uboot_idle()
            self.assertEqual(ev.state, CUT_UBOOT_IDLE)
            now = ev.observed_mono + 1.0
            self.assertEqual(ev.validate_static(board_id="x5-lab-01", session_id=session.session_id, now_mono=now)[0], True)
            ok, reason = ev.validate_static(board_id="x5-lab-02", session_id=session.session_id, now_mono=now)
            self.assertFalse(ok); self.assertIn("different board", reason)
            ok, reason = ev.validate_static(board_id="x5-lab-01", session_id="other", now_mono=now)
            self.assertFalse(ok); self.assertIn("different serial", reason)
            ok, reason = ev.validate_static(board_id="x5-lab-01", session_id=session.session_id, now_mono=ev.expires_mono + 0.1)
            self.assertFalse(ok); self.assertIn("expired", reason)
            session.close()

    def test_poweroff_evidence_requires_fresh_power_down_marker(self):
        from fnos.lab.uart_session import CommandResult

        def responder(written: bytes) -> bytes:
            if b"poweroff" in written:
                return b"systemd-shutdown[1]: Powering off.\r\nPower down\r\n"
            return b""

        with board_lock(self.state, "x5-lab-01") as held:
            session = SerialSession(
                board_id="x5-lab-01", held=held, port="/dev/fake-poweroff",
                state_root=self.state,
                transport=FakeSerialTransport(
                    initial=b"root@x5:~# \r\n", responder=responder
                ),
            )
            session.open()
            gate = CutEvidenceGate(board_id="x5-lab-01", session=session, ttl_seconds=5)
            forged = CommandResult(
                command="sync; poweroff", state_before="linux-shell",
                state_after="linux-boot", nonce="a" * 32,
                output="Power down", started_at="", finished_at="", bytes_read=10,
                receipt_id="forged-receipt", terminal_epoch=1,
            )
            with self.assertRaises(Refused):
                gate.poweroff_complete(forged)
            result = session.linux_normal_shutdown(timeout=1.0)
            ev = gate.poweroff_complete(result)
            self.assertEqual(ev.state, CUT_LINUX_POWEROFF)
            gate.assert_valid(ev)
            session.close()

    def test_cut_validator_blocks_power_manager(self):
        calls = []
        client = FakeCp5Client(value=True)
        backend = Cp5PowerBackend(client, board_id="x5-lab-01", sleep=lambda _: None)
        manager = PowerManager(
            backend, phase=lambda: "preflight",
            emit=lambda *a, **k: calls.append((a, k)), save=lambda: None,
            sleep=lambda _: None,
        )
        with self.assertRaises(Refused):
            manager.turn_off(
                "test", safe_to_cut=True, cut_validator=lambda: False
            )
        self.assertEqual(client.calls, [])
        self.assertTrue(any(a and a[0] == "power_cut_refused" for a, _ in calls))


class IdentityTests(unittest.TestCase):
    def _outputs(self, **changes):
        values = {
            "cat /proc/device-tree/model": "Horizon X5 RDK X5\0",
            "cat /proc/device-tree/compatible": "d-robotics,rdk-x5\0",
            "cat /proc/device-tree/serial-number": "X5SERIAL",
            "uname -r": "6.18.0-x5",
            "cat /proc/sys/kernel/random/boot_id": UID,
            "findmnt -n -o SOURCE,UUID /": f"/dev/mmcblk1p2 {UID}",
            "cat /etc/os-release": 'ID=debian\nNAME="Debian GNU/Linux"\nVERSION_ID="12"\n',
            "cat /usr/trim/etc/version": "1.2.0701\n",
            "cat /etc/fnos-x5/source-identity.txt": "source_image=fnOS ARM 1.2.0701 友善 NanoPi R3S\n",
            "dpkg-query -W -f='${Version}' trim": "1.2.0701\n",
            "dpkg-query -W -f='${Status}' trim": "install ok installed",
        }
        values.update(changes)
        return values

    def test_parse_and_verify_identity(self):
        identity = parse_runtime_outputs(
            self._outputs(), session_id="s1", source="serial", collected_at="now"
        )
        ok, failures = verify_identity(
            identity,
            IdentityExpectation(
                board_model_contains="RDK X5", kernel_release="6.18.0-x5",
                fnos_id="fnos", fnos_version="1.2.0701", root_uuid=UID,
            ),
        )
        self.assertTrue(ok, failures)
        for changes in (
            {"cat /proc/device-tree/model": "Other board\0"},
            {"cat /proc/sys/kernel/random/boot_id": "not-a-boot-id"},
            {"findmnt -n -o SOURCE,UUID /": f"/dev/mmcblk1p2 {'0'*32}"},
            {"cat /usr/trim/etc/version": "", "cat /etc/fnos-x5/source-identity.txt": ""},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(HardwareFailure):
                    parse_runtime_outputs(
                        self._outputs(**changes), session_id="s1",
                        source="serial", collected_at="now",
                    )
        wrong_kernel = parse_runtime_outputs(
            self._outputs(**{"uname -r": "6.1.0"}), session_id="s1",
            source="serial", collected_at="now",
        )
        ok, failures = verify_identity(
            wrong_kernel, IdentityExpectation(kernel_release="6.18.0-x5")
        )
        self.assertFalse(ok)
        self.assertTrue(any("kernelrelease" in x for x in failures))


    def test_product_trim_mismatch_is_rejected(self):
        outputs = self._outputs(**{
            "dpkg-query -W -f='${Version}' trim": "2.0.0",
            "dpkg-query -W -f='${Status}' trim": "install ok installed",
        })
        with self.assertRaises(HardwareFailure):
            parse_runtime_outputs(
                outputs, session_id="s1", source="serial", collected_at="now"
            )


    def test_trim_status_must_be_installed(self):
        outputs = self._outputs(**{
            "dpkg-query -W -f='${Status}' trim": "deinstall ok config-files"
        })
        with self.assertRaises(HardwareFailure):
            parse_runtime_outputs(
                outputs, session_id="s1", source="serial", collected_at="now"
            )

    def test_wrong_expectation_rejected(self):
        identity = parse_runtime_outputs(
            self._outputs(), session_id="s1", source="serial", collected_at="now"
        )
        ok, failures = verify_identity(
            identity, IdentityExpectation(kernel_release="6.1.0", fnos_version="2.0.0")
        )
        self.assertFalse(ok)
        self.assertTrue(any("kernelrelease" in x for x in failures))
        self.assertTrue(any("fnOS product version" in x for x in failures))


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _manifest(self, **changes):
        raw = {
            "schema": "fnos-lab-candidate/v1",
            "candidate_id": "assembly-20261106",
            "board_model": "RDK X5",
            "kernel_release": "6.18.0-x5",
            "fnos_id": "fnos",
            "fnos_version": "1.2.0701",
            "base_os_id": "debian",
            "base_os_version": "12",
            "root_uuid": UID,
            "image_sha256": "a" * 64,
            "image_size_bytes": 1024,
            "source": "server-build",
            "created_at": "2026-11-06T00:00:00Z",
            "accepted": True,
        }
        raw.update(changes)
        p = self.tmp / "manifest.json"
        p.write_text(json.dumps(raw), encoding="utf-8")
        return p


    def test_assembly_manifest_product_field_is_used(self):
        from fnos.lab.candidate import load_candidate
        source = Path(ROOT / "fnos/logs/fnos-x5-candidate-20261106/00-candidate.manifest.json")
        raw = __import__("json").loads(source.read_text())
        raw["product"] = {"trim_version": "1.2.0701"}
        path = Path(self._tmp.name) / "assembly-product.json"
        path.write_text(__import__("json").dumps(raw))
        manifest = load_candidate(path)
        self.assertEqual(manifest.fnos_version, "1.2.0701")

    def test_manifest_load_and_image_binding(self):
        m = load_candidate(self._manifest())
        self.assertEqual(m.candidate_id, "assembly-20261106")
        image = self.tmp / "image.img"
        image.write_bytes(b"x" * 1024)
        import hashlib
        raw = json.loads((self.tmp / "manifest.json").read_text())
        raw["image_sha256"] = hashlib.sha256(image.read_bytes()).hexdigest()
        (self.tmp / "manifest.json").write_text(json.dumps(raw))
        m = load_candidate(self.tmp / "manifest.json")
        self.assertEqual(verify_candidate_image(m, image)["size_bytes"], 1024)
        image.write_bytes(b"y" * 1024)
        with self.assertRaises(Refused):
            verify_candidate_image(m, image)

    def test_candidate_rejects_missing_or_bad_identity(self):
        m = load_candidate(self._manifest())
        identity = parse_runtime_outputs(
            {
                "cat /proc/device-tree/model": "Horizon X5 RDK X5\0",
                "cat /proc/device-tree/compatible": "d-robotics,rdk-x5\0",
                "cat /proc/device-tree/serial-number": "X5SERIAL",
                "uname -r": "6.18.0-x5",
                "cat /proc/sys/kernel/random/boot_id": UID,
                "findmnt -n -o SOURCE,UUID /": f"/dev/mmcblk1p2 {UID}",
                "cat /etc/os-release": 'ID=debian\nNAME="Debian GNU/Linux"\nVERSION_ID="12"\n',
            "cat /usr/trim/etc/version": "1.2.0701\n",
            "cat /etc/fnos-x5/source-identity.txt": "source_image=fnOS ARM 1.2.0701 友善 NanoPi R3S\n",
            "dpkg-query -W -f='${Version}' trim": "1.2.0701\n",
            "dpkg-query -W -f='${Status}' trim": "install ok installed",
            },
            session_id="s1", source="serial", collected_at="now",
        )
        require_candidate_identity(m, identity)
        bad = parse_runtime_outputs(
            {
                "cat /proc/device-tree/model": "Horizon X5 RDK X5\0",
                "cat /proc/device-tree/compatible": "d-robotics,rdk-x5\0",
                "cat /proc/device-tree/serial-number": "X5SERIAL",
                "uname -r": "6.1.0",
                "cat /proc/sys/kernel/random/boot_id": UID,
                "findmnt -n -o SOURCE,UUID /": f"/dev/mmcblk1p2 {UID}",
                "cat /etc/os-release": 'ID=debian\nNAME="Debian GNU/Linux"\nVERSION_ID="12"\n',
            "cat /usr/trim/etc/version": "1.2.0701\n",
            "cat /etc/fnos-x5/source-identity.txt": "source_image=fnOS ARM 1.2.0701 友善 NanoPi R3S\n",
            "dpkg-query -W -f='${Version}' trim": "1.2.0701\n",
            "dpkg-query -W -f='${Status}' trim": "install ok installed",
            },
            session_id="s1", source="serial", collected_at="now",
        )
        with self.assertRaises(HardwareFailure):
            require_candidate_identity(m, bad)


class TargetMappingTests(unittest.TestCase):
    def _disk(self, **changes):
        values = dict(
            device="/dev/disk9", serial="00112233445566778899aabbccddeeff",
            model="UMS disk 0", size_bytes=64 * 1024**3, bus="USB",
            whole_disk=True, internal=False, mounted=False, read_only=False,
            virtual=False, topology="USB:UMS disk 0:disk9",
        )
        values.update(changes)
        return TargetIdentity(**values)

    def test_parse_mmc_and_unique_mapping(self):
        self.assertEqual(parse_mmc_list("sdio-host@1: 0\nsdio-host@2: 1\n"), {0: "sdio-host@1", 1: "sdio-host@2"})
        media = parse_mmc_info(
            "Name: SC64G\nSector Count: 134217728\nBlock Length: 512\nCID: 00112233445566778899aabbccddeeff\n",
            mmc_index=1,
        )
        self.assertEqual(media.size_bytes, 64 * 1024**3)
        target = map_unique(
            board_id="x5-lab-01", board_media=media, disks=[self._disk()]
        )
        self.assertEqual(target.host_device, "/dev/disk9")
        self.assertIn("cid_equals_host_serial", target.criteria)
        with self.assertRaises(Refused):
            map_unique(
                board_id="x5-lab-01", board_media=media,
                disks=[self._disk(), self._disk(device="/dev/disk10")],
            )
        with self.assertRaises(Refused):
            map_unique(
                board_id="x5-lab-01", board_media=media,
                disks=[self._disk(serial="different")],
            )

    def test_no_serial_requires_reviewed_multi_evidence_record(self):
        media = BoardMedia(mmc_index=1, name="SC64G", size_bytes=64 * 1024**3, size_exact=True)
        disk = self._disk(serial=None)
        with self.assertRaises(Refused):
            map_unique(board_id="x5-lab-01", board_media=media, disks=[disk])
        record = MappingRecord(
            schema="fnos-lab-target-map/v1", board_id="x5-lab-01",
            board_media=media, host_device="/dev/disk9",
            host_size_bytes=64 * 1024**3, host_model="UMS disk 0",
            host_serial=None, evidence=("capacity", "usb-topology", "single-candidate"),
            reviewed_by="codex/reviewed-2026-11-06", created_at="now",
        )
        target = map_unique(
            board_id="x5-lab-01", board_media=media, disks=[disk],
            reviewed_record=record,
        )
        self.assertTrue(target.reviewed_record)
        with self.assertRaises(Refused):
            reidentify(
                target, board_media=media,
                disks=[self._disk(serial=None, device="/dev/disk11")],
                reviewed_record=record,
            )

    def test_mounted_or_internal_disks_are_rejected(self):
        media = BoardMedia(mmc_index=1, name="SC64G", size_bytes=64 * 1024**3,
                           cid="00112233445566778899aabbccddeeff", size_exact=True)
        for changes in ({"internal": True}, {"mounted": True}, {"virtual": True}):
            with self.subTest(changes=changes), self.assertRaises(Refused):
                map_unique(
                    board_id="x5-lab-01", board_media=media,
                    disks=[self._disk(**changes)],
                )



    def test_approximate_capacity_is_refused(self):
        media = parse_mmc_info("Name: SC64G\nCapacity: 64 GiB\n", mmc_index=1)
        self.assertFalse(media.size_exact)
        with self.assertRaises(Refused):
            map_unique(board_id="x5-lab-01", board_media=media, disks=[self._disk()])

    def test_mapping_must_match_plan_device_and_size(self):
        from fnos.lab.target_mapping import verify_mapping_against_plan
        media = BoardMedia(
            mmc_index=1, name="SC64G", size_bytes=64 * 1024**3,
            cid="00112233445566778899aabbccddeeff", size_exact=True,
        )
        mapping = map_unique(board_id="x5-lab-01", board_media=media, disks=[self._disk()])
        actual = self._disk()
        verify_mapping_against_plan(
            mapping,
            plan_target={"device": "/dev/disk9", "size_bytes": 64 * 1024**3},
            actual=actual,
        )
        with self.assertRaises(Refused):
            verify_mapping_against_plan(
                mapping,
                plan_target={"device": "/dev/disk99", "size_bytes": 64 * 1024**3},
                actual=actual,
            )
        with self.assertRaises(Refused):
            verify_mapping_against_plan(
                mapping,
                plan_target={"device": "/dev/disk9", "size_bytes": 2048},
                actual=actual,
            )

class BackupTests(unittest.TestCase):
    def _source(self, **changes):
        values = dict(
            device="/dev/disk9", serial="X5CARD", model="UMS disk 0",
            size_bytes=1024 * 1024, bus="USB", whole_disk=True, internal=False,
            mounted=False, read_only=False, virtual=False,
        )
        values.update(changes)
        return TargetIdentity(**values)

    def test_plan_is_non_executed_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = build_backup_plan(
                board_id="x5-lab-01", source=self._source(), destination_dir=root,
                destination_name="backup.img", expect_serial="X5CARD",
            )
            self.assertFalse(plan.write_executed)
            self.assertEqual(plan.status, "planned-not-executed")
            out = root / "plan.json"
            write_backup_plan(plan, out)
            self.assertFalse(load_backup_plan(out)["write_executed"])
            with self.assertRaises(Exception):
                write_backup_plan(plan, out)
            with self.assertRaises(Refused):
                build_backup_plan(
                    board_id="x5-lab-01", source=self._source(mounted=True),
                    destination_dir=root, destination_name="backup2.img",
                    expect_serial="X5CARD",
                )
            with self.assertRaises(Refused):
                build_backup_plan(
                    board_id="x5-lab-01", source=self._source(serial=None),
                    destination_dir=root, destination_name="backup3.img",
                    expect_serial="X5CARD",
                )


if __name__ == "__main__":
    unittest.main()
