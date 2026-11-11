#!/usr/bin/env python3
"""Single-entry local deployment: verified whole-card backup -> r3 write -> full readback.

The build server (192.0.2.10) is unreachable in the 2026-11-06 night session,
so this entry keeps the same safety gates but stores the backup and verifies the
image locally.  It never powers the board, never starts/exits UMS and never
boots: those stay in the reviewed serial/CP5 entry.

Run as root from a foreground Terminal (deploy_r3_local.command).  Default is a
read-only plan that does not open the raw device.
"""
from __future__ import annotations

import argparse, hashlib, json, os, plistlib, shutil, stat, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fnos.integration.hardware import macos_identity
from fnos.lab.errors import HardwareFailure
from fnos.lab.flash import (MacDiskutilBackend, open_trusted_image, readback_image,
                            require_full_readback, write_image)
from fnos.lab.locks import board_lock
from fnos.lab.uart_session import SessionLock

BOARD = "x5-lab-01"
LOCK_ROOT = "/home/your-user/.local/state/fnos-lab"
READ_CHUNK = 4 * 1024 * 1024
DEFAULT_EVIDENCE = ROOT / "fnos/logs/fnos-x5-candidate-r3-20261106/deploy-local"


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def file_sha(path: Path, *, cancel=None):
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as f:
        while True:
            if cancel is not None and cancel():
                raise SystemExit("cancelled while re-hashing backup")
            chunk = f.read(READ_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
    return h.hexdigest(), total


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dirfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)


class RunState:
    def __init__(self, evidence_dir: Path, run_id: str):
        self.dir = evidence_dir
        self.run_id = run_id
        self.path = evidence_dir / "state.json"
        self.cancel_path = evidence_dir / "CANCEL"

    def phase(self, phase: str, **extra):
        value = {"schema": "fnos-local-deploy-state/v1", "run_id": self.run_id,
                 "phase": phase, "updated_at": now(), **extra}
        atomic_json(self.path, value)
        print(json.dumps(value, ensure_ascii=False), flush=True)

    def cancelled(self) -> bool:
        return self.cancel_path.exists()


def unmount_children(device: str) -> list[str]:
    """Unmount only mounted child volumes; never eject the whole UMS disk.

    macOS 26 can leave a pending whole-disk eject after ``unmountDisk`` on a
    re-enumerated U-Boot gadget, so the whole-disk form is deliberately unused.
    """
    proc = subprocess.run(["diskutil", "list", "-plist", device],
                          capture_output=True, timeout=30, check=False)
    if proc.returncode != 0:
        return []
    try:
        listing = plistlib.loads(proc.stdout)
    except Exception:
        return []
    name = Path(device).name
    targets = []
    for disk in listing.get("AllDisksAndPartitions") or []:
        if not isinstance(disk, dict) or disk.get("DeviceIdentifier") != name:
            continue
        for part in disk.get("Partitions") or []:
            if isinstance(part, dict) and part.get("MountPoint") and part.get("DeviceIdentifier"):
                targets.append("/dev/" + part["DeviceIdentifier"])
    done = []
    for target in targets:
        r = subprocess.run(["diskutil", "unmount", target], capture_output=True,
                           text=True, timeout=60, check=False)
        if r.returncode == 0:
            done.append(target)
    return done


