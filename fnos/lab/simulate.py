"""Persistent, hardware-free simulation of the flashing state machine.

There is no disk I/O, no serial access and no power API in this module.  The
simulator models the *policy*: power is refused while writing/reading, a hash
mismatch stops before boot, cancellation waits for a safe boundary, and an
interrupted process is never resumed implicitly.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .errors import Cancelled, Refused, SimulationFailure
from .locks import board_lock
from .model import (
    FAILURE_NEXT_ACTION,
    PHASES,
    POWER_OFF_FORBIDDEN,
    RESUME_POLICY,
    STATE_SCHEMA,
    validate_transition,
)
from .plan import load_plan, verify_image_against_plan
from .state import RunStore, new_run_id, validate_run_id
from .util import utc_now

CANCEL_STAGES = ("preflight", "writing", "reading")
CRASH_STAGES = ("writing", "reading")
FAULTS = (
    "hash_mismatch",
    "cloud_unknown",
    "recovery_failed",
    "power_off_at_writing",
    "power_off_at_reading",
)


def _initial_state(plan: dict[str, Any], run_id: str) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema": STATE_SCHEMA,
        "run_id": run_id,
        "simulated": True,
        "board_id": (plan.get("board") or {}).get("board_id"),
        "plan_path": plan.get("_loaded_from"),
        "plan_payload_sha256": (plan.get("integrity") or {}).get("payload_sha256"),
        "phase": "created",
        "status": "running",
        "pid": os.getpid(),
        "power": {
            "state": "unknown",
            "last_action": None,
            "last_result": "not_queried",
        },
        "media": {
            "write_started": False,
            "write_complete": False,
            "bytes_written": 0,
            "readback_started": False,
            "readback_complete": False,
            "bytes_read": 0,
            "verified": False,
            "incomplete": False,
        },
        "cancel_requested": False,
        "failure": None,
        "resume_allowed": False,
        "resume_policy": RESUME_POLICY,
        "next_action": "run the simulation",
        "created_at": now,
        "updated_at": now,
    }


class Simulation:
    def __init__(
        self,
        plan: dict[str, Any],
        *,
        plan_path: Path,
        state_root: Path,
        run_id: str | None = None,
        cancel_at: str | None = None,
        crash_at: str | None = None,
        fault: str | None = None,
    ):
        if plan.get("simulated") is not True:
            raise Refused(
                "only simulated plans can run in this batch; a real plan has no wired backend"
            )
        if cancel_at is not None and cancel_at not in CANCEL_STAGES:
            raise Refused(f"cancel stage must be one of {', '.join(CANCEL_STAGES)}")
        if crash_at is not None and crash_at not in CRASH_STAGES:
            raise Refused(f"crash stage must be one of {', '.join(CRASH_STAGES)}")
        if fault is not None and fault not in FAULTS:
            raise Refused(f"fault must be one of {', '.join(FAULTS)}")
        if cancel_at and crash_at:
            raise Refused("choose either cancel or crash, not both")

        self.plan = dict(plan)
        self.plan["_loaded_from"] = str(Path(plan_path).absolute())
        self.state_root = Path(state_root).expanduser()
        self.run_id = validate_run_id(run_id) if run_id else new_run_id()
        self.cancel_at = cancel_at
        self.crash_at = crash_at
        self.fault = fault
        self._fault_used = False
        self._recovery_attempted = False
        self.board_id = (self.plan.get("board") or {}).get("board_id")
        if not isinstance(self.board_id, str) or not self.board_id:
            raise Refused("plan has no board_id")
        self.store = RunStore(self.state_root, self.run_id)
        self.state = _initial_state(self.plan, self.run_id)

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        # Fail before creating any run state if the image already changed.
        verify_image_against_plan(self.plan)
        with board_lock(self.state_root, self.board_id):
            verify_image_against_plan(self.plan)
            self.store.create(self.state)
            self._event(
                "run_started",
                board_id=self.board_id,
                plan_path=self.plan["_loaded_from"],
                image_sha256=self.plan["image"]["sha256"],
                image_size_bytes=self.plan["image"]["size_bytes"],
                fault=self.fault,
                cancel_at=self.cancel_at,
                crash_at=self.crash_at,
            )
            try:
                self._run_stages()
            except Cancelled as exc:
                self._finish_cancelled(exc.stage)
            except SimulationFailure as exc:
                self._finish_failed(exc)
            except Refused as exc:
                self._finish_failed(SimulationFailure("refused", str(exc)))
            except OSError as exc:
                # Do not include paths or environment in the persisted message.
                self._finish_failed(
                    SimulationFailure("io_error", f"simulated I/O error: {exc.strerror}")
                )
            self._save()
            return self.state

    def summary(self) -> dict[str, Any]:
        return self.store.public_summary(self.state) | {
            "plan_path": self.state.get("plan_path"),
            "next_action": self.state.get("next_action"),
            "cancel_at": self.cancel_at,
            "crash_at": self.crash_at,
            "fault": self.fault,
        }

    # ------------------------------------------------------------------
    # stages
    # ------------------------------------------------------------------
    def _run_stages(self) -> None:
        self._set_phase("preflight")
        self._verify_plan_file("preflight")
        self._verify_image("preflight")
        self._event("preflight_passed", checks=self._preflight_checks())
        if self.cancel_at == "preflight":
            self.state["cancel_requested"] = True
            self._event("cancel_requested", stage="preflight")
            raise Cancelled("preflight")

        self._set_phase("serial_capture")
        # Pre-listen BEFORE any power action so the U-Boot window is not missed.
        # Deliberately no serial open: this is a modelled capture only.
        serial_port = self.plan.get("serial", {}).get("port")
        self._event("serial_listen_started_simulated", port=serial_port)
        self._event("serial_capture_simulated", port=serial_port)

        self._set_phase("power_cycle")
        self._power_off("enter-ums")
        self._power_on("enter-ums")

        self._set_phase("ums_ready")
        self._event("ums_ready_simulated")

        self._write()
        self._read_back()

        self._set_phase("verified")
        self._event("readback_verified", sha256=self.plan["image"]["sha256"])

        # Releasing UMS and cold-booting is allowed only after verification.
        self._power_off("cold-boot")
        self._power_on("cold-boot")
        self._set_phase("booting")
        self._event("boot_simulated")
        self._set_phase("validating")
        self._event("validation_simulated")
        self._set_phase("passed")
        self.state["status"] = "passed"
        self.state["next_action"] = "none; simulation evidence is complete"
        self._save()
        self._event("run_passed")

    def _preflight_checks(self) -> dict[str, Any]:
        return {
            "plan_simulated": True,
            "image_hash_matches_plan": True,
            "board_id": self.board_id,
            "power_channel": self.plan.get("power", {}).get("channel"),
            "power_off_forbidden_during": sorted(POWER_OFF_FORBIDDEN),
            "target_identity": self.plan.get("target", {}).get("identity_status"),
        }

    def _write(self) -> None:
        self._verify_plan_file("writing")
        self._verify_image("writing")
        self._set_phase("writing")
        self.state["media"]["write_started"] = True
        self._save()
        self._event(
            "write_started_simulated",
            bytes_expected=self.plan["image"]["size_bytes"],
        )
        if self.cancel_at == "writing":
            self.state["cancel_requested"] = True
            self._event("cancel_requested", stage="writing")
        if self.fault == "power_off_at_writing":
            self._attempt_forbidden_power_off("injected-during-writing")
        self._maybe_crash("writing")

        self.state["media"]["bytes_written"] = self.plan["image"]["size_bytes"]
        self.state["media"]["write_complete"] = True
        self._save()
        self._event(
            "write_completed_simulated",
            bytes_written=self.state["media"]["bytes_written"],
        )
        self._set_phase("written")
        if self.state["cancel_requested"]:
            raise Cancelled("writing")

    def _read_back(self) -> None:
        self._verify_plan_file("reading")
        self._verify_image("reading")
        self._set_phase("reading")
        self.state["media"]["readback_started"] = True
        self._save()
        self._event(
            "readback_started_simulated",
            bytes_expected=self.plan["image"]["size_bytes"],
        )
        if self.fault == "power_off_at_reading":
            self._attempt_forbidden_power_off("injected-during-reading")
        self._maybe_crash("reading")
        self._verify_image("reading")

        if self.cancel_at == "reading":
            self.state["cancel_requested"] = True
            self.state["media"]["bytes_read"] = self.plan["image"]["size_bytes"] // 2
            self._event("cancel_requested", stage="reading")
            raise Cancelled("reading")

        expected = self.plan["image"]["sha256"]
        actual = "0" * 64 if self.fault == "hash_mismatch" else expected
        self.state["media"]["readback_complete"] = True
        self.state["media"]["bytes_read"] = self.plan["image"]["size_bytes"]
        self._save()
        self._event(
            "readback_completed_simulated",
            bytes_read=self.state["media"]["bytes_read"],
            sha256=actual,
        )
        if actual != expected:
            raise SimulationFailure(
                "hash_mismatch",
                "full read-back SHA-256 differs from the planned image; boot is blocked",
            )
        self.state["media"]["verified"] = True
        self._save()

    # ------------------------------------------------------------------
    # power model
    # ------------------------------------------------------------------
    def _power_off(self, origin: str) -> None:
        phase = self.state["phase"]
        if phase in POWER_OFF_FORBIDDEN:
            self._event(
                "power_off_refused",
                phase=phase,
                origin=origin,
                reason="power-off is forbidden during writing/read-back/boot",
            )
            raise Refused(
                f"power-off refused in phase {phase}; writing/read-back/boot must stay powered"
            )
        if self.fault == "cloud_unknown" and not self._fault_used:
            self._fault_used = True
            self.state["power"] = {
                "state": "unknown",
                "last_action": "off",
                "last_result": "unknown",
            }
            self._save()
            self._event(
                "power_action",
                action="off",
                result="unknown",
                origin=origin,
                phase=phase,
                channel=self.plan.get("power", {}).get("channel"),
            )
            raise SimulationFailure(
                "power_state_unknown",
                "simulated cloud power-off result is unknown; no blind OFF retry",
            )
        self.state["power"] = {
            "state": "off",
            "last_action": "off",
            "last_result": "confirmed",
        }
        self._save()
        self._event(
            "power_action",
            action="off",
            result="confirmed",
            origin=origin,
            phase=phase,
            channel=self.plan.get("power", {}).get("channel"),
        )

    def _power_on(self, origin: str) -> None:
        phase = self.state["phase"]
        if self.fault == "recovery_failed" and not self._fault_used:
            self._fault_used = True
            self.state["power"] = {
                "state": "unknown",
                "last_action": "on",
                "last_result": "failed",
            }
            self._save()
            self._event(
                "power_action",
                action="on",
                result="failed",
                origin=origin,
                phase=phase,
                channel=self.plan.get("power", {}).get("channel"),
            )
            raise SimulationFailure(
                "power_recovery_failed",
                "simulated power-on recovery failed; manual power-on is required",
                manual_recovery=True,
            )
        self.state["power"] = {
            "state": "on",
            "last_action": "on",
            "last_result": "confirmed",
        }
        self._save()
        self._event(
            "power_action",
            action="on",
            result="confirmed",
            origin=origin,
            phase=phase,
            channel=self.plan.get("power", {}).get("channel"),
        )

    def _attempt_forbidden_power_off(self, origin: str) -> None:
        try:
            self._power_off(origin)
        except Refused:
            # The refusal is the expected, safe result of the injection.
            return

    def _recover_power_once(self) -> None:
        if self._recovery_attempted:
            return
        self._recovery_attempted = True
        self._event("power_recovery_started", attempts=1)
        try:
            self._power_on("after-unknown-off")
        except SimulationFailure:
            self.state["power"] = {
                "state": "unknown",
                "last_action": "on",
                "last_result": "failed",
            }
            self._event("power_recovery_failed", attempts=1)
        else:
            self._event("power_recovery_succeeded", attempts=1)

    # ------------------------------------------------------------------
    # failures and cancellation
    # ------------------------------------------------------------------
    def _finish_cancelled(self, stage: str) -> None:
        self._set_phase("cancelled")
        self.state["status"] = "cancelled"
        self.state["cancel_requested"] = True
        if self.state["media"]["write_started"] and not self.state["media"]["verified"]:
            self.state["media"]["incomplete"] = True
        self.state["next_action"] = (
            "keep power; inspect the medium; start a new full run (no implicit resume)"
        )
        self._save()
        self._event("run_cancelled", stage=stage, power=self.state["power"]["state"])

    def _finish_failed(self, failure: SimulationFailure) -> None:
        self.state["status"] = "failed"
        self.state["failure"] = {
            "code": failure.code,
            "message": str(failure),
            "manual_recovery": failure.manual_recovery,
        }
        if self.state["media"]["write_started"] and not self.state["media"]["verified"]:
            self.state["media"]["incomplete"] = True
        if failure.code == "power_state_unknown":
            self._recover_power_once()
        if failure.code == "power_recovery_failed":
            self.state["power"] = {
                "state": "unknown",
                "last_action": "on",
                "last_result": "failed",
            }
        self.state["next_action"] = FAILURE_NEXT_ACTION.get(
            failure.code, "inspect the run state; start a new full run"
        )
        self._save()
        self._event(
            "run_failed",
            code=failure.code,
            message=str(failure),
            manual_recovery=failure.manual_recovery,
            power=self.state["power"]["state"],
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _verify_plan_file(self, stage: str) -> None:
        try:
            current = load_plan(Path(self.plan["_loaded_from"]))
        except Refused as exc:
            raise SimulationFailure(
                "plan_changed", f"plan invalid at {stage}: {exc}"
            ) from None
        if (current.get("integrity") or {}).get(
            "payload_sha256"
        ) != (self.plan.get("integrity") or {}).get("payload_sha256"):
            raise SimulationFailure(
                "plan_changed", f"plan changed at {stage}; refusing to continue"
            )

    def _verify_image(self, stage: str) -> None:
        try:
            verify_image_against_plan(self.plan)
        except Refused as exc:
            raise SimulationFailure(
                "image_changed", f"image/plan invalid at {stage}: {exc}"
            ) from None

    def _set_phase(self, phase: str) -> None:
        if phase not in PHASES:
            raise SimulationFailure("invalid_transition", f"unknown phase {phase}")
        validate_transition(self.state["phase"], phase)
        self.state["phase"] = phase
        self._save()
        self._event("phase_changed", phase=phase)

    def _maybe_crash(self, stage: str) -> None:
        if self.crash_at != stage:
            return
        self.state["phase"] = stage
        self.state["next_action"] = (
            "status must detect the dead process; no implicit resume"
        )
        if self.state["media"]["write_started"] and not self.state["media"]["verified"]:
            self.state["media"]["incomplete"] = True
        self._save()
        self._event("process_interrupted_simulated", stage=stage, pid=os.getpid())
        # Simulate an abrupt process death for the status/crash-recovery test.
        os._exit(70)

    def _save(self) -> None:
        self.store.save(self.state)

    def _event(self, kind: str, **fields: Any) -> None:
        self.store.append_event(kind, **fields)


def run_simulation(
    plan: dict[str, Any],
    *,
    plan_path: Path,
    state_root: Path,
    run_id: str | None = None,
    cancel_at: str | None = None,
    crash_at: str | None = None,
    fault: str | None = None,
) -> tuple[Simulation, dict[str, Any]]:
    simulation = Simulation(
        plan,
        plan_path=plan_path,
        state_root=state_root,
        run_id=run_id,
        cancel_at=cancel_at,
        crash_at=crash_at,
        fault=fault,
    )
    state = simulation.run()
    return simulation, state


def load_state_summary(state_root: Path, run_id: str) -> dict[str, Any]:
    store = RunStore(state_root, run_id)
    state = store.load()
    return store.public_summary(state)


def read_events(state_root: Path, run_id: str) -> list[dict[str, Any]]:
    return RunStore(state_root, run_id).read_events()
