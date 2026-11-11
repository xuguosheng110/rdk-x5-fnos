"""Owned serial command session with receipt-bound responses.

The session is the single reader/writer for a physical serial port.  It holds
the board lock plus a port-identity ownership lock, classifies the current
terminal state from *ordered received events*, and registers every command
response as an opaque receipt.  Safe-cut evidence references a receipt; a
dataclass with plausible strings is not accepted by the gate.

Real use is gated by FNOS_LAB_ENABLE_REAL_BACKENDS=1.  Tests use the
in-memory transport only.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
import re
import secrets
import select
import stat
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .authorization import require_real_gate
from .errors import HardwareFailure, Refused
from .locks import HeldLock
from .util import secure_dir

STATE_UNKNOWN = "unknown"
STATE_BOOTROM = "bootrom"
STATE_UBOOT = "u-boot"
STATE_UBOOT_UMS = "u-boot-ums"
STATE_LINUX_BOOT = "linux-boot"
STATE_LINUX_LOGIN = "linux-login"
STATE_LINUX_SHELL = "linux-shell"

# A countdown is only actionable for slightly less than its advertised 1 s;
# the extra margin is transport/scheduler jitter, never an open-ended window.
COUNTDOWN_MAX_AGE = 1.0

UBOOT_READONLY = re.compile(
    r"^(mmc list|mmc info|bdinfo|printenv(?:\s+[A-Za-z0-9_]+)?|"
    r"part list mmc [0-9]+|mmc cid)$"
)
UBOOT_UMS = re.compile(r"^ums 0 mmc [0-9]+$")

UBOOT_SAFE_PROBES = {
    "guard_hex_gt": "if test 0x120b838a -gt 0x20000000; then echo R2_GUARD_GT; else echo R2_GUARD_LE; fi",
    "limit_hex_gt": "if test 0x20000000 -gt 0x120b838a; then echo R2_LIMIT_GT; else echo R2_LIMIT_LE; fi",
    "boundary_hex_gt": "if test 0x20000001 -gt 0x20000000; then echo R2_BOUNDARY_GT; else echo R2_BOUNDARY_LE; fi",
    "zero_lt": "if test 0 -lt 0x120b838a; then echo R2_ZERO_LT; else echo R2_ZERO_GE; fi",
    "colon_true": "if :; then echo R2_COLON_TRUE; else echo R2_COLON_FALSE; fi",
}

LINUX_IDENTITY_COMMANDS = (
    "cat /proc/device-tree/model",
    "cat /proc/device-tree/compatible",
    "cat /proc/device-tree/serial-number",
    "uname -r",
    "cat /proc/sys/kernel/random/boot_id",
    "findmnt -n -o SOURCE,UUID /",
    "cat /etc/os-release",
    "cat /usr/trim/etc/version",
    "cat /etc/fnos-x5/source-identity.txt",
    "dpkg-query -W -f='${Version}' trim",
    "dpkg-query -W -f='${Status}' trim",
)

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
PROMPT_RE = re.compile(
    r"(?m)^\s*(?:\[[^\]]+\]\s*)?[^\s@]+@[^\s:]+:[^\r\n#$]*[#$]\s*"
)
TRAILING_PROMPT_RE = re.compile(
    r"[ \t]*[^\s@]+@[^\s:]+:[^\r\n#$]*[#$]\s*$"
)
MARKER_RE = re.compile(r"^__FNOS_(?:BEGIN|END)_[0-9a-f]{16,64}__$")
COUNTDOWN_MARKER = "Hit <SPACE> key to stop autoboot"
POWERDOWN_RE = re.compile(
    r"(?i)(power down|reboot: power down|systemd-shutdown.*powering off|"
    r"system is powering down)"
)


class SessionRefused(Refused):
    pass


class SessionTimeout(HardwareFailure):
    def __init__(self, message: str):
        super().__init__("serial_session_timeout", message)


class SessionProtocolError(HardwareFailure):
    def __init__(self, code: str, message: str):
        super().__init__(code, message)


@dataclass(frozen=True)
class CommandReceipt:
    receipt_id: str
    session_id: str
    terminal_epoch: int
    nonce: str
    command: str
    output: str
    raw_sha256: str
    state_before: str
    state_after: str
    issued_mono: float
    completed_mono: float

    def digest(self) -> str:
        return hashlib.sha256(
            (
                self.command
                + "\0"
                + self.nonce
                + "\0"
                + self.output
                + "\0"
                + self.state_before
                + "\0"
                + self.state_after
                + "\0"
                + str(self.terminal_epoch)
            ).encode("utf-8", "replace")
        ).hexdigest()


@dataclass(frozen=True)
class CommandResult:
    command: str
    state_before: str
    state_after: str
    nonce: str
    output: str
    started_at: str
    finished_at: str
    bytes_read: int
    receipt_id: str = ""
    terminal_epoch: int = 0

    def digest(self) -> str:
        return hashlib.sha256(
            (
                self.command
                + "\0"
                + self.nonce
                + "\0"
                + self.output
                + "\0"
                + self.state_before
                + "\0"
                + self.state_after
                + "\0"
                + str(self.terminal_epoch)
            ).encode("utf-8", "replace")
        ).hexdigest()


class SerialTransport(Protocol):
    def open(self) -> None: ...
    def read(self, timeout: float, max_bytes: int = 65536) -> bytes: ...
    def write(self, data: bytes) -> None: ...
    def close(self) -> None: ...
    def is_open(self) -> bool: ...


class FakeSerialTransport:
    """In-memory transport.  ``responder`` gets the written bytes."""

    def __init__(
        self,
        *,
        initial: bytes = b"",
        responder: Callable[[bytes], bytes] | None = None,
    ):
        self.buffer = bytearray(initial)
        self.responder = responder
        self.writes: list[bytes] = []
        self._open = False

    def open(self) -> None:
        self._open = True

    def feed(self, data: bytes) -> None:
        self.buffer.extend(data)

    def read(self, timeout: float, max_bytes: int = 65536) -> bytes:
        data = bytes(self.buffer[:max_bytes])
        del self.buffer[:max_bytes]
        return data

    def write(self, data: bytes) -> None:
        if not self._open:
            raise SessionRefused("transport is not open")
        self.writes.append(bytes(data))
        if self.responder is not None:
            response = self.responder(bytes(data))
            if response:
                self.buffer.extend(response)

    def close(self) -> None:
        self._open = False

    def is_open(self) -> bool:
        return self._open


class RealSerialTransport:
    """Real 115200 8N1 transport.  Never called by this batch's execution."""

    def __init__(self, port: str, *, baud: int = 115200):
        require_real_gate()
        if not isinstance(port, str) or not port.startswith("/dev/"):
            raise SessionRefused("serial port must be absolute /dev/...")
        self.port = port
        self.baud = baud
        self.fd = -1

    def open(self) -> None:
        if self.fd >= 0:
            return
        try:
            fd = os.open(
                self.port,
                os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK | os.O_NOFOLLOW,
            )
        except OSError as exc:
            raise SessionRefused(f"cannot open serial port: {exc.strerror}") from None
        try:
            import termios

            attrs = termios.tcgetattr(fd)
            cflag = attrs[2]
            for name in ("CSIZE", "PARENB", "CSTOPB", "HUPCL", "CRTSCTS",
                         "CRTS_IFLOW", "CCTS_OFLOW", "PARODD"):
                cflag &= ~getattr(termios, name, 0)
            cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[0] = 0
            attrs[1] = 0
            attrs[2] = cflag
            attrs[3] = 0
            attrs[4] = getattr(termios, f"B{self.baud}", termios.B115200)
            attrs[5] = attrs[4]
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception:
            os.close(fd)
            raise
        self.fd = fd

    def read(self, timeout: float, max_bytes: int = 65536) -> bytes:
        if self.fd < 0:
            raise SessionRefused("transport is not open")
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return b""
            try:
                ready, _, _ = select.select([self.fd], [], [], min(remaining, 0.2))
            except InterruptedError:
                continue
            if not ready:
                continue
            try:
                return os.read(self.fd, max_bytes)
            except BlockingIOError:
                continue

    def write(self, data: bytes) -> None:
        if self.fd < 0:
            raise SessionRefused("transport is not open")
        view = memoryview(data)
        while view:
            count = os.write(self.fd, view)
            if count <= 0:
                raise SessionProtocolError("short_serial_write", "serial write made no progress")
            view = view[count:]

    def close(self) -> None:
        if self.fd >= 0:
            try:
                import termios

                attrs = termios.tcgetattr(self.fd)
                attrs[2] &= ~termios.HUPCL
                termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            except Exception:
                pass
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1

    def is_open(self) -> bool:
        return self.fd >= 0


