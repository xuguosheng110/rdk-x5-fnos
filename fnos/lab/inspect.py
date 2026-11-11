"""Read-only host inventory.

``inspect`` distinguishes a *discovered* block device from an identity-verified
one, and never grants flashing eligibility.  Real mode only runs read-only
``diskutil`` queries and lists ``/dev/cu.*`` names; it does not open a serial
port, call a power API or connect to SSH.  Fixture mode does not touch host
hardware at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import glob
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
from typing import Any

from .errors import Refused
from .model import FIXTURE_SCHEMA
from .util import read_json, sha256_bytes

TOOL_NAMES = (
    "diskutil",
    "hdiutil",
    "system_profiler",
    "ioreg",
    "python3",
    "mijia-cp5",
    "lsof",
)

IDENTITY_VERIFIED = "verified"
IDENTITY_INSUFFICIENT = "insufficient_identity"
IDENTITY_INTERNAL = "rejected_internal"
IDENTITY_MOUNTED = "rejected_mounted"
IDENTITY_READ_ONLY = "rejected_read_only"
IDENTITY_NOT_WHOLE = "rejected_not_whole"
IDENTITY_UNKNOWN_LAYOUT = "unknown_layout"


@dataclass
class Inventory:
    mode: str
    simulated: bool
    tools: list[dict[str, Any]] = field(default_factory=list)
    serial_ports: list[dict[str, Any]] = field(default_factory=list)
    disks: list[dict[str, Any]] = field(default_factory=list)
    boards: dict[str, dict[str, Any]] = field(default_factory=dict)
    source: str | None = None
    source_sha256: str | None = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        verified = [d for d in self.disks if d["identity_status"] == IDENTITY_VERIFIED]
        return {
            "schema": "fnos-lab-inspect/v1",
            "mode": self.mode,
            "simulated": self.simulated,
            "source": self.source,
            "source_sha256": self.source_sha256,
            "tools": self.tools,
            "serial_ports": self.serial_ports,
            "disks": self.disks,
            "verified_identity": [d["device"] for d in verified],
            # This batch has no real backend, so the list is intentionally
            # empty even when an identity is fully verified.
            "verified_writable": [],
            "write_eligible": False,
            "hardware_actions": {
                "serial_opened": False,
                "power_api_called": False,
                "ssh_connected": False,
                "disk_written": False,
            },
            "policy": (
                "inspect is read-only; identity verification is not write "
                "authorization. plan re-checks one target and this batch has "
                "no real flashing backend."
            ),
            "boards": self.boards,
            "warnings": self.warnings,
        }


def _as_bool(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _clean_text(value: Any, *, limit: int = 256) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > limit:
        return None
    return value


def normalize_disk(raw: dict[str, Any]) -> dict[str, Any]:
    """Whitelist disk fields.  Unknown fixture keys are intentionally dropped."""
    if not isinstance(raw, dict):
        raise Refused("each disk entry must be a JSON object")
    device = _clean_text(raw.get("device"))
    if not device or not device.startswith("/dev/"):
        raise Refused("each disk must have an absolute /dev/... device path")
    size = raw.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise Refused(f"disk {device} has no positive integer size_bytes")
    mount_points = raw.get("mount_points") or []
    if not isinstance(mount_points, list) or not all(
        isinstance(item, str) for item in mount_points
    ):
        raise Refused(f"disk {device} mount_points must be a list of strings")
    disk = {
        "device": device,
        "diskN": Path(device).name,
        "size_bytes": size,
        "model": _clean_text(raw.get("model")),
        "serial": _clean_text(raw.get("serial")),
        "bus": _clean_text(raw.get("bus")) or "unknown",
        "topology": _clean_text(raw.get("topology")),
        "internal": _as_bool(raw.get("internal")),
        "removable": (
            raw.get("removable") if isinstance(raw.get("removable"), bool) else None
        ),
        "whole_disk": _as_bool(raw.get("whole_disk"), True),
        "read_only": _as_bool(raw.get("read_only")),
        "virtual": _as_bool(raw.get("virtual")),
        "system_disk": _as_bool(raw.get("system_disk")),
        "mount_points": list(mount_points),
        "provenance": "fixture",
    }
    return disk


def analyze_disk(disk: dict[str, Any]) -> dict[str, Any]:
    """Attach identity status and a *non-authorizing* eligibility flag.

    Unknown ``Internal``/``Whole`` values are never treated as a verified
    external whole disk.  Mount points are expected to include child
    partitions/containers, not only the whole-disk ``MountPoint``.
    """
    disk = dict(disk)
    reasons: list[str] = []
    mounts = disk.get("mount_points") or []
    whole = disk.get("whole_disk")
    internal = disk.get("internal")
    size = disk.get("size_bytes")

    if internal is True or disk.get("system_disk") is True or "/" in mounts:
        status = IDENTITY_INTERNAL
        reasons.append("internal/system disk is never a valid target")
    elif whole is False:
        status = IDENTITY_NOT_WHOLE
        reasons.append("not a whole physical disk")
    elif whole is not True:
        status = IDENTITY_UNKNOWN_LAYOUT
        reasons.append("whole-disk status is missing; refusing to assume a whole disk")
    elif internal is not False:
        status = IDENTITY_UNKNOWN_LAYOUT
        reasons.append("internal/external status is missing; refusing to assume external")
    elif disk.get("virtual") is True:
        status = IDENTITY_INSUFFICIENT
        reasons.append("virtual media has no stable physical identity")
    elif disk.get("read_only") is True:
        status = IDENTITY_READ_ONLY
        reasons.append("media is reported read-only")
    elif mounts:
        status = IDENTITY_MOUNTED
        reasons.append("disk or one of its partitions has mounted volumes")
    elif not isinstance(size, int) or size <= 0:
        status = IDENTITY_INSUFFICIENT
        reasons.append("disk size is missing or not positive")
    elif not disk["serial"]:
        status = IDENTITY_INSUFFICIENT
        reasons.append("serial number is missing; diskN alone is not identity")
    elif not disk["model"]:
        status = IDENTITY_INSUFFICIENT
        reasons.append("model/media name is missing")
    else:
        status = IDENTITY_VERIFIED
        reasons.append("whole external media with serial and model")

    disk["identity_status"] = status
    disk["identity_reasons"] = reasons
    # Never true in this batch, regardless of identity_status.
    disk["write_eligible"] = False
    return disk


def _normalize_board(board_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise Refused(f"board {board_id} must be a JSON object")
    power = raw.get("power") or {}
    serial = raw.get("serial") or {}
    if not isinstance(power, dict) or not isinstance(serial, dict):
        raise Refused(f"board {board_id} power/serial must be JSON objects")
    allowed = raw.get("allowed_target_serials") or []
    if not isinstance(allowed, list) or not all(isinstance(x, str) for x in allowed):
        raise Refused(f"board {board_id} allowed_target_serials must be strings")
    return {
        "board_id": board_id,
        "model": _clean_text(raw.get("model")) or "unknown",
        "power": {
            "backend": _clean_text(power.get("backend")) or "",
            "channel": _clean_text(power.get("channel")) or "",
            "mapping_verified": _as_bool(power.get("mapping_verified")),
            "wired": _as_bool(power.get("wired")),
        },
        "serial": {
            "port": _clean_text(serial.get("port")) or "",
            "baud": serial.get("baud") if isinstance(serial.get("baud"), int) else 115200,
        },
        "expected_kernel": _clean_text(raw.get("expected_kernel")),
        "allowed_target_serials": list(allowed),
    }


def load_fixture(path: Path) -> Inventory:
    path = Path(path)
    if path.is_symlink():
        raise Refused(f"fixture must not be a symlink: {path}")
    if not path.is_file():
        raise Refused(f"fixture is missing: {path}")
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise Refused(f"cannot read fixture: {exc.strerror}") from None
    if not raw_bytes:
        raise Refused(f"fixture is empty: {path}")
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refused(f"invalid fixture JSON: {type(exc).__name__}") from None
    if not isinstance(raw, dict):
        raise Refused("fixture root must be a JSON object")
    if raw.get("schema") != FIXTURE_SCHEMA:
        raise Refused(f"fixture schema must be {FIXTURE_SCHEMA}")
    if raw.get("simulated") is not True:
        raise Refused(
            "fixture must declare simulated=true; a fixture can never represent real write eligibility"
        )

    host = raw.get("host") or {}
    if not isinstance(host, dict):
        raise Refused("fixture host must be a JSON object")
    tools: list[dict[str, Any]] = []
    for item in host.get("tools") or []:
        if isinstance(item, str):
            tools.append({"name": item, "path": None, "fixture": True})
        elif isinstance(item, dict):
            name = _clean_text(item.get("name"))
            if name:
                tools.append(
                    {
                        "name": name,
                        "path": _clean_text(item.get("path")),
                        "fixture": True,
                    }
                )
    ports = []
    for item in host.get("serial_ports") or []:
        if isinstance(item, str) and item.startswith("/dev/"):
            ports.append({"path": item, "opened": False, "fixture": True})

    disks = [analyze_disk(normalize_disk(item)) for item in raw.get("disks") or []]
    boards_raw = raw.get("boards") or {}
    if not isinstance(boards_raw, dict):
        raise Refused("fixture boards must be a JSON object")
    boards = {bid: _normalize_board(bid, val) for bid, val in boards_raw.items()}
    return Inventory(
        mode="fixture",
        simulated=True,
        tools=tools,
        serial_ports=ports,
        disks=disks,
        boards=boards,
        source=str(path),
        source_sha256=sha256_bytes(raw_bytes),
        warnings=["fixture input: no host hardware was inspected"],
    )


def _run_readonly(argv: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        capture_output=True,
        check=False,
        timeout=timeout,
        text=False,
    )


def _diskutil_info(device: str) -> dict[str, Any]:
    try:
        result = _run_readonly(["diskutil", "info", "-plist", device])
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    try:
        value = plistlib.loads(result.stdout)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _collect_mount_points(value: Any) -> list[str]:
    """Collect mounts from a whole disk entry, including partitions/containers."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if key == "MountPoint":
                    if isinstance(child, str) and child:
                        found.add(child)
                elif key == "MountPoints":
                    if isinstance(child, list):
                        for item in child:
                            if isinstance(item, str) and item:
                                found.add(item)
                else:
                    walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return sorted(found)


