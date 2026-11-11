"""File-backed substitutes for the offline test and ``--backends fake`` path.

The fake target is an ordinary sparse file under the private state directory;
it is never ``/dev``.  Identity is derived from the reviewed plan and can be
changed only by rewriting the metadata file in a test.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .flash import FileFlashBackend, TargetIdentity, expected_identity
from .power import Cp5PowerBackend, FakeCp5Client
from .runner import BackendSet
from .serial import FakeSerialCapture


def fake_identity(plan: dict[str, Any]) -> TargetIdentity:
    expected = expected_identity(plan)
    target = plan.get("target") or {}
    return TargetIdentity(
        device=expected.device,
        serial=expected.serial,
        model=expected.model,
        size_bytes=expected.size_bytes,
        bus=expected.bus,
        whole_disk=True,
        internal=False,
        mounted=False,
        read_only=False,
        virtual=False,
        topology=target.get("topology"),
        mount_points=(),
    )


def build_fake_backends(
    plan: dict[str, Any],
    *,
    state_root: Path,
    serial_lines: list[str] | None = None,
) -> BackendSet:
    root = Path(state_root).expanduser()
    target = root / "fake-target.img"
    size = int((plan.get("target") or {}).get("size_bytes") or 0)
    image_size = int((plan.get("image") or {}).get("size_bytes") or 0)
    if not target.exists():
        fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            # Sparse and bounded by the image length; identity still reports
            # the planned full target capacity.
            os.ftruncate(fd, min(size, image_size))
        finally:
            os.close(fd)
    identity = fake_identity(plan)
    client = FakeCp5Client(value=True)
    return BackendSet(
        power=Cp5PowerBackend(client, board_id=plan["board"]["board_id"], sleep=lambda _: None),
        serial=FakeSerialCapture(
            lines=serial_lines
            if serial_lines is not None
            else ["U-Boot SPL", "Hobot> ", "Linux version OLD", "login:"],
            boot_lines=["Linux version 6.18", "login:"],
            boot_record={"boot_id": "fake-boot-id", "rootfs": True},
            cut_safe=True,
        ),
        flash=FileFlashBackend(target, identity=identity),
    )
