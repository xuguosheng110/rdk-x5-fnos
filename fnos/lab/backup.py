"""Non-destructive backup planning for the current test medium.

This module only plans; it never opens the source device, writes the
destination or starts a stream.  A plan requires a uniquely identified
external whole-disk test medium, a new destination path and enough free space.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Any

from .errors import Refused
from .flash import TargetIdentity
from .util import atomic_write_json, utc_now

SCHEMA = "fnos-lab-backup-plan/v1"


@dataclass(frozen=True)
class BackupPlan:
    schema: str
    board_id: str
    source_device: str
    source_size_bytes: int
    source_serial: str | None
    source_model: str | None
    destination_dir: str
    destination_path: str
    expected_bytes: int
    method: str
    write_executed: bool
    status: str
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "board_id": self.board_id,
            "source_device": self.source_device,
            "source_size_bytes": self.source_size_bytes,
            "source_serial_present": self.source_serial is not None,
            "source_model": self.source_model,
            "destination_dir": self.destination_dir,
            "destination_path": self.destination_path,
            "expected_bytes": self.expected_bytes,
            "method": self.method,
            "write_executed": self.write_executed,
            "status": self.status,
            "created_at": self.created_at,
        }


def build_backup_plan(
    *,
    board_id: str,
    source: TargetIdentity,
    destination_dir: Path,
    destination_name: str,
    expect_serial: str,
) -> BackupPlan:
    if not isinstance(board_id, str) or not board_id:
        raise Refused("backup plan requires board_id")
    if not source.whole_disk or source.internal:
        raise Refused("backup source must be an external whole disk")
    if source.size_bytes <= 0:
        raise Refused("backup source has no positive capacity")
    if not source.serial or source.serial != expect_serial:
        raise Refused("backup source serial is missing or does not match the expected test disk")
    if source.mounted or source.mount_points:
        raise Refused("backup source or a child volume is mounted; unmount first")
    if source.read_only:
        raise Refused("backup source is read-only")
    if source.virtual:
        raise Refused("backup source is virtual, not the physical test medium")
    if not destination_name or "/" in destination_name or destination_name in (".", ".."):
        raise Refused("destination_name must be a plain filename")
    dest_dir = Path(destination_dir).expanduser()
    if not dest_dir.is_absolute() or not dest_dir.is_dir() or dest_dir.is_symlink():
        raise Refused("backup destination must be an existing absolute non-symlink directory")
    dest = dest_dir / destination_name
    if dest.exists() or dest.is_symlink():
        raise Refused("backup destination already exists; never overwrite a recovery copy")
    free = shutil.disk_usage(dest_dir).free
    reserve = 256 * 1024 * 1024
    if free < source.size_bytes + reserve:
        raise Refused(
            f"backup destination has {free} bytes free; need {source.size_bytes + reserve}"
        )
    plan = BackupPlan(
        schema=SCHEMA,
        board_id=board_id,
        source_device=source.device,
        source_size_bytes=source.size_bytes,
        source_serial=source.serial,
        source_model=source.model,
        destination_dir=str(dest_dir),
        destination_path=str(dest),
        expected_bytes=source.size_bytes,
        method="stream-whole-disk-to-file+sha256+size-check (not executed)",
        write_executed=False,
        status="planned-not-executed",
        created_at=utc_now(),
    )
    return plan


def write_backup_plan(plan: BackupPlan, path: Path) -> None:
    path = Path(path).expanduser()
    atomic_write_json(path, plan.as_dict(), mode=0o600, overwrite=False)


def load_backup_plan(path: Path) -> dict[str, Any]:
    from .util import read_json

    path = Path(path).expanduser()
    if path.is_symlink():
        raise Refused("backup plan must not be a symlink")
    value = read_json(path)
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise Refused(f"backup plan schema must be {SCHEMA}")
    if value.get("write_executed") is not False:
        raise Refused("backup plan is not a non-executed plan")
    return value