def _real_disks(warnings: list[str]) -> list[dict[str, Any]]:
    if not shutil.which("diskutil"):
        warnings.append("diskutil is not available; no real disks were enumerated")
        return []
    try:
        result = _run_readonly(["diskutil", "list", "-plist"])
        listing = plistlib.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, plistlib.InvalidFileException):
        warnings.append("diskutil list failed; no real disks were enumerated")
        return []
    entries = listing.get("AllDisksAndPartitions") if isinstance(listing, dict) else None
    if not isinstance(entries, list):
        warnings.append("diskutil list did not return AllDisksAndPartitions")
        return []

    disks: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        device_id = entry.get("DeviceIdentifier")
        if not isinstance(device_id, str) or not device_id.startswith("disk"):
            continue
        info = _diskutil_info("/dev/" + device_id)
        mount_points = _collect_mount_points(entry)
        info_mount = info.get("MountPoint")
        if isinstance(info_mount, str) and info_mount:
            mount_points = sorted(set(mount_points) | {info_mount})
        internal = _optional_bool(info.get("Internal"))
        whole = _optional_bool(info.get("WholeDisk", info.get("Whole")))
        size = info.get("TotalSize") or entry.get("Size") or 0
        if not isinstance(size, int) or isinstance(size, bool):
            size = 0
        virtual_raw = info.get("VirtualOrPhysical")
        virtual = True if virtual_raw == "Virtual" else (
            False if virtual_raw == "Physical" else None
        )
        read_only = _optional_bool(
            info.get("ReadOnlyMedia") or info.get("MediaReadOnly")
        )
        writable = _optional_bool(info.get("Writable"))
        if read_only is None and writable is not None:
            read_only = not writable
        disks.append(
            analyze_disk(
                {
                    "device": "/dev/" + device_id,
                    "diskN": device_id,
                    "size_bytes": size,
                    "model": info.get("MediaName") or info.get("DeviceModel"),
                    "serial": info.get("SerialNumber")
                    or info.get("MediaSerialNumber"),
                    "bus": info.get("BusProtocol") or "unknown",
                    "topology": (
                        f"{info.get('BusProtocol') or 'unknown'}:"
                        f"{info.get('MediaName') or info.get('DeviceModel') or 'unknown'}:"
                        f"{device_id}"
                    ),
                    "internal": internal,
                    "removable": (
                        info.get("Removable")
                        if isinstance(info.get("Removable"), bool)
                        else None
                    ),
                    "whole_disk": whole,
                    "read_only": read_only,
                    "virtual": virtual,
                    "system_disk": internal is True or "/" in mount_points,
                    "mount_points": mount_points,
                    "provenance": "diskutil",
                }
            )
        )
    return disks


