"""Persistent per-run state and append-only event log.

The simulator and ``status`` share this store.  A state file is replaced
atomically; events are appended with ``O_NOFOLLOW`` and fsynced.  Run ids are
strict so a state directory cannot be escaped through the CLI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
from typing import Any

from .errors import Refused
from .model import EVENT_SCHEMA, STATE_SCHEMA
from .util import atomic_write_json, pid_alive, read_json, secure_dir, utc_now

RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def new_run_id() -> str:
    stamp = utc_now().replace("-", "").replace(":", "").replace("T", "t").replace("Z", "z")
    return f"run-{stamp}-{secrets.token_hex(4)}"


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise Refused(
            "run id must be 1..64 characters of letters, digits, dot, dash or underscore"
        )
    return run_id


class RunStore:
    def __init__(self, state_root: Path, run_id: str):
        self.state_root = Path(state_root).expanduser()
        self.run_id = validate_run_id(run_id)
        self.run_dir = self.state_root / "runs" / self.run_id
        self.state_path = self.run_dir / "state.json"
        self.events_path = self.run_dir / "events.jsonl"

    def exists(self) -> bool:
        return self.run_dir.exists()

    def create(self, state: dict[str, Any]) -> None:
        secure_dir(self.state_root / "runs")
        # mkdir is the atomic occupancy claim: exactly one process can create
        # the directory, regardless of board id or prior exists() checks.
        try:
            os.mkdir(self.run_dir, 0o700)
        except FileExistsError:
            raise Refused(
                f"run {self.run_id} already exists; implicit resume is not allowed"
            ) from None
        except OSError as exc:
            raise Refused(f"cannot create run directory: {exc.strerror}") from None
        try:
            self.run_dir.chmod(0o700)
        except OSError:
            pass
        atomic_write_json(self.state_path, state, mode=0o600)

    def load(self) -> dict[str, Any]:
        if not self.run_dir.is_dir():
            raise Refused(f"run {self.run_id} does not exist in this state directory")
        state = read_json(self.state_path)
        if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA:
            raise Refused("run state has an unsupported schema")
        return state

    def save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = utc_now()
        atomic_write_json(self.state_path, state, mode=0o600)

    def append_event(
        self, kind: str, *, simulated: bool | None = None, **fields: Any
    ) -> dict[str, Any]:
        """Append one sanitized event.  Callers pass whitelisted fields only."""
        event = {
            "schema": EVENT_SCHEMA,
            "ts": utc_now(),
            "run_id": self.run_id,
            "simulated": True if simulated is None else bool(simulated),
            "kind": kind,
            **fields,
        }
        line = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        if not self.run_dir.is_dir():
            raise Refused("cannot append an event before the run directory exists")
        try:
            fd = os.open(
                self.events_path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                0o600,
            )
        except OSError as exc:
            raise Refused(f"cannot append event log: {exc.strerror}") from None
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
        return event

    def read_events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        events = []
        with open(self.events_path, "r", encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events

    def public_summary(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "schema": state.get("schema"),
            "simulated": state.get("simulated") is True,
            "write_eligible": bool(
                (state.get("execution") or {}).get("write_eligible", False)
            ),
            "board_id": state.get("board_id"),
            "phase": state.get("phase"),
            "status": state.get("status"),
            "pid": state.get("pid"),
            "pid_alive": pid_alive(state.get("pid")),
            "power": state.get("power"),
            "media": state.get("media"),
            "cancel_requested": state.get("cancel_requested"),
            "failure": state.get("failure"),
            "next_action": state.get("next_action"),
            "resume_allowed": False,
            "resume_policy": state.get("resume_policy"),
            "created_at": state.get("created_at"),
            "updated_at": state.get("updated_at"),
        }
