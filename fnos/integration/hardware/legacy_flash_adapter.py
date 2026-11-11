#!/usr/bin/env python3
"""Reviewed adapter for the legacy macOS flash writer.

The old ``flash_macos.py`` can only take a lock *path*.  This adapter does not
hold the board lock itself; it passes the canonical board lock path to the
child, so the child is the one and only owner of the same physical lock used by
``fnos.lab run`` and the CP5 USB entry.  A parent-held lock cannot be combined
with this adapter (``--lock-fd`` is explicitly refused), which prevents the
"parent holds, child tries to acquire the same lock" deadlock.

Direct legacy invocation is *not* silently treated as integrated: the board
binding marks it disabled, and this adapter refuses ``--direct-legacy``.
Manual/HA/other raw paths remain outside the file lock and are listed by
``fnos.lab.registry``.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fnos.lab.authorization import require_real_gate  # noqa: E402
from fnos.lab.errors import Refused  # noqa: E402
from fnos.lab.inspect import load_bindings  # noqa: E402
from fnos.lab.locks import board_lock_path  # noqa: E402
from fnos.lab.registry import require_path_enabled  # noqa: E402

DEFAULT_BINDINGS = ROOT / "fnos" / "integration" / "hardware" / "board-bindings.json"
DEFAULT_LEGACY = Path.home() / ".codex" / "skills" / "rdk-x5-usb-flash" / "scripts" / "flash_macos.py"


def _load(board_id: str, path: Path) -> dict:
    boards = load_bindings(path)
    binding = boards.get(board_id)
    if not isinstance(binding, dict):
        raise Refused(f"board {board_id} is not in the reviewed board bindings")
    if binding.get("reviewed") is not True:
        raise Refused(
            f"board {board_id} is not marked reviewed=true; complete the read-only preflight first"
        )
    require_path_enabled(binding, "legacy-flash-adapter")
    # The direct path remains recorded as disabled_for_board; the adapter must
    # not resurrect it.  It is checked by registry tests, not enabled here.
    return binding


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", required=True)
    parser.add_argument("--bindings", type=Path, default=DEFAULT_BINDINGS)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--device", required=True, help="current whole /dev/diskN")
    parser.add_argument("--target-size", required=True, type=int)
    parser.add_argument("--media-name", default="UMS disk 0")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--legacy-script", type=Path, default=DEFAULT_LEGACY)
    parser.add_argument("--lock-root", type=Path, help="defaults to FNOS_BOARD_LOCK_ROOT")
    parser.add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--direct-legacy", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        if args.direct_legacy:
            raise Refused(
                "direct legacy flash is disabled for this board; use this adapter, "
                "which passes the canonical board lock to the child"
            )
        if args.lock_fd is not None:
            raise Refused(
                "the legacy writer cannot adopt an inherited fd; do not hold the "
                "board lock before invoking it (lab run uses the internal backend)"
            )
        binding = _load(args.board, args.bindings)
        if args.write:
            require_real_gate()
            if os.geteuid() != 0:
                raise Refused(
                    "legacy adapter --write requires root; no command was started"
                )
        lock = board_lock_path(
            args.lock_root or os.environ.get("FNOS_BOARD_LOCK_ROOT"),
            args.board,
        )
        script = Path(args.legacy_script).expanduser()
        if not script.is_file() or script.is_symlink():
            raise Refused("legacy flash script is missing or is a symlink")
        if not args.image.is_absolute() or not args.report.is_absolute():
            raise Refused("image and report paths must be absolute")
        if not args.image.is_file() or args.image.is_symlink():
            raise Refused("image must be an absolute regular file, not a symlink")
        if len(args.sha256) != 64:
            raise Refused("a full 64-hex SHA-256 is required")
        argv_child = [
            sys.executable,
            str(script),
            "--image",
            str(args.image),
            "--sha256",
            args.sha256.lower(),
            "--device",
            args.device,
            "--target-size",
            str(args.target_size),
            "--media-name",
            args.media_name,
            "--lock",
            str(lock),
            "--report",
            str(args.report),
        ]
        if args.write:
            argv_child.append("--write")
        # Do NOT acquire the lock here: the child owns the same canonical file.
        proc = subprocess.run(argv_child, check=False)
        return proc.returncode
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(
            f"FAILED: {type(exc).__name__} (details withheld; no hardware action taken)",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