BINDINGS_SCHEMA = "fnos-board-bindings/v1"
DEFAULT_BINDINGS = (
    Path(__file__).resolve().parents[2]
    / "fnos"
    / "integration"
    / "hardware"
    / "board-bindings.json"
)


def load_bindings(path: Path) -> dict[str, dict[str, Any]]:
    """Load the reviewed, non-secret board registry.

    Bindings contain only board ids, allowlisted models, serial device names,
    the CP5 usb channel and lock/legacy policy.  Credentials never belong here.
    """
    path = Path(path).expanduser()
    if path.is_symlink():
        raise Refused(f"board bindings must not be a symlink: {path}")
    if not path.is_file():
        raise Refused(f"board bindings are missing: {path}")
    raw = read_json(path)
    if not isinstance(raw, dict) or raw.get("schema") != BINDINGS_SCHEMA:
        raise Refused(f"board bindings schema must be {BINDINGS_SCHEMA}")
    boards_raw = raw.get("boards")
    if not isinstance(boards_raw, dict):
        raise Refused("board bindings must contain a boards object")
    boards: dict[str, dict[str, Any]] = {}
    for board_id, value in boards_raw.items():
        board = _normalize_board(board_id, value)
        power = value.get("power") or {}
        board["cp5"] = dict(power.get("cp5") or {}) if isinstance(power.get("cp5"), dict) else {}
        board["legacy"] = (
            dict(value.get("legacy") or {}) if isinstance(value.get("legacy"), dict) else {}
        )
        board["access_paths"] = (
            list(value.get("access_paths") or [])
            if isinstance(value.get("access_paths"), list)
            else []
        )
        board["reviewed"] = value.get("reviewed") is True
        boards[board_id] = board
    return boards


