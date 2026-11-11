"""Build and verify a versioned, atomic flashing plan.

A plan is a *precondition record*, not an authorization to write.  Every plan
produced in this batch has ``execution.write_eligible: false``.  Fixture plans
are marked ``simulated: true`` and can only be used by the simulator.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .errors import Refused
from .inspect import Inventory, IDENTITY_VERIFIED
from .locks import board_lock_path, lock_root
from .model import (
    LOCK_PROTOCOL,
    PLAN_SCHEMA,
    POWER_BACKENDS,
    POWER_CHANNELS,
)
from .util import (
    atomic_write_json,
    canonical_json_bytes,
    image_fingerprint,
    read_json,
    sha256_bytes,
    utc_now,
)

DEFAULT_RESERVE = 64 * 1024 * 1024


def _payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if key != "integrity"}


def sign_plan(plan: dict[str, Any]) -> dict[str, Any]:
    body = _payload(plan)
    plan["integrity"] = {
        "algorithm": "sha256",
        "payload_sha256": sha256_bytes(canonical_json_bytes(body)),
    }
    return plan


def verify_plan_integrity(plan: dict[str, Any]) -> None:
    if not isinstance(plan, dict):
        raise Refused("plan root must be a JSON object")
    if plan.get("schema") != PLAN_SCHEMA:
        raise Refused(f"plan schema must be {PLAN_SCHEMA}")
    integrity = plan.get("integrity")
    if not isinstance(integrity, dict) or integrity.get("algorithm") != "sha256":
        raise Refused("plan has no supported integrity block")
    body = _payload(plan)
    expected = sha256_bytes(canonical_json_bytes(body))
    if integrity.get("payload_sha256") != expected:
        raise Refused("plan integrity mismatch: the plan was modified after generation")


def load_plan(path: Path) -> dict[str, Any]:
    plan = read_json(Path(path))
    verify_plan_integrity(plan)
    if not isinstance(plan.get("simulated"), bool):
        raise Refused("plan is missing the simulated flag")
    return plan


def _find_target(inventory: Inventory, selector: str) -> dict[str, Any]:
    selector = selector.strip()
    if not selector:
        raise Refused("target selector is empty")
    if selector.startswith("/dev/"):
        matches = [d for d in inventory.disks if d["device"] == selector]
    elif selector.startswith("disk"):
        matches = [d for d in inventory.disks if d["diskN"] == selector]
    else:
        matches = [
            d
            for d in inventory.disks
            if d["device"] == selector or d["diskN"] == selector
        ]
    if not matches:
        raise Refused(f"target is not present in the current inventory: {selector}")
    if len(matches) > 1:
        raise Refused(
            f"target selector {selector} is ambiguous ({len(matches)} entries)"
        )
    target = matches[0]
    if target["identity_status"] != IDENTITY_VERIFIED:
        reasons = "; ".join(target.get("identity_reasons") or ["identity not verified"])
        raise Refused(f"target {target['device']} is not eligible: {reasons}")
    return target


def _check_unique_serial(inventory: Inventory, target: dict[str, Any]) -> None:
    serial = target["serial"]
    others = [
        d
        for d in inventory.disks
        if d["device"] != target["device"] and d.get("serial") == serial
    ]
    if others:
        raise Refused(
            f"target identity is ambiguous: serial {serial} also appears on "
            + ", ".join(d["device"] for d in others)
        )


def _resolved(path: Path) -> Path:
    try:
        return Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise Refused(f"cannot resolve path {path}: {exc.strerror}") from None


def _check_output_path(
    out_path: Path, *, image_path: Path, fixture_path: Path | None
) -> Path:
    """Validate the destination before reading/writing anything.

    The plan output must be a new path in an existing directory.  It may not
    alias the input image or fixture through ``..``, a symlinked parent or the
    same file.  Existing files are never overwritten (plan files are immutable
    evidence; re-planning needs a new path).
    """
    out_path = Path(out_path).expanduser()
    if not out_path.is_absolute():
        raise Refused("plan output path must be absolute")
    if out_path.is_symlink():
        raise Refused("plan output path must not be a symlink")
    parent = out_path.parent
    if not parent.is_dir():
        raise Refused(f"plan output directory does not exist: {parent}")

    resolved_parent = _resolved(parent)
    resolved_out = resolved_parent / out_path.name
    protected = [("image", image_path)]
    if fixture_path:
        protected.append(("fixture", fixture_path))
    for label, candidate in protected:
        resolved_candidate = _resolved(Path(candidate))
        if resolved_out == resolved_candidate:
            raise Refused(
                f"plan output path conflicts with the input {label}: {out_path}"
            )
    if out_path.exists():
        for label, candidate in protected:
            try:
                if os.path.samefile(out_path, Path(candidate)):
                    raise Refused(
                        f"plan output path conflicts with the input {label}: {out_path}"
                    )
            except OSError:
                pass
        raise Refused(f"refusing to overwrite an existing output path: {out_path}")
    return out_path


def build_plan(
    inventory: Inventory,
    *,
    board_id: str,
    image_path: Path,
    target_selector: str,
    expect_serial: str,
    out_path: Path,
    reserve_bytes: int = DEFAULT_RESERVE,
) -> dict[str, Any]:
    if not isinstance(board_id, str) or not board_id.strip():
        raise Refused("board_id is required")
    if reserve_bytes < 0:
        raise Refused("capacity reserve must not be negative")
    if not isinstance(expect_serial, str) or not expect_serial.strip():
        raise Refused(
            "an expected serial number is required; diskN names alone are not identity"
        )
    expect_serial = expect_serial.strip()

    out_path = _check_output_path(
        Path(out_path),
        image_path=Path(image_path),
        fixture_path=Path(inventory.source) if inventory.source else None,
    )

    board = inventory.boards.get(board_id)
    if not isinstance(board, dict):
        raise Refused(f"board {board_id} is not bound in the reviewed inventory")
    if not inventory.simulated and board.get("reviewed") is not True:
        raise Refused(
            f"board {board_id} binding is not reviewed=true; complete the read-only preflight"
        )

    power = board.get("power") or {}
    if power.get("backend") not in POWER_BACKENDS:
        raise Refused(f"board {board_id} has no reviewed power backend")
    if power.get("channel") not in POWER_CHANNELS:
        raise Refused(f"board {board_id} has no bound power channel")
    if power.get("mapping_verified") is not True:
        raise Refused(f"board {board_id} power channel mapping is not verified")
    if power.get("wired") is not True:
        raise Refused(f"board {board_id} power channel is not marked as wired")
    if not inventory.simulated:
        if power.get("backend") != "cp5-usb":
            raise Refused(
                f"board {board_id} real operations require the reviewed cp5-usb backend"
            )
        if power.get("channel") != "usb":
            raise Refused(
                f"board {board_id} real operations require the confirmed CP5 usb group"
            )

    serial = board.get("serial") or {}
    serial_port = serial.get("port") or ""
    if not isinstance(serial_port, str) or not serial_port.startswith("/dev/"):
        raise Refused(f"board {board_id} has no bound serial port")

    image = image_fingerprint(Path(image_path))
    if image["size_bytes"] <= 0:
        raise Refused("image is empty")

    target = _find_target(inventory, target_selector)
    if target["serial"] != expect_serial:
        raise Refused(
            f"target serial {target['serial']} does not match expected serial {expect_serial}"
        )
    _check_unique_serial(inventory, target)

    allowed = board.get("allowed_target_serials") or []
    if not inventory.simulated and not allowed:
        raise Refused(
            f"board {board_id} has no allowed_target_serials; real plans require an allowlist"
        )
    if allowed and target["serial"] not in allowed:
        raise Refused(
            f"target serial {target['serial']} is not in board {board_id}'s allowed target list"
        )

    required = image["size_bytes"] + reserve_bytes
    if target["size_bytes"] < required:
        raise Refused(
            "target capacity is insufficient: "
            f"{target['size_bytes']} bytes available, {required} bytes required "
            f"(image {image['size_bytes']} + reserve {reserve_bytes})"
        )

    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "created_at": utc_now(),
        "simulated": inventory.simulated,
        "execution": {
            "mode": "simulation-only" if inventory.simulated else "hardware-required",
            "real_backend_wired": not inventory.simulated,
            "write_eligible": False,
            "authorization": "none" if inventory.simulated else "required",
            "note": (
                "Plan validation does not authorize a disk write. Real execution "
                "additionally needs FNOS_LAB_ENABLE_REAL_BACKENDS=1, --backends real, "
                "--authorize-write, --confirm-board and a plan-bound authorization token."
            ),
        },
        "lock": {
            "protocol": LOCK_PROTOCOL,
            "board_id": board_id,
            "name": board_lock_path(None, board_id).name,
            "root": str(lock_root()),
            "path": str(board_lock_path(None, board_id)),
        },
        "board": {
            "board_id": board_id,
            "model": board.get("model") or "unknown",
        },
        "power": {
            "backend": power["backend"],
            "channel": power["channel"],
            "mapping_verified": True,
            "wired": True,
            "simulated": inventory.simulated,
        },
        "serial": {
            "port": serial_port,
            "baud": serial.get("baud") or 115200,
        },
        "validation": {
            "expected_kernel": board.get("expected_kernel") or "",
        },
        "image": image,
        "target": {
            "device": target["device"],
            "diskN": target["diskN"],
            "serial": target["serial"],
            "model": target["model"],
            "size_bytes": target["size_bytes"],
            "bus": target["bus"],
            "topology": target.get("topology"),
            "identity_status": target["identity_status"],
            "provenance": target.get("provenance"),
        },
        "preconditions": {
            "whole_disk": True,
            "internal_or_system_disk": False,
            "mounted": False,
            "read_only": False,
            "serial_verified": True,
            "identity_unique": True,
            "capacity_reserve_bytes": reserve_bytes,
            "required_bytes": required,
        },
        "fixture": (
            {
                "path": inventory.source,
                "sha256": inventory.source_sha256,
            }
            if inventory.source
            else None
        ),
    }
    sign_plan(plan)
    atomic_write_json(out_path, plan, mode=0o600, overwrite=False)
    return plan


def verify_image_against_plan(plan: dict[str, Any]) -> dict[str, Any]:
    expected = plan.get("image") or {}
    path = expected.get("path")
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise Refused("plan image path is missing or not absolute")
    actual = image_fingerprint(Path(path))
    if actual["sha256"] != expected.get("sha256"):
        raise Refused("image SHA-256 changed after the plan was created")
    if actual["size_bytes"] != expected.get("size_bytes"):
        raise Refused("image size changed after the plan was created")
    return actual


def plan_summary(plan: dict[str, Any], path: Path) -> dict[str, Any]:
    return {
        "plan": str(path),
        "schema": plan.get("schema"),
        "simulated": plan.get("simulated"),
        "write_eligible": bool(plan.get("execution", {}).get("write_eligible")),
        "board_id": plan.get("board", {}).get("board_id"),
        "power_channel": plan.get("power", {}).get("channel"),
        "image": plan.get("image"),
        "target": plan.get("target"),
        "integrity": plan.get("integrity"),
    }
