#!/usr/bin/env python3
"""Fallback: leave an already-running U-Boot UMS session and boot the SD.

Use only if the passive post-flash boot reports that U-Boot re-entered the
"UMS update system" path instead of `distro_bootcmd`.  It sends exactly one
Ctrl+C to the U-Boot UMS command, waits for `Hobot>`, then sends one
`run distro_bootcmd` and records the boot output.  No disk write, no CP5 power
action, no password login.

  FNOS_LAB_ENABLE_REAL_BACKENDS=1 python3 \
    fnos/integration/hardware/ums_boot_fallback.py --execute
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fnos.integration.hardware.boot_r3 import CaptureLog, CaptureTee  # noqa: E402
from fnos.lab.locks import board_lock  # noqa: E402
from fnos.lab.uart_session import (  # noqa: E402
    STATE_LINUX_BOOT, STATE_LINUX_LOGIN, STATE_LINUX_SHELL, STATE_UBOOT,
    SerialSession,
)

BOARD = "x5-lab-01"
PORT = "/dev/cu.usbserial-13220"
LOCK_ROOT = Path("/home/your-user/.local/state/fnos-lab")
OUT = ROOT / "fnos/logs/fnos-x5-candidate-r3-20261106/postflash-boot-fallback"
LOGIN_RE = __import__("re").compile(r"(?im)^[^\r\n]{0,64}\blogin:\s*$")
PROMPT_RE = __import__("re").compile(r"(?m)^\s*(Hobot>|=>)\s*$")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--out-dir", type=Path, default=OUT)
    ap.add_argument("--max-seconds", type=int, default=300)
    args = ap.parse_args()
    plan = {"schema": "fnos-ums-boot-fallback-plan/v1", "execute": args.execute,
            "serial": PORT, "board": BOARD, "actions": ["one Ctrl+C", "run distro_bootcmd"],
            "disk_write": False, "power_action": False}
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if not args.execute:
        return 0
    os.environ.setdefault("FNOS_LAB_ENABLE_REAL_BACKENDS", "1")
    out = args.out_dir / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=False)
    capture = CaptureLog(out / "serial.log", clock=time.monotonic, wall=time.time)
    result = {"schema": "fnos-ums-boot-fallback/v1", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "plan": plan, "ctrl_c": 0, "command": None, "status": "started"}
    session = None
    try:
        with board_lock(LOCK_ROOT, BOARD) as held:
            session = SerialSession(board_id=BOARD, held=held, port=PORT,
                                    state_root=LOCK_ROOT)
            state = session.open(passive_timeout=1.0)
            result["open_state"] = state
            session.transport = CaptureTee(session.transport, capture)
            if state in (STATE_LINUX_BOOT, STATE_LINUX_LOGIN, STATE_LINUX_SHELL):
                raise SystemExit(f"refusing: board is already in Linux state {state}")
            capture.mark("takeover", state=state)
            if state != STATE_UBOOT:
                session.transport.write(b"\x03")
                capture.write(b"\x03")
                result["ctrl_c"] = 1
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    session.refresh(0.2)
                    if session.state == STATE_UBOOT:
                        break
                result["after_ctrl_c"] = session.state
                if session.state != STATE_UBOOT:
                    raise SystemExit(f"no U-Boot prompt after one Ctrl+C (state={session.state})")
            capture.mark("distro-bootcmd", state=session.state)
            command = b"run distro_bootcmd\n"
            session.transport.write(command)
            capture.write(command)
            result["command"] = "run distro_bootcmd"
            deadline = time.monotonic() + max(10, args.max_seconds)
            last = time.monotonic()
            while time.monotonic() < deadline:
                session.refresh(0.2)
                text = session.text()
                if LOGIN_RE.search(text):
                    result["status"] = "login"
                    break
                if time.monotonic() - last > 30:
                    capture.flush(fsync=True)
                    last = time.monotonic()
            else:
                result["status"] = "timeout"
            text = session.text()
            result["kernel_seen"] = "Linux version" in text
            result["login_seen"] = bool(LOGIN_RE.search(text))
            result["last_text"] = text[-2000:]
    except SystemExit as exc:
        result["status"] = "refused"
        result["error"] = str(exc)
        raise
    except Exception as exc:  # noqa: BLE001
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        capture.close()
        result["capture"] = capture.summary()
        (out / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if result.get("status") == "login" else 2


if __name__ == "__main__":
    sys.exit(main())
