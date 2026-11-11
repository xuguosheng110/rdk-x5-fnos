#!/usr/bin/env python3
"""Persist the minimal U-Boot fix and verify an unattended boot.

Two independent defects were observed on 2026-11-06:

1. ``img_update`` always enters the UMS update system when the SD exists, so
   ``distro_bootcmd`` is never reached unattended.
2. ``booti`` fails to relocate the 302 MB initrd below the default
   ``initrd_high=0x90000000`` (``ramdisk - allocation error``).

This entry sets ``img_update`` to a no-op and the initrd/bootm addresses in the
U-Boot environment, runs ``saveenv`` (SPI NAND ``ubootenv``), then ``reset`` and
verifies the board reaches ``login:`` without any serial input.  To flash again,
interrupt autoboot and run ``ums 0 mmc 1`` manually; no SD write here.
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
from fnos.lab.uart_session import STATE_UBOOT, SerialSession  # noqa: E402

BOARD = "x5-lab-01"
PORT = "/dev/cu.usbserial-13220"
LOCK_ROOT = Path("/home/your-user/.local/state/fnos-lab")
OUT = ROOT / "fnos/logs/fnos-x5-candidate-r3-20261106/persist-boot"
COMMANDS = [
    b"setenv img_update 'echo img_update disabled for fnOS'\n",
    b"setenv initrd_high 0xffffffff\n",
    b"setenv bootm_low 0x84000000\n",
    b"setenv bootm_size 0x100000000\n",
    b"saveenv\n",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--capture-seconds", type=int, default=150)
    args = ap.parse_args()
    plan = {"schema": "fnos-persist-boot-plan/v1", "execute": args.execute,
            "serial": PORT, "commands": [c.decode().strip() for c in COMMANDS],
            "then": "reset + passive capture", "sd_write": False, "spi_env_write": True}
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if not args.execute:
        return 0
    os.environ.setdefault("FNOS_LAB_ENABLE_REAL_BACKENDS", "1")
    out = OUT / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=False)
    capture = CaptureLog(out / "serial.log", clock=time.monotonic, wall=time.time)
    result = {"schema": "fnos-persist-boot/v1", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "plan": plan, "status": "started", "saveenv_output": [], "login_seen": False}
    session = None
    try:
        with board_lock(LOCK_ROOT, BOARD) as held:
            session = SerialSession(board_id=BOARD, held=held, port=PORT, state_root=LOCK_ROOT)
            state = session.open(passive_timeout=1.0)
            result["open_state"] = state
            session.transport = CaptureTee(session.transport, capture)
            # The board may be at the prompt, in UMS, or in a fresh boot loop.
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                session.refresh(0.1)
                if session.state == STATE_UBOOT:
                    break
                if session._countdown_mono is not None:
                    try:
                        session.interrupt_autoboot(timeout=3.0)
                        break
                    except Exception:
                        pass
            if session.state != STATE_UBOOT:
                session.transport.write(b"\r\n")
                capture.write(b"\r\n")
                session.refresh(1.0)
            if session.state != STATE_UBOOT:
                # UMS does not print a prompt; one Ctrl+C returns to it.
                session.transport.write(b"\x03")
                capture.write(b"\x03")
                session.refresh(1.5)
            result["prompt_state"] = session.state
            if session.state != STATE_UBOOT:
                raise SystemExit(f"no U-Boot prompt (state={session.state})")
            capture.mark("env")
            for command in COMMANDS:
                session.transport.write(command)
                capture.write(command)
                session.refresh(0.8)
            result["env_text"] = session.text()[-1500:]
            result["saved_env_ok"] = "Saving Environment" in result["env_text"] or "saved" in result["env_text"].lower()
            capture.mark("reset")
            session.transport.write(b"reset\n")
            capture.write(b"reset\n")
            deadline = time.monotonic() + max(30, args.capture_seconds)
            while time.monotonic() < deadline:
                session.refresh(0.2)
                if "login:" in session.text():
                    result["login_seen"] = True
                    result["status"] = "login"
                    break
            else:
                result["status"] = "timeout"
            text = session.text()
            result["kernel_seen"] = "Linux version" in text
            result["ums_seen"] = "UMS update" in text
            result["ramdisk_error"] = "ramdisk - allocation error" in text
            result["last_text"] = text[-3000:]
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
        print(json.dumps({k: v for k, v in result.items() if k != "last_text"},
                         ensure_ascii=False, indent=2))
        print("last_text:", result.get("last_text", "")[-1200:])
    return 0 if result.get("status") == "login" else 2


if __name__ == "__main__":
    sys.exit(main())
