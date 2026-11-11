"""Layered runtime identity from a current, receipt-bound Linux terminal.

``/etc/os-release`` describes the system base (the real candidate is Debian
12).  fnOS product identity is separate: it must come from the actual trim
package/version file or source-identity marker in the running image.  Kernel
banners, login prompts, a cached prior boot, or a forged os-release ID are not
identity.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from .errors import HardwareFailure, Refused
from .uart_session import (
    CommandResult,
    SerialSession,
    STATE_LINUX_SHELL,
    clean_terminal_output,
)

BOOT_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
UUID_RE = BOOT_ID_RE
OS_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
TRIM_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z._-]+)?$")
SOURCE_VERSION_RE = re.compile(
    r"(?i)(?:^|\s)fnOS\b.*?\b(\d+\.\d+\.\d+)\b"
)
PRODUCT_MARKER_RE = re.compile(r"(?im)^\s*source_image\s*=\s*(.+)$")


@dataclass(frozen=True)
class IdentityExpectation:
    board_model_contains: str = "RDK X5"
    base_os_id: str | None = None
    base_os_version: str | None = None
    kernel_release: str | None = None
    fnos_id: str | None = None
    fnos_version: str | None = None
    root_uuid: str | None = None
    boot_id: str | None = None
    require_serial: bool = False
    require_version_file: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "board_model_contains": self.board_model_contains,
            "base_os_id": self.base_os_id,
            "base_os_version": self.base_os_version,
            "kernel_release": self.kernel_release,
            "fnos_id": self.fnos_id,
            "fnos_version": self.fnos_version,
            "root_uuid": self.root_uuid,
            "boot_id": self.boot_id,
            "require_serial": self.require_serial,
            "require_version_file": self.require_version_file,
        }


@dataclass(frozen=True)
class RuntimeIdentity:
    board_model: str
    board_compatible: str
    board_serial: str | None
    base_os_id: str
    base_os_version: str
    kernel_release: str
    boot_id: str
    root_source: str
    root_uuid: str
    fnos_id: str
    fnos_name: str
    fnos_version: str
    product_source: str
    product_source_kind: str
    product_evidence: str
    collected_at: str
    session_id: str
    source: str
    command_output_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "board_model": self.board_model,
            "board_compatible": self.board_compatible,
            "board_serial_present": self.board_serial is not None,
            "base_os_id": self.base_os_id,
            "base_os_version": self.base_os_version,
            "kernel_release": self.kernel_release,
            "boot_id": self.boot_id,
            "root_source": self.root_source,
            "root_uuid": self.root_uuid,
            "fnos_id": self.fnos_id,
            "fnos_name": self.fnos_name,
            "fnos_version": self.fnos_version,
            "product_source": self.product_source,
            "product_source_kind": self.product_source_kind,
            "product_evidence_sha256": hashlib.sha256(
                self.product_evidence.encode("utf-8", "replace")
            ).hexdigest(),
            "collected_at": self.collected_at,
            "session_id": self.session_id,
            "source": self.source,
            "command_output_sha256": self.command_output_sha256,
        }


def _first_line(text: str) -> str:
    for line in clean_terminal_output(text).split("\n"):
        line = line.strip()
        if line:
            return line
    return ""


def parse_os_release(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in clean_terminal_output(text).split("\n"):
        match = OS_RE.match(line.strip())
        if match:
            values[match.group(1)] = match.group(2).strip().strip('"')
    return values


def _parse_product(version_text: str, source_text: str, trim_text: str, trim_status_text: str) -> tuple[str, str, str, str]:
    version_clean = _first_line(
        clean_terminal_output(version_text, command="cat /usr/trim/etc/version")
    )
    trim_clean = _first_line(
        clean_terminal_output(trim_text, command="dpkg-query -W -f='${Version}' trim")
    )
    trim_status = _first_line(
        clean_terminal_output(trim_status_text, command="dpkg-query -W -f='${Status}' trim")
    )
    if trim_status != "install ok installed":
        raise HardwareFailure(
            "wrong_fnos_product_identity",
            f"trim package is not installed: {trim_status!r}",
        )
    source_clean = clean_terminal_output(source_text)
    source_line = ""
    for line in source_clean.split("\n"):
        match = PRODUCT_MARKER_RE.match(line)
        if match:
            source_line = match.group(1).strip()
            break
    if version_clean:
        if not TRIM_VERSION_RE.fullmatch(version_clean):
            raise HardwareFailure(
                "wrong_fnos_product_identity",
                f"/usr/trim/etc/version is malformed: {version_clean!r}",
            )
        product_version = version_clean
        product_source = version_clean
        source_kind = "trim-version-file"
    elif source_line:
        match = SOURCE_VERSION_RE.search(source_line)
        if not match:
            raise HardwareFailure(
                "wrong_fnos_product_identity",
                f"product source marker has no trim version: {source_line!r}",
            )
        product_version = match.group(1)
        product_source = source_line
        source_kind = "source-identity-fallback"
    else:
        raise HardwareFailure(
            "wrong_fnos_product_identity",
            "no /usr/trim/etc/version and no source-identity fallback",
        )
    if not trim_clean:
        raise HardwareFailure(
            "wrong_fnos_product_identity",
            "trim package version is missing",
        )
    if not TRIM_VERSION_RE.fullmatch(trim_clean):
        raise HardwareFailure(
            "wrong_fnos_product_identity",
            f"trim package version is malformed: {trim_clean!r}",
        )
    if trim_clean != product_version:
        raise HardwareFailure(
            "wrong_fnos_product_identity",
            f"trim package {trim_clean!r} != product version {product_version!r}",
        )
    return "fnos", product_version, product_source, source_kind


def parse_runtime_outputs(
    outputs: dict[str, str],
    *,
    session_id: str,
    source: str,
    collected_at: str,
) -> RuntimeIdentity:
    """Parse raw or cleaned terminal command outputs; no banner fallback."""
    clean = {
        key: clean_terminal_output(value, command=key)
        for key, value in outputs.items()
    }
    model = _first_line(clean.get("cat /proc/device-tree/model", ""))
    compatible = " ".join(
        clean.get("cat /proc/device-tree/compatible", "").replace("\x00", " ").split()
    )
    serial_raw = clean.get("cat /proc/device-tree/serial-number", "").strip()
    board_serial = serial_raw if serial_raw and "No such file" not in serial_raw else None
    kernel = _first_line(clean.get("uname -r", ""))
    boot_id = _first_line(clean.get("cat /proc/sys/kernel/random/boot_id", ""))
    findmnt = _first_line(clean.get("findmnt -n -o SOURCE,UUID /", ""))
    parts = findmnt.split()
    root_source = parts[0] if parts else ""
    root_uuid = parts[1] if len(parts) > 1 else ""
    os_release = parse_os_release(outputs.get("cat /etc/os-release", ""))
    base_os_id = os_release.get("ID", "")
    base_os_version = os_release.get("VERSION_ID", "")
    fnos_id, fnos_version, product_source, product_source_kind = _parse_product(
        outputs.get("cat /usr/trim/etc/version", ""),
        outputs.get("cat /etc/fnos-x5/source-identity.txt", ""),
        outputs.get("dpkg-query -W -f='${Version}' trim", ""),
        outputs.get("dpkg-query -W -f='${Status}' trim", ""),
    )
    if not model:
        raise HardwareFailure("identity_incomplete", "device-tree model is missing")
    if "RDK X5" not in model:
        raise HardwareFailure("wrong_board_identity", f"device-tree model is not RDK X5: {model!r}")
    if not base_os_id or not base_os_version:
        raise HardwareFailure("identity_incomplete", "base os-release ID/VERSION_ID is missing")
    if not kernel:
        raise HardwareFailure("identity_incomplete", "kernelrelease is missing")
    if not BOOT_ID_RE.fullmatch(boot_id):
        raise HardwareFailure("identity_incomplete", "boot_id is missing or malformed")
    if not root_source.startswith("/dev/"):
        raise HardwareFailure("identity_incomplete", f"root source is invalid: {root_source!r}")
    if not UUID_RE.fullmatch(root_uuid):
        raise HardwareFailure("identity_incomplete", f"root UUID is invalid: {root_uuid!r}")
    digest = hashlib.sha256(
        json.dumps(outputs, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return RuntimeIdentity(
        board_model=model,
        board_compatible=compatible,
        board_serial=board_serial,
        base_os_id=base_os_id,
        base_os_version=base_os_version,
        kernel_release=kernel,
        boot_id=boot_id,
        root_source=root_source,
        root_uuid=root_uuid,
        fnos_id=fnos_id,
        fnos_name="fnOS",
        fnos_version=fnos_version,
        product_source=product_source,
        product_source_kind=product_source_kind,
        product_evidence=product_source,
        collected_at=collected_at,
        session_id=session_id,
        source=source,
        command_output_sha256=digest,
    )


def collect_runtime_identity(
    session: SerialSession,
    *,
    source: str = "serial",
) -> RuntimeIdentity:
    if session.state != STATE_LINUX_SHELL:
        raise Refused(
            f"runtime identity requires a logged-in Linux shell, current session state is {session.state}"
        )
    commands = (
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
    outputs: dict[str, str] = {}
    for command in commands:
        result: CommandResult = session.linux_identity(command)
        outputs[command] = result.output
    import time

    return parse_runtime_outputs(
        outputs,
        session_id=session.session_id,
        source=source,
        collected_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    )


def verify_identity(
    identity: RuntimeIdentity, expectation: IdentityExpectation
) -> tuple[bool, list[str]]:
    failures: list[str] = []
    if expectation.board_model_contains not in identity.board_model:
        failures.append(
            f"board model {identity.board_model!r} does not contain {expectation.board_model_contains!r}"
        )
    if expectation.base_os_id and identity.base_os_id.lower() != expectation.base_os_id.lower():
        failures.append(f"base OS ID {identity.base_os_id!r} != {expectation.base_os_id!r}")
    if expectation.base_os_version and identity.base_os_version != expectation.base_os_version:
        failures.append(
            f"base OS version {identity.base_os_version!r} != {expectation.base_os_version!r}"
        )
    if expectation.kernel_release and identity.kernel_release != expectation.kernel_release:
        failures.append(
            f"kernelrelease {identity.kernel_release!r} != expected {expectation.kernel_release!r}"
        )
    if expectation.fnos_id and identity.fnos_id.lower() != expectation.fnos_id.lower():
        failures.append(f"fnOS product ID {identity.fnos_id!r} != expected {expectation.fnos_id!r}")
    if expectation.fnos_version and identity.fnos_version != expectation.fnos_version:
        failures.append(
            f"fnOS product version {identity.fnos_version!r} != expected {expectation.fnos_version!r}"
        )
    if expectation.root_uuid and identity.root_uuid != expectation.root_uuid:
        failures.append(
            f"root UUID {identity.root_uuid!r} != expected {expectation.root_uuid!r}"
        )
    if expectation.boot_id and identity.boot_id != expectation.boot_id:
        failures.append("boot_id differs from the expected boot")
    if expectation.require_serial and not identity.board_serial:
        failures.append("board serial number is missing")
    if expectation.require_version_file and identity.product_source_kind != "trim-version-file":
        failures.append("fnOS product identity did not come from /usr/trim/etc/version")
    if not BOOT_ID_RE.fullmatch(identity.boot_id):
        failures.append("boot_id is malformed")
    if not UUID_RE.fullmatch(identity.root_uuid):
        failures.append("root UUID is malformed")
    if identity.source not in ("serial", "ssh"):
        failures.append(f"identity source is not a live board source: {identity.source}")
    return not failures, failures


def require_identity(
    identity: RuntimeIdentity, expectation: IdentityExpectation
) -> None:
    ok, failures = verify_identity(identity, expectation)
    if not ok:
        raise HardwareFailure(
            "identity_verification_failed",
            "runtime identity rejected: " + "; ".join(failures),
        )


@dataclass(frozen=True)
class BootIdentityBinding:
    """Reject cached identities from the previous boot."""
    previous_boot_id: str | None

    def require_new(self, identity: RuntimeIdentity) -> None:
        if not BOOT_ID_RE.fullmatch(identity.boot_id):
            raise HardwareFailure("identity_incomplete", "new boot has no valid boot_id")
        if self.previous_boot_id and identity.boot_id == self.previous_boot_id:
            raise HardwareFailure(
                "stale_boot_identity",
                "runtime identity is from the previous boot, not the newly booted system",
            )
