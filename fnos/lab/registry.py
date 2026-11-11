"""Which programmatic paths can touch a board, and which ones are disabled.

A lock is meaningful only for paths that actually use it.  This module makes
the inventory explicit and testable.  The lab executor, the CP5 USB entry and
the legacy flash adapter use the same ``board_id``-derived lock.  Direct legacy
flash/HA entries are marked disabled for the board; manual phone/unplug paths
are marked outside the file lock instead of being silently claimed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import Refused

UNIFIED = "fnos-board-lock/v1"

# id, kind, unified?, description
ACCESS_PATHS: tuple[dict[str, Any], ...] = (
    {
        "id": "fnos-lab-run",
        "kind": "executor",
        "unified_lock": True,
        "description": "plan/run/status/cancel executor (power+serial+flash+boot)",
    },
    {
        "id": "mijia-cp5-usb",
        "kind": "power",
        "unified_lock": True,
        "description": "CP5 usb-on/usb-off/usb-status with --board (SIID 7/PIID 1)",
    },
    {
        "id": "legacy-flash-adapter",
        "kind": "flash",
        "unified_lock": True,
        "description": "reviewed adapter passes the same absolute lock to flash_macos.py",
    },
    {
        "id": "legacy-flash-direct",
        "kind": "flash",
        "unified_lock": False,
        "description": "direct rdk-x5-usb-flash invocation with its own RDK_OPERATION_LOCK",
    },
    {
        "id": "legacy-flash-command",
        "kind": "flash",
        "unified_lock": False,
        "description": "repository scripts/flash-rdk-x5-sd.command direct dd path",
    },
    {
        "id": "ha-power",
        "kind": "power",
        "unified_lock": False,
        "description": "legacy Home Assistant power entity",
    },
    {
        "id": "raw-device-write",
        "kind": "flash",
        "unified_lock": False,
        "description": "dd/other raw writer that does not open the board lock",
    },
    {
        "id": "mijia-app",
        "kind": "manual",
        "unified_lock": False,
        "description": "phone app/manual cloud operation; cannot be blocked by a file lock",
    },
    {
        "id": "manual-unplug",
        "kind": "manual",
        "unified_lock": False,
        "description": "physical cable/USB removal; cannot be blocked by a file lock",
    },
)

UNIFIED_IDS = frozenset(p["id"] for p in ACCESS_PATHS if p["unified_lock"])
LEGACY_IDS = frozenset(
    {
        "legacy-flash-direct",
        "legacy-flash-command",
        "ha-power",
        "raw-device-write",
        "mijia-app",
        "manual-unplug",
    }
)


def _legacy_policy(binding: dict[str, Any], path_id: str) -> str:
    legacy = binding.get("legacy") or {}
    defaults = {
        "legacy-flash-direct": "not_integrated",
        "legacy-flash-command": "blocked_by_guard",
        "ha-power": "not_integrated",
        "raw-device-write": "outside_file_lock",
        "mijia-app": "outside_file_lock",
        "manual-unplug": "outside_file_lock",
    }
    key = {
        "legacy-flash-direct": "direct_flash",
        "legacy-flash-command": "direct_command",
        "ha-power": "ha_power",
        "raw-device-write": "raw_device",
        "mijia-app": "mijia_app",
        "manual-unplug": "manual_unplug",
    }.get(path_id, path_id.replace("-", "_"))
    value = legacy.get(key)
    if isinstance(value, str) and value:
        return value
    return defaults[path_id]


def access_matrix(binding: dict[str, Any] | None) -> list[dict[str, Any]]:
    board_id = (binding or {}).get("board_id")
    rows: list[dict[str, Any]] = []
    for entry in ACCESS_PATHS:
        row = dict(entry)
        if entry["unified_lock"]:
            row["status"] = "enforced_unified"
            row["enforced"] = True
            row["lock"] = UNIFIED
            row["board_id"] = board_id
        else:
            row["status"] = _legacy_policy(binding or {}, entry["id"])
            # A repository-local direct dd command is actually blocked by its
            # own guard.  External global tools cannot be blocked by this
            # workspace and are marked not_integrated, not "disabled".
            row["enforced"] = row["status"] == "blocked_by_guard"
            row["lock"] = None
        rows.append(row)
    return rows


def require_path_enabled(binding: dict[str, Any], path_id: str) -> None:
    """Refuse a legacy path that is not explicitly enabled for this board."""
    for row in access_matrix(binding):
        if row["id"] != path_id:
            continue
        if row["unified_lock"]:
            return
        if row["status"] in (
            "disabled_for_board",
            "disabled",
            "not_integrated",
            "blocked_by_guard",
        ):
            raise Refused(
                f"access path {path_id} is not an integrated board path "
                f"({row['status']}); use the unified fnos-lab executor, the CP5 "
                "usb-* entry or the legacy flash adapter"
            )
        if row["status"] == "outside_file_lock":
            raise Refused(
                f"access path {path_id} is outside the board file lock and is not a "
                "reviewed automatic entry"
            )
        return
    raise Refused(f"unknown access path: {path_id}")


def binding_path(root: Path | None = None) -> Path:
    base = Path(root) if root else Path(__file__).resolve().parents[2] / "fnos"
    return base / "integration" / "hardware" / "board-bindings.json"


def load_binding(
    board_id: str, *, path: Path | None = None
) -> dict[str, Any]:
    from .inspect import load_bindings

    boards = load_bindings(path or binding_path())
    binding = boards.get(board_id)
    if not isinstance(binding, dict):
        raise Refused(f"board {board_id} is not in the reviewed board bindings")
    return binding
