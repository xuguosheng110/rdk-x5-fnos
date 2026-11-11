"""Live safe-cut evidence bound to an opaque serial receipt and terminal epoch.

A cloud OFF report, a user boolean, an old transcript, or a constructible
``CommandResult`` is not evidence.  Evidence can only reference a receipt that
the *current* ``SerialSession`` registered for a command it actually issued.
The gate re-queries that registry and current terminal epoch immediately before
the power cut.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import time
from typing import Any, Callable

from .errors import HardwareFailure, Refused
from .uart_session import (
    CommandResult,
    SerialSession,
    STATE_UBOOT,
    STATE_LINUX_BOOT,
    STATE_UNKNOWN,
)

CUT_UBOOT_IDLE = "u-boot-idle"
CUT_UMS_STOPPED = "ums-storage-stopped"
CUT_LINUX_POWEROFF = "linux-poweroff-complete"
CUT_STATES = frozenset({CUT_UBOOT_IDLE, CUT_UMS_STOPPED, CUT_LINUX_POWEROFF})
POWEROFF_RE = re.compile(
    r"(?i)(power down|reboot: power down|systemd-shutdown.*powering off|"
    r"system is powering down)"
)
EXPECTED_RECEIPT_STATE = {
    CUT_UBOOT_IDLE: STATE_UBOOT,
    CUT_UMS_STOPPED: STATE_UBOOT,
    CUT_LINUX_POWEROFF: STATE_LINUX_BOOT,
}


@dataclass(frozen=True)
class CutEvidence:
    board_id: str
    session_id: str
    state: str
    observed_at: float
    expires_at: float
    nonce: str
    command: str
    output_sha256: str
    receipt_id: str
    terminal_epoch: int
    observed_mono: float
    expires_mono: float
    source: str = "serial-session"

    def as_dict(self) -> dict[str, Any]:
        return {
            "board_id": self.board_id,
            "session_id": self.session_id,
            "state": self.state,
            "observed_at": self.observed_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
            "command": self.command,
            "output_sha256": self.output_sha256,
            "receipt_id": self.receipt_id,
            "terminal_epoch": self.terminal_epoch,
            "source": self.source,
        }

    def validate_static(
        self,
        *,
        board_id: str,
        session_id: str,
        now_mono: float,
        max_age: float | None = None,
    ) -> tuple[bool, str]:
        if self.board_id != board_id:
            return False, "safe-cut evidence belongs to a different board"
        if self.session_id != session_id:
            return False, "safe-cut evidence belongs to a different serial session"
        if self.state not in CUT_STATES:
            return False, f"unsupported safe-cut state: {self.state}"
        if now_mono >= self.expires_mono:
            return False, "safe-cut evidence has expired"
        if now_mono + 1.0 < self.observed_mono:
            return False, "safe-cut evidence timestamp is in the future"
        if max_age is not None and now_mono - self.observed_mono > max_age:
            return False, "safe-cut evidence is older than the allowed age"
        if not self.nonce or len(self.nonce) < 16:
            return False, "safe-cut evidence has no fresh nonce"
        if not self.output_sha256 or len(self.output_sha256) != 64:
            return False, "safe-cut evidence has no command-output digest"
        if not self.receipt_id:
            return False, "safe-cut evidence has no session receipt"
        if self.terminal_epoch < 1:
            return False, "safe-cut evidence has no terminal epoch"
        return True, "ok"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


class CutEvidenceGate:
    """Produces and revalidates evidence from one live ``SerialSession``."""

    def __init__(
        self,
        *,
        board_id: str,
        session: SerialSession,
        ttl_seconds: float = 10.0,
        now: Callable[[], float] = time.time,
    ):
        if ttl_seconds <= 0 or ttl_seconds > 60:
            raise Refused("safe-cut evidence TTL must be 1..60 seconds")
        self.board_id = board_id
        self.session = session
        self.ttl_seconds = ttl_seconds
        self.wall_now = now

    def _make(self, result: CommandResult, state: str, *, source: str) -> CutEvidence:
        receipt = self.session.receipt(result.receipt_id)
        mono = self.session.clock()
        wall = self.wall_now()
        return CutEvidence(
            board_id=self.board_id,
            session_id=self.session.session_id,
            state=state,
            observed_at=wall,
            expires_at=wall + self.ttl_seconds,
            nonce=receipt.nonce,
            command=receipt.command,
            output_sha256=_sha(receipt.output),
            receipt_id=receipt.receipt_id,
            terminal_epoch=receipt.terminal_epoch,
            observed_mono=mono,
            expires_mono=mono + self.ttl_seconds,
            source=source,
        )

    def uboot_idle(self) -> CutEvidence:
        self.session.refresh(0.2)
        if self.session.state != STATE_UBOOT:
            raise HardwareFailure(
                "safe_cut_not_verified",
                f"serial state is {self.session.state}, not an idle U-Boot prompt",
            )
        result = self.session.uboot_readonly("mmc info")
        if self.session.state != STATE_UBOOT:
            raise HardwareFailure(
                "safe_cut_not_verified",
                "U-Boot prompt was not present after the idle query",
            )
        return self._make(result, CUT_UBOOT_IDLE, source="u-boot-mmc-info")

    def ums_stopped(self) -> CutEvidence:
        self.session.refresh(0.2)
        if self.session.state != STATE_UBOOT:
            raise HardwareFailure(
                "safe_cut_not_verified",
                "UMS is not proven stopped; no live U-Boot prompt",
            )
        result = self.session.uboot_readonly("mmc list")
        receipt = self.session.receipt(result.receipt_id)
        if "UMS" in receipt.output or self.session.state != STATE_UBOOT:
            raise HardwareFailure(
                "safe_cut_not_verified",
                "fresh U-Boot response still mentions UMS",
            )
        return self._make(result, CUT_UMS_STOPPED, source="u-boot-mmc-list")

    def poweroff_complete(self, result: CommandResult) -> CutEvidence:
        receipt = self.session.receipt(result.receipt_id)
        if receipt.command != "sync; poweroff":
            raise HardwareFailure(
                "safe_cut_not_verified",
                "poweroff evidence must come from the reviewed sync; poweroff command",
            )
        if not receipt.output or not POWEROFF_RE.search(receipt.output):
            raise HardwareFailure(
                "safe_cut_not_verified",
                "no fresh normal-shutdown 'Power down' evidence",
            )
        return self._make(result, CUT_LINUX_POWEROFF, source="normal-shutdown")

    def assert_valid(
        self,
        evidence: CutEvidence,
        *,
        now_mono: float | None = None,
        max_age: float | None = None,
    ) -> None:
        mono = self.session.clock() if now_mono is None else now_mono
        ok, reason = evidence.validate_static(
            board_id=self.board_id,
            session_id=self.session.session_id,
            now_mono=mono,
            max_age=max_age,
        )
        if not ok:
            raise HardwareFailure("safe_cut_not_verified", reason)
        expected = EXPECTED_RECEIPT_STATE.get(evidence.state)
        if expected is None:
            raise HardwareFailure("safe_cut_not_verified", "unknown cut state")
        try:
            self.session.validate_receipt(
                evidence.receipt_id,
                terminal_epoch=evidence.terminal_epoch,
                expected_state=expected,
                allow_poweroff=evidence.state == CUT_LINUX_POWEROFF,
            )
        except Refused as exc:
            raise HardwareFailure("safe_cut_not_verified", str(exc)) from None


def provider_for_gate(gate: CutEvidenceGate, state: str = CUT_UBOOT_IDLE):
    def provider(origin: str) -> CutEvidence:
        if state == CUT_UBOOT_IDLE:
            return gate.uboot_idle()
        if state == CUT_UMS_STOPPED:
            return gate.ums_stopped()
        raise HardwareFailure("safe_cut_not_verified", f"unknown cut state {state}")
    return provider
