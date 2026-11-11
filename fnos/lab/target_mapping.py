"""Unique board-media <-> USB UMS target mapping.

Historical ``/dev/diskN``/``mmc 1`` is not identity.  Board capacity is accepted
only when exact (sector count * block length or bytes); an approximate GiB
string keeps the target read-only.  The mapping is also cross-checked against
the approved plan and the flash backend's live identify at every target bound.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import re
from typing import Any, Iterable

from .errors import Refused
from .flash import TargetIdentity
from .util import read_json

SCHEMA = "fnos-lab-target-map/v1"
CID_RE = re.compile(r"^(?:CID:\s*)?([0-9a-fA-F]{32})$", re.MULTILINE)
SECTOR_RE = re.compile(r"Sector Count:\s*(\d+)", re.IGNORECASE)
BLOCK_RE = re.compile(r"Block Length:\s*(\d+)", re.IGNORECASE)
ERASE_RE = re.compile(r"Erase Block Size:\s*(\d+)", re.IGNORECASE)
BYTES_RE = re.compile(r"Capacity:\s*(\d+)\s*bytes", re.IGNORECASE)
CAPACITY_TEXT_RE = re.compile(r"Capacity:\s*([0-9.]+\s*[KMGT]iB)", re.IGNORECASE)


@dataclass(frozen=True)
class BoardMedia:
    mmc_index: int
    name: str | None
    size_bytes: int | None
    manufacturer: str | None = None
    oem: str | None = None
    cid: str | None = None
    raw_sha256: str | None = None
    capacity_text: str | None = None
    size_exact: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "mmc_index": self.mmc_index,
            "name": self.name,
            "size_bytes": self.size_bytes,
            "manufacturer": self.manufacturer,
            "oem": self.oem,
            "cid": self.cid,
            "raw_sha256": self.raw_sha256,
            "capacity_text": self.capacity_text,
            "size_exact": self.size_exact,
        }


@dataclass(frozen=True)
class MappingRecord:
    schema: str
    board_id: str
    board_media: BoardMedia
    host_device: str
    host_size_bytes: int
    host_model: str | None
    host_serial: str | None
    evidence: tuple[str, ...]
    reviewed_by: str
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "board_id": self.board_id,
            "board_media": self.board_media.as_dict(),
            "host_device": self.host_device,
            "host_size_bytes": self.host_size_bytes,
            "host_model": self.host_model,
            "host_serial": self.host_serial,
            "evidence": list(self.evidence),
            "reviewed_by": self.reviewed_by,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class TargetMap:
    board_id: str
    board_media: BoardMedia
    host_device: str
    host_serial: str | None
    size_bytes: int
    criteria: tuple[str, ...]
    reviewed_record: bool
    digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "board_id": self.board_id,
            "board_media": self.board_media.as_dict(),
            "host_device": self.host_device,
            "host_serial_present": self.host_serial is not None,
            "size_bytes": self.size_bytes,
            "criteria": list(self.criteria),
            "reviewed_record": self.reviewed_record,
            "digest": self.digest,
        }


def parse_mmc_list(text: str) -> dict[int, str]:
    found: dict[int, str] = {}
    for line in text.replace("\r", "\n").split("\n"):
        match = re.match(r"^\s*(\S+):\s*(\d+)\s*$", line)
        if match:
            found[int(match.group(2))] = match.group(1)
    return found


def parse_mmc_info(text: str, *, mmc_index: int) -> BoardMedia:
    clean = text.replace("\r", "\n")
    sectors = SECTOR_RE.search(clean)
    block = BLOCK_RE.search(clean) or ERASE_RE.search(clean)
    exact_bytes = None
    if sectors and block:
        exact_bytes = int(sectors.group(1)) * int(block.group(1))
    else:
        bytes_match = BYTES_RE.search(clean)
        if bytes_match:
            exact_bytes = int(bytes_match.group(1))
    capacity_text_match = CAPACITY_TEXT_RE.search(clean)
    capacity_text = capacity_text_match.group(1) if capacity_text_match else None
    if exact_bytes is not None and exact_bytes <= 0:
        exact_bytes = None
    name = manufacturer = oem = None
    for line in clean.split("\n"):
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip().lower(), value.strip()
        if key == "name":
            name = value
        elif key in ("manufacturer id", "manufacturer"):
            manufacturer = value
        elif key == "oem":
            oem = value
    cid_match = CID_RE.search(clean)
    cid = cid_match.group(1).lower() if cid_match else None
    return BoardMedia(
        mmc_index=mmc_index,
        name=name,
        size_bytes=exact_bytes,
        manufacturer=manufacturer,
        oem=oem,
        cid=cid,
        raw_sha256=hashlib.sha256(clean.encode("utf-8", "replace")).hexdigest(),
        capacity_text=capacity_text,
        size_exact=exact_bytes is not None,
    )


def load_mapping_record(path: Path) -> MappingRecord:
    path = Path(path).expanduser()
    if path.is_symlink():
        raise Refused("target mapping record must not be a symlink")
    raw = read_json(path)
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise Refused(f"target mapping schema must be {SCHEMA}")
    for field in ("board_id", "host_device", "reviewed_by", "created_at"):
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            raise Refused(f"target mapping field {field} is missing/empty")
    media_raw = raw.get("board_media")
    if not isinstance(media_raw, dict):
        raise Refused("target mapping board_media is missing")
    size = media_raw.get("size_bytes")
    media = BoardMedia(
        mmc_index=int(media_raw.get("mmc_index", -1)),
        name=media_raw.get("name") if isinstance(media_raw.get("name"), str) else None,
        size_bytes=size if isinstance(size, int) and not isinstance(size, bool) else None,
        manufacturer=media_raw.get("manufacturer") if isinstance(media_raw.get("manufacturer"), str) else None,
        oem=media_raw.get("oem") if isinstance(media_raw.get("oem"), str) else None,
        cid=media_raw.get("cid") if isinstance(media_raw.get("cid"), str) else None,
        raw_sha256=media_raw.get("raw_sha256") if isinstance(media_raw.get("raw_sha256"), str) else None,
        capacity_text=media_raw.get("capacity_text") if isinstance(media_raw.get("capacity_text"), str) else None,
        size_exact=media_raw.get("size_exact") is True,
    )
    evidence = raw.get("evidence")
    if not isinstance(evidence, list) or len(evidence) < 2 or not all(isinstance(x, str) and x for x in evidence):
        raise Refused("target mapping record needs at least two non-empty evidence items")
    return MappingRecord(
        schema=raw["schema"],
        board_id=raw["board_id"],
        board_media=media,
        host_device=raw["host_device"],
        host_size_bytes=int(raw.get("host_size_bytes", 0)),
        host_model=raw.get("host_model") if isinstance(raw.get("host_model"), str) else None,
        host_serial=raw.get("host_serial") if isinstance(raw.get("host_serial"), str) else None,
        evidence=tuple(evidence),
        reviewed_by=raw["reviewed_by"],
        created_at=raw["created_at"],
    )


def _candidate_disks(disks: Iterable[TargetIdentity], size_bytes: int) -> list[TargetIdentity]:
    return [
        disk
        for disk in disks
        if disk.size_bytes == size_bytes
        and disk.bus.lower() == "usb"
        and disk.whole_disk
        and not disk.internal
        and not disk.mounted
        and not disk.read_only
        and not disk.virtual
    ]


def map_unique(
    *,
    board_id: str,
    board_media: BoardMedia,
    disks: Iterable[TargetIdentity],
    reviewed_record: MappingRecord | None = None,
) -> TargetMap:
    if not board_media.size_exact or board_media.size_bytes is None:
        raise Refused(
            "board media capacity is approximate/unknown; exact sector/block or bytes "
            "evidence is required before mapping or writing"
        )
    candidates = _candidate_disks(disks, board_media.size_bytes)
    if len(candidates) != 1:
        raise Refused(
            f"target mapping is not unique: {len(candidates)} USB whole disks match "
            f"{board_media.size_bytes} bytes"
        )
    disk = candidates[0]
    criteria = [
        "exact_capacity_bytes",
        "whole_disk",
        "external",
        "usb_bus",
        "unmounted",
        "not_read_only",
        "not_virtual",
    ]
    reviewed = False
    if board_media.cid and disk.serial:
        if board_media.cid.lower() != disk.serial.lower():
            raise Refused("board CID and host disk serial do not match")
        criteria.append("cid_equals_host_serial")
    elif board_media.cid or disk.serial:
        if board_media.cid and not disk.serial:
            pass
        elif disk.serial and not board_media.cid:
            pass
        else:
            raise Refused("board CID/host serial evidence is inconsistent")
    if not (board_media.cid and disk.serial):
        if reviewed_record is None:
            raise Refused(
                "bridge did not expose a serial/CID; a reviewed multi-evidence "
                "mapping record is required (a boolean is not accepted)"
            )
        if reviewed_record.board_id != board_id:
            raise Refused("reviewed mapping record belongs to a different board")
        if reviewed_record.host_device != disk.device:
            raise Refused("reviewed mapping host device changed")
        if reviewed_record.host_size_bytes != disk.size_bytes:
            raise Refused("reviewed mapping host capacity changed")
        if reviewed_record.host_model != disk.model:
            raise Refused("reviewed mapping host model changed")
        if reviewed_record.host_serial != disk.serial:
            raise Refused("reviewed mapping host serial changed")
        if reviewed_record.board_media.size_bytes != board_media.size_bytes:
            raise Refused("reviewed mapping board capacity changed")
        if reviewed_record.board_media.name != board_media.name:
            raise Refused("reviewed mapping board media name changed")
        if reviewed_record.board_media.cid != board_media.cid:
            raise Refused("reviewed mapping board CID changed")
        if len(reviewed_record.evidence) < 2:
            raise Refused("reviewed mapping has insufficient evidence")
        criteria.extend(["reviewed_multi_evidence", f"reviewed_by:{reviewed_record.reviewed_by}"])
        reviewed = True
    body = {
        "board_id": board_id,
        "board_media": board_media.as_dict(),
        "host_device": disk.device,
        "host_serial": disk.serial,
        "size_bytes": disk.size_bytes,
        "criteria": criteria,
        "reviewed_record": reviewed,
    }
    digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return TargetMap(
        board_id=board_id,
        board_media=board_media,
        host_device=disk.device,
        host_serial=disk.serial,
        size_bytes=disk.size_bytes,
        criteria=tuple(criteria),
        reviewed_record=reviewed,
        digest=digest,
    )


def verify_mapping_against_plan(
    mapping: TargetMap,
    *,
    plan_target: dict[str, Any],
    actual: TargetIdentity,
) -> None:
    """Cross-check the board mapping, approved plan target and live flash."""
    problems: list[str] = []
    expected_device = str(plan_target.get("device") or "")
    expected_size = int(plan_target.get("size_bytes") or 0)
    if mapping.host_device != expected_device:
        problems.append(f"mapping device {mapping.host_device} != plan {expected_device}")
    if mapping.size_bytes != expected_size:
        problems.append(f"mapping size {mapping.size_bytes} != plan {expected_size}")
    if actual.device != expected_device:
        problems.append(f"live flash device {actual.device} != plan {expected_device}")
    if actual.size_bytes != expected_size:
        problems.append(f"live flash size {actual.size_bytes} != plan {expected_size}")
    if actual.device != mapping.host_device:
        problems.append("live flash device changed from the mapped target")
    if actual.size_bytes != mapping.size_bytes:
        problems.append("live flash capacity changed from the mapped target")
    if mapping.host_serial and actual.serial and mapping.host_serial != actual.serial:
        problems.append("mapped serial differs from live flash serial")
    if not actual.serial:
        problems.append("live flash serial is missing")
    if problems:
        raise Refused("target mapping/plan/flash mismatch: " + "; ".join(problems))


def reidentify(
    target_map: TargetMap,
    *,
    board_media: BoardMedia,
    disks: Iterable[TargetIdentity],
    reviewed_record: MappingRecord | None = None,
) -> TargetMap:
    return map_unique(
        board_id=target_map.board_id,
        board_media=board_media,
        disks=disks,
        reviewed_record=reviewed_record,
    )