def real_inventory(
    *, bindings: dict[str, dict[str, Any]] | None = None
) -> Inventory:
    warnings: list[str] = []
    tools = [
        {"name": name, "path": shutil.which(name), "fixture": False}
        for name in TOOL_NAMES
    ]
    ports = [
        {"path": path, "opened": False, "fixture": False}
        for path in sorted(set(glob.glob("/dev/cu.*")))
    ]
    disks = _real_disks(warnings)
    bound = bindings or {}
    if not bound:
        warnings.append(
            "real board/power bindings are not configured; plan will refuse"
        )
    return Inventory(
        mode="real-readonly",
        simulated=False,
        tools=tools,
        serial_ports=ports,
        disks=disks,
        boards=bound,
        warnings=warnings,
    )


def load_inventory(
    fixture: Path | None, *, bindings: Path | None = None
) -> Inventory:
    if fixture:
        return load_fixture(fixture)
    path = Path(bindings) if bindings else (DEFAULT_BINDINGS if DEFAULT_BINDINGS.is_file() else None)
    return real_inventory(bindings=load_bindings(path) if path else None)


def host_text_summary(inventory: Inventory) -> str:
    lines = [
        f"mode: {inventory.mode} (simulated={str(inventory.simulated).lower()})",
        f"write_eligible: false",
        "serial ports (not opened): "
        + (", ".join(p["path"] for p in inventory.serial_ports) or "none"),
    ]
    if not inventory.disks:
        lines.append("disks: none discovered")
    for disk in inventory.disks:
        lines.append(
            "disk {device} {size} bytes model={model!r} serial={serial!r} "
            "internal={internal} identity={identity}".format(
                device=disk["device"],
                size=disk["size_bytes"],
                model=disk["model"],
                serial=disk["serial"],
                internal=disk["internal"],
                identity=disk["identity_status"],
            )
        )
    lines.append("verified_writable: none (inspect never grants flashing eligibility)")
    return "\n".join(lines)