def check_space(directory: Path, need: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(directory).free
    if free < need:
        raise SystemExit(f"insufficient local free space: {free} < {need} at {directory}")


def read_card(raw: str, out_path: Path, expected: int, state: RunState):
    """Stream the whole card to a new exclusive raw file; hash the source stream."""
    fd = os.open(raw, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISCHR(st.st_mode):
            raise SystemExit(f"{raw} is not a character device")
        out = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            h = hashlib.sha256()
            total = 0
            last = time.monotonic()
            while total < expected:
                if state.cancelled():
                    raise SystemExit("cancelled during read-only backup; partial backup kept, no write attempted")
                chunk = os.read(fd, READ_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(out, view)
                    view = view[written:]
                total += len(chunk)
                if time.monotonic() - last >= 30:
                    print(json.dumps({"backup_progress_bytes": total, "expected": expected,
                                      "percent": round(total * 100.0 / expected, 2),
                                      "at": now()}), flush=True)
                    last = time.monotonic()
            os.fsync(out)
        finally:
            os.close(out)
    finally:
        os.close(fd)
    if total != expected:
        raise SystemExit(f"short card read: {total} != {expected}")
    return h.hexdigest(), total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="/dev/disk12")
    ap.add_argument("--image", type=Path,
                    default=Path("/home/your-user/.cache/rdk-fnos-deploy/fnos-x5-candidate-r3.img"))
    ap.add_argument("--image-sha256",
                    default="653ffe7a5e20b019f0a5192799c5ddbda3752ffe5d8f6d67ba87e1afc498f599")
    ap.add_argument("--image-size", type=int, default=13157531648)
    ap.add_argument("--reviewed-target", type=Path,
                    default=ROOT / "fnos/coordination/codex-ums-reviewed-target-20261106.json")
    ap.add_argument("--acceptance", type=Path,
                    default=ROOT / "fnos/coordination/codex-candidate-r3-acceptance-20261106.json")
    ap.add_argument("--backup-dir", type=Path, default=Path("/Volumes/data/fnos-backups/20261106-sd"))
    ap.add_argument("--backup-name", default="sd-current-20261106-local.img")
    ap.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE)
    ap.add_argument("--serial-port", default="/dev/cu.usbserial-13220")
    ap.add_argument("--plan", action="store_true", help="read-only plan (default)")
    ap.add_argument("--no-backup", action="store_true",
                    help="user-authorized: card is empty; skip the whole-card backup and write directly")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    run_id = time.strftime("%Y%m%d-%H%M%S")
    evidence_dir = args.evidence_dir / run_id
    state = RunState(evidence_dir, run_id)

    facts = macos_identity.live(args.device)
    reviewed = json.loads(args.reviewed_target.read_text())
    macos_identity.compare(reviewed, facts)

    acc = json.loads(args.acceptance.read_text())
    acc_sha = acc.get("image_sha256") or (acc.get("candidate") or {}).get("sha256")
    acc_size = acc.get("image_size") or (acc.get("candidate") or {}).get("size")
    if acc.get("accepted") is not True or acc_sha != args.image_sha256 or acc_size != args.image_size:
        raise SystemExit("historical static acceptance does not match this exact image")
    if not args.image.exists() or args.image.is_symlink() or args.image.stat().st_size != args.image_size:
        raise SystemExit("local image is missing, symlinked or has the wrong size")
    backup_path = args.backup_dir / args.backup_name
    if not args.no_backup and backup_path.exists():
        raise SystemExit(f"backup destination already exists: {backup_path}")

    plan = {
        "schema": "fnos-local-deploy-plan/v1",
        "run_id": run_id,
        "execute": args.execute,
        "device": args.device,
        "target": {k: facts.get(k) for k in
                   ("device", "model", "size_bytes", "whole", "internal", "bus", "virtual",
                    "mounts", "partition_count", "usb_gadget_serial", "usb_path")},
        "backup": ({"skipped": True, "reason": "user_authorized_empty_card"}
                   if args.no_backup else {"path": str(backup_path), "expected_bytes": facts["size_bytes"]}),
        "image": {"path": str(args.image), "sha256": args.image_sha256, "size": args.image_size},
        "acceptance": str(args.acceptance),
        "reviewed_target": str(args.reviewed_target),
        "phases": (["image-verify", "write", "readback", "evidence"] if args.no_backup
                   else ["backup", "backup-rehash", "image-verify", "write", "readback", "evidence"]),
        "power_or_serial_action": False,
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
    if not args.execute:
        return 0
    if os.geteuid() != 0:
        raise SystemExit("--execute must run as root via deploy_r3_local.command")
    if args.plan:
        raise SystemExit("--plan and --execute are mutually exclusive")

    if not args.no_backup:
        check_space(args.backup_dir, facts["size_bytes"] + 1024 ** 3)
    check_space(evidence_dir, 64 * 1024 ** 2)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    with board_lock(Path(LOCK_ROOT), BOARD) as held:
        held.revalidate()
        port_lock = SessionLock(Path(LOCK_ROOT), args.serial_port)
        port_lock.acquire()
        try:
            live = macos_identity.live(args.device)
            macos_identity.compare(reviewed, live)

            if args.no_backup:
                # Explicit user decision (2026-11-06): the SD is empty and is
                # overwritten without a whole-card backup.  This is recorded as
                # a skipped backup, never as a verified backup.
                state.phase("backup-skipped", reason="user_authorized_empty_card")
                file_sha256 = None
                file_bytes = None
                atomic_json(evidence_dir / "backup-evidence.json", {
                    "schema": "fnos-local-backup-evidence/v1", "ok": True, "skipped": True,
                    "authorized_by": "user", "run_id": run_id, "at": now(),
                    "reason": "user stated the card is empty and asked to overwrite it without a backup",
                    "bytes": 0, "sha256": None,
                    "target": {k: live.get(k) for k in
                               ("device", "model", "size_bytes", "usb_gadget_serial", "usb_path")},
                })
            else:
                state.phase("backup-start", backup=str(backup_path))
                raw = args.device.replace("/dev/disk", "/dev/rdisk", 1)
                stream_sha, stream_bytes = read_card(raw, backup_path, facts["size_bytes"], state)
                state.phase("backup-written", backup=str(backup_path), bytes=stream_bytes,
                            stream_sha256=stream_sha)

                state.phase("backup-rehash")
                file_sha256, file_bytes = file_sha(backup_path)
                if file_bytes != stream_bytes or file_sha256 != stream_sha:
                    raise SystemExit("backup file re-hash differs from source stream")

                after = macos_identity.live(args.device)
                macos_identity.compare(reviewed, after)
                backup_ev = {"schema": "fnos-local-backup-evidence/v1", "ok": True, "run_id": run_id,
                             "at": now(), "path": str(backup_path), "bytes": file_bytes,
                             "sha256": file_sha256, "source_stream_sha256": stream_sha,
                             "source_stream_bytes": stream_bytes, "rehash_verified": True,
                             "target": {k: after.get(k) for k in
                                        ("device", "model", "size_bytes", "usb_gadget_serial", "usb_path")}}
                atomic_json(evidence_dir / "backup-evidence.json", backup_ev)
                state.phase("backup-verified", sha256=file_sha256, bytes=file_bytes)

            if state.cancelled():
                raise SystemExit("cancelled before image write; no write attempted")

            state.phase("image-verify")
            image = open_trusted_image({"image": {"path": str(args.image), "sha256": args.image_sha256,
                                                  "size_bytes": args.image_size}})
            try:
                if image.sha256 != args.image_sha256 or image.size_bytes != args.image_size:
                    raise SystemExit("local image hash/size differs from the accepted candidate")

                state.phase("write", image_sha256=image.sha256)
                backend = MacDiskutilBackend(args.device)
                fd = backend.open_target()
                try:
                    write = write_image(backend, fd, image, cancel=lambda: False)
                    if not write.complete or write.bytes_written != image.size_bytes:
                        raise HardwareFailure("write_incomplete",
                                              f"wrote {write.bytes_written}/{image.size_bytes}")
                    backend.sync()
                    print(json.dumps({"write_ok": True, "bytes": write.bytes_written, "at": now()}), flush=True)

                    # A new FAT p1 can be auto-mounted by macOS as soon as the
                    # partition table exists.  Unmount only mounted children
                    # before the verification read so mount metadata cannot
                    # change bytes under the readback.
                    pre_read_unmounted = unmount_children(args.device)
                    print(json.dumps({"pre_readback_unmounted": pre_read_unmounted, "at": now()}), flush=True)

                    state.phase("readback")
                    rb_sha, rb_bytes, rb_complete = readback_image(
                        backend, fd, image, cancel=lambda: state.cancelled())
                    require_full_readback(rb_sha, image, bytes_read=rb_bytes, complete=rb_complete)
                finally:
                    backend.close_target(fd)
            finally:
                image.close()

            state.phase("readback-verified", readback_sha256=rb_sha, bytes=rb_bytes)
            unmounted = unmount_children(args.device)
            print(json.dumps({"unmounted_children": unmounted, "at": now()}), flush=True)
            atomic_json(evidence_dir / "readback-evidence.json", {
                "schema": "fnos-local-readback-evidence/v1", "ok": True, "run_id": run_id,
                "at": now(), "target": args.device,
                "image_sha256": args.image_sha256, "image_size": args.image_size,
                "readback_sha256": rb_sha, "bytes": rb_bytes, "complete": rb_complete,
                "written_bytes": write.bytes_written, "unmounted_children": unmounted,
            })
            deploy_ev = {"schema": "fnos-local-deploy-evidence/v1", "ok": True, "run_id": run_id,
                         "at": now(), "device": args.device, "card_bytes": facts["size_bytes"],
                         "backup_skipped": bool(args.no_backup),
                         "backup": (None if args.no_backup else str(backup_path)),
                         "backup_bytes": file_bytes,
                         "backup_sha256": file_sha256, "image": str(args.image),
                         "image_bytes": args.image_size, "image_sha256": args.image_sha256,
                         "written_bytes": write.bytes_written,
                         "readback_bytes": rb_bytes, "readback_sha256": rb_sha,
                         "readback_complete": rb_complete,
                         "power_or_serial_action": False,
                         "next": "exit UMS via reviewed serial entry, then CP5 cold boot"}
            atomic_json(evidence_dir / "deploy-evidence.json", deploy_ev)
            state.phase("complete", readback_sha256=rb_sha)
            print(json.dumps(deploy_ev, ensure_ascii=False, indent=2), flush=True)
            return 0
        except BaseException as exc:
            state.phase("failed", error=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    sys.exit(main())
