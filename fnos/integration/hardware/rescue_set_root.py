#!/usr/bin/env python3
"""One-time rescue: boot the initramfs shell, set a root password, reboot.

Needed because the r3 image ships ``root:!`` (locked) and the board has no
network link, so the fnOS Web first-boot cannot be reached.  This entry:

1. CP5 USB OFF/ON (board is idle at login);
2. stops autoboot and manually loads DTB/Image/initrd with
   ``rdinit=/bin/sh`` (the r3 initrd is a normal gzip cpio with busybox);
3. mounts the ext4 rootfs, replaces the locked root password field with a
   SHA-512 crypt hash, verifies only SET/LOCKED (never prints the hash);
4. ``busybox reboot -f`` and waits for the normal unattended boot to login.

The password itself lives only in the macOS Keychain (service ``fnos-root``)
and a chmod-600 hash file; it is never written to the repo or the serial log.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fnos.integration.hardware.boot_r3 import CaptureLog, CaptureTee  # noqa: E402
from fnos.lab.locks import board_lock  # noqa: E402
from fnos.lab.power import Cp5CliClient  # noqa: E402
from fnos.lab.uart_session import STATE_UBOOT, SerialSession  # noqa: E402

BOARD = "x5-lab-01"
PORT = "/dev/cu.usbserial-13220"
LOCK_ROOT = Path("/home/your-user/.local/state/fnos-lab")
CP5_SCRIPT = ROOT / "scripts/mijia-cp5/cp5.py"
OUT = ROOT / "fnos/logs/fnos-x5-candidate-r3-20261106/rescue-root"
HASH_FILE = Path("/tmp/fnos-root-hash.txt")

UBOOT = [
    b"mmc dev 1\n",
    b"ext4load mmc 1:2 0x84000000 /boot/hobot/x5-rdk-v1p0.dtb\n",
    b"ext4load mmc 1:2 0x85800000 /boot/Image\n",
    b"ext4load mmc 1:2 0xc0000000 /boot/initrd.img-6.18.0\n",
    b"setenv initrd_size 0x${filesize}\n",
    b'setenv bootargs "console=ttyS0,115200 root=/dev/mmcblk1p2 rw rootwait rootfstype=ext4 rdinit=/bin/sh"\n',
    b"setenv initrd_high 0xffffffff\n",
    b"setenv bootm_low 0x84000000\n",
    b"setenv bootm_size 0x100000000\n",
    b"booti 0x85800000 0xc0000000:${initrd_size} 0x84000000\n",
]




def send_wait(session, capture, command: bytes, timeout: float, delimiter: str = "Hobot>") -> bool:
    """Send one command and wait for a new prompt after the command echo."""
    before = len(session.text())
    session.transport.write(command)
    capture.write(command)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session.refresh(0.2)
        text = session.text()
        if len(text) > before and text.rstrip().endswith(delimiter):
            return True
    return False

def shell_commands(digest: str) -> list[bytes]:
    sed = ("sed -i 's#^root:[^:]*:#root:" + digest + ":#' /root/etc/shadow\n").encode()
    return [
        b"export PATH=/bin:/sbin\n",
        b"mount -t proc proc /proc 2>/dev/null\n",
        b"mount -t sysfs sysfs /sys 2>/dev/null\n",
        b"mount -t devtmpfs devtmpfs /dev 2>/dev/null\n",
        b"for m in ext4 mmc_block; do modprobe $m 2>/dev/null; done\n",
        b"mount -t ext4 -o rw /dev/mmcblk1p2 /root\n",
        sed,
        b"if grep -q '^root:!' /root/etc/shadow; then echo ROOT_STILL_LOCKED; else echo ROOT_PASSWORD_SET; fi\n",
        b"busybox sync\n",
        b"busybox umount /root\n",
        b"busybox reboot -f\n",
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--capture-seconds", type=int, default=150)
    ap.add_argument("--reboot-method", choices=("sysrq", "cp5"), default="sysrq")
    args = ap.parse_args()
    plan = {"schema": "fnos-rescue-root-plan/v1", "execute": args.execute,
            "serial": PORT, "reboot_method": args.reboot_method,
            "uboot": [c.decode().strip() for c in UBOOT],
            "initramfs": "rdinit=/bin/sh + sha512 shadow edit, no hash printed",
            "sd_write": False}
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if not args.execute:
        return 0
    if not HASH_FILE.is_file():
        raise SystemExit(f"missing chmod-600 hash file {HASH_FILE}")
    digest = HASH_FILE.read_text().strip()
    if not digest.startswith("$6$") or len(digest) < 40:
        raise SystemExit("hash file is not a SHA-512 crypt digest")
    os.environ.setdefault("FNOS_LAB_ENABLE_REAL_BACKENDS", "1")

    out = OUT / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=False)
    capture = CaptureLog(out / "serial.log", clock=time.monotonic, wall=time.time)
    result = {"schema": "fnos-rescue-root/v1", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "plan": plan, "status": "started"}
    session = None
    try:
        with board_lock(LOCK_ROOT, BOARD) as held:
            session = SerialSession(board_id=BOARD, held=held, port=PORT, state_root=LOCK_ROOT)
            session.open(passive_timeout=0.5)
            session.transport = CaptureTee(session.transport, capture)
            if args.reboot_method == "cp5":
                client = Cp5CliClient(CP5_SCRIPT, board_id=BOARD, lock=held)
                capture.mark("power-off")
                if client.read_usb():
                    client.set_usb(False)
                time.sleep(6.0)
                capture.mark("power-on")
                client.set_usb(True)
            else:
                import termios
                capture.mark("sysrq-reboot")
                termios.tcsendbreak(session.transport.fd, 0)
                time.sleep(0.3)
                session.transport.write(b"b")
                result["sysrq_break_sent"] = True
            # catch the 1 s autoboot countdown
            deadline = time.monotonic() + 45.0
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
            result["prompt_state"] = session.state
            if session.state != STATE_UBOOT:
                raise SystemExit(f"no U-Boot prompt (state={session.state})")
            capture.mark("uboot-load")
            for line in UBOOT:
                if b"booti " in line:
                    session.transport.write(line)
                    capture.write(line)
                    break
                timeout = 60.0 if b"initrd" in line else 20.0
                if not send_wait(session, capture, line, timeout, "Hobot>"):
                    result["uboot_command_timeout"] = line.decode().strip()
                    raise SystemExit(f"U-Boot command timeout: {line.decode().strip()}")
            result["uboot_text"] = session.text()[-1500:]
            # wait for the initramfs shell prompt
            deadline = time.monotonic() + 60.0
            while time.monotonic() < deadline:
                session.refresh(0.2)
                tail = session.text()[-200:]
                if tail.rstrip().endswith(b"#".decode()):
                    break
            capture.mark("initramfs-shell")
            for line in shell_commands(digest):
                if not send_wait(session, capture, line, 20.0, "#"):
                    result.setdefault("shell_command_timeout", []).append(line.decode().strip())
            result["shell_text"] = session.text()[-2000:]
            result["root_password_set"] = "ROOT_PASSWORD_SET" in result["shell_text"]
            result["root_still_locked"] = "ROOT_STILL_LOCKED" in result["shell_text"]
            # the initramfs reboots; wait for the normal login prompt
            deadline = time.monotonic() + max(60, args.capture_seconds)
            while time.monotonic() < deadline:
                session.refresh(0.2)
                if "login:" in session.text():
                    result["login_seen"] = True
                    result["status"] = "login"
                    break
            else:
                result["status"] = "timeout"
            result["last_text"] = session.text()[-2500:]
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
        print(json.dumps({k: v for k, v in result.items() if k not in ("last_text", "uboot_text", "shell_text")},
                         ensure_ascii=False, indent=2))
        print("last_text:", result.get("last_text", "")[-1200:])
    return 0 if result.get("root_password_set") and result.get("status") == "login" else 2


if __name__ == "__main__":
    sys.exit(main())
