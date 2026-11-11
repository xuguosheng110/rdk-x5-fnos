"""CP5 USB-group power backend and power policy.

Only the already-confirmed ``usb`` group (SIID 7 / PIID 1) is allowed.  The
master switch and the four AC channels are not addressable from this backend.
A timeout is *unknown*, never success.  Off is refused during writing, flush,
read-back, boot validation and first-boot rootfs expansion.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .authorization import require_real_gate
from .errors import PowerUnknown, Refused
from .locks import HeldLock
from .model import POWER_OFF_FORBIDDEN

USB_SIID = 7
USB_PIID = 1
USB_PORT = "usb"
USB_MODELS = ("cuco.plug.cp5", "cuco.plug.cp5d")


class PowerClientError(RuntimeError):
    """A client error whose message is safe to persist (no credentials)."""


class Cp5Client(Protocol):
    def read_usb(self) -> bool: ...

    def set_usb(self, value: bool) -> None: ...


@dataclass
class FakeCp5Client:
    """File-free CP5 substitute used only by tests and ``--backends fake``."""

    value: bool = True
    read_error: Exception | None = None
    write_error: Exception | None = None
    sticky_on_error: bool = False
    no_confirm: bool = False
    write_delay: float = 0.0

    def __post_init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reads: list[bool] = []

    def read_usb(self) -> bool:
        if self.read_error is not None:
            raise self.read_error
        self.reads.append(bool(self.value))
        return bool(self.value)

    def set_usb(self, value: bool) -> None:
        if not isinstance(value, bool):
            raise PowerClientError("CP5 write value must be a boolean")
        # Record the intent before a possible failure: the command may have
        # reached the cloud even when the response is lost.
        self.calls.append({"siid": USB_SIID, "piid": USB_PIID, "value": value})
        if self.write_delay:
            time.sleep(self.write_delay)
        if self.write_error is not None:
            if not self.sticky_on_error:
                self.value = value
            raise self.write_error
        if not self.no_confirm:
            self.value = value

    def planned_cut(self) -> None:
        """Fake-only: simulate the authorized test power loss."""
        self.value = False


class Cp5PowerBackend:
    """Policy-enforcing wrapper around a reviewed CP5 USB client."""

    name = "cp5-usb"

    def __init__(
        self,
        client: Cp5Client,
        *,
        board_id: str,
        wait_seconds: float = 20.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not isinstance(board_id, str) or not board_id:
            raise Refused("CP5 backend requires a board_id")
        self.client = client
        self.board_id = board_id
        self.wait_seconds = wait_seconds
        self.sleep = sleep

    def read(self) -> str:
        try:
            value = self.client.read_usb()
        except Exception as exc:
            raise PowerUnknown(
                "CP5 USB status could not be read; treating power state as unknown "
                f"({type(exc).__name__})",
                action="read",
            ) from None
        if type(value) is not bool:
            raise PowerUnknown("CP5 USB status was not a boolean; state is unknown")
        return "on" if value else "off"

    def set(self, value: bool) -> None:
        if type(value) is not bool:
            raise Refused("CP5 power action must be an explicit boolean on/off")
        try:
            self.client.set_usb(value)
        except Exception as exc:
            # No retry and no toggle: the command may have been sent.
            raise PowerUnknown(
                f"CP5 USB {'on' if value else 'off'} result is unknown "
                f"({type(exc).__name__}); query state before any retry",
                action="on" if value else "off",
            ) from None

    def verify(self, expected: bool, *, deadline: float | None = None) -> str:
        if type(expected) is not bool:
            raise Refused("CP5 state verification requires an explicit boolean")
        end = time.monotonic() + (
            self.wait_seconds if deadline is None else max(0.0, deadline)
        )
        last = "unknown"
        while True:
            last = self.read()
            if last == ("on" if expected else "off"):
                return last
            if time.monotonic() >= end:
                raise PowerUnknown(
                    f"CP5 USB state was not confirmed {'on' if expected else 'off'} "
                    "before the deadline; result is unknown",
                    action="verify",
                )
            self.sleep(0.05)


class Cp5CliClient:
    """Real CP5 client that calls the reviewed canonical CLI.

    The parent passes its already-held unified board lock fd.  The child adopts
    the capability and never re-acquires the same lock, so a parent-held lock
    cannot self-deadlock.  No credential value is read, printed or copied here.
    """

    def __init__(
        self,
        script: Path,
        *,
        board_id: str,
        lock: HeldLock,
        python: str | None = None,
        timeout: float = 45.0,
        extra_env: dict[str, str] | None = None,
    ):
        require_real_gate()
        raw_script = Path(script).expanduser()
        try:
            # The repository's canonical CP5 script is intentionally a symlink
            # to the single skill source; follow it and require a regular file.
            self.script = raw_script.resolve(strict=True)
        except OSError:
            raise Refused("CP5 CLI script is missing or unresolvable") from None
        if not self.script.is_file():
            raise Refused("CP5 CLI script is not a regular file")
        self.board_id = board_id
        self.lock = lock
        default_python = Path.home() / ".local/share/mijia-cp5/venv/bin/python"
        self.python = (
            python
            or os.environ.get("FNOS_LAB_CP5_PYTHON")
            or (str(default_python) if default_python.is_file() else "python3")
        )
        self.timeout = timeout
        self.extra_env = dict(extra_env or {})
        if lock.board_id != board_id:
            raise Refused("CP5 client lock belongs to a different board")

    def _run(self, action: str, extra: tuple[str, ...] = ()) -> dict[str, Any]:
        # The child inherits the same open file description.  It must not
        # acquire a second independent lock; cp5.py adopts FNOS_BOARD_LOCK_FD.
        env = {
            **os.environ,
            **self.extra_env,
            **self.lock.child_env(),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        argv = [
            self.python,
            str(self.script),
            action,
            "--board",
            self.board_id,
            "--lock-fd",
            str(self.lock.fd),
            *extra,
        ]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
                pass_fds=self.lock.pass_fds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PowerClientError(
                f"CP5 child did not complete ({type(exc).__name__})"
            ) from None
        if proc.returncode != 0:
            # The CP5 CLI already redacts credential-bearing details.  Keep
            # only one short, non-secret status line.
            detail = (proc.stderr or "").strip().splitlines()
            summary = detail[-1][:200] if detail else f"exit {proc.returncode}"
            raise PowerClientError(f"CP5 child failed: {summary}")
        try:
            value = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise PowerClientError("CP5 child returned invalid JSON") from None
        if not isinstance(value, dict):
            raise PowerClientError("CP5 child returned a non-object response")
        return value

    def read_usb(self) -> bool:
        result = self._run("usb-status")
        state = result.get("reported_state")
        if type(state) is not bool:
            raise PowerClientError("CP5 USB status response lacks a boolean state")
        return state

    def set_usb(self, value: bool) -> None:
        if type(value) is not bool:
            raise PowerClientError("CP5 write value must be a boolean")
        action = "usb-on" if value else "usb-off"
        extra = () if value else ("--load-ready",)
        result = self._run(action, extra)
        state = result.get("reported_state")
        if type(state) is not bool or state is not value:
            raise PowerClientError(
                "CP5 USB command did not report the requested state; result is unknown"
            )


@dataclass
class PowerEvent:
    action: str
    result: str
    state: str
    origin: str
    phase: str
    channel: str = USB_PORT


class PowerManager:
    """Applies the phase policy before every explicit on/off."""

    def __init__(
        self,
        backend: Cp5PowerBackend,
        *,
        phase: Callable[[], str],
        emit: Callable[..., None],
        save: Callable[[], None],
        first_boot_expansion: Callable[[], bool] = lambda: False,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.backend = backend
        self.phase = phase
        self.emit = emit
        self.save = save
        self.first_boot_expansion = first_boot_expansion
        self.sleep = sleep

    def _record(self, action: str, result: str, state: str, origin: str) -> None:
        self.emit(
            "power_action",
            action=action,
            result=result,
            origin=origin,
            phase=self.phase(),
            channel=USB_PORT,
        )
        self.save()

    def read(self) -> str:
        return self.backend.read()

    def turn_off(
        self,
        origin: str,
        *,
        kind: str = "operation",
        safe_to_cut: bool = False,
        cut_validator: Callable[[], bool] | None = None,
    ) -> str:
        current_phase = self.phase()
        if kind == "cancel":
            raise Refused("cancel never cuts power; it only requests a safe boundary")
        if current_phase in POWER_OFF_FORBIDDEN:
            self.emit(
                "power_off_refused",
                phase=current_phase,
                origin=origin,
                operation=kind,
                reason="power-off is forbidden during writing/read-back/boot/validation",
            )
            self.save()
            raise Refused(
                f"power-off refused in phase {current_phase}; writing/read-back/boot "
                "must stay powered"
            )
        if self.first_boot_expansion():
            self.emit(
                "power_off_refused",
                phase=current_phase,
                origin=origin,
                operation=kind,
                reason="first-boot rootfs expansion must not be interrupted",
            )
            self.save()
            raise Refused(
                "power-off refused during first-boot rootfs expansion; keep power"
            )
        if kind not in ("operation", "normal_shutdown"):
            raise Refused(f"unknown power operation kind: {kind}")
        if safe_to_cut is not True:
            self.emit(
                "power_cut_refused",
                phase=current_phase,
                origin=origin,
                operation=kind,
                reason="no live safe-to-cut evidence; refusing OFF",
            )
            self.save()
            raise Refused(
                "safe-to-cut evidence is required before any power-off"
            )
        if cut_validator is not None:
            try:
                still_valid = cut_validator() is True
            except Exception as exc:
                still_valid = False
                self.emit(
                    "power_cut_refused",
                    phase=current_phase,
                    origin=origin,
                    operation=kind,
                    reason=f"safe-cut revalidation failed: {type(exc).__name__}",
                )
            if not still_valid:
                self.emit(
                    "power_cut_refused",
                    phase=current_phase,
                    origin=origin,
                    operation=kind,
                    reason="safe-cut evidence failed revalidation immediately before OFF",
                )
                self.save()
                raise Refused(
                    "safe-cut evidence failed revalidation immediately before OFF"
                )
        if kind == "normal_shutdown":
            # These are recorded as distinct operations.  The actual board
            # shutdown/planned-fault path is never a generic cycle.
            self.emit("power_operation_selected", operation=kind, origin=origin)
        state = self.backend.read()
        if state == "off":
            self._record("off", "already_reported", state, origin)
            return state
        if state != "on":
            raise PowerUnknown("power state is unknown before an OFF request")
        self.backend.set(False)
        state = self.backend.verify(False)
        self._record("off", "confirmed", state, origin)
        return state

    def turn_on(self, origin: str) -> str:
        state = self.backend.read()
        if state == "on":
            self._record("on", "already_reported", state, origin)
            return state
        if state != "off":
            raise PowerUnknown("power state is unknown before an ON request")
        self.backend.set(True)
        state = self.backend.verify(True)
        self._record("on", "confirmed", state, origin)
        return state

    def recover_unknown_off_once(self) -> bool:
        """After an unknown OFF, try one explicit ON and verify it.

        Never retries OFF.  A failed recovery leaves the state unknown and
        requires manual inspection.
        """
        self.emit("power_recovery_started", attempts=1)
        self.save()
        try:
            self.backend.set(True)
            self.backend.verify(True)
        except PowerUnknown:
            self.emit("power_recovery_failed", attempts=1)
            self.save()
            return False
        self.emit("power_recovery_succeeded", attempts=1)
        self.save()
        return True
