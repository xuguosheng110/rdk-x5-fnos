"""Read a run, detect a dead owner/residual writer, and never resume."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import Refused
from .model import RESUME_POLICY
from .plan import load_plan
from .state import RunStore
from .util import pid_alive, read_json, sha256_file

INTERRUPTED_NEXT_ACTION = (
    "inspect residual process/media; keep power; start a new full run "
    "(no implicit resume, no blind power cut)"
)

FALSE_ACTIONS = {
    "serial_opened": False,
    "power_api_called": False,
    "disk_written": False,
    "ssh_connected": False,
}


def _plan_integrity(state: dict[str, Any]) -> tuple[bool, dict[str, Any] | None, str | None]:
    path = state.get("plan_path")
    if not isinstance(path, str) or not path:
        return False, None, "state has no plan path"
    try:
        plan = load_plan(Path(path))
    except Refused as exc:
        return False, None, str(exc)
    payload_sha = (plan.get("integrity") or {}).get("payload_sha256")
    if payload_sha != state.get("plan_payload_sha256"):
        return False, plan, "plan changed since the run started"
    return True, plan, None


def _image_status(
    plan: dict[str, Any] | None, *, verify_hash: bool
) -> tuple[str, dict[str, Any] | None]:
    if not plan:
        return "unknown", None
    image = plan.get("image") or {}
    path = image.get("path")
    if not isinstance(path, str):
        return "unknown", None
    image_path = Path(path)
    if image_path.is_symlink():
        return "changed", {"reason": "image path became a symlink"}
    if not image_path.is_file():
        return "missing", {"path": path}
    try:
        stat = image_path.stat()
    except OSError:
        return "unknown", {"path": path, "reason": "cannot stat image"}
    observed = {
        "path": path,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    if stat.st_size != image.get("size_bytes"):
        return "changed", observed | {"reason": "size differs from plan"}
    if stat.st_mtime_ns != image.get("mtime_ns"):
        if verify_hash:
            try:
                digest, _ = sha256_file(image_path)
            except OSError:
                return "unknown", observed | {"reason": "cannot hash image"}
            observed["sha256"] = digest
            if digest != image.get("sha256"):
                return "changed", observed | {"reason": "SHA-256 differs from plan"}
            return "unchanged", observed | {"reason": "mtime changed but content matches"}
        return "maybe_changed", observed | {
            "reason": "mtime differs from plan; re-run with --verify-image to hash"
        }
    return "unchanged", observed


def _cancel_info(store: RunStore) -> dict[str, Any] | None:
    path = store.run_dir / "cancel.request"
    if not path.exists():
        return None
    try:
        value = read_json(path)
    except Refused:
        return {"schema": "fnos-lab-cancel/v1", "reason": "unreadable"}
    return value if isinstance(value, dict) else None


def inspect_run(
    state_root: Path,
    run_id: str,
    *,
    verify_hash: bool = False,
) -> dict[str, Any]:
    store = RunStore(state_root, run_id)
    state = store.load()

    was_running = state.get("status") == "running"
    alive = pid_alive(state.get("pid"))
    children = state.get("active_children") or []
    residual = []
    if isinstance(children, list):
        for child in children:
            if not isinstance(child, dict):
                continue
            pid = child.get("pid")
            if pid_alive(pid):
                residual.append({**child, "alive": True})
    residual_write = any(
        child.get("kind") in ("flash", "writer", "cp5") for child in residual
    )

    if was_running and not alive:
        state["status"] = "interrupted"
        if not isinstance(state.get("interruption"), dict):
            state["interruption"] = {
                "kind": "controller_crash_or_kill",
                "stage": state.get("phase"),
                "resume_allowed": False,
            }
        state["next_action"] = INTERRUPTED_NEXT_ACTION
        if state.get("media", {}).get("write_started") and not state.get(
            "media", {}
        ).get("verified"):
            state["media"]["incomplete"] = True
        store.save(state)
        store.append_event(
            "interrupted_detected",
            simulated=state.get("simulated") is True,
            pid=state.get("pid"),
            phase=state.get("phase"),
            residual_write_process=residual_write,
            reason="recorded process is no longer alive",
        )

    plan_valid, plan, plan_error = _plan_integrity(state)
    image_status, image_observed = _image_status(plan, verify_hash=verify_hash)
    summary = store.public_summary(state)
    actions = dict(FALSE_ACTIONS)
    if isinstance(state.get("hardware_actions"), dict):
        for key in actions:
            actions[key] = state["hardware_actions"].get(key) is True
    summary.update(
        {
            "interrupted": state.get("status") == "interrupted",
            "recorded_process_alive": alive,
            "plan_valid": plan_valid,
            "plan_error": plan_error,
            "image_status": image_status,
            "image_observed": image_observed,
            "hardware_actions": actions,
            "execution": state.get("execution"),
            "serial": state.get("serial"),
            "target": state.get("target"),
            "validation": state.get("validation"),
            "interruption": state.get("interruption"),
            "active_children": residual,
            "residual_write_process": residual_write,
            "cancel": _cancel_info(store),
            "authorization": state.get("authorization"),
            "resume_allowed": False,
            "resume_policy": RESUME_POLICY,
            "state_valid": True,
        }
    )
    return summary
