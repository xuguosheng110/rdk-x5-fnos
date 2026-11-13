"""Replayable boot-asset revision on the verified r2 rootfs image.

This intentionally does NOT rebuild rootfs/kernel/initrd.  It copies the
verified r2 whole-disk image, regenerates only boot.cmd/boot.scr from the real
X5 boot.cmd, writes them into the copied p2 through a private-image FUSE mount,
and re-runs the final-media verifier.  No manual patch step is involved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from .assemble import (
    MIB, P2_START_LBA, SEC, harden_boot_cmd, sha256_image_regions, sha256, verify_final,
)

B = Path("/srv/fnos-build/assembly-20261106")


def log(msg: str) -> None:
    print(f"[r3] {msg}", flush=True)


def run(cmd, timeout=3600):
    log("run: " + " ".join(str(x) for x in cmd))
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"failed {p.returncode}: {cmd}\n{p.stdout[-3000:]}\n{p.stderr[-3000:]}")
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-image", type=Path, required=True)
    ap.add_argument("--base-manifest", type=Path, required=True)
    ap.add_argument("--boot-cmd", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--release", default="r3")
    ap.add_argument("--live-bdinfo", type=Path, required=True)
    ap.add_argument("--live-numeric", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite {args.output}")
    args.work.mkdir(parents=True, exist_ok=True)
    base = json.loads(args.base_manifest.read_text())
    base_sha = base["candidate"]["sha256"]
    if sha256(args.base_image) != base_sha:
        raise SystemExit("base r2 SHA mismatch")
    log(f"copying verified base r2: {base_sha}")
    run(["cp", "--sparse=always", str(args.base_image), str(args.output)])
    if sha256(args.output) != base_sha:
        raise SystemExit("copy SHA mismatch")

    boot_cmd_text = harden_boot_cmd(args.boot_cmd.read_text(encoding="utf-8", errors="replace"))
    if "then :" in boot_cmd_text or "if :" in boot_cmd_text:
        raise SystemExit("unsupported ':' remains")
    cmd_path = args.work / "boot.cmd"
    scr_path = args.work / "boot.scr"
    cmd_path.write_text(boot_cmd_text, encoding="utf-8")
    mkimage = shutil.which("mkimage")
    if not mkimage:
        raise SystemExit("mkimage not in PATH")
    run([mkimage, "-C", "none", "-A", "arm", "-T", "script", "-d", str(cmd_path), str(scr_path)])
    run(["mkimage", "-l", str(scr_path)])

    mount = Path(tempfile.mkdtemp(prefix="fnos-r3-mnt-", dir="/tmp"))
    fuse2fs = shutil.which("fuse2fs")
    if not fuse2fs:
        raise SystemExit("fuse2fs not in PATH")
    mounted = False
    try:
        run([fuse2fs, "-o", f"fakeroot,offset={P2_START_LBA * SEC}", str(args.output), str(mount)])
        mounted = True
        time.sleep(1)
        shutil.copyfile(cmd_path, mount / "boot/boot.cmd")
        shutil.copyfile(scr_path, mount / "boot/boot.scr")
        import os
        os.chmod(mount / "boot/boot.cmd", 0o755)
        os.chmod(mount / "boot/boot.scr", 0o644)
        run(["sync"], timeout=300)
        log("boot.cmd/boot.scr replaced in copied p2")
    finally:
        if mounted:
            subprocess.run(["fusermount3", "-u", str(mount)], capture_output=True)
            time.sleep(0.3)
            shutil.rmtree(mount, ignore_errors=True)

    final_sha, p1_sha, p2_sha = sha256_image_regions(
        args.output, 8192 * SEC, 524288 * SEC, P2_START_LBA * SEC, 12 * 1024 * 1024 * 1024
    )
    checks = verify_final(args.output, args.work, base["verification"]["final_p1_sha256"], p2_sha)
    live_b = json.loads(args.live_bdinfo.read_text())
    live_n = json.loads(args.live_numeric.read_text())
    bdinfo = next((x["output"] for x in live_b["commands"] if x["command"] == "bdinfo"), "")
    live = {
        "bdinfo_path": str(args.live_bdinfo),
        "bdinfo_sha256": hashlib.sha256(args.live_bdinfo.read_bytes()).hexdigest(),
        "numeric_path": str(args.live_numeric),
        "numeric_sha256": hashlib.sha256(args.live_numeric.read_bytes()).hexdigest(),
        "dram_banks": [
            {"start": "0x84000000", "end": "0x100000000", "size": "0x7c000000"},
            {"start": "0x100000000", "end": "0x280000000", "size": "0x180000000"},
        ],
        "lmb_reserved": [{"start": "0x8be990d0", "end": "0x90000000", "size": "0x4166f30"}],
        "ramdisk_addr_r": "undefined in live printenv",
        "initrd_high": "undefined in live printenv",
        "fdt_high": "undefined in live printenv",
        "bootm_low": "undefined in live printenv",
        "bootm_size": "undefined in live printenv",
        "colon_builtin": "unsupported: Unknown command ':' (numeric probe)",
        "hex_comparison": "confirmed by live literal probes",
        "bdinfo_excerpt": bdinfo,
        "numeric_results": live_n["probes"],
    }
    manifest = {
        "schema": "fnos-x5-assembly/v2",
        "release": args.release,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": "replayable boot-asset revision on verified r2 rootfs; private-image FUSE; final p2 readback; no physical disk",
        "base": {
            "release": "r2",
            "path": str(args.base_image),
            "sha256": base_sha,
            "manifest": str(args.base_manifest),
            "manifest_sha256": hashlib.sha256(args.base_manifest.read_bytes()).hexdigest(),
        },
        "rootfs": {"uuid": base["rootfs"]["uuid"], "size": base["rootfs"]["size"], "fstype": "ext4"},
        "candidate": {
            "release": args.release,
            "path": str(args.output),
            "sha256": final_sha,
            "size": args.output.stat().st_size,
        },
        "components": {
            "p1": base["components"]["p1"],
            "p2": {
                "origin": "verified r2 p2, only /boot/boot.cmd and /boot/boot.scr replaced",
                "final_sha256": p2_sha,
                "size": 12 * 1024 * 1024 * 1024,
            },
            "boot_cmd": {"path": str(cmd_path), "sha256": sha256(cmd_path)},
            "boot_scr": {"path": str(scr_path), "sha256": sha256(scr_path)},
            "initrd": {"path": "/boot/initrd.img-6.18.0", "size": checks["initrd_size"], "sha256": checks["initrd_sha256"]},
            "kernel": {"path": "/boot/Image", "sha256": checks["image_sha256"]},
            "fdt": {"path": "/boot/hobot/x5-rdk-v1p0.dtb", "size": checks["selected_dtb_size"], "sha256": checks["selected_dtb_sha256"]},
        },
        "changes": {
            "modules_release": "6.18.0",
            "rootfs": "reused verified r2 p2; no mke2fs",
            "kernel": "unchanged",
            "initrd": "unchanged",
            "boot_cmd": "regenerated from real X5 boot.cmd; ':' success branch replaced with echo; explicit 0xc0000000; hex filesize guard",
            "boot_scr": "regenerated with mkimage",
        },
        "live_uboot": live,
        "relocation_review": {
            "initrd_high": "undefined in live env; no explicit cap added",
            "fdt_high": "undefined in live env",
            "bootm_low_size": "undefined in live env",
            "decision": "keep explicit initrd address 0xc0000000; it is within live DRAM bank0 and above live LMB reserved 0x8be990d0..0x90000000; runtime relocation is not proven, so this remains a boot-test item",
        },
        "os": checks["os"],
        "boot": checks["boot"],
        "services": checks["services"],
        "verification": checks,
        "final_readback": checks,
        "open_p0": [
            "real X5 boot / SPI Miniboot/U-Boot interaction not tested",
            "runtime initrd relocation and dtoverlay/setpin DT updates not exercised",
            "network/SMB/Web/Docker/NFS/storage not tested",
            "X5 firmware not imported; rootfs is ext4 not btrfs; no first-boot resize",
        ],
    }
    mpath = args.output.with_suffix(args.output.suffix + ".manifest.json")
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"manifest: {mpath}")
    if not checks["all_pass"]:
        print("error: final readback verification failed", file=sys.stderr)
        return 3
    log(f"candidate: {args.output} sha256={final_sha} size={args.output.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