def physical_port_identity(port: str) -> str:
    """Canonical identity shared by ``cu``/``tty`` aliases and symlinks."""
    try:
        resolved = os.path.realpath(port)
    except OSError:
        resolved = str(port)
    name = os.path.basename(resolved)
    for prefix in ("cu.", "tty."):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name or resolved


def classify_state(text: str) -> str:
    """Classify from the *last* complete prompt/login/banner line.

    An old U-Boot prompt earlier in a long transcript must not beat a newer
    Linux login/shell prompt.  Plain command output does not erase a state.
    """
    clean = normalize_terminal_text(text)
    lines = [line.strip() for line in clean.split("\n") if line.strip()]
    for line in reversed(lines):
        if re.fullmatch(r"(Hobot>|=>)", line):
            return STATE_UBOOT
        if re.fullmatch(r"login:", line):
            return STATE_LINUX_LOGIN
        if re.search(r"[#$]$", line) and not line.startswith(("U-Boot", "NOTICE")):
            return STATE_LINUX_SHELL
        if "Linux version" in line or "Booting Linux" in line:
            return STATE_LINUX_BOOT
        if "UMS" in line or "ums 0 mmc" in line:
            return STATE_UBOOT_UMS
        if "BOOTROM" in line or "BootROM" in line:
            return STATE_BOOTROM
    return STATE_UNKNOWN


