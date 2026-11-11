#!/usr/bin/env python3
"""Read-only device-binding preflight.

Runs ``diskutil`` identity queries, ``stat`` on the serial device name (never
``open``) and an image hash.  It never opens the serial port, calls the CP5
cloud API, writes a disk or switches power.  A non-ready result is a checklist,
not an error.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fnos.lab.inspect import load_bindings, real_inventory  # noqa: E402
from fnos.lab.locks import board_lock_path  # noqa: E402
from fnos.lab.registry import access_matrix  # noqa: E402
from fnos.lab.util import image_fingerprint  # noqa: E402

DEFAULT_BINDINGS = ROOT / "fnos" / "integration" / "hardware" / "board-bindings.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", required=True)
    parser.add_argument("--bindings", type=Path, default=DEFAULT_BINDINGS)
    parser.add_argument("--target", help="current whole /dev/diskN")
    parser.add_argument("--expect-serial", help="expected target disk serial")
    parser.add_argument("--image", type=Path)
    parser.add_argument("--sha256", help="trusted image SHA-256 to compare")
    parser.add_argument("--state-dir", type=Path, help="lock root override")
    args = parser.parse_args(argv)

    boards = load_bindings(args.bindings)
    binding = boards.get(args.board)
    if binding is None:
        print(json.dumps({"ready_for_plan": False, "error": "board not bound"}, indent=2))
        return 2

    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    lock = board_lock_path(
        args.state_dir or os.environ.get("FNOS_BOARD_LOCK_ROOT"), args.board
    )
    check("board_reviewed", binding.get("reviewed") is True, "binding reviewed=true")
    check("lock_protocol", lock.name.startswith("board-"), str(lock))
    power = binding.get("power") or {}
    check(
        "cp5_usb_channel",
        power.get("backend") == "cp5-usb" and power.get("channel") == "usb",
        f"backend={power.get('backend')} channel={power.get('channel')}",
    )
    check(
        "cp5_mapping_verified",
        power.get("mapping_verified") is True,
        "physical usb-group mapping must be confirmed by the user",
    )
    serial = binding.get("serial") or {}
    port = serial.get("port") or ""
    if port:
        try:
            info = os.stat(port, follow_symlinks=False)
            is_char = stat.S_ISCHR(info.st_mode)
        except OSError:
            is_char = False
        check("serial_binding_stat", is_char, f"{port} (not opened)")
    else:
        check("serial_binding_stat", False, "serial.port is empty")

    inventory = real_inventory(bindings=boards)
    target = None
    if args.target:
        target = next((d for d in inventory.disks if d["device"] == args.target), None)
        check("target_present", target is not None, args.target or "")
        if target is not None:
            check(
                "target_identity_verified",
                target.get("identity_status") == "verified",
                target.get("identity_status") or "unknown",
            )
            if args.expect_serial:
                check(
                    "target_serial_matches",
                    target.get("serial") == args.expect_serial,
                    f"expected {args.expect_serial}",
                )
    else:
        check("target_present", False, "--target was not supplied")

    if args.image:
        try:
            fingerprint = image_fingerprint(args.image)
            check("image_is_regular", True, str(args.image))
            if args.sha256:
                check(
                    "image_sha256_matches",
                    fingerprint["sha256"] == args.sha256.lower(),
                    fingerprint["sha256"],
                )
            else:
                check("image_sha256_matches", False, "--sha256 was not supplied")
        except Exception as exc:
            check("image_is_regular", False, type(exc).__name__)
    else:
        check("image_is_regular", False, "--image was not supplied")

    ready = all(item["ok"] for item in checks)
    result = {
        "schema": "fnos-lab-preflight/v1",
        "ready_for_plan": ready,
        "board_id": args.board,
        "checks": checks,
        "lock": {
            "protocol": "fnos-board-lock/v1",
            "path": str(lock),
        },
        "access_paths": access_matrix(binding),
        "target": target,
        "hardware_actions": {
            "serial_opened": False,
            "power_api_called": False,
            "disk_written": False,
            "ssh_connected": False,
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
