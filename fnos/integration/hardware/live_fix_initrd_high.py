#!/usr/bin/env python3
"""Live bring-up: stop U-Boot, set initrd_high in RAM, run distro_bootcmd.

The r3 boot.log shows ``ERROR: Failed to allocate 0x120b810a bytes below
0x90000000 / ramdisk - allocation error`` because U-Boot tries to relocate the
302 MB initrd below 0x90000000 while ``ramdisk_addr_r=0xc0000000``.  This entry
sets ``initrd_high=0xffffffff`` (RAM only, no saveenv), then runs the normal
``distro_bootcmd`` and captures the boot.  No disk write, no CP5 action.
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
OUT = ROOT / "fnos/logs/fnos-x5-candidate-r3-20261106/initrd-high-boot"
LOGIN = "login:"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--capture-seconds", type=int, default=180)
    args = ap.parse_args()
    plan = {"schema": "fnos-initrd-high-boot-plan/v1", "execute": args.execute,
            "serial": PORT, "setenv": "initrd_high 0xffffffff (RAM only, no saveenv)",
            "command": "run distro_bootcmd", "disk_write": False, "power_action": False}
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if not args.execute:
        return 0
    os.environ.setdefault("FNOS_LAB_ENABLE_REAL_BACKENDS", "1")
    out = OUT / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=False)
    capture = CaptureLog(out / "serial.log", clock=time.monotonic, wall=time.time)
    result = {"schema": "fnos-initrd-high-boot/v1", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "plan": plan, "space_sent": 0, "setenv_sent": False, "status": "started"}
    session = None
    try:
        with board_lock(LOCK_ROOT, BOARD) as held:
            session = SerialSession(board_id=BOARD, held=held, port=PORT, state_root=LOCK_ROOT)
            state = session.open(passive_timeout=1.0)
            result["open_state"] = state
            session.transport = CaptureTee(session.transport, capture)
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                session.refresh(0.1)
                if session.state == STATE_UBOOT:
                    break
                if session._countdown_mono is not None:
                    capture.mark("autoboot-stop")
                    try:
                        session.interrupt_autoboot(timeout=3.0)
                        result["space_sent"] = 1
                        break
                    except Exception as exc:  # stale countdown / timeout
                        result.setdefault("interrupt_errors", []).append(
                            f"{type(exc).__name__}: {exc}")
            result["after_stop_state"] = session.state
            if session.state != STATE_UBOOT:
                raise SystemExit(f"no U-Boot prompt (state={session.state})")
            capture.mark("setenv")
            commands = [
                b"setenv initrd_high 0xffffffff\n",
                b"setenv bootm_low 0x84000000\n",
                b"setenv bootm_size 0x100000000\n",
            ]
            for command in commands:
                session.transport.write(command)
                capture.write(command)
                session.refresh(0.3)
            result["setenv_sent"] = True
            result["setenv_commands"] = [c.decode().strip() for c in commands]
            capture.mark("distro-bootcmd")
            boot = b"run distro_bootcmd\n"
            session.transport.write(boot)
            capture.write(boot)
            deadline = time.monotonic() + max(30, args.capture_seconds)
            while time.monotonic() < deadline:
                session.refresh(0.2)
                if LOGIN in session.text():
                    result["status"] = "login"
                    break
            else:
                result["status"] = "timeout"
            text = session.text()
            result["kernel_seen"] = "Linux version" in text
            result["login_seen"] = LOGIN in text
            result["ramdisk_alloc_error"] = "ramdisk - allocation error" in text
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
        print("last_text:", result.get("last_text", "")[-1000:])
    return 0 if result.get("status") == "login" else 2


if __name__ == "__main__":
    sys.exit(main())