def normalize_terminal_text(text: str) -> str:
    text = ANSI_RE.sub("", text)
    text = text.replace("\x00", "\n").replace("\r\n", "\n").replace("\r", "\n")
    return text


def clean_terminal_output(text: str, *, command: str | None = None) -> str:
    """Remove shell echo, prompts, NUL separators, markers and CRLF noise."""
    clean = normalize_terminal_text(text)
    result: list[str] = []
    for raw in clean.split("\n"):
        line = raw.strip(" \t")
        if not line:
            continue
        if MARKER_RE.match(line):
            continue
        if command and (line == command or line == f"{command}\\n"):
            continue
        if line.startswith(("printf '", "echo __FNOS_")):
            continue
        line = PROMPT_RE.sub("", line)
        line = TRAILING_PROMPT_RE.sub("", line)
        line = line.strip()
        if not line:
            continue
        if command and line == command:
            continue
        if line.startswith(("printf '", "printf \"", "printf end")):
            continue
        if MARKER_RE.match(line):
            continue
        result.append(line)
    return "\n".join(result)


def _lines(data: bytes) -> list[bytes]:
    return normalize_terminal_text(data.decode("utf-8", "replace")).encode(
        "utf-8", "replace"
    ).split(b"\n")


def _marker_index(data: bytes, marker: bytes) -> int | None:
    for index, line in enumerate(_lines(data)):
        if line.strip(b" \t") == marker:
            return index
    return None


def _between_markers(data: bytes, begin: bytes, end: bytes) -> bytes | None:
    lines = _lines(data)
    start = next(
        (i for i, line in enumerate(lines) if line.strip(b" \t") == begin), None
    )
    if start is None:
        return None
    for index in range(start + 1, len(lines)):
        if lines[index].strip(b" \t") == end:
            return b"\n".join(lines[start + 1:index])
    return None


