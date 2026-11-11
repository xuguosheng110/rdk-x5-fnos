"""Production live-provider wiring for the one owned serial session.

This module is deliberately small: it is the same orchestration used by the
real CLI and by the offline end-to-end tests (which inject a fake transport,
fake disks and fake flash).  It does not itself open a device or power port.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Callable, Iterable

from .errors import HardwareFailure, Refused
from .flash import TargetIdentity
from .runtime_identity import RuntimeIdentity, collect_runtime_identity
from .safe_cut import CutEvidence, CutEvidenceGate
from .target_mapping import (
    BoardMedia,
    MappingRecord,
    TargetMap,
    map_unique,
    parse_mmc_info,
    parse_mmc_list,
)
from .uart_session import SerialSession, STATE_UBOOT, STATE_UBOOT_UMS


@dataclass
class LiveProviderContext:
    plan: dict[str, Any]
    session: SerialSession | None = None
    disk_provider: Callable[[], Iterable[TargetIdentity]] | None = None
    reviewed_record: MappingRecord | None = None
    authorize_ums: bool = False
    mmc_index: int | None = None
    previous_boot_id: str | None = None
    _gate: CutEvidenceGate | None = field(default=None, init=False)
    _last_identity: RuntimeIdentity | None = field(default=None, init=False)
    _board_media: BoardMedia | None = field(default=None, init=False)

    def bind_session(self, session: SerialSession) -> None:
        self.session = session
        self._gate = CutEvidenceGate(board_id=self.plan["board"]["board_id"], session=session)

    def _require(self) -> tuple[SerialSession, CutEvidenceGate]:
        if self.session is None or self._gate is None:
            raise Refused("live serial session is not bound")
        return self.session, self._gate

    def cut_provider(self, origin: str) -> CutEvidence:
        session, gate = self._require()
        return gate.uboot_idle()

    def cut_validator(self, evidence: CutEvidence) -> None:
        _, gate = self._require()
        gate.assert_valid(evidence)

    def runtime_identity(self) -> RuntimeIdentity:
        session, _ = self._require()
        identity = collect_runtime_identity(session)
        if self.previous_boot_id and identity.boot_id == self.previous_boot_id:
            raise HardwareFailure(
                "stale_boot_identity",
                "runtime identity still reports the previous boot_id; refusing cached identity",
            )
        self.previous_boot_id = identity.boot_id
        self._last_identity = identity
        return identity

    def read_board_media(self) -> BoardMedia:
        session, _ = self._require()
        listing = session.uboot_readonly("mmc list").output
        devices = parse_mmc_list(listing)
        explicit = self.mmc_index if self.mmc_index is not None else (self.plan.get("target") or {}).get("mmc_index")
        if explicit is not None:
            index = int(explicit)
        elif len(devices) == 1:
            index = next(iter(devices))
        else:
            raise Refused(
                "board has multiple mmc devices and plan has no exact mmc_index; "
                "refusing fuzzy target selection"
            )
        media = parse_mmc_info(session.uboot_readonly("mmc info").output, mmc_index=index)
        if not media.size_exact:
            raise Refused(
                "board mmc info has only approximate capacity; exact sector/block or "
                "bytes evidence is required before write"
            )
        if media.cid is None:
            try:
                cid = session.uboot_readonly("mmc cid").output
            except Exception:
                cid = ""
            import re
            match = re.search(r"\b([0-9a-fA-F]{32})\b", cid)
            if match:
                media = BoardMedia(
                    mmc_index=media.mmc_index,
                    name=media.name,
                    size_bytes=media.size_bytes,
                    manufacturer=media.manufacturer,
                    oem=media.oem,
                    cid=match.group(1).lower(),
                    raw_sha256=media.raw_sha256,
                    capacity_text=media.capacity_text,
                    size_exact=media.size_exact,
                )
        return media

    def target_map(self) -> TargetMap:
        if self.disk_provider is None:
            raise Refused("live target mapping requires a disk inventory provider")
        media = self._board_media or self.read_board_media()
        mapping = map_unique(
            board_id=self.plan["board"]["board_id"],
            board_media=media,
            disks=list(self.disk_provider()),
            reviewed_record=self.reviewed_record,
        )
        target = self.plan.get("target") or {}
        if mapping.host_device != target.get("device"):
            raise Refused("board mapping does not match the approved plan device")
        if mapping.size_bytes != int(target.get("size_bytes") or 0):
            raise Refused("board mapping does not match the approved plan capacity")
        return mapping

    def normal_shutdown(self) -> CutEvidence:
        session, gate = self._require()
        identity = self.runtime_identity()
        result = session.linux_normal_shutdown()
        evidence = gate.poweroff_complete(result)
        self.previous_boot_id = identity.boot_id
        return evidence

    def prepare_cold_boot(self) -> None:
        session, _ = self._require()
        if not self.authorize_ums:
            raise Refused("cold-boot preparation requires an authorized write run")
        if session.state == STATE_UBOOT_UMS:
            session.uboot_stop_ums()
        if session._countdown_mono is not None:
            session.interrupt_autoboot()
        if session.state != STATE_UBOOT:
            raise Refused(f"cannot prepare cold boot from serial state {session.state}")

    def prepare_ums(self) -> None:
        session, _ = self._require()
        if not self.authorize_ums:
            raise Refused("UMS start requires an explicitly authorized write run")
        if session.state != STATE_UBOOT or session._countdown_mono is not None:
            deadline = session.clock() + 20.0
            while session.clock() < deadline:
                session.refresh(0.2)
                if session._countdown_mono is not None or session.state == STATE_UBOOT:
                    break
            if session._countdown_mono is not None:
                session.interrupt_autoboot()
        if session.state != STATE_UBOOT:
            raise Refused(f"cannot start UMS from serial state {session.state}")
        media = self.read_board_media()
        self._board_media = media
        target = self.plan.get("target") or {}
        explicit = target.get("mmc_index")
        index = int(explicit) if explicit is not None else media.mmc_index
        if self.disk_provider is None:
            raise Refused("UMS start requires a host disk inventory provider")
        before = {d.device for d in self.disk_provider()}
        session.uboot_ums(index)
        # UMS enumeration is asynchronous.  Require a newly appeared whole
        # disk and a unique mapping; a pre-existing disk is not evidence.
        deadline = session.clock() + 20.0
        last_error: Exception | None = None
        while session.clock() < deadline:
            current = list(self.disk_provider())
            new = [d for d in current if d.device not in before]
            if new:
                try:
                    self.target_map()
                    return
                except Exception as exc:
                    last_error = exc
            time.sleep(0.2)
        raise Refused(
            f"UMS produced no new uniquely mappable host disk: {last_error}"
        )
