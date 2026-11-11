#!/usr/bin/env python3
"""One-shot post-flash r3 boot: external-UMS takeover -> safe cut -> passive boot.

This is deliberately a *separate* entry from ``fnos.lab run``.  It never flashes,
never powers the board by itself before an explicit ``--execute``, and never
starts UMS.  Default output is a read-only plan.

Real execution requires all of:
  * a post-flash acceptance JSON with ``accepted=true``, the exact r3 SHA/size,
    the precise stable UMS target and SHA-256 bindings for both the whole-card
    backup evidence and the full read-back result evidence.  The historical
    Codex-only reviewer check is retired (2026-11-06 handoff); a DeepSeek
    self-verification record is accepted but labelled ``self``;
  * no deployment/backup/dd process still alive;
  * the unified ``x5-lab-01`` board lock (non-blocking);
  * a live stable USB identity with no mounted volume;
  * ``FNOS_LAB_ENABLE_REAL_BACKENDS=1``.

UMS was started by an earlier session.  A new serial session is therefore
allowed to see ``unknown`` (or the gadget's own UMS line).  It cannot claim
``u-boot-ums`` as fresh evidence.  ``VerifiedUmsSession`` proves the live USB
binding and the completed read-back again, sends exactly one Ctrl+C, and only
accepts a ``Hobot>`` line that arrived *after* that write.  The real
``CutEvidenceGate`` then issues the receipt.

The power path is CP5 USB only (SIID 7 / PIID 1), with one OFF attempt, a
confirmed OFF, a >= 5 s hold, then a confirmed ON.  Old serial bytes are
discarded and the terminal epoch is reset immediately before ON.  The boot
capture is passive: it never sends a space during the U-Boot countdown and
never guesses a login credential.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fnos.integration.hardware import macos_identity  # noqa: E402
from fnos.lab.authorization import require_real_gate  # noqa: E402
from fnos.lab.errors import HardwareFailure, LabError, PowerUnknown, Refused  # noqa: E402
from fnos.lab.locks import HeldLock, board_lock  # noqa: E402
from fnos.lab.model import CUT_HOLD_SECONDS  # noqa: E402
from fnos.lab.power import (  # noqa: E402
    USB_PIID,
    USB_PORT,
    USB_SIID,
    Cp5CliClient,
    Cp5PowerBackend,
    PowerManager,
)
from fnos.lab.runtime_identity import (  # noqa: E402
    BootIdentityBinding,
    IdentityExpectation,
    collect_runtime_identity,
    require_identity,
)
from fnos.lab.safe_cut import CutEvidenceGate  # noqa: E402
from fnos.lab.uart_session import (  # noqa: E402
    STATE_LINUX_SHELL,
    STATE_UBOOT,
    STATE_UBOOT_UMS,
    STATE_UNKNOWN,
    SerialSession,
    SessionRefused,
    SessionTimeout,
    normalize_terminal_text,
)

BOARD_ID = "x5-lab-01"
LOCK_ROOT = Path("/home/your-user/.local/state/fnos-lab")
SERIAL_PORT = "/dev/cu.usbserial-13220"
SERIAL_BAUD = 115200
CP5_SCRIPT = ROOT / "scripts" / "mijia-cp5" / "cp5.py"

TARGET_DEVICE = "/dev/disk12"
TARGET_MODEL = "UMS disk 0"
TARGET_BYTES = 31914983424
TARGET_USB_SERIAL = "32815562041507940378d9da0012004"

CANDIDATE_SHA256 = "653ffe7a5e20b019f0a5192799c5ddbda3752ffe5d8f6d67ba87e1afc498f599"
CANDIDATE_SIZE = 13157531648
CANDIDATE_KERNEL = "6.18.0"
CANDIDATE_ROOT_UUID = "c2d3e4f5-a6b7-4c8d-9e0f-1a2b3c4d5e6f"
CANDIDATE_FNOS_VERSION = "1.2.0701"
CANDIDATE_BASE_OS_ID = "debian"
CANDIDATE_BASE_OS_VERSION = "12"

ACCEPTANCE_SCHEMA = "fnos-postflash-boot-acceptance/v1"
RESULT_SCHEMA = "fnos-postflash-boot-result/v1"
DEFAULT_REVIEWED_TARGET = ROOT / "fnos/coordination/codex-ums-reviewed-target-20261106.json"
DEFAULT_ACCEPTANCE = ROOT / "fnos/coordination/codex-postflash-boot-acceptance-20261106.json"
DEFAULT_OUT_DIR = ROOT / "fnos/logs/fnos-x5-candidate-r3-20261106/postflash-boot"
NAS_CHECKLIST = ROOT / "fnos/logs/fnos-x5-candidate-r3-20261106/first-boot-nas-checklist.md"
NAS_SMOKE = ROOT / "fnos/logs/fnos-x5-candidate-r3-20261106/first-boot-nas-smoke.sh"

HOLD_SECONDS = float(CUT_HOLD_SECONDS)
CAPTURE_DEFAULT_SECONDS = 300
CAPTURE_MAX_SECONDS = 600
UMS_STOP_TIMEOUT = 5.0
EVIDENCE_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
PIPELINE_MARKERS = (
    "deploy_r3",
    "sd_backup.py",
    "flash_r2.py",
    "dd if=/dev/rdisk",
    "run_from_terminal",
)
KERNEL_RE = re.compile(r"Linux version ([0-9][0-9A-Za-z._+-]*)")
LOGIN_RE = re.compile(r"(?im)^[^\r\n]{0,64}\blogin:\s*$")
SHELL_RE = re.compile(r"(?m)^\s*[^\s@]+@[^\s:]+:[^\r\n#$]*[#$]\s*$")
UBOOT_PROMPT_RE = re.compile(r"(?m)^\s*(Hobot>|=>)\s*$")


def _iso(wall: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() if wall is None else wall))


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_regular_json(path: Path, label: str) -> tuple[dict[str, Any], str]:
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file():
        raise Refused(f"{label} must be a regular non-symlink file: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise Refused(f"cannot read {label}: {exc.strerror}") from None
    if not raw:
        raise Refused(f"{label} is empty: {path}")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Refused(f"{label} is not valid JSON: {path}") from None
    if not isinstance(value, dict):
        raise Refused(f"{label} must be a JSON object: {path}")
    return value, _sha_bytes(raw)


def _evidence_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise Refused(f"{label} path is missing")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    if path.is_symlink() or not path.is_file():
        raise Refused(f"{label} is not a regular non-symlink file: {path}")
    return path


def _require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not EVIDENCE_SHA_RE.fullmatch(value.lower()):
        raise Refused(f"{label} must be a 64-character SHA-256")
    return value.lower()


@dataclass(frozen=True)
class Acceptance:
    path: Path
    sha256: str
    candidate_sha256: str
    candidate_size: int
    device: str
    backup_path: Path
    backup_sha256: str
    readback_path: Path
    readback_sha256: str
    target_stable: dict[str, Any]
    reviewer: str = ""
    reviewer_independence: str = "self"


def verify_acceptance(
    path: Path,
    *,
    reviewed_target: Path = DEFAULT_REVIEWED_TARGET,
    device: str = TARGET_DEVICE,
) -> Acceptance:
    """Strictly re-check the Codex post-flash acceptance and its file bindings."""
    reviewed_raw, _ = _load_regular_json(reviewed_target, "reviewed UMS target")
    if reviewed_raw.get("reviewed_mapping") is not True:
        raise Refused("reviewed UMS target is not marked reviewed_mapping=true")
    reviewed = macos_identity.reviewed_facts(reviewed_raw)

    raw, file_sha = _load_regular_json(path, "post-flash acceptance")
    if raw.get("schema") != ACCEPTANCE_SCHEMA:
        raise Refused(f"acceptance schema must be {ACCEPTANCE_SCHEMA}")
    if raw.get("accepted") is not True:
        raise Refused("acceptance.accepted is not true")
    reviewer = raw.get("reviewer") or raw.get("reviewed_by")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise Refused("acceptance must name its reviewer/executor")
    # The 2026-11-06 handoff retires the hard-coded Codex gate: execution and
    # verification are now done by DeepSeek Harness.  A self-verified record is
    # accepted but never labelled independent; the hash bindings below remain
    # the actual safety gate.
    independence = "independent" if "codex" in reviewer.lower() else str(
        raw.get("reviewer_independence") or "self"
    )
    if independence not in ("independent", "self"):
        raise Refused("acceptance.reviewer_independence must be independent or self")

    candidate = raw.get("candidate")
    if not isinstance(candidate, dict):
        raise Refused("acceptance.candidate is missing")
    candidate_sha = _require_sha(candidate.get("sha256"), "candidate.sha256")
    if candidate_sha != CANDIDATE_SHA256:
        raise Refused("acceptance candidate SHA-256 is not the reviewed r3 image")
    size = candidate.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size != CANDIDATE_SIZE:
        raise Refused("acceptance candidate size is not the reviewed r3 image size")
    optional = {
        "kernel": CANDIDATE_KERNEL,
        "root_uuid": CANDIDATE_ROOT_UUID,
        "fnos_version": CANDIDATE_FNOS_VERSION,
    }
    for field, expected in optional.items():
        if field in candidate and candidate[field] != expected:
            raise Refused(f"acceptance candidate.{field} differs from the reviewed r3 image")

    target = raw.get("target")
    if not isinstance(target, dict):
        raise Refused("acceptance.target is missing")
    expected_target = {
        "device": device,
        "model": reviewed["model"],
        "size_bytes": reviewed["size_bytes"],
        "usb_gadget_serial": reviewed["usb_gadget_serial"],
        "usb_vid": reviewed["usb_vid"],
        "usb_pid": reviewed["usb_pid"],
        "usb_location_id": reviewed["usb_location_id"],
        "usb_path": reviewed["usb_path"],
        "whole": reviewed_raw.get("whole"),
        "internal": reviewed_raw.get("internal"),
        "bus": reviewed_raw.get("bus"),
    }
    if expected_target["whole"] is not True or expected_target["internal"] is not False:
        raise Refused("reviewed target is not a whole external disk")
    if (expected_target["bus"] or "").lower() != "usb":
        raise Refused("reviewed target is not USB")
    for key, expected in expected_target.items():
        if target.get(key) != expected:
            raise Refused(f"acceptance target.{key} does not match the reviewed stable target")

    evidence = raw.get("evidence")
    if not isinstance(evidence, dict):
        raise Refused("acceptance.evidence is missing")
    bindings: dict[str, tuple[Path, str]] = {}
    for label in ("backup", "readback"):
        entry = evidence.get(label)
        if not isinstance(entry, dict):
            raise Refused(f"acceptance.evidence.{label} is missing")
        file_path = _evidence_path(entry.get("path"), f"evidence.{label}")
        declared = _require_sha(entry.get("sha256"), f"evidence.{label}.sha256")
        actual = sha256_file(file_path)
        if actual != declared:
            raise Refused(f"evidence.{label} SHA-256 does not match its file")
        bindings[label] = (file_path, actual)

    backup, _ = _load_regular_json(bindings["backup"][0], "backup evidence")
    if backup.get("ok") is not True:
        raise Refused("backup evidence is not ok=true")
    if backup.get("skipped") is True:
        # Explicit user authorization (2026-11-06): the card was empty and is
        # overwritten without a backup.  Recorded separately from a real backup.
        if backup.get("authorized_by") != "user":
            raise Refused("skipped backup lacks authorized_by=user")
        if not backup.get("reason"):
            raise Refused("skipped backup lacks a reason")
    else:
        if backup.get("bytes") != TARGET_BYTES:
            raise Refused("backup evidence byte length is not the full card capacity")
        _require_sha(backup.get("sha256"), "backup evidence sha256")
        if not isinstance(backup.get("remote_path") or backup.get("path"), str) or not (
            backup.get("remote_path") or backup.get("path")
        ):
            raise Refused("backup evidence has no path/remote_path")

    readback, _ = _load_regular_json(bindings["readback"][0], "read-back evidence")
    if readback.get("ok") is not True:
        raise Refused("read-back evidence is not ok=true")
    if readback.get("readback_sha256") != CANDIDATE_SHA256:
        raise Refused("read-back SHA-256 is not the reviewed r3 image")
    read_bytes = readback.get("bytes", readback.get("bytes_read"))
    if read_bytes != CANDIDATE_SIZE:
        raise Refused("read-back byte length is not the reviewed r3 image size")
    if "image_sha256" in readback and readback["image_sha256"] != CANDIDATE_SHA256:
        raise Refused("read-back image_sha256 differs from the reviewed r3 image")
    if "target" in readback and readback["target"] != device:
        raise Refused("read-back evidence target differs from the requested device")

    return Acceptance(
        path=Path(path),
        sha256=file_sha,
        candidate_sha256=candidate_sha,
        candidate_size=size,
        device=device,
        backup_path=bindings["backup"][0],
        backup_sha256=bindings["backup"][1],
        readback_path=bindings["readback"][0],
        readback_sha256=bindings["readback"][1],
        target_stable=expected_target,
        reviewer=reviewer,
        reviewer_independence=independence,
    )


def parse_pipeline_processes(text: str, *, own_pid: int | None = None) -> list[dict[str, Any]]:
    """Return still-running deployment/backup/dd processes (never this process)."""
    found: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_text, _, command = line.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if own_pid is not None and pid == own_pid:
            continue
        if any(marker in command for marker in PIPELINE_MARKERS):
            found.append({"pid": pid, "command": command[:200]})
    return found


def list_pipeline_processes() -> list[dict[str, Any]]:
    proc = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        raise Refused("cannot enumerate processes before serial takeover")
    return parse_pipeline_processes(proc.stdout, own_pid=os.getpid())


@dataclass(frozen=True)
class ExternalUmsProof:
    """This-run proof that UMS is the live gadget and the read-back completed."""

    device: str
    size_bytes: int
    usb_gadget_serial: str
    usb_path: tuple[str, ...]
    readback_sha256: str
    acceptance_sha256: str
    captured_at: str

    @classmethod
    def from_live(cls, live: dict[str, Any], acceptance: Acceptance) -> "ExternalUmsProof":
        return cls(
            device=acceptance.device,
            size_bytes=int(live.get("size_bytes") or 0),
            usb_gadget_serial=str(live.get("usb_gadget_serial") or ""),
            usb_path=tuple(live.get("usb_path") or ()),
            readback_sha256=acceptance.candidate_sha256,
            acceptance_sha256=acceptance.sha256,
            captured_at=_iso(),
        )

    def validate(self) -> None:
        if not self.device:
            raise SessionRefused("external UMS proof has no device")
        if self.size_bytes != TARGET_BYTES:
            raise SessionRefused("external UMS proof has the wrong card capacity")
        if self.usb_gadget_serial != TARGET_USB_SERIAL:
            raise SessionRefused("external UMS proof has the wrong USB gadget serial")
        if not self.usb_path:
            raise SessionRefused("external UMS proof has no USB ancestor path")
        if self.readback_sha256 != CANDIDATE_SHA256:
            raise SessionRefused("external UMS proof has no reviewed r3 read-back")
        if not EVIDENCE_SHA_RE.fullmatch(self.acceptance_sha256):
            raise SessionRefused("external UMS proof has no acceptance digest")


@dataclass(frozen=True)
class UmsStop:
    ctrl_c_writes: int
    response_bytes: int
    response_sha256: str
    response_text: str
    terminal_epoch: int
    discarded_bytes: int
    discarded_sha256: str
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ctrl_c_writes": self.ctrl_c_writes,
            "fresh_prompt": True,
            "response_bytes": self.response_bytes,
            "response_sha256": self.response_sha256,
            "response_text": self.response_text,
            "terminal_epoch": self.terminal_epoch,
            "discarded_pre_takeover_bytes": self.discarded_bytes,
            "discarded_pre_takeover_sha256": self.discarded_sha256,
            "at": self.at,
        }


def _looks_like_linux(text: str) -> bool:
    return bool(
        KERNEL_RE.search(text)
        or LOGIN_RE.search(text)
        or SHELL_RE.search(text)
        or re.search(r"(?i)systemd\[|kernel panic|Booting Linux", text)
    )


class VerifiedUmsSession(SerialSession):
    """Serial session that can adopt an externally started UMS gadget safely."""

    @property
    def epoch(self) -> int:
        return self._epoch

    def absorb(self, data: bytes) -> str:
        self._note_rx(data, allow_state_change=True)
        return self.state

    def drain(self, *, quiet: float = 0.05, limit: int = 65536, max_seconds: float = 1.0) -> bytes:
        """Discard already-buffered bytes; they are not evidence of a new prompt."""
        deadline = self.clock() + max(0.0, max_seconds)
        data = bytearray()
        while len(data) < limit and self.clock() < deadline:
            chunk = self.transport.read(quiet, min(65536, limit - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    def adopt_external_ums(
        self,
        proof: ExternalUmsProof,
        *,
        timeout: float = UMS_STOP_TIMEOUT,
        on_write: Callable[[bytes], None] | None = None,
        on_bytes: Callable[[bytes], None] | None = None,
    ) -> UmsStop:
        """Send one Ctrl+C and require a prompt in bytes received after it."""
        proof.validate()
        self.held.revalidate()
        if self.state == STATE_UBOOT:
            raise SessionRefused(
                "a U-Boot prompt is already present; external UMS is not running, "
                "so a blind Ctrl+C would not be owned by this session"
            )
        if self.state not in (STATE_UNKNOWN, STATE_UBOOT_UMS):
            raise SessionRefused(
                f"refusing external UMS takeover in serial state {self.state}"
            )

        discarded = self.drain()
        if _looks_like_linux(normalize_terminal_text(discarded.decode("utf-8", "replace"))):
            raise SessionRefused(
                "serial delivered Linux boot/login bytes before takeover; refusing to cut power"
            )

        self._invalidate("external UMS takeover")
        epoch = self._epoch
        self.transport.write(b"\x03")
        if on_write is not None:
            on_write(b"\x03")

        seen = bytearray()
        deadline = self.clock() + max(0.5, timeout)
        while self.clock() < deadline:
            chunk = self.transport.read(
                min(0.2, max(0.0, deadline - self.clock())), 65536
            )
            if chunk:
                seen.extend(chunk)
                if on_bytes is not None:
                    on_bytes(bytes(chunk))
                self._note_rx(chunk, allow_state_change=True)
                fresh = normalize_terminal_text(seen.decode("utf-8", "replace"))
                if UBOOT_PROMPT_RE.search(fresh.rstrip(" \t\r\n")):
                    self.state = STATE_UBOOT
                    self._countdown_mono = None
                    self._countdown_epoch = None
                    return UmsStop(
                        ctrl_c_writes=1,
                        response_bytes=len(seen),
                        response_sha256=_sha_bytes(bytes(seen)),
                        response_text=fresh[-2000:],
                        terminal_epoch=epoch,
                        discarded_bytes=len(discarded),
                        discarded_sha256=_sha_bytes(discarded),
                        at=_iso(),
                    )
            self.held.revalidate()

        # Never cut power on a timeout, and never claim the old U-Boot text.
        self._invalidate("external UMS takeover timeout")
        raise SessionTimeout(
            "one Ctrl+C produced no fresh U-Boot prompt after the external UMS gadget"
        )

    def begin_new_boot_epoch(self) -> tuple[int, str]:
        """Invalidate all old serial bytes before restoring power."""
        previous = self._text
        self.on_power_on()
        self._text = ""
        return self._epoch, previous


class CaptureLog:
    """Append-only raw serial capture with per-segment timestamps and hashes."""

    def __init__(self, path: Path, *, clock: Callable[[], float], wall: Callable[[], float]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fd = os.open(
            self.path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        self._clock = clock
        self._wall = wall
        self._hash = hashlib.sha256()
        self.bytes = 0
        self.segments: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None
        self.closed = False

    def _close_segment(self) -> None:
        current = self._current
        if current is None:
            return
        current["ended_at"] = _iso(self._wall())
        current["duration_s"] = round(self._clock() - current.pop("_started_mono"), 3)
        current["bytes"] = current.pop("_bytes")
        current["sha256"] = current.pop("_hash").hexdigest()
        self.segments.append(current)
        self._current = None

    def mark(self, label: str, **meta: Any) -> None:
        if self.closed:
            raise Refused("capture log is already closed")
        self._close_segment()
        self._current = {
            "label": label,
            "started_at": _iso(self._wall()),
            "start_offset": self.bytes,
            "_started_mono": self._clock(),
            "_bytes": 0,
            "_hash": hashlib.sha256(),
            **meta,
        }

    def write(self, data: bytes) -> None:
        if not data:
            return
        if self.closed:
            raise Refused("capture log is already closed")
        if self._current is None:
            self.mark("capture")
        view = memoryview(data)
        while view:
            count = os.write(self.fd, view)
            if count <= 0:
                raise OSError("short serial capture write")
            view = view[count:]
        self._hash.update(data)
        self.bytes += len(data)
        self._current["_hash"].update(data)
        self._current["_bytes"] += len(data)

    def flush(self, *, fsync: bool = False) -> None:
        if fsync and not self.closed:
            os.fsync(self.fd)

    def close(self) -> None:
        if self.closed:
            return
        self._close_segment()
        try:
            os.fsync(self.fd)
        finally:
            os.close(self.fd)
            self.closed = True

    def summary(self) -> dict[str, Any]:
        if not self.closed:
            raise Refused("capture must be closed before its summary")
        return {
            "raw_path": str(self.path),
            "raw_sha256": self._hash.hexdigest(),
            "raw_bytes": self.bytes,
            "segments": list(self.segments),
        }


class CaptureTee:
    """Record every byte the serial session receives into the raw capture.

    Installed after the external-UMS takeover so the safe-cut probe and the
    runtime-identity commands also have their real board replies in
    ``serial.log``.  Writes are not recorded here: TX is recorded explicitly by
    the caller (the single Ctrl+C).  ``enabled`` is dropped while discarding
    stale pre-power-on bytes so old text cannot masquerade as new-boot evidence.
    """

    def __init__(self, inner, capture: "CaptureLog"):
        self.inner = inner
        self.capture = capture
        self.enabled = True
        self.bytes_captured = 0

    def read(self, timeout: float, max_bytes: int = 65536) -> bytes:
        data = self.inner.read(timeout, max_bytes)
        if data and self.enabled:
            self.capture.write(data)
            self.bytes_captured += len(data)
        return data

    def write(self, data: bytes) -> int:
        return self.inner.write(data)

    def close(self) -> None:
        return self.inner.close()

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


def capture_boot(
    session: VerifiedUmsSession,
    capture: CaptureLog,
    *,
    max_seconds: int,
    clock: Callable[[], float],
    emit: Callable[[dict[str, Any]], None],
    heartbeat_seconds: float = 10.0,
) -> str:
    """Passively read the boot log.  Never writes a byte to the board."""
    deadline = clock() + max_seconds
    next_heartbeat = clock() + heartbeat_seconds
    last_state = STATE_UNKNOWN
    heartbeats = 0
    while clock() < deadline:
        remaining = max(0.0, deadline - clock())
        chunk = session.transport.read(min(0.2, remaining), 65536)
        session.held.revalidate()
        if chunk:
            session.absorb(chunk)
            if session.state != last_state:
                capture.mark(f"boot:{session.state}", state=session.state)
                last_state = session.state
        now = clock()
        if now >= next_heartbeat:
            capture.flush(fsync=True)
            heartbeats += 1
            emit(
                {
                    "heartbeat": True,
                    "elapsed_s": round(max_seconds - max(0.0, deadline - now), 1),
                    "bytes": capture.bytes,
                    "state": session.state,
                    "last_rx_age_s": None
                    if session._last_rx_mono is None
                    else round(now - session._last_rx_mono, 1),
                }
            )
            next_heartbeat = now + heartbeat_seconds
        text = session.text()
        if LOGIN_RE.search(text) or SHELL_RE.search(text):
            break
        if session.state == STATE_UBOOT_UMS or "UMS: LUN" in text[-8192:]:
            break
    capture.flush(fsync=True)
    return session.text()


def _boot_status(text: str, session: VerifiedUmsSession) -> dict[str, Any]:
    match = KERNEL_RE.search(text)
    release = match.group(1) if match else ""
    login_seen = bool(LOGIN_RE.search(text))
    shell_seen = session.state == STATE_LINUX_SHELL or bool(SHELL_RE.search(text))
    ums_seen = session.state == STATE_UBOOT_UMS or "UMS: LUN" in text[-8192:]
    kernel_ok = release == CANDIDATE_KERNEL
    kernel_login_ok = kernel_ok and (login_seen or shell_seen)
    if not kernel_ok:
        status = "failed"
    elif not kernel_login_ok:
        status = "boot-banner"
    elif shell_seen:
        status = "runtime-identity"
    else:
        status = "login"
    return {
        "kernel_banner": match.group(0) if match else "",
        "kernel_release": release,
        "kernel_ok": kernel_ok,
        "login_seen": login_seen,
        "shell_seen": shell_seen,
        "kernel_login_ok": kernel_login_ok,
        "ums_started_after_power_on": ums_seen,
        # A kernel + login is not product acceptance.  boot_ok is set only
        # after the fresh runtime boot_id/trim identity is verified too.
        "boot_ok": False,
        "status": status,
    }


def _write_result(path: Path, result: dict[str, Any]) -> None:
    raw = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(raw)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("short result write")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _error_dict(exc: BaseException) -> dict[str, Any]:
    return {
        "type": type(exc).__name__,
        "code": getattr(exc, "code", ""),
        "message": str(exc)[:500],
        "manual_recovery": bool(getattr(exc, "manual_recovery", False)),
    }


def _refuse_existing(out_dir: Path) -> None:
    for name in ("result.json", "serial.log"):
        if (out_dir / name).exists():
            raise Refused(
                f"one-shot output already exists: {out_dir / name}; move it before a new attempt"
            )


def _real_session(held: HeldLock) -> VerifiedUmsSession:
    return VerifiedUmsSession(
        board_id=BOARD_ID,
        held=held,
        port=SERIAL_PORT,
        state_root=LOCK_ROOT,
        baud=SERIAL_BAUD,
    )


def _real_power(held: HeldLock, emit: Callable[[dict[str, Any]], None]) -> PowerManager:
    client = Cp5CliClient(CP5_SCRIPT, board_id=BOARD_ID, lock=held)
    backend = Cp5PowerBackend(client, board_id=BOARD_ID)
    return PowerManager(
        backend,
        phase=lambda: "postflash-boot",
        emit=lambda event, **fields: emit({"event": event, **fields}),
        save=lambda: None,
    )


def _base_result(acceptance: Acceptance, live: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "board_id": BOARD_ID,
        "device": acceptance.device,
        "at": _iso(),
        "acceptance": {
            "path": str(acceptance.path),
            "sha256": acceptance.sha256,
            "reviewer": acceptance.reviewer,
            "reviewer_independence": acceptance.reviewer_independence,
            "evidence": {
                "backup": {
                    "path": str(acceptance.backup_path),
                    "sha256": acceptance.backup_sha256,
                },
                "readback": {
                    "path": str(acceptance.readback_path),
                    "sha256": acceptance.readback_sha256,
                },
            },
        },
        "candidate": {
            "sha256": acceptance.candidate_sha256,
            "size": acceptance.candidate_size,
            "kernel": CANDIDATE_KERNEL,
            "root_uuid": CANDIDATE_ROOT_UUID,
            "fnos_version": CANDIDATE_FNOS_VERSION,
        },
        "target": {
            "model": live.get("model"),
            "size_bytes": live.get("size_bytes"),
            "usb_gadget_serial": live.get("usb_gadget_serial"),
            "usb_vid": live.get("usb_vid"),
            "usb_pid": live.get("usb_pid"),
            "usb_location_id": live.get("usb_location_id"),
            "usb_path": live.get("usb_path"),
            "mounts": live.get("mounts") or [],
            "whole": live.get("whole"),
            "internal": live.get("internal"),
            "bus": live.get("bus"),
            "partition_layout_required": False,
        },
        "serial": {"port": SERIAL_PORT, "baud": SERIAL_BAUD},
        "phases": {},
        "boot": {},
        "runtime_identity": None,
        "product_identity_ok": False,
        "nas": {
            "status": "not_attempted",
            "checklist_path": str(NAS_CHECKLIST),
            "smoke_path": str(NAS_SMOKE),
            "next": (
                "after an operator authenticates interactively and creates the admin "
                "credential, obtain the board IP, check Web on 80/8000/443, then create "
                "one temporary SMB share and run the smoke script with a chmod-600 "
                "credential file; this entry never creates accounts, shares or passwords"
            ),
        },
        "status": "started",
        "boot_ok": False,
        "complete": False,
        "error": None,
    }


def _execute_locked(
    *,
    held: HeldLock,
    acceptance: Acceptance,
    live: dict[str, Any],
    out_dir: Path,
    max_seconds: int,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    emit: Callable[[dict[str, Any]], None],
    session_factory: Callable[[HeldLock], VerifiedUmsSession] | None,
    power_factory: Callable[[HeldLock, Callable[[dict[str, Any]], None]], PowerManager] | None,
) -> dict[str, Any]:
    _refuse_existing(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    capture = CaptureLog(out_dir / "serial.log", clock=clock, wall=time.time)
    result = _base_result(acceptance, live)
    session: VerifiedUmsSession | None = None
    power: PowerManager | None = None
    pending: BaseException | None = None
    try:
        session = (session_factory or _real_session)(held)
        power = (power_factory or _real_power)(held, emit)

        state = session.open(passive_timeout=0.5)
        result["serial"]["open_state"] = state
        preamble = session.text()
        result["serial"]["preamble_chars"] = len(preamble)
        result["serial"]["preamble_sha256"] = _sha_bytes(preamble.encode("utf-8", "replace"))
        if state not in (STATE_UNKNOWN, STATE_UBOOT_UMS):
            raise Refused(
                f"external UMS is not the current serial state ({state}); refusing takeover"
            )

        capture.mark("takeover", state=state)
        proof = ExternalUmsProof.from_live(live, acceptance)
        stop = session.adopt_external_ums(
            proof,
            timeout=UMS_STOP_TIMEOUT,
            on_write=capture.write,
            on_bytes=capture.write,
        )
        result["phases"]["ums_takeover"] = stop.as_dict()

        # From here on every received byte (safe-cut probe, identity commands,
        # boot log) is written to serial.log as real evidence.
        session.transport = CaptureTee(session.transport, capture)

        capture.mark("safe-cut", state=session.state)
        off_confirmed = False
        on_attempted = False
        on_confirmed = False
        gate = CutEvidenceGate(board_id=BOARD_ID, session=session, ttl_seconds=30.0)
        evidence = gate.ums_stopped()
        gate.assert_valid(evidence)
        result["phases"]["safe_cut"] = evidence.as_dict()

        try:
            off = power.turn_off(
                "postflash-boot",
                safe_to_cut=True,
                cut_validator=lambda: gate.assert_valid(evidence) or True,
            )
        except PowerUnknown as exc:
            recovered = power.recover_unknown_off_once()
            result["phases"]["power_off"] = {
                "state": "unknown",
                "error": str(exc)[:300],
                "recovery_on_attempts": 1,
                "recovered": recovered,
            }
            raise HardwareFailure(
                "power_state_unknown",
                f"OFF result was unknown; one ON recovery {'succeeded' if recovered else 'failed'}; "
                "OFF was never retried",
                manual_recovery=not recovered,
            ) from None
        if off != "off":
            raise HardwareFailure("power_off_not_confirmed", f"CP5 USB reported {off!r}, not off")
        off_confirmed = True
        on_attempted = False
        on_confirmed = False
        result["phases"]["power_off"] = {"state": off, "at": _iso()}
        capture.mark("power-off-hold", state="off")
        try:
            hold_started_mono = clock()
            hold_started_at = _iso()
            while clock() - hold_started_mono < HOLD_SECONDS:
                sleep(max(0.0, HOLD_SECONDS - (clock() - hold_started_mono)))
            hold_actual = clock() - hold_started_mono
            if hold_actual + 1e-6 < HOLD_SECONDS:
                raise HardwareFailure(
                    "cut_hold_too_short",
                    f"OFF was held {hold_actual:.3f}s, less than {HOLD_SECONDS:.0f}s",
                )
            result["phases"]["hold"] = {
                "required_seconds": HOLD_SECONDS,
                "actual_seconds": round(hold_actual, 3),
                "started_at": hold_started_at,
                "completed_at": _iso(),
            }

            # Bytes buffered while power is off are not new-boot evidence.
            tee = session.transport if isinstance(session.transport, CaptureTee) else None
            if tee is not None:
                tee.enabled = False
            try:
                stale = session.drain()
            finally:
                if tee is not None:
                    tee.enabled = True
            result["serial"]["discarded_pre_on_bytes"] = len(stale)
            result["serial"]["discarded_pre_on_sha256"] = _sha_bytes(stale)
            if _looks_like_linux(normalize_terminal_text(stale.decode("utf-8", "replace"))):
                raise Refused("Linux bytes were already buffered before ON; refusing a false boot")

            # New cursor/epoch *before* ON: old U-Boot/UMS text is invalid.
            epoch, previous_text = session.begin_new_boot_epoch()
            cursor = session.cursor()
            result["serial"]["pre_on_text_chars"] = len(previous_text)
            result["serial"]["pre_on_text_sha256"] = _sha_bytes(
                previous_text.encode("utf-8", "replace")
            )
            capture.mark("power-on", state=STATE_UNKNOWN)
            on_attempted = True
            on = power.turn_on("postflash-boot")
            if on != "on":
                raise HardwareFailure("power_on_not_confirmed", f"CP5 USB reported {on!r}, not on")
            on_confirmed = True
            result["phases"]["power_on"] = {
                "state": on,
                "at": _iso(),
                "capture_epoch": epoch,
                "capture_cursor": cursor,
            }
            capture.mark("new-boot", state=STATE_UNKNOWN)
        except BaseException:
            # A failure during the hold/drain/epoch preparation must not leave
            # the board unpowered.  If ON was never attempted, restore it once.
            if off_confirmed and not on_confirmed and not on_attempted:
                try:
                    restored = power.turn_on("postflash-boot-restore")
                    result["phases"]["power_restore"] = {
                        "state": restored,
                        "reason": "exception before ON; one restore attempt",
                    }
                except BaseException as restore_exc:
                    result["phases"]["power_restore"] = {
                        "state": "unknown",
                        "error": _error_dict(restore_exc),
                        "reason": "exception before ON; restore failed",
                    }
            elif off_confirmed and not on_confirmed and on_attempted:
                # ON was ambiguous/refused: one best-effort ON recovery, never
                # another OFF.  Power remains unknown if this also fails.
                try:
                    recovered = power.recover_unknown_off_once()
                    result["phases"]["power_restore"] = {
                        "state": "on" if recovered else "unknown",
                        "reason": "ON result was not confirmed; one recovery attempt",
                    }
                except BaseException as restore_exc:
                    result["phases"]["power_restore"] = {
                        "state": "unknown",
                        "error": _error_dict(restore_exc),
                        "reason": "ON recovery failed; manual inspection required",
                    }
            raise

        text = capture_boot(
            session,
            capture,
            max_seconds=max_seconds,
            clock=clock,
            emit=emit,
        )
        boot = _boot_status(text, session)
        result["boot"] = boot
        result["status"] = boot["status"]
        result["boot_ok"] = False

        if boot["shell_seen"]:
            capture.mark("runtime-identity", state=session.state)
            try:
                identity = collect_runtime_identity(session)
                BootIdentityBinding(previous_boot_id=None).require_new(identity)
                if identity.boot_id and identity.boot_id in previous_text:
                    raise HardwareFailure(
                        "stale_boot_identity",
                        "runtime boot_id was already present before power-on",
                    )
                require_identity(
                    identity,
                    IdentityExpectation(
                        board_model_contains="RDK X5",
                        base_os_id=CANDIDATE_BASE_OS_ID,
                        base_os_version=CANDIDATE_BASE_OS_VERSION,
                        kernel_release=CANDIDATE_KERNEL,
                        fnos_id="fnos",
                        fnos_version=CANDIDATE_FNOS_VERSION,
                        root_uuid=CANDIDATE_ROOT_UUID,
                        require_version_file=True,
                    ),
                )
                result["runtime_identity"] = identity.as_dict()
                result["product_identity_ok"] = True
                result["status"] = "runtime-identity"
                result["boot_ok"] = bool(boot["kernel_login_ok"])
            except LabError as exc:
                result["runtime_identity_error"] = _error_dict(exc)
                result["status"] = "runtime-identity-mismatch"
        result["complete"] = False
    except BaseException as exc:  # noqa: BLE001 - persist controlled and unexpected failures
        pending = exc
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        capture.close()

    result["serial"]["capture"] = capture.summary()
    if pending is None:
        _write_result(out_dir / "result.json", result)
        return result

    result["error"] = _error_dict(pending)
    result["status"] = "refused" if isinstance(pending, Refused) else "failed"
    result["boot_ok"] = False
    result["complete"] = False
    _write_result(out_dir / "result.json", result)
    if isinstance(pending, LabError):
        return result
    raise pending


def run(
    *,
    acceptance_path: Path = DEFAULT_ACCEPTANCE,
    reviewed_target: Path = DEFAULT_REVIEWED_TARGET,
    device: str = TARGET_DEVICE,
    out_dir: Path = DEFAULT_OUT_DIR,
    lock_root: Path = LOCK_ROOT,
    execute: bool = False,
    max_capture_seconds: int = CAPTURE_DEFAULT_SECONDS,
    process_lister: Callable[[], list[dict[str, Any]]] | None = None,
    usb_live: Callable[[str], dict[str, Any]] | None = None,
    session_factory: Callable[[HeldLock], VerifiedUmsSession] | None = None,
    power_factory: Callable[[HeldLock, Callable[[dict[str, Any]], None]], PowerManager] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if max_capture_seconds < 10 or max_capture_seconds > CAPTURE_MAX_SECONDS:
        raise Refused(f"max capture must be 10..{CAPTURE_MAX_SECONDS} seconds")
    emit = emit or (lambda value: print(json.dumps(value, ensure_ascii=False), flush=True))

    reviewed_raw, _ = _load_regular_json(reviewed_target, "reviewed UMS target")
    reviewed = macos_identity.reviewed_facts(reviewed_raw)
    acceptance: Acceptance | None = None
    acceptance_error = ""
    try:
        acceptance = verify_acceptance(
            acceptance_path, reviewed_target=reviewed_target, device=device
        )
    except Refused as exc:
        acceptance_error = str(exc)

    procs = (process_lister or list_pipeline_processes)()
    blockers: list[str] = []
    if acceptance is None:
        blockers.append(f"acceptance rejected: {acceptance_error}")
    if procs:
        blockers.append(
            "deployment/backup processes still running: "
            + ", ".join(str(item["pid"]) for item in procs)
        )

    plan = {
        "schema": "fnos-postflash-boot-plan/v1",
        "execute": False,
        "board_id": BOARD_ID,
        "acceptance": {
            "path": str(acceptance_path),
            "ok": acceptance is not None,
            "error": acceptance_error,
            "sha256": acceptance.sha256 if acceptance else None,
        },
        "candidate": {
            "sha256": CANDIDATE_SHA256,
            "size": CANDIDATE_SIZE,
            "kernel": CANDIDATE_KERNEL,
            "root_uuid": CANDIDATE_ROOT_UUID,
            "fnos_version": CANDIDATE_FNOS_VERSION,
        },
        "target": {"device": device, **reviewed},
        "serial": {"port": SERIAL_PORT, "baud": SERIAL_BAUD},
        "power": {"backend": "cp5-usb", "channel": USB_PORT, "siid": USB_SIID, "piid": USB_PIID, "hold_seconds": HOLD_SECONDS},
        "capture": {"max_seconds": max_capture_seconds, "passive": True, "writes_during_boot": 0},
        "out_dir": str(out_dir),
        "preflight": {"blockers": blockers, "pipeline_processes": procs},
        "ready": not blockers,
        "note": (
            "plan only: no serial port, CP5, USB gadget, disk or network action was taken"
        ),
    }
    if not execute:
        return plan
    if blockers:
        raise Refused("; ".join(blockers))

    with board_lock(lock_root, BOARD_ID) as held:
        late = (process_lister or list_pipeline_processes)()
        if late:
            raise Refused(
                "deployment/backup processes appeared after the lock: "
                + ", ".join(str(item["pid"]) for item in late)
            )
        # Re-verify the exact acceptance/evidence bindings under the lock.
        acceptance = verify_acceptance(
            acceptance_path, reviewed_target=reviewed_target, device=device
        )
        try:
            live = (usb_live or macos_identity.live)(device)
        except ValueError as exc:
            raise Refused(f"live USB target rejected: {exc}") from None
        except Exception as exc:
            raise Refused(f"cannot re-identify the live USB target: {type(exc).__name__}") from None
        # Stable binding only: after the write the card no longer has the old
        # single-FAT layout, so compare_initial_layout is intentionally unused.
        try:
            macos_identity.compare_stable(reviewed, live)
        except ValueError as exc:
            raise Refused(f"live USB target is not the reviewed binding: {exc}") from None
        return _execute_locked(
            held=held,
            acceptance=acceptance,
            live=live,
            out_dir=out_dir,
            max_seconds=max_capture_seconds,
            clock=clock,
            sleep=sleep,
            emit=emit,
            session_factory=session_factory,
            power_factory=power_factory,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="one-shot post-flash r3 boot; default is a read-only plan"
    )
    parser.add_argument("--acceptance", type=Path, default=DEFAULT_ACCEPTANCE)
    parser.add_argument("--reviewed-target", type=Path, default=DEFAULT_REVIEWED_TARGET)
    parser.add_argument("--device", default=TARGET_DEVICE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--max-capture-seconds", type=int, default=CAPTURE_DEFAULT_SECONDS)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.execute:
            require_real_gate()
        result = run(
            acceptance_path=args.acceptance,
            reviewed_target=args.reviewed_target,
            device=args.device,
            out_dir=args.out_dir,
            execute=args.execute,
            max_capture_seconds=args.max_capture_seconds,
        )
    except LabError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "kind": exc.kind,
                    "error": _error_dict(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return exc.exit_code

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not args.execute:
        return 0
    if result.get("boot_ok"):
        return 0
    return 2 if result.get("status") == "refused" else 3


if __name__ == "__main__":
    sys.exit(main())
