#!/usr/bin/env python3
"""Small serial-console helper: log in as root and run commands.

The board's root password lives only in the macOS Keychain (service
``fnos-root``).  Prompts are matched on fresh bytes, never on accumulated
scrollback, because the fnOS getty reprints its banner.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fnos.lab.locks import board_lock  # noqa: E402
from fnos.lab.uart_session import SerialSession  # noqa: E402

BOARD = "x5-lab-01"
PORT = "/dev/cu.usbserial-13220"
LOCK_ROOT = Path("/home/your-user/.local/state/fnos-lab")


def password() -> str:
    out = subprocess.run(["security", "find-generic-password", "-s", "fnos-root", "-w"],
                         capture_output=True, text=True, check=False)
    if out.returncode != 0 or not out.stdout.strip():
        raise SystemExit("root password not in Keychain (service fnos-root)")
    return out.stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("commands", nargs="*")
    ap.add_argument("--timeout", type=float, default=20.0)
    args = ap.parse_args()
    if not args.commands:
        raise SystemExit("give at least one command")
    with board_lock(LOCK_ROOT, BOARD) as held:
        s = SerialSession(board_id=BOARD, held=held, port=PORT, state_root=LOCK_ROOT)
        s.open(passive_timeout=0.5)

        def fresh_read(seconds: float = 0.5) -> None:
            d = s.transport.read(seconds, 65536)
            if d:
                s._note_rx(d)

        def marker() -> int:
            return len(s.text())

        def tail_from(mark: int) -> str:
            return s.text()[mark:]

        # login prompt (fresh)
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            fresh_read(0.4)
            if tail_from(max(0, marker() - 80)).rstrip().endswith("trim login:"):
                break
        s.transport.write(b"root\r")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            fresh_read(0.4)
            if "Password:" in tail_from(max(0, marker() - 120)):
                break
        s.transport.write(password().encode() + b"\r")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            fresh_read(0.4)
            if re.search(r"[#$] $", s.text()[-40:]):
                break
        if not re.search(r"[#$] $", s.text()[-40:]):
            raise SystemExit("root login did not reach a shell prompt")
        for command in args.commands:
            mark = marker()
            s.transport.write(command.encode() + b"\n")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                fresh_read(0.3)
                if s.text()[mark:].rstrip().endswith("#"):
                    break
            print(f"### {command}")
            print(tail_from(mark).replace("\r", ""))
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
