"""Executable plan/run/status/cancel engine with the shared board lock.

The engine is deliberately backend-agnostic: tests and ``--backends fake`` use
the file serial/CP5/flash substitutes, while a real run constructs the CP5 CLI,
passive serial listener and macOS diskutil backend inside the held lock.

Ordering rules encoded here:

* plan/image/preconditions are re-checked before and after acquiring the lock;
* the serial listener starts *before* the first power action;
* every power action is explicit ON/OFF, never a toggle or master switch;
* writing and full-length read-back are never interrupted by a cancel-triggered
  power cut; cancellation after a completed write skips read-back and boot;
* a hash mismatch keeps power on, blocks boot and is never retried;
* an interrupted controller leaves a persisted state; no process auto-resumes
  and no process blindly cuts/restores power.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Callable

from .candidate import CandidateManifest, require_candidate_identity
from .authorization import (
    require_real_gate,
    token_fingerprint,
    verify_authorization,
)
from .errors import (
    AuthorizationError,
    Cancelled,
    HardwareFailure,
    HashMismatch,
    PowerUnknown,
    Refused,
    SimulationFailure,
)
from .flash import (
    FlashBackend,
    FlashFailure,
    ImageHandle,
    compare_identity,
    expected_identity,
    open_trusted_image,
    readback_image,
    require_full_readback,
    write_image,
)
from .locks import HeldLock, board_lock, board_lock_name, lock_root
from .model import (
    CUT_HOLD_SECONDS,
    FAILURE_NEXT_ACTION,
    MAX_CUT_HOLD_SECONDS,
    POWER_OFF_FORBIDDEN,
    RESUME_POLICY,
    STATE_SCHEMA,
    validate_transition,
)
from .plan import load_plan, verify_image_against_plan
from .power import Cp5PowerBackend, PowerManager
from .runtime_identity import BootIdentityBinding, RuntimeIdentity
from .safe_cut import CutEvidence
from .serial import BOOT_PATTERN, SerialCapture, SerialFailure, start_before_power
from .target_mapping import TargetMap, verify_mapping_against_plan
from .state import RunStore, new_run_id, validate_run_id
from .util import atomic_write_json, read_json, utc_now

CANCEL_FILE = "cancel.request"
PLANNED_FAULT_STAGES = ("writing", "reading")
FAKE_MODES = ("fake",)
REAL_MODES = ("real",)


class CancelSignal:
    def __init__(self, store: RunStore):
        self.store = store
        self.path = store.run_dir / CANCEL_FILE

    def request(self, *, reason: str = "user") -> dict[str, Any]:
        payload = {"schema": "fnos-lab-cancel/v1", "reason": reason, "at": utc_now()}
        try:
            atomic_write_json(self.path, payload, mode=0o600, overwrite=False)
        except Refused:
            # Idempotent: a second cancel request is not an error.
            pass
        return payload

    def is_set(self) -> bool:
        return self.path.exists()

    def info(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            value = read_json(self.path)
        except Refused:
            return {"schema": "fnos-lab-cancel/v1", "reason": "unreadable"}
        return value if isinstance(value, dict) else None


@dataclass
class BackendSet:
    power: Cp5PowerBackend
    serial: SerialCapture
    flash: FlashBackend


def _initial_state(
    plan: dict[str, Any],
    run_id: str,
    *,
    execution_mode: str,
    authorization: dict[str, Any] | None,
) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema": STATE_SCHEMA,
        "run_id": run_id,
        "simulated": execution_mode != "real",
        "execution": {
            "mode": execution_mode,
            "real_hardware": execution_mode == "real",
            "write_eligible": False,
            "authorization": "plan-bound-token" if authorization else "none",
        },
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
            "hold_seconds": None,
            "last_off_at": None,
            "hold_completed_at": None,
        },
        "validation": None,
        "serial": {
            "port": (plan.get("serial") or {}).get("port"),
            "started": False,
            "read_only": True,
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
        "target": None,
        "cancel_requested": False,
        "failure": None,
        "interruption": None,
        "active_children": [],
        "authorization": (
            {
                "scope": authorization.get("scope"),
                "expires_at": authorization.get("expires_at"),
                "token_sha256": token_fingerprint(authorization),
            }
            if authorization
            else None
        ),
        "resume_allowed": False,
        "resume_policy": RESUME_POLICY,
        "next_action": "run the reviewed plan",
        "created_at": now,
        "updated_at": now,
        "hardware_actions": {
            "serial_opened": False,
            "power_api_called": False,
            "disk_written": False,
            "ssh_connected": False,
        },
    }


class RunEngine:
    def __init__(
        self,
        plan: dict[str, Any],
        *,
        plan_path: Path,
        state_root: Path,
        backend_factory: Callable[[HeldLock], BackendSet],
        execution_mode: str,
        run_id: str | None = None,
        authorization: dict[str, Any] | None = None,
        confirm_board: str | None = None,
        allow_planned_fault: bool = False,
        planned_fault_at: str | None = None,
        crash_at: str | None = None,
        boot_timeout: float = 30.0,
        cut_hold_seconds: int = CUT_HOLD_SECONDS,
        cut_evidence_provider: Callable[[str], CutEvidence] | None = None,
        cut_evidence_validator: Callable[[CutEvidence], None] | None = None,
        cut_session_id: str | None = None,
        candidate_manifest: CandidateManifest | None = None,
        runtime_identity_provider: Callable[[], RuntimeIdentity] | None = None,
        boot_identity_binding: BootIdentityBinding | None = None,
        target_map_provider: Callable[[], TargetMap] | None = None,
        ums_prepare_provider: Callable[[], None] | None = None,
        normal_shutdown_provider: Callable[[], CutEvidence] | None = None,
        cold_boot_prepare_provider: Callable[[], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if execution_mode not in ("fake", "real"):
            raise Refused("execution mode must be fake or real")
        if (
            not isinstance(cut_hold_seconds, int)
            or isinstance(cut_hold_seconds, bool)
            or not CUT_HOLD_SECONDS <= cut_hold_seconds <= MAX_CUT_HOLD_SECONDS
        ):
            raise Refused(
                f"cut hold must be {CUT_HOLD_SECONDS}..{MAX_CUT_HOLD_SECONDS} seconds"
            )
        if planned_fault_at is not None and planned_fault_at not in PLANNED_FAULT_STAGES:
            raise Refused(
                f"planned fault stage must be one of {', '.join(PLANNED_FAULT_STAGES)}"
            )
        if crash_at is not None and crash_at not in PLANNED_FAULT_STAGES:
            raise Refused(f"crash stage must be one of {', '.join(PLANNED_FAULT_STAGES)}")
        if crash_at and planned_fault_at:
            raise Refused("choose either cancel, crash or planned fault, not several")
        if planned_fault_at and not allow_planned_fault:
            raise Refused(
                "planned power fault requires --allow-planned-fault and a fixture/test plan"
            )
        if planned_fault_at and (execution_mode != "fake" or plan.get("simulated") is not True):
            raise Refused(
                "planned power faults are limited to offline fake backends and fixture plans"
            )
        if crash_at and execution_mode != "fake":
            raise Refused("crash injection is limited to offline fake backends")
        if execution_mode == "real":
            if plan.get("simulated") is not False:
                raise AuthorizationError(
                    "real execution requires a real (simulated=false) plan"
                )
            require_real_gate()
            if authorization is None:
                raise AuthorizationError(
                    "real execution requires a plan-bound authorization token"
                )
            if confirm_board != (plan.get("board") or {}).get("board_id"):
                raise AuthorizationError(
                    "--confirm-board must exactly match the plan's board_id"
                )
            if os.geteuid() != 0:
                raise AuthorizationError(
                    "real raw-device execution requires root; no action was taken"
                )
            shared = Path(lock_root()).expanduser().absolute()
            requested = Path(state_root).expanduser().absolute()
            if requested != shared:
                raise AuthorizationError(
                    "real runs must use the shared board-lock root as --state-dir "
                    "(set FNOS_BOARD_LOCK_ROOT/FNOS_LAB_STATE_DIR and pass the same path); "
                    "otherwise the CP5/flash/adapter would use a different physical lock"
                )
            plan_lock = plan.get("lock") or {}
            expected_name = board_lock_name((plan.get("board") or {}).get("board_id", ""))
            if plan_lock.get("name") and plan_lock.get("name") != expected_name:
                raise AuthorizationError("plan lock path is not canonical for this board")
            if plan_lock.get("root"):
                planned_root = Path(plan_lock["root"]).expanduser().absolute()
                if planned_root != shared:
                    raise AuthorizationError(
                        "plan was created for a different shared lock root; re-plan "
                        "with the current FNOS_BOARD_LOCK_ROOT"
                    )
        if not isinstance(plan.get("board"), dict) or not plan["board"].get("board_id"):
            raise Refused("plan has no board_id")

        self.plan = dict(plan)
        self.plan["_loaded_from"] = str(Path(plan_path).absolute())
        self.plan_path = Path(plan_path)
        self.state_root = Path(state_root).expanduser()
        self.run_id = validate_run_id(run_id) if run_id else new_run_id()
        self.execution_mode = execution_mode
        self.authorization = authorization
        self.backend_factory = backend_factory
        self.board_id = self.plan["board"]["board_id"]
        self.allow_planned_fault = allow_planned_fault
        self.planned_fault_at = planned_fault_at
        self.crash_at = crash_at
        self.boot_timeout = boot_timeout
        self.cut_hold_seconds = cut_hold_seconds
        self.cut_evidence_provider = cut_evidence_provider
        self.cut_evidence_validator = cut_evidence_validator
        self.cut_session_id = cut_session_id
        self.candidate_manifest = candidate_manifest
        self.runtime_identity_provider = runtime_identity_provider
        self.boot_identity_binding = boot_identity_binding
        self.target_map_provider = target_map_provider
        self.ums_prepare_provider = ums_prepare_provider
        self.normal_shutdown_provider = normal_shutdown_provider
        self.cold_boot_prepare_provider = cold_boot_prepare_provider
        self._target_map_digest: str | None = None
        self._current_target_map: TargetMap | None = None
        self.sleep = sleep
        self.clock = clock
        self.store = RunStore(self.state_root, self.run_id)
        self.cancel = CancelSignal(self.store)
        self.state = _initial_state(
            self.plan,
            self.run_id,
            execution_mode=execution_mode,
            authorization=authorization,
        )
        self.held: HeldLock | None = None
        self.backends: BackendSet | None = None
        self.power: PowerManager | None = None
        self.image: ImageHandle | None = None
        self._recovery_attempted = False

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        # Refuse before creating run state if plan/image already changed.
        verify_image_against_plan(self.plan)
        if self.execution_mode == "real":
            require_real_gate()
            verify_authorization(
                self.authorization,
                self.plan,
                state_root=self.state_root,
            )
        with board_lock(self.state_root, self.board_id) as held:
            self.held = held
            held.revalidate()
            self._verify_plan_file("preflight")
            # The image is opened and hashed under the same lock; its fd is
            # used for the whole write/read-back transaction.
            self.image = open_trusted_image(self.plan)
            self.backends = self.backend_factory(held)
            self.power = PowerManager(
                self.backends.power,
                phase=lambda: self.state["phase"],
                emit=self._event,
                save=self._save,
                first_boot_expansion=lambda: self.state.get("phase")
                in ("booting", "validating"),
                sleep=self.sleep,
            )
            self.store.create(self.state)
            self._event(
                "run_started",
                board_id=self.board_id,
                mode=self.execution_mode,
                plan_path=self.plan["_loaded_from"],
                image_sha256=self.plan["image"]["sha256"],
                image_size_bytes=self.plan["image"]["size_bytes"],
                lock_id=held.lock_id,
                authorized=self.authorization is not None,
            )
            try:
                self._run_stages()
            except Cancelled as exc:
                self._finish_cancelled(exc.stage)
            except PowerUnknown as exc:
                self._finish_power_unknown(exc)
            except HashMismatch as exc:
                self._finish_failed(exc)
            except FlashFailure as exc:
                self._finish_failed(exc)
            except SerialFailure as exc:
                self._finish_failed(exc)
            except HardwareFailure as exc:
                self._finish_failed(exc)
            except SimulationFailure as exc:
                self._finish_failed(
                    HardwareFailure("invalid_transition", str(exc))
                )
            except Refused as exc:
                self._finish_failed(HardwareFailure("refused", str(exc)))
            except OSError as exc:
                self._finish_failed(
                    HardwareFailure("io_error", f"I/O error: {exc.strerror}")
                )
            finally:
                self._stop_serial()
                self._save()
                if self.image is not None:
                    self.image.close()
                    self.image = None
            return self.state

    def normal_shutdown(
        self, origin: str = "normal-shutdown", *, safe_to_cut: bool = False
    ) -> str:
        """Explicit normal-shutdown operation, distinct from cancel/fault.

        The real board-side OS shutdown (sync/poweroff via serial/SSH) is not
        implemented in this batch.  Fake runs may exercise the explicit OFF
        path to prove it is never reused as a cancel or planned fault.
        """
        if self.power is None:
            raise Refused("normal shutdown requires an active backend session")
        if self.execution_mode == "real":
            if self.normal_shutdown_provider is None or self.cut_evidence_validator is None:
                raise Refused(
                    "real normal shutdown requires a live poweroff receipt provider/validator"
                )
            evidence = self.normal_shutdown_provider()
            self.cut_evidence_validator(evidence)
            self.state["interruption"] = {
                "kind": "normal_shutdown",
                "stage": self.state["phase"],
                "power_cut": True,
            }
            result = self.power.turn_off(
                origin,
                kind="normal_shutdown",
                safe_to_cut=True,
                cut_validator=lambda: self.cut_evidence_validator(evidence) or True,
            )
            self._save()
            return result
        phase = self.state["phase"]
        if phase in POWER_OFF_FORBIDDEN:
            raise Refused(
                f"normal shutdown refused in phase {phase}; writing/read-back/boot "
                "must stay powered"
            )
        self.state["interruption"] = {
            "kind": "normal_shutdown",
            "stage": phase,
            "power_cut": True,
        }
        result = self.power.turn_off(
            origin, kind="normal_shutdown", safe_to_cut=safe_to_cut
        )
        self._save()
        return result

    def summary(self) -> dict[str, Any]:
        return self.store.public_summary(self.state) | {
            "plan_path": self.state.get("plan_path"),
            "execution": self.state.get("execution"),
            "serial": self.state.get("serial"),
            "target": self.state.get("target"),
            "validation": self.state.get("validation"),
            "active_children": self.state.get("active_children"),
            "authorization": self.state.get("authorization"),
            "interruption": self.state.get("interruption"),
            "cancel": self.cancel.info(),
            "next_action": self.state.get("next_action"),
        }

    # ------------------------------------------------------------------
    # stages
    # ------------------------------------------------------------------
    def _run_stages(self) -> None:
        self._set_phase("preflight")
        self._verify_plan_file("preflight")
        self._event(
            "preflight_passed",
            checks={
                "plan_simulated": self.plan.get("simulated"),
                "image_hash_matches_plan": True,
                "board_id": self.board_id,
                "power_channel": (self.plan.get("power") or {}).get("channel"),
                "power_off_forbidden_during": sorted(POWER_OFF_FORBIDDEN),
                "real_hardware_gate": self.execution_mode == "real",
            },
        )
        if self.cancel.is_set():
            self._mark_cancel("preflight")
            raise Cancelled("preflight")
        if self.execution_mode == "real":
            missing = []
            if self.cut_evidence_provider is None or self.cut_evidence_validator is None:
                missing.append("live-safe-cut-evidence-and-validator")
            if self.target_map_provider is None:
                missing.append("current-board-usb-target-mapping")
            if self.candidate_manifest is None or self.runtime_identity_provider is None:
                missing.append("candidate-manifest-and-runtime-identity")
            elif not self.candidate_manifest.accepted:
                missing.append("candidate-not-accepted")
            if self.ums_prepare_provider is None:
                missing.append("ums-recovery-controller")
            if self.cold_boot_prepare_provider is None:
                missing.append("cold-boot-recovery-controller")
            if missing:
                raise HardwareFailure(
                    "real_preflight_not_ready",
                    "real run refused before serial/power/write: missing " + ", ".join(missing),
                )

        # The listener must be live before the first ON/OFF.
        start_before_power(
            self.backends.serial,
            emit=self._event,
            port=(self.plan.get("serial") or {}).get("port") or "",
        )
        self.state["serial"]["started"] = True
        if self.execution_mode == "real":
            self.state["hardware_actions"]["serial_opened"] = True
        self._save()
        self._set_phase("serial_capture")
        self._event(
            "serial_capture_ready",
            port=(self.plan.get("serial") or {}).get("port"),
            read_only=True,
        )
        if self.cancel.is_set():
            self._mark_cancel("serial_capture")
            raise Cancelled("serial_capture")

        self._set_phase("power_cycle")
        initial = self.power.read()
        self._set_power_state(initial)
        if initial == "unknown":
            raise PowerUnknown("power state is unknown before any cycle")
        if initial == "off":
            self.power.turn_on("preflight")
        self._power_cycle("enter-ums")
        if self.execution_mode == "real":
            self.ums_prepare_provider()

        self._set_phase("ums_ready")
        self._reidentify("ums_ready")
        if self.cancel.is_set():
            self._mark_cancel("ums_ready")
            raise Cancelled("ums_ready")

        self._write()
        self._read_back()
        self._cold_boot()

    def _cut_readiness(self, origin: str) -> bool:
        """Fail closed for the fake path when no live cut-safe flag exists."""
        if not getattr(self.backends.serial, "cut_safe", False):
            reason = (
                "no live U-Boot prompt or completed normal shutdown; refusing power cut"
            )
            self._event("safe_cut_refused", origin=origin, reason=reason)
            raise HardwareFailure("safe_cut_not_verified", reason)
        return True

    def _cut_evidence(self, origin: str) -> CutEvidence | None:
        if self.execution_mode != "real":
            return None
        if self.cut_evidence_provider is None or self.cut_evidence_validator is None:
            reason = (
                "real power cut refused: no live serial-session safe-cut provider/"
                "receipt validator is attached"
            )
            self._event("safe_cut_refused", origin=origin, reason=reason)
            raise HardwareFailure("safe_cut_not_verified", reason)
        evidence = self.cut_evidence_provider(origin)
        try:
            self.cut_evidence_validator(evidence)
        except Exception as exc:
            reason = f"safe-cut receipt validation failed: {exc}"
            self._event("safe_cut_refused", origin=origin, reason=reason)
            raise HardwareFailure("safe_cut_not_verified", reason) from None
        self._event(
            "safe_cut_evidence",
            origin=origin,
            state=evidence.state,
            session_id=evidence.session_id,
            expires_at=evidence.expires_at,
            nonce=evidence.nonce,
            command=evidence.command,
            output_sha256=evidence.output_sha256,
        )
        return evidence

    def _power_cycle(self, origin: str) -> None:
        self._lock_guard()
        if self.cancel.is_set():
            # Do not start a new cut after a cancel has reached this boundary.
            raise Cancelled("power-cycle")
        evidence = self._cut_evidence(origin)
        if evidence is not None:
            safe = True
            validator = lambda: self.cut_evidence_validator(evidence) or True
        else:
            safe = self._cut_readiness(origin)
            validator = None
        self.power.turn_off(
            origin, safe_to_cut=safe, cut_validator=validator
        )
        self.state["power"]["hold_seconds"] = self.cut_hold_seconds
        self.state["power"]["last_off_at"] = utc_now()
        self._save()
        self._event(
            "power_hold_started",
            origin=origin,
            seconds=self.cut_hold_seconds,
        )
        try:
            self.sleep(self.cut_hold_seconds)
            self.state["power"]["hold_completed_at"] = utc_now()
            self._save()
            self._event(
                "power_hold_completed",
                origin=origin,
                seconds=self.cut_hold_seconds,
            )
        finally:
            # A cancel/interrupt during the hold must not leave the board
            # unpowered.  Restore power before propagating the exception.
            self.power.turn_on(origin)
            self.backends.serial.on_power_on()

    def _write(self) -> None:
        self._lock_guard()
        self._set_phase("writing")
        self.state["media"]["write_started"] = True
        self._save()
        self._event(
            "write_started",
            bytes_expected=self.plan["image"]["size_bytes"],
        )
        self._maybe_crash("writing")
        if self._planned_fault("writing"):
            self._inject_planned_fault("writing")
            raise HardwareFailure(
                "planned_power_fault",
                "authorized test power loss at writing; medium is incomplete",
                manual_recovery=True,
            )
        target_fd = self.backends.flash.open_target()
        try:
            result = write_image(
                self.backends.flash,
                target_fd,
                self.image,
                # A cancel does not cut power or abort mid-write.  It is
                # observed at the completed-write boundary below.
                cancel=lambda: False,
                on_progress=self._write_progress,
            )
        finally:
            self.backends.flash.close_target(target_fd)
        self.state["media"]["bytes_written"] = result.bytes_written
        self.state["media"]["write_complete"] = result.complete
        if self.execution_mode == "real":
            self.state["hardware_actions"]["disk_written"] = True
        self._save()
        self._event(
            "write_completed",
            bytes_written=result.bytes_written,
            complete=result.complete,
        )
        if not result.complete:
            raise FlashFailure("write_incomplete", "image write did not cover the full image")
        self._set_phase("written")
        self._reidentify("written")
        if self.cancel.is_set():
            self._mark_cancel("writing")
            raise Cancelled("writing")

    def _read_back(self) -> None:
        self._lock_guard()
        self._set_phase("reading")
        self.state["media"]["readback_started"] = True
        self._save()
        self._event(
            "readback_started",
            bytes_expected=self.plan["image"]["size_bytes"],
        )
        self._maybe_crash("reading")
        if self._planned_fault("reading"):
            self._inject_planned_fault("reading")
            raise HardwareFailure(
                "planned_power_fault",
                "authorized test power loss at read-back; no verification is claimed",
                manual_recovery=True,
            )
        target_fd = self.backends.flash.open_target()
        try:
            actual, bytes_read, complete = readback_image(
                self.backends.flash,
                target_fd,
                self.image,
                cancel=self.cancel.is_set,
                on_progress=self._read_progress,
            )
        finally:
            self.backends.flash.close_target(target_fd)
        self.state["media"]["bytes_read"] = bytes_read
        self._save()
        if not complete:
            self._mark_cancel("reading")
            raise Cancelled("reading")
        self._reidentify("readback")
        require_full_readback(
            actual,
            self.image,
            bytes_read=bytes_read,
            complete=complete,
        )
        self.state["media"]["readback_complete"] = True
        self.state["media"]["verified"] = True
        self._save()
        self._event("readback_verified", sha256=actual, bytes_read=bytes_read)
        self._set_phase("verified")

    def _cold_boot(self) -> None:
        self._lock_guard()
        if self.execution_mode == "real":
            self.cold_boot_prepare_provider()
        self._reidentify("before-cold-boot")
        if self.cancel.is_set():
            self._mark_cancel("verified")
            raise Cancelled("verified")
        # Only a verified full read-back can reach this point.  The cursor is
        # taken before the cut; stale pre-existing log lines are excluded.
        cursor = self.backends.serial.cursor()
        self._power_cycle("cold-boot")
        if self.cancel.is_set():
            self._mark_cancel("verified")
            raise Cancelled("verified")
        self._set_phase("booting")
        if not self.backends.serial.wait_for_new(
            BOOT_PATTERN, self.boot_timeout, cursor
        ):
            raise SerialFailure(
                "boot_evidence_missing",
                "no new serial boot evidence after the cold-boot cursor; "
                "power is kept for inspection",
            )
        expected_kernel = ((self.plan.get("validation") or {}).get("expected_kernel") or None)
        evidence = self.backends.serial.boot_evidence(
            cursor, expected_kernel=expected_kernel
        )
        self.state["validation"] = evidence.as_dict()
        self.state["validation"]["source"] = (
            "fake" if self.execution_mode == "fake" else "real-passive-serial"
        )
        self.state["validation"]["hardware_acceptance"] = False
        self._save()
        self._event("boot_evidence", **evidence.as_dict())
        if self.execution_mode == "real":
            if self.runtime_identity_provider is None or self.candidate_manifest is None:
                self._set_phase("validating")
                raise SerialFailure(
                    "runtime_identity_not_collected",
                    "real runtime identity/manifest provider is not attached; "
                    "boot evidence is recorded but the run is not passed",
                )
            identity = self.runtime_identity_provider()
            if self.boot_identity_binding is not None:
                self.boot_identity_binding.require_new(identity)
            require_candidate_identity(self.candidate_manifest, identity)
            self.state["validation"]["runtime_identity"] = identity.as_dict()
            self.state["validation"]["candidate_id"] = self.candidate_manifest.candidate_id
            self._save()
        if self.execution_mode != "real" and not evidence.complete:
            self._set_phase("validating")
            raise SerialFailure(
                "boot_evidence_incomplete",
                f"new boot evidence is incomplete: {evidence.reason}",
            )
        self._set_phase("validating")
        self._event(
            "validation_passed",
            boot_evidence=evidence.as_dict(),
        )
        self._set_phase("passed")
        self.state["status"] = "passed"
        self.state["next_action"] = (
            "none; verified write/readback and receipt-bound runtime identity "
            "(not a full fnOS functional acceptance)"
            if self.execution_mode == "real"
            else "none; verified write and complete fake boot evidence "
            "(not a real board acceptance)"
        )
        self._save()
        self._event("run_passed")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _lock_guard(self) -> None:
        if self.held is None:
            raise Refused("no held board lock")
        self.held.revalidate()

    def _verify_target_map(self, stage: str) -> None:
        if self.target_map_provider is None:
            if self.execution_mode == "real":
                raise HardwareFailure(
                    "target_mapping_not_configured",
                    "real target mapping provider is not attached; refusing target operation",
                )
            return
        mapping = self.target_map_provider()
        if mapping.board_id != self.board_id:
            raise HardwareFailure("target_mapping_board_mismatch", "target mapping board differs")
        if self._target_map_digest is None:
            self._target_map_digest = mapping.digest
            self._event("target_mapping_bound", stage=stage, digest=mapping.digest,
                        host_device=mapping.host_device, criteria=list(mapping.criteria))
        self._current_target_map = mapping
        if self._target_map_digest is not None and mapping.digest != self._target_map_digest:
            raise HardwareFailure(
                "target_mapping_changed",
                f"target mapping changed at {stage}; re-plan and re-identify",
            )

    def _reidentify(self, stage: str) -> None:
        self._lock_guard()
        self._verify_target_map(stage)
        if self.backends is None:
            raise Refused("backends were not constructed")
        actual = self.backends.flash.identify()
        compare_identity(actual, expected_identity(self.plan))
        if self._current_target_map is not None:
            verify_mapping_against_plan(
                self._current_target_map,
                plan_target=self.plan.get("target") or {},
                actual=actual,
            )
        self.state["target"] = actual.as_dict()
        self._save()
        self._event("target_reidentified", stage=stage, identity=actual.as_dict())

    def _write_progress(self, done: int, total: int) -> None:
        self.state["media"]["bytes_written"] = done
        if done == total or done % (16 * 1024 * 1024) == 0:
            self._save()
            self._event("write_progress", bytes=done, total=total)

    def _read_progress(self, done: int, total: int) -> None:
        self.state["media"]["bytes_read"] = done
        if done == total or done % (16 * 1024 * 1024) == 0:
            self._save()
            self._event("readback_progress", bytes=done, total=total)

    def _planned_fault(self, stage: str) -> bool:
        return self.planned_fault_at == stage and self.allow_planned_fault

    def _inject_planned_fault(self, stage: str) -> None:
        # Fake-only: simulate loss of power at a fixed, explicitly authorized
        # test stage.  This is not a cancel and is never a normal power cycle.
        try:
            cut = getattr(self.backends.power.client, "planned_cut", None)
            if callable(cut):
                cut()
        except Exception:
            pass
        self._set_power_state("off")
        self.state["media"]["incomplete"] = True
        self.state["interruption"] = {
            "kind": "planned_power_fault",
            "stage": stage,
            "authorized": True,
        }
        self._save()
        self._event("planned_power_fault_injected", stage=stage)

    def _maybe_crash(self, stage: str) -> None:
        if self.crash_at != stage:
            return
        self.state["interruption"] = {
            "kind": "controller_crash",
            "stage": stage,
            "resume_allowed": False,
        }
        if self.state["media"]["write_started"] and not self.state["media"]["verified"]:
            self.state["media"]["incomplete"] = True
        self.state["next_action"] = (
            "status must detect the dead process; inspect residual write processes; "
            "no implicit resume and no blind power cut"
        )
        self._save()
        self._event("process_interrupted", stage=stage, pid=os.getpid())
        os._exit(70)

    def _set_power_state(self, state: str) -> None:
        self.state["power"]["state"] = state
        self.state["power"]["last_result"] = "confirmed" if state in ("on", "off") else "unknown"
        self._save()

    def _mark_cancel(self, stage: str) -> None:
        self.state["cancel_requested"] = True
        self.state["cancel_requested_at"] = utc_now()
        self._save()
        self._event("cancel_requested", stage=stage)

    def _stop_serial(self) -> None:
        if self.backends is None:
            return
        try:
            self.backends.serial.stop()
        except Exception:
            pass
        self.state["serial"]["started"] = False
        self.state["serial"]["stopped"] = True

    def _set_phase(self, phase: str) -> None:
        validate_transition(self.state["phase"], phase)
        self.state["phase"] = phase
        self._save()
        self._event("phase_changed", phase=phase)

    def _verify_plan_file(self, stage: str) -> None:
        try:
            current = load_plan(self.plan_path)
        except Refused as exc:
            raise HardwareFailure("plan_changed", f"plan invalid at {stage}: {exc}") from None
        if (current.get("integrity") or {}).get(
            "payload_sha256"
        ) != (self.plan.get("integrity") or {}).get("payload_sha256"):
            raise HardwareFailure(
                "plan_changed", f"plan changed at {stage}; refusing to continue"
            )

    def _event(self, kind: str, **fields: Any) -> None:
        if kind == "power_action":
            result = fields.get("result")
            action = fields.get("action")
            if result == "confirmed" and action in ("on", "off"):
                state = action
            elif result == "already_reported":
                state = self.state["power"].get("state", "unknown")
            else:
                state = "unknown"
            power = self.state.setdefault("power", {})
            power.update(
                {
                    "state": state,
                    "last_action": action,
                    "last_result": result,
                }
            )
            if self.execution_mode == "real":
                self.state["hardware_actions"]["power_api_called"] = True
        self.store.append_event(
            kind, simulated=self.state.get("simulated") is True, **fields
        )

    def _save(self) -> None:
        self.store.save(self.state)

    # ------------------------------------------------------------------
    # terminal states
    # ------------------------------------------------------------------
    def _finish_cancelled(self, stage: str) -> None:
        if self.state["phase"] not in ("cancelled",):
            self._set_phase("cancelled")
        self.state["status"] = "cancelled"
        self.state["cancel_requested"] = True
        self.state["interruption"] = {
            "kind": "cancel",
            "stage": stage,
            "power_cut": False,
        }
        if self.state["media"]["write_started"] and not self.state["media"]["verified"]:
            self.state["media"]["incomplete"] = True
        self.state["next_action"] = (
            "keep power; inspect the medium; start a new full run (no implicit resume)"
        )
        self._save()
        self._event(
            "run_cancelled",
            stage=stage,
            power=self.state["power"]["state"],
            power_cut=False,
        )

    def _finish_power_unknown(self, failure: PowerUnknown) -> None:
        self.state["failure"] = {
            "code": failure.code,
            "message": str(failure),
            "manual_recovery": True,
        }
        self.state["interruption"] = {
            "kind": "power_state_unknown",
            "action": failure.action,
        }
        recovered = False
        if failure.action == "off" and not self._recovery_attempted:
            self._recovery_attempted = True
            recovered = self.power.recover_unknown_off_once()
        self.state["power"]["state"] = "on" if recovered else "unknown"
        self.state["power"]["last_result"] = "confirmed" if recovered else "unknown"
        self.state["status"] = "failed"
        if self.state["media"]["write_started"] and not self.state["media"]["verified"]:
            self.state["media"]["incomplete"] = True
        self.state["next_action"] = (
            "power recovery succeeded; inspect media and re-plan"
            if recovered
            else "manual power-on/inspection required; do not blind-retry OFF"
        )
        self._save()
        self._event(
            "run_failed",
            code=failure.code,
            message=str(failure),
            manual_recovery=not recovered,
            power=self.state["power"]["state"],
        )

    def _finish_failed(self, failure: HardwareFailure) -> None:
        self.state["status"] = "failed"
        self.state["failure"] = {
            "code": failure.code,
            "message": str(failure),
            "manual_recovery": failure.manual_recovery,
        }
        if self.state["media"]["write_started"] and not self.state["media"]["verified"]:
            self.state["media"]["incomplete"] = True
        if failure.code in (
            "hash_mismatch",
            "target_changed",
            "write_incomplete",
            "readback_short",
            "readback_incomplete",
            "boot_not_observed",
            "boot_evidence_missing",
            "boot_evidence_incomplete",
            "post_boot_validation_not_implemented",
            "safe_cut_not_verified",
        ):
            self.state["interruption"] = {
                "kind": failure.code,
                "power_cut": False,
            }
        self.state["next_action"] = FAILURE_NEXT_ACTION.get(
            failure.code,
            "keep power; inspect the run state and start a new full run",
        )
        self._save()
        self._event(
            "run_failed",
            code=failure.code,
            message=str(failure),
            manual_recovery=failure.manual_recovery,
            power=self.state["power"]["state"],
        )


def execute(
    plan: dict[str, Any],
    *,
    plan_path: Path,
    state_root: Path,
    backend_factory: Callable[[HeldLock], BackendSet],
    execution_mode: str,
    run_id: str | None = None,
    authorization: dict[str, Any] | None = None,
    confirm_board: str | None = None,
    allow_planned_fault: bool = False,
    planned_fault_at: str | None = None,
    crash_at: str | None = None,
    boot_timeout: float = 30.0,
    cut_hold_seconds: int = CUT_HOLD_SECONDS,
    cut_evidence_provider: Callable[[str], CutEvidence] | None = None,
    cut_evidence_validator: Callable[[CutEvidence], None] | None = None,
    cut_session_id: str | None = None,
    candidate_manifest: CandidateManifest | None = None,
    runtime_identity_provider: Callable[[], RuntimeIdentity] | None = None,
    boot_identity_binding: BootIdentityBinding | None = None,
    target_map_provider: Callable[[], TargetMap] | None = None,
    ums_prepare_provider: Callable[[], None] | None = None,
    normal_shutdown_provider: Callable[[], CutEvidence] | None = None,
    cold_boot_prepare_provider: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[RunEngine, dict[str, Any]]:
    engine = RunEngine(
        plan,
        plan_path=plan_path,
        state_root=state_root,
        backend_factory=backend_factory,
        execution_mode=execution_mode,
        run_id=run_id,
        authorization=authorization,
        confirm_board=confirm_board,
        allow_planned_fault=allow_planned_fault,
        planned_fault_at=planned_fault_at,
        crash_at=crash_at,
        boot_timeout=boot_timeout,
        cut_hold_seconds=cut_hold_seconds,
        cut_evidence_provider=cut_evidence_provider,
        cut_evidence_validator=cut_evidence_validator,
        cut_session_id=cut_session_id,
        candidate_manifest=candidate_manifest,
        runtime_identity_provider=runtime_identity_provider,
        boot_identity_binding=boot_identity_binding,
        target_map_provider=target_map_provider,
        ums_prepare_provider=ums_prepare_provider,
        normal_shutdown_provider=normal_shutdown_provider,
        cold_boot_prepare_provider=cold_boot_prepare_provider,
        sleep=sleep,
        clock=clock,
    )
    return engine, engine.run()


def request_cancel(
    state_root: Path, run_id: str, *, reason: str = "user"
) -> dict[str, Any]:
    """Persist a cancel request.  This function cannot cut power."""
    store = RunStore(state_root, run_id)
    state = store.load()
    if state.get("status") in ("passed", "failed", "cancelled", "interrupted"):
        raise Refused(
            f"run {run_id} is already terminal ({state.get('status')}); nothing to cancel"
        )
    signal = CancelSignal(store)
    signal.request(reason=reason)
    state["cancel_requested"] = True
    state["cancel_requested_at"] = utc_now()
    store.save(state)
    store.append_event(
        "cancel_requested",
        simulated=state.get("simulated") is True,
        stage=state.get("phase"),
        power_cut=False,
    )
    return store.public_summary(state) | {"cancel": signal.info()}
