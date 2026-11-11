"""Typed failures.  The CLI maps these to stable exit codes."""

from __future__ import annotations


class LabError(Exception):
    """A controlled failure with no embedded credentials or environment data."""

    exit_code = 1
    kind = "failed"


class Refused(LabError):
    """A precondition was not met; no unsafe action was attempted."""

    exit_code = 2
    kind = "refused"


class SimulationFailure(LabError):
    """The simulator deliberately stopped after a modelled failure."""

    exit_code = 1
    kind = "simulation_failed"

    def __init__(self, code: str, message: str, *, manual_recovery: bool = False):
        super().__init__(message)
        self.code = code
        self.manual_recovery = manual_recovery


class Cancelled(LabError):
    """A cancel request reached a safe boundary."""

    exit_code = 3
    kind = "cancelled"

    def __init__(self, stage: str):
        super().__init__(f"cancel requested at {stage}")
        self.stage = stage


class HardwareFailure(LabError):
    """A real/fake backend stopped after a controlled hardware-path failure."""

    exit_code = 1
    kind = "hardware_failed"

    def __init__(self, code: str, message: str, *, manual_recovery: bool = False):
        super().__init__(message)
        self.code = code
        self.manual_recovery = manual_recovery


class PowerUnknown(HardwareFailure):
    """The power command may have been sent; the resulting state is unknown."""

    def __init__(self, message: str, *, action: str = "unknown"):
        super().__init__("power_state_unknown", message, manual_recovery=True)
        self.action = action


class HashMismatch(HardwareFailure):
    """Full-length read-back differs from the trusted image."""

    def __init__(self, message: str):
        super().__init__("hash_mismatch", message)


class AuthorizationError(Refused):
    """The explicit execution gate/token is missing, forged or expired."""


class Interrupted(LabError):
    """A persisted run was found with no live owner; never auto-resumed."""

    exit_code = 70
    kind = "interrupted"
