"""Versioned contracts and the small simulation state model."""

from __future__ import annotations

from .errors import SimulationFailure

FIXTURE_SCHEMA = "fnos-lab-fixture/v1"
PLAN_SCHEMA = "fnos-lab-plan/v1"
STATE_SCHEMA = "fnos-lab-state/v1"
EVENT_SCHEMA = "fnos-lab-event/v1"

POWER_CHANNELS = ("s1", "s2", "s3", "s4", "usb")
POWER_BACKENDS = ("simulated-cp5", "cp5-usb")
LOCK_PROTOCOL = "fnos-board-lock/v1"
CUT_HOLD_SECONDS = 5
MAX_CUT_HOLD_SECONDS = 60

PHASES = (
    "created",
    "preflight",
    "serial_capture",
    "power_cycle",
    "ums_ready",
    "writing",
    "written",
    "reading",
    "verified",
    "booting",
    "validating",
    "passed",
    "cancelled",
    "failed",
    "interrupted",
)

# Power may not be removed in these phases.  "written" is included because a
# completed write without a verified read-back is not a success boundary.
POWER_OFF_FORBIDDEN = frozenset({"writing", "written", "reading", "booting", "validating"})

# Explicit state machine edges.  No phase may be entered from an arbitrary
# predecessor; terminal phases have no outgoing edge.
TRANSITIONS = {
    "created": frozenset({"preflight"}),
    "preflight": frozenset({"serial_capture", "cancelled"}),
    "serial_capture": frozenset({"power_cycle", "cancelled"}),
    "power_cycle": frozenset({"ums_ready", "cancelled"}),
    "ums_ready": frozenset({"writing", "cancelled"}),
    "writing": frozenset({"written"}),
    "written": frozenset({"reading", "cancelled"}),
    "reading": frozenset({"verified", "cancelled"}),
    "verified": frozenset({"booting", "cancelled"}),
    "booting": frozenset({"validating"}),
    "validating": frozenset({"passed"}),
    "passed": frozenset(),
    "cancelled": frozenset(),
    "failed": frozenset(),
    "interrupted": frozenset(),
}


def validate_transition(current: str, target: str) -> str:
    if target not in TRANSITIONS.get(current, frozenset()):
        raise SimulationFailure(
            "invalid_transition",
            f"invalid simulation transition {current} -> {target}",
        )
    return target

RESUME_POLICY = (
    "No implicit resume and no mid-image continuation.  A new full run with a "
    "new plan is required; interrupted media is kept powered for inspection."
)

FAILURE_NEXT_ACTION = {
    "hash_mismatch": "keep power; do not boot; re-plan a full reflash and full read-back",
    "power_state_unknown": "keep power policy conservative; inspect power and serial, then start a new full run",
    "power_recovery_failed": "manual power-on/inspection required; do not blind-retry OFF",
    "image_changed": "keep power; rebuild/review the image and create a new plan",
    "plan_changed": "reject the run; review and create a new plan",
    "invalid_transition": "reject the run; the state machine refused an invalid phase jump",
    "target_changed": "keep power; re-identify the external whole USB target and create a new plan",
    "write_incomplete": "keep power; medium is incomplete; inspect and re-plan a full write",
    "readback_short": "keep power; no verified result; inspect target/link and start a new full run",
    "readback_incomplete": "keep power; no verified result; start a new full run",
    "boot_not_observed": "keep power; inspect serial/boot; do not blind-retry power or flash",
    "planned_power_fault": "test-only power loss recorded; keep the scene, re-plan; no automatic resume",
    "serial_unavailable": "no power action should have been taken; inspect serial holder/wiring",
    "serial_config_failed": "no power action should have been taken; inspect serial permissions/settings",
    "safe_cut_not_verified": "keep power; obtain live U-Boot prompt or completed normal-shutdown evidence before any cut",
    "boot_evidence_missing": "keep power; no new serial bytes after cold boot; inspect boot/power/serial",
    "boot_evidence_incomplete": "keep power; new boot evidence is incomplete; do not claim login/boot_id/rootfs validation",
    "post_boot_validation_not_implemented": "keep power; real boot_id/rootfs verification is not implemented in this batch",
}