def _prompt_after_end(data: bytes, end: bytes) -> str | None:
    lines = _lines(data)
    end_index = next(
        (i for i, line in enumerate(lines) if line.strip(b" \t") == end), None
    )
    if end_index is None:
        return None
    tail = b"\n".join(lines[end_index + 1:]).decode("utf-8", "replace")
    if re.search(r"(?m)^\s*(Hobot>|=>)\s*$", tail):
        return STATE_UBOOT
    if re.search(r"(?m)^[^\r\n#$]{1,120}[#$]\s*$", tail):
        return STATE_LINUX_SHELL
    return None


class SessionLock:
    """Port-identity ownership lock independent of board aliases."""

    def __init__(self, locks_root: Path, port: str):
        self.identity = physical_port_identity(port)
        digest = hashlib.sha256(self.identity.encode()).hexdigest()[:32]
        self.path = Path(locks_root).expanduser() / "locks" / f"serial-{digest}.lock"
        self.fd = -1

    def acquire(self) -> None:
        secure_dir(self.path.parent)
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            self.fd = os.open(self.path, flags, 0o600)
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SessionRefused("serial ownership path is not a regular private file")
            os.fchmod(self.fd, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.release()
            raise SessionRefused(
                "another serial session owns this physical port; refusing a second holder"
            ) from None
        except OSError as exc:
            self.release()
            raise SessionRefused(f"cannot acquire serial ownership lock: {exc.strerror}") from None

    def release(self) -> None:
        if self.fd >= 0:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1


class SerialSession:
    def __init__(
        self,
        *,
        board_id: str,
        held: HeldLock,
        port: str,
        state_root: Path,
        transport: SerialTransport | None = None,
        baud: int = 115200,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_output_bytes: int = 1024 * 1024,
    ):
        if held.board_id != board_id:
            raise SessionRefused("serial session lock belongs to a different board")
        self.board_id = board_id
        self.held = held
        self.port = port
        self.state_root = Path(state_root).expanduser()
        self.transport = transport or RealSerialTransport(port, baud=baud)
        self.clock = clock
        self.sleep = sleep
        self.max_output_bytes = max_output_bytes
        self.session_id = secrets.token_hex(16)
        self.opened_at = ""
        self.state = STATE_UNKNOWN
        self._text = ""
        self._epoch = 0
        self._receipts: dict[str, CommandReceipt] = {}
        self._last_rx_mono: float | None = None
        self._countdown_mono: float | None = None
        self._countdown_epoch: int | None = None
        self._lock = SessionLock(self.state_root, port)
        self._opened = False
        self._closed = False

    # ------------------------------------------------------------------
    # lifetime / events
    # ------------------------------------------------------------------
    def open(self, *, passive_timeout: float = 1.0) -> str:
        self.held.revalidate()
        self._lock.acquire()
        try:
            self.transport.open()
            self._opened = True
            self._closed = False
            self._epoch += 1
            self._receipts.clear()
            self.opened_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            initial = self.transport.read(passive_timeout, 65536)
            self._note_rx(initial, allow_state_change=True)
            return self.state
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        try:
            if self._opened:
                self.transport.close()
        finally:
            self._opened = False
            self._closed = True
            self._epoch += 1
            self._receipts.clear()
            self._lock.release()

    def _invalidate(self, reason: str) -> None:
        self._epoch += 1
        self._receipts.clear()
        self._countdown_mono = None
        self._countdown_epoch = None

    def _note_rx(self, data: bytes, *, allow_state_change: bool = False) -> None:
        if not data:
            return
        self._last_rx_mono = self.clock()
        chunk = data.decode("utf-8", "replace")
        self._text = (self._text + chunk)[-262144:]
        now = self.clock()
        if COUNTDOWN_MARKER in chunk:
            self._countdown_mono = now
            self._countdown_epoch = self._epoch
        new_state = classify_state(self._text[-16384:])
        if new_state != STATE_UNKNOWN and new_state != self.state:
            if allow_state_change:
                self.state = new_state
            else:
                self.state = new_state
                self._invalidate("terminal state changed")

    def text(self) -> str:
        return self._text

    def text_since(self, cursor: int) -> str:
        if cursor < 0 or cursor > len(self._text):
            raise SessionRefused("invalid serial cursor")
        return self._text[cursor:]

    def cursor(self) -> int:
        return len(self._text)

    def on_power_on(self) -> None:
        """Power was restored: old prompts/receipts are no longer evidence."""
        self._epoch += 1
        self._receipts.clear()
        self.state = STATE_UNKNOWN
        self._countdown_mono = None
        self._countdown_epoch = None
        self._last_rx_mono = None

    def refresh(self, timeout: float = 0.2) -> str:
        if not self._opened:
            raise SessionRefused("session is not open")
        self.held.revalidate()
        data = self.transport.read(timeout, 65536)
        self._note_rx(data)
        return self.state

    def interrupt_autoboot(self, *, timeout: float = 2.0) -> str:
        if not self._opened:
            raise SessionRefused("serial session is not open")
        if self.state in (STATE_LINUX_LOGIN, STATE_LINUX_SHELL, STATE_LINUX_BOOT):
            raise SessionRefused("refusing to send a U-Boot interrupt to a Linux/login state")
        if self._countdown_mono is None or self._countdown_epoch != self._epoch:
            raise SessionRefused("no fresh U-Boot autoboot countdown in this terminal epoch")
        age = self.clock() - self._countdown_mono
        if age < 0 or age > COUNTDOWN_MAX_AGE:
            raise SessionRefused(
                f"autoboot countdown is stale ({age:.3f}s old); refusing blind space"
            )
        marker = self._text.rfind(COUNTDOWN_MARKER)
        tail = self._text[marker + len(COUNTDOWN_MARKER):]
        if re.search(r"(?i)(switch to partitions|UMS update|resetting \.\.\.)", tail):
            raise SessionRefused("the visible autoboot countdown already passed")
        self.held.revalidate()
        self._invalidate("autoboot interrupt")
        self.transport.write(b" ")
        seen = bytearray()
        deadline = self.clock() + max(0.1, timeout)
        while self.clock() < deadline:
            chunk = self.transport.read(min(0.2, max(0.0, deadline - self.clock())), 65536)
            if chunk:
                seen.extend(chunk)
                self._note_rx(chunk, allow_state_change=True)
                if re.search(r"(?m)^\s*(Hobot>|=>)\s*$", normalize_terminal_text(
                    seen.decode("utf-8", "replace")
                )):
                    self.state = STATE_UBOOT
                    self._countdown_mono = None
                    self._countdown_epoch = None
                    return self.state
            self.held.revalidate()
        self._invalidate("autoboot interrupt timeout")
        raise SessionTimeout("space was sent but no fresh U-Boot prompt appeared")

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    def _require_state(self, expected: tuple[str, ...], action: str) -> None:
        if not self._opened or self._closed:
            raise SessionRefused("serial session is not open")
        if self.state not in expected:
            raise SessionRefused(
                f"refusing {action} in serial state {self.state}; expected one of {', '.join(expected)}"
            )
        self.held.revalidate()

    def uboot_readonly(self, command: str, *, timeout: float = 5.0) -> CommandResult:
        command = command.strip()
        self._require_state((STATE_UBOOT,), "U-Boot command")
        if not UBOOT_READONLY.fullmatch(command):
            raise SessionRefused(
                f"U-Boot command is not in the reviewed read-only allowlist: {command}"
            )
        return self._command(command, u_boot=True, timeout=timeout)

    def uboot_safe_probe(self, name: str, *, timeout: float = 5.0) -> CommandResult:
        """Run one fixed, no-write expression probe from the reviewed set."""
        self._require_state((STATE_UBOOT,), "U-Boot safe probe")
        if name not in UBOOT_SAFE_PROBES:
            raise SessionRefused(f"unknown fixed U-Boot probe: {name}")
        return self._command(UBOOT_SAFE_PROBES[name], u_boot=True, timeout=timeout)

    def uboot_select_mmc(self, index: int, *, timeout: float = 5.0) -> CommandResult:
        """Select an already-listed MMC device for read-only info queries.

        This changes only U-Boot's current-device selector; it sends no
        storage read/write and is deliberately separate from the read-only
        allowlist so it cannot be reached accidentally.
        """
        self._require_state((STATE_UBOOT,), "MMC select")
        if not isinstance(index, int) or index < 0:
            raise SessionRefused("MMC index must be a non-negative integer")
        command = f"mmc dev {index}"
        if re.fullmatch(r"mmc dev [0-9]+", command) is None:
            raise SessionRefused("MMC select command is not allowlisted")
        return self._command(command, u_boot=True, timeout=timeout)

    def uboot_ums(self, index: int, *, timeout: float = 1.0) -> CommandResult:
        self._require_state((STATE_UBOOT,), "UMS start")
        if not isinstance(index, int) or index < 0:
            raise SessionRefused("UMS mmc index must be a non-negative integer")
        command = f"ums 0 mmc {index}"
        if not UBOOT_UMS.fullmatch(command):
            raise SessionRefused("UMS command is not allowlisted")
        nonce = secrets.token_hex(16)
        self._invalidate("UMS start")
        epoch = self._epoch
        before = self.state
        started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        issued = self.clock()
        self.held.revalidate()
        self.transport.write((command + "\n").encode())
        data = bytearray()
        deadline = self.clock() + max(0.2, timeout)
        while self.clock() < deadline:
            chunk = self.transport.read(min(0.2, max(0.0, deadline - self.clock())), 65536)
            if chunk:
                data.extend(chunk)
                self._text = (self._text + chunk.decode("utf-8", "replace"))[-262144:]
                self._last_rx_mono = self.clock()
        output = clean_terminal_output(data.decode("utf-8", "replace"))
        if "UMS" not in output or "LUN" not in output:
            self._invalidate("UMS start without gadget output")
            raise SessionProtocolError(
                "ums_start_not_confirmed",
                "UMS start produced no current gadget/LUN output; refusing UBOOT_UMS state",
            )
        receipt = CommandReceipt(
            receipt_id=secrets.token_hex(16),
            session_id=self.session_id,
            terminal_epoch=epoch,
            nonce=nonce,
            command=command,
            output=output,
            raw_sha256=hashlib.sha256(bytes(data)).hexdigest(),
            state_before=before,
            state_after=STATE_UBOOT_UMS,
            issued_mono=issued,
            completed_mono=self.clock(),
        )
        self._receipts[receipt.receipt_id] = receipt
        self.state = STATE_UBOOT_UMS
        return CommandResult(
            command=command,
            state_before=before,
            state_after=self.state,
            nonce=nonce,
            output=output,
            started_at=started,
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            bytes_read=len(data),
            receipt_id=receipt.receipt_id,
            terminal_epoch=epoch,
        )

    def uboot_stop_ums(self, *, timeout: float = 5.0) -> None:
        """Stop the U-Boot UMS gadget with Ctrl+C and wait for its prompt."""
        self._require_state((STATE_UBOOT_UMS, STATE_UBOOT), "UMS stop")
        self.held.revalidate()
        self._invalidate("UMS stop")
        self.transport.write(b"\x03")
        deadline = self.clock() + max(0.5, timeout)
        seen = bytearray()
        while self.clock() < deadline:
            chunk = self.transport.read(min(0.2, max(0.0, deadline - self.clock())), 65536)
            if chunk:
                seen.extend(chunk)
                self._note_rx(chunk, allow_state_change=True)
                fresh = normalize_terminal_text(seen.decode("utf-8", "replace"))
                # Require a prompt in bytes received *after* Ctrl+C.  An old
                # Hobot still present in self._text is not proof UMS stopped.
                if re.search(r"(?m)^\s*(Hobot>|=>)\s*$", fresh.rstrip(" \t\r\n")):
                    self.state = STATE_UBOOT
                    return
            self.held.revalidate()
        self._invalidate("UMS stop no fresh prompt")
        raise SessionTimeout("Ctrl+C did not return a fresh U-Boot prompt after UMS")

    def uboot_reset(self, *, timeout: float = 5.0) -> None:
        """Authorized recovery reset from a U-Boot prompt; no receipt/evidence."""
        self._require_state((STATE_UBOOT, STATE_UBOOT_UMS), "U-Boot reset")
        self.held.revalidate()
        self._invalidate("U-Boot reset")
        self.transport.write(b"reset\n")
        deadline = self.clock() + max(1.0, timeout)
        while self.clock() < deadline:
            chunk = self.transport.read(min(0.2, max(0.0, deadline - self.clock())), 65536)
            if chunk:
                self._note_rx(chunk)
                if COUNTDOWN_MARKER in self._text[-8192:]:
                    return
            self.held.revalidate()
        raise SessionTimeout("U-Boot reset produced no fresh autoboot countdown")

    def linux_identity(self, command: str, *, timeout: float = 5.0) -> CommandResult:
        command = command.strip()
        self._require_state((STATE_LINUX_SHELL,), "Linux command")
        if command not in LINUX_IDENTITY_COMMANDS:
            raise SessionRefused(
                f"Linux command is not in the reviewed identity allowlist: {command}"
            )
        return self._command(command, u_boot=False, timeout=timeout)

    def _command(self, command: str, *, u_boot: bool, timeout: float) -> CommandResult:
        nonce = secrets.token_hex(16)
        begin = f"__FNOS_BEGIN_{nonce}__".encode()
        end = f"__FNOS_END_{nonce}__".encode()
        if u_boot:
            wire = (
                "echo " + begin.decode() + "; " + command + "; echo "
                + end.decode() + "\n"
            ).encode()
        else:
            wire = (
                "printf '\\n" + begin.decode() + "\\n'; " + command
                + "; printf '\\n" + end.decode() + "\\n'\n"
            ).encode()
        before = self.state
        epoch = self._epoch
        started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        issued = self.clock()
        self.held.revalidate()
        try:
            self.transport.write(wire)
            data = bytearray()
            deadline = self.clock() + max(0.1, timeout)
            prompt_state: str | None = None
            while self.clock() < deadline:
                if len(data) > self.max_output_bytes:
                    raise SessionProtocolError(
                        "serial_output_too_large", "command response exceeded the review cap"
                    )
                chunk = self.transport.read(
                    min(0.2, max(0.0, deadline - self.clock())), 65536
                )
                if chunk:
                    data.extend(chunk)
                    self._note_rx(chunk)
                    prompt_state = _prompt_after_end(bytes(data), end)
                    if prompt_state is not None:
                        break
                self.held.revalidate()
            if prompt_state is None:
                raise SessionTimeout(
                    f"command {command!r} produced no fresh terminal prompt before timeout"
                )
            payload = _between_markers(bytes(data), begin, end)
            if payload is None:
                raise SessionProtocolError(
                    "serial_marker_missing",
                    "response did not contain a real begin/end marker pair",
                )
            expected_prompt = STATE_UBOOT if u_boot else STATE_LINUX_SHELL
            if prompt_state != expected_prompt:
                raise SessionProtocolError(
                    "serial_state_changed",
                    f"command ended in {prompt_state}, expected {expected_prompt}",
                )
            output = clean_terminal_output(
                payload.decode("utf-8", "replace"), command=command
            )
            completed = self.clock()
            receipt = CommandReceipt(
                receipt_id=secrets.token_hex(16),
                session_id=self.session_id,
                terminal_epoch=epoch,
                nonce=nonce,
                command=command,
                output=output,
                raw_sha256=hashlib.sha256(bytes(data)).hexdigest(),
                state_before=before,
                state_after=prompt_state,
                issued_mono=issued,
                completed_mono=completed,
            )
            if epoch != self._epoch:
                raise SessionProtocolError(
                    "serial_epoch_changed", "terminal state changed during the command"
                )
            self._receipts[receipt.receipt_id] = receipt
            self.state = prompt_state
            return CommandResult(
                command=command,
                state_before=before,
                state_after=prompt_state,
                nonce=nonce,
                output=output,
                started_at=started,
                finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                bytes_read=len(data),
                receipt_id=receipt.receipt_id,
                terminal_epoch=epoch,
            )
        except Exception:
            self._invalidate("command failure")
            raise

    def linux_normal_shutdown(self, *, timeout: float = 30.0) -> CommandResult:
        self._require_state((STATE_LINUX_SHELL,), "normal shutdown")
        nonce = secrets.token_hex(16)
        before = self.state
        epoch = self._epoch
        started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        issued = self.clock()
        self.held.revalidate()
        self.transport.write(b"sync; poweroff\n")
        data = bytearray()
        deadline = self.clock() + max(1.0, timeout)
        while self.clock() < deadline:
            chunk = self.transport.read(min(0.2, max(0.0, deadline - self.clock())), 65536)
            if chunk:
                data.extend(chunk)
                self._text = (self._text + chunk.decode("utf-8", "replace"))[-262144:]
                self._last_rx_mono = self.clock()
                if POWERDOWN_RE.search(normalize_terminal_text(
                    data.decode("utf-8", "replace")
                )):
                    break
            self.held.revalidate()
        output = clean_terminal_output(data.decode("utf-8", "replace"))
        if not POWERDOWN_RE.search(output):
            self._invalidate("poweroff transcript missing")
            raise SessionTimeout("no fresh power-down transcript")
        receipt = CommandReceipt(
            receipt_id=secrets.token_hex(16),
            session_id=self.session_id,
            terminal_epoch=epoch,
            nonce=nonce,
            command="sync; poweroff",
            output=output,
            raw_sha256=hashlib.sha256(bytes(data)).hexdigest(),
            state_before=before,
            state_after=STATE_LINUX_BOOT,
            issued_mono=issued,
            completed_mono=self.clock(),
        )
        self._receipts[receipt.receipt_id] = receipt
        self.state = STATE_LINUX_BOOT
        return CommandResult(
            command="sync; poweroff",
            state_before=before,
            state_after=self.state,
            nonce=nonce,
            output=output,
            started_at=started,
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            bytes_read=len(data),
            receipt_id=receipt.receipt_id,
            terminal_epoch=epoch,
        )

    # ------------------------------------------------------------------
    # receipt / evidence helpers
    # ------------------------------------------------------------------
    def receipt(self, receipt_id: str) -> CommandReceipt:
        receipt = self._receipts.get(receipt_id)
        if receipt is None:
            raise SessionRefused("command receipt is not registered in this session")
        return receipt

    def validate_receipt(
        self,
        receipt_id: str,
        *,
        terminal_epoch: int,
        expected_state: str,
        allow_poweroff: bool = False,
    ) -> CommandReceipt:
        if not self._opened or self._closed:
            raise SessionRefused("serial session is closed; evidence is stale")
        receipt = self.receipt(receipt_id)
        if receipt.session_id != self.session_id:
            raise SessionRefused("receipt belongs to a different serial session")
        if receipt.terminal_epoch != terminal_epoch or terminal_epoch != self._epoch:
            raise SessionRefused("receipt/terminal epoch is stale")
        if receipt.state_after != expected_state:
            raise SessionRefused(
                f"receipt terminal state {receipt.state_after} != {expected_state}"
            )
        if allow_poweroff:
            if receipt.command != "sync; poweroff":
                raise SessionRefused("poweroff receipt is not a normal shutdown command")
        else:
            if self.state != expected_state:
                raise SessionRefused(
                    f"current terminal state {self.state} changed since the receipt"
                )
        if self._last_rx_mono is not None and receipt.completed_mono > self._last_rx_mono + 0.001:
            raise SessionRefused("receipt claims a future completion time")
        return receipt

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
