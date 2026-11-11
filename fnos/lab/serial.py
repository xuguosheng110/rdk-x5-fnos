"""Passive serial listener with per-boot evidence boundaries.

The real listener is read-only: it opens the callout device non-blocking and
never writes a byte.  Entering U-Boot/UMS by typing commands is intentionally
not implemented in this batch.  Tests use ``FakeSerialCapture`` only.

Boot evidence is *not* "the buffer contains a kernel banner".  A cursor is
taken immediately before the cold-boot power action; only bytes appended after
that cursor count.  A complete fake record is still explicitly fake.  The real
listener cannot verify boot_id/rootfs in this batch and therefore never reports
a complete real boot validation.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import select
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .authorization import require_real_gate
from .errors import HardwareFailure, Refused

BOOT_PATTERN = r"(?i)(login:|Linux version|systemd|(?<![A-Za-z0-9_])fnOS(?![A-Za-z0-9_]))"
KERNEL_PATTERN = re.compile(r"Linux version\s+([^\s]+)")
LOGIN_PATTERN = re.compile(r"(?i)(login:|fnOS login)")


class SerialFailure(HardwareFailure):
    def __init__(self, code: str, message: str):
        super().__init__(code, message)


@dataclass(frozen=True)
class BootEvidence:
    new_bytes: int
    kernel_version: str | None
    login_prompt: bool
    boot_id: str | None
    rootfs_mounted: bool
    complete: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "new_bytes": self.new_bytes,
            "kernel_version": self.kernel_version,
            "login_prompt": self.login_prompt,
            "boot_id_verified": self.boot_id is not None,
            "rootfs_verified": self.rootfs_mounted,
            "complete": self.complete,
            "reason": self.reason,
        }


class SerialCapture:
    def start(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def wait_for(
        self, pattern: str, timeout: float, since: int | None = None
    ) -> bool:  # pragma: no cover
        raise NotImplementedError

    def cursor(self) -> int:  # pragma: no cover - interface
        raise NotImplementedError

    def wait_for_new(self, pattern: str, timeout: float, since: int) -> bool:
        raise NotImplementedError

    def boot_evidence(
        self, since: int, *, expected_kernel: str | None = None
    ) -> BootEvidence:  # pragma: no cover - interface
        raise NotImplementedError

    def on_power_on(self) -> None:
        return None

    @property
    def cut_safe(self) -> bool:
        return False

    def text(self) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class FakeSerialCapture(SerialCapture):
    """Deterministic listener substitute; no device is opened.

    ``lines`` are pre-existing/stale log lines.  ``boot_lines`` are appended
    only when a power-on action occurs, so they can be distinguished from old
    logs by the runner's cursor.  ``boot_record`` is fake-only structured
    evidence for boot_id/rootfs.
    """

    def __init__(
        self,
        *,
        lines: list[str] | None = None,
        boot_lines: list[str] | None = None,
        boot_record: dict[str, Any] | None = None,
        cut_safe: bool | None = None,
        ready: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.lines = list(lines or [])
        self._initial_lines = list(self.lines)
        self.boot_lines = list(boot_lines or [])
        self.boot_record = dict(boot_record) if isinstance(boot_record, dict) else None
        self._cut_safe = cut_safe
        self.ready = ready
        self.started = False
        self.stopped = False
        self.start_calls = 0
        self.power_on_count = 0
        self.clock = clock

    def start(self) -> None:
        if not self.ready:
            raise SerialFailure("serial_unavailable", "fake serial listener is not available")
        self.started = True
        self.start_calls += 1

    def stop(self) -> None:
        self.stopped = True

    def text(self) -> str:
        return "\n".join(self.lines)

    def cursor(self) -> int:
        return len(self.lines)

    def on_power_on(self) -> None:
        self.power_on_count += 1
        if self.boot_lines:
            self.lines.extend(self.boot_lines)

    @property
    def cut_safe(self) -> bool:
        # Explicit injection only.  A pre-existing "Hobot>" line may be stale
        # and is not evidence that the board is currently safe to cut.
        return self._cut_safe is True

    def wait_for(
        self, pattern: str, timeout: float, since: int | None = None
    ) -> bool:
        if not self.started:
            raise SerialFailure("serial_not_started", "serial listener was not started")
        start = len(self.lines) if since is None else since
        return self.wait_for_new(pattern, timeout, start)

    def wait_for_new(self, pattern: str, timeout: float, since: int) -> bool:
        if not self.started:
            raise SerialFailure("serial_not_started", "serial listener was not started")
        if since < 0 or since > len(self.lines):
            raise SerialFailure("serial_cursor_invalid", "invalid serial cursor")
        return any(re.search(pattern, line) for line in self.lines[since:])

    def boot_evidence(
        self, since: int, *, expected_kernel: str | None = None
    ) -> BootEvidence:
        if since < 0 or since > len(self.lines):
            raise SerialFailure("serial_cursor_invalid", "invalid serial cursor")
        new_lines = self.lines[since:]
        text = "\n".join(new_lines)
        new_bytes = len(text.encode("utf-8"))
        match = KERNEL_PATTERN.search(text)
        kernel = match.group(1) if match else None
        login = bool(LOGIN_PATTERN.search(text))
        record = self.boot_record if (new_lines and isinstance(self.boot_record, dict)) else {}
        boot_id = record.get("boot_id") if isinstance(record.get("boot_id"), str) else None
        rootfs = record.get("rootfs") is True
        reason = "ok"
        if new_bytes <= 0:
            reason = "no bytes captured after the cold-boot cursor"
            complete = False
        elif kernel is None:
            reason = "no new kernel banner after the cold-boot cursor"
            complete = False
        elif expected_kernel and kernel != expected_kernel:
            reason = f"kernel {kernel} does not match expected {expected_kernel}"
            complete = False
        elif not login:
            reason = "no new login prompt after the cold-boot cursor"
            complete = False
        elif not record:
            reason = "boot_id/rootfs verification is not implemented for this run"
            complete = False
        elif not boot_id:
            reason = "new fake evidence has no boot_id"
            complete = False
        elif not rootfs:
            reason = "new fake evidence does not prove rootfs mount"
            complete = False
        else:
            complete = True
        return BootEvidence(
            new_bytes=new_bytes,
            kernel_version=kernel,
            login_prompt=login,
            boot_id=boot_id,
            rootfs_mounted=rootfs,
            complete=complete,
            reason=reason,
        )


class PosixSerialCapture(SerialCapture):
    """Read-only POSIX serial capture (8N1 by default).  Never sends bytes."""

    def __init__(
        self,
        port: str,
        *,
        baud: int = 115200,
        clock: Callable[[], float] = time.monotonic,
    ):
        require_real_gate()
        if not isinstance(port, str) or not port.startswith("/dev/"):
            raise Refused("serial port must be an absolute /dev/... path")
        self.port = port
        self.baud = baud
        self.clock = clock
        self._fd = -1
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._buffer = bytearray()

    def start(self) -> None:
        if self._fd >= 0:
            return
        try:
            fd = os.open(
                self.port,
                os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK | os.O_NOFOLLOW,
            )
        except OSError as exc:
            raise SerialFailure(
                "serial_unavailable", f"cannot open serial port: {exc.strerror}"
            ) from None
        try:
            import termios

            attrs = termios.tcgetattr(fd)
            speed = getattr(termios, f"B{self.baud}", termios.B115200)
            attrs[0] = 0
            attrs[1] = 0
            attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
            attrs[3] = 0
            attrs[4] = speed
            attrs[5] = speed
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception as exc:
            os.close(fd)
            raise SerialFailure(
                "serial_config_failed",
                f"cannot configure serial port ({type(exc).__name__})",
            ) from None
        self._fd = fd
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._reader, name="fnos-lab-serial", daemon=True
        )
        self._thread.start()

    def _reader(self) -> None:
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.2)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            try:
                data = os.read(self._fd, 4096)
            except OSError:
                return
            if not data:
                continue
            with self._lock:
                self._buffer.extend(data)

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2)
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1

    def text(self) -> str:
        with self._lock:
            return self._buffer.decode("utf-8", errors="replace")

    def _text_since(self, since: int) -> str:
        with self._lock:
            if since < 0 or since > len(self._buffer):
                raise SerialFailure("serial_cursor_invalid", "invalid serial cursor")
            return self._buffer[since:].decode("utf-8", errors="replace")

    def cursor(self) -> int:
        with self._lock:
            return len(self._buffer)

    def wait_for(
        self, pattern: str, timeout: float, since: int | None = None
    ) -> bool:
        if self._fd < 0:
            raise SerialFailure("serial_not_started", "serial listener was not started")
        start = self.cursor() if since is None else since
        return self.wait_for_new(pattern, timeout, start)

    def wait_for_new(self, pattern: str, timeout: float, since: int) -> bool:
        if self._fd < 0:
            raise SerialFailure("serial_not_started", "serial listener was not started")
        deadline = self.clock() + max(0.0, timeout)
        while True:
            if re.search(pattern, self._text_since(since)):
                return True
            if self.clock() >= deadline:
                return False
            time.sleep(0.05)

    def boot_evidence(
        self, since: int, *, expected_kernel: str | None = None
    ) -> BootEvidence:
        text = self._text_since(since)
        new_bytes = len(text.encode("utf-8"))
        match = KERNEL_PATTERN.search(text)
        kernel = match.group(1) if match else None
        login = bool(LOGIN_PATTERN.search(text))
        reason = "ok"
        if new_bytes <= 0:
            reason = "no bytes captured after the cold-boot cursor"
        elif kernel is None:
            reason = "no new kernel banner after the cold-boot cursor"
        elif expected_kernel and kernel != expected_kernel:
            reason = f"kernel {kernel} does not match expected {expected_kernel}"
        elif not login:
            reason = "no new login prompt after the cold-boot cursor"
        else:
            reason = "real boot_id/rootfs verification is not implemented in this batch"
        # No real boot_id/rootfs check exists in this batch; never claim complete.
        return BootEvidence(
            new_bytes=new_bytes,
            kernel_version=kernel,
            login_prompt=login,
            boot_id=None,
            rootfs_mounted=False,
            complete=False,
            reason=reason,
        )

    @property
    def cut_safe(self) -> bool:
        # No live U-Boot prompt or completed sync/poweroff evidence path is
        # implemented; real power cuts must fail closed.
        return False


def start_before_power(
    capture: SerialCapture,
    *,
    emit: Callable[..., None],
    port: str,
) -> None:
    """Start the listener and prove it started *before* any power action."""
    capture.start()
    if not getattr(capture, "started", True):
        raise SerialFailure("serial_not_started", "serial listener did not report ready")
    emit("serial_listen_started", port=port, read_only=True)
    if not hasattr(capture, "started"):
        emit("serial_listener_ready", port=port)


class SessionSerialCapture(SerialCapture):
    """One owned ``SerialSession`` used for both passive capture and commands."""

    def __init__(self, session, *, clock: Callable[[], float] = time.monotonic):
        self.session = session
        self.clock = clock
        self.started = False

    def start(self) -> None:
        self.session.open()
        self.started = True

    def stop(self) -> None:
        self.started = False
        self.session.close()

    def on_power_on(self) -> None:
        self.session.on_power_on()

    def text(self) -> str:
        return self.session.text()

    def cursor(self) -> int:
        return self.session.cursor()

    def wait_for_new(self, pattern: str, timeout: float, since: int) -> bool:
        if not self.started:
            raise SerialFailure("serial_not_started", "serial session was not started")
        deadline = self.clock() + max(0.0, timeout)
        while True:
            if re.search(pattern, self.session.text_since(since)):
                return True
            if self.clock() >= deadline:
                return False
            self.session.refresh(0.1)
            time.sleep(0.02)

    def boot_evidence(
        self, since: int, *, expected_kernel: str | None = None
    ) -> BootEvidence:
        text = self.session.text_since(since)
        new_bytes = len(text.encode("utf-8"))
        match = KERNEL_PATTERN.search(text)
        kernel = match.group(1) if match else None
        login = bool(LOGIN_PATTERN.search(text))
        if new_bytes <= 0:
            reason = "no bytes captured after the cold-boot cursor"
        elif kernel is None:
            reason = "no new kernel banner after the cold-boot cursor"
        elif expected_kernel and kernel != expected_kernel:
            reason = f"kernel {kernel} does not match expected {expected_kernel}"
        elif not login:
            reason = "no new login prompt after the cold-boot cursor"
        else:
            reason = "passive boot stage; receipt-bound runtime identity is authoritative"
        return BootEvidence(
            new_bytes=new_bytes,
            kernel_version=kernel,
            login_prompt=login,
            boot_id=None,
            rootfs_mounted=False,
            complete=False,
            reason=reason,
        )

    @property
    def cut_safe(self) -> bool:
        return False
