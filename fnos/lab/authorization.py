"""Explicit, plan-bound authorization for real hardware execution.

A command-line boolean is not an execution gate.  The gate used by ``run`` is:

1. ``FNOS_LAB_ENABLE_REAL_BACKENDS=1`` in the process environment;
2. a real plan (``simulated=false``) and an explicit ``--backends real``;
3. ``--authorize-write`` plus ``--confirm-board <board_id>``;
4. a token signed with a device-local key and bound to the plan digest, image,
   target identity and board id;
5. the unified board lock (adopted or acquired by ``run``).

The token is created by the separate ``authorize`` command.  It is deliberately
not a field in the plan: editing ``write_eligible``/``authorized`` in the plan
cannot create one.  Local root can still read the key; this protects against
accidental/automated bypass, not against a hostile administrator.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import time
from typing import Any

from .errors import AuthorizationError, Refused
from .util import (
    atomic_write_json,
    canonical_json_bytes,
    read_json,
    secure_dir,
)

TOKEN_SCHEMA = "fnos-lab-authorization/v1"
TOKEN_SCOPE = "full-flash"
KEY_BYTES = 32
DEFAULT_TTL_SECONDS = 1800
MAX_TTL_SECONDS = 6 * 3600


def real_gate_enabled() -> bool:
    """The real-hardware switch is deliberately an environment opt-in."""
    return os.environ.get("FNOS_LAB_ENABLE_REAL_BACKENDS") == "1"


def require_real_gate() -> None:
    if not real_gate_enabled():
        raise AuthorizationError(
            "real hardware backends are disabled by default; set "
            "FNOS_LAB_ENABLE_REAL_BACKENDS=1 only for a reviewed board operation"
        )


def _private_dir(state_root: Path) -> Path:
    return secure_dir(Path(state_root).expanduser() / "private")


def _key_path(state_root: Path) -> Path:
    return _private_dir(state_root) / "device.key"


def load_or_create_key(state_root: Path) -> bytes:
    """Load the 0600 device key, creating it atomically on first use."""
    path = _key_path(state_root)
    if path.is_symlink():
        raise Refused("device authorization key must not be a symlink")
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        fd = -1
    except OSError as exc:
        raise Refused(f"cannot create authorization key: {exc.strerror}") from None
    if fd >= 0:
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, secrets.token_bytes(KEY_BYTES))
            os.fsync(fd)
        finally:
            os.close(fd)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise Refused(f"cannot open authorization key: {exc.strerror}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refused("authorization key is not a regular file")
        if info.st_nlink != 1:
            raise Refused("authorization key has extra hard links")
        os.fchmod(fd, 0o600)
        key = os.read(fd, KEY_BYTES + 1)
    finally:
        os.close(fd)
    if len(key) != KEY_BYTES:
        raise Refused("authorization key has an unexpected length")
    return key


def _plan_binding(plan: dict[str, Any]) -> dict[str, Any]:
    image = plan.get("image") or {}
    target = plan.get("target") or {}
    board = plan.get("board") or {}
    return {
        "board_id": board.get("board_id"),
        "plan_payload_sha256": (plan.get("integrity") or {}).get("payload_sha256"),
        "image_sha256": image.get("sha256"),
        "image_size_bytes": image.get("size_bytes"),
        "target": {
            "device": target.get("device"),
            "serial": target.get("serial"),
            "model": target.get("model"),
            "size_bytes": target.get("size_bytes"),
            "bus": target.get("bus"),
        },
    }


def issue_authorization(
    plan: dict[str, Any],
    *,
    state_root: Path,
    scope: str = TOKEN_SCOPE,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> dict[str, Any]:
    """Create a signed capability for exactly this plan/target/image."""
    if scope != TOKEN_SCOPE:
        raise Refused(f"unsupported authorization scope: {scope}")
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool):
        raise Refused("authorization TTL must be an integer number of seconds")
    if not 30 <= ttl_seconds <= MAX_TTL_SECONDS:
        raise Refused(f"authorization TTL must be 30..{MAX_TTL_SECONDS} seconds")
    binding = _plan_binding(plan)
    for field in ("board_id", "plan_payload_sha256", "image_sha256"):
        if not isinstance(binding[field], str) or not binding[field]:
            raise Refused(f"cannot authorize a plan without {field}")
    if not isinstance(binding["target"].get("device"), str):
        raise Refused("cannot authorize a plan without a target device")
    issued = int(now if now is not None else time.time())
    payload = {
        "schema": TOKEN_SCHEMA,
        "scope": scope,
        "issued_at": issued,
        "expires_at": issued + ttl_seconds,
        "nonce": secrets.token_hex(16),
        **binding,
    }
    signature = hmac.new(
        load_or_create_key(Path(state_root)),
        canonical_json_bytes(payload),
        hashlib.sha256,
    ).hexdigest()
    return {"schema": TOKEN_SCHEMA, "payload": payload, "signature": signature}


def token_fingerprint(token: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(token)).hexdigest()


def verify_authorization(
    token: Any,
    plan: dict[str, Any],
    *,
    state_root: Path,
    now: float | None = None,
    scope: str = TOKEN_SCOPE,
) -> dict[str, Any]:
    """Verify signature, scope, expiry and every plan-bound identity field."""
    if not isinstance(token, dict) or token.get("schema") != TOKEN_SCHEMA:
        raise AuthorizationError("authorization token has an unsupported schema")
    payload = token.get("payload")
    signature = token.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature, str):
        raise AuthorizationError("authorization token is malformed")
    expected = hmac.new(
        load_or_create_key(Path(state_root)),
        canonical_json_bytes(payload),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise AuthorizationError("authorization token signature is invalid")
    if payload.get("scope") != scope:
        raise AuthorizationError("authorization token scope does not match this run")
    current = int(now if now is not None else time.time())
    try:
        expires = int(payload["expires_at"])
        issued = int(payload["issued_at"])
    except (KeyError, TypeError, ValueError):
        raise AuthorizationError("authorization token has no valid validity window") from None
    if issued > current + 300:
        raise AuthorizationError("authorization token was issued in the future")
    if current >= expires:
        raise AuthorizationError("authorization token has expired")
    if not isinstance(payload.get("nonce"), str) or len(payload["nonce"]) < 16:
        raise AuthorizationError("authorization token lacks a nonce")

    binding = _plan_binding(plan)
    for field in ("board_id", "plan_payload_sha256", "image_sha256", "image_size_bytes"):
        if payload.get(field) != binding.get(field):
            raise AuthorizationError(
                f"authorization token is not bound to this plan ({field} mismatch)"
            )
    token_target = payload.get("target")
    if not isinstance(token_target, dict):
        raise AuthorizationError("authorization token has no target binding")
    for field in ("device", "serial", "model", "size_bytes", "bus"):
        if token_target.get(field) != binding["target"].get(field):
            raise AuthorizationError(
                f"authorization token target changed ({field} mismatch)"
            )
    return payload


def save_authorization(path: Path, token: dict[str, Any]) -> None:
    path = Path(path).expanduser()
    atomic_write_json(path, token, mode=0o600, overwrite=False)


def load_authorization(path: Path) -> dict[str, Any]:
    path = Path(path).expanduser()
    value = read_json(path)
    if not isinstance(value, dict):
        raise AuthorizationError("authorization file root must be an object")
    return value
